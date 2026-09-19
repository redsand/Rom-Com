"""MCP in both directions.

The client tests run against a fake MCP server rather than a live one, because the point is
to pin *our* behaviour (handshake, prefixing, caching, degradation), not to test someone
else's server. The server tests are the more important half: they pin that `/mcp` re-serves
exactly the in-app registry, since that is the guarantee that stops the two directions from
drifting apart.
"""
import json

import pytest

from romcom import chattools, mcpclient, mcpserver, webauth
from romcom.db import connect
from romcom.web import create_app


@pytest.fixture(autouse=True)
def _clear_caches():
    """Both caches are module state keyed on config; without this one test's servers would
    leak into the next test's registry."""
    mcpclient._cache.update(path=None, mtime=None, servers=[], checked=False)
    mcpclient._tools_cache.update(key=None, tools={})
    yield
    mcpclient._cache.update(path=None, mtime=None, servers=[], checked=False)
    mcpclient._tools_cache.update(key=None, tools={})


def write_servers(tmp_path, servers):
    p = tmp_path / "mcp-servers.json"
    p.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return str(p)


def use_config(monkeypatch, path=None, enabled=True):
    monkeypatch.setenv("ROMCOM_MCP_ENABLED", "true" if enabled else "false")
    if path:
        monkeypatch.setenv("ROMCOM_MCP_SERVERS", path)
    from romcom.config import invalidate
    invalidate()


# ------------------------------------------------------------------------- fake server

class FakeMCP:
    """A minimal streamable-HTTP MCP server: initialize, tools/list, tools/call."""

    def __init__(self, tools=None, sse=False, fail_list=False, fail_all=False):
        self.tools = tools or {}
        self.sse = sse
        self.fail_list = fail_list
        self.fail_all = fail_all
        self.calls = []          # (method, params) for every request that carried an id
        self.initialized = 0

    def post(self, url, payload=None, headers=None, timeout=None):
        # Named `payload` rather than `json`, which would shadow the module and make the
        # `json.dumps` below call a dict.
        body = payload or {}
        method = body.get("method")
        if self.fail_all:
            return _Resp(status=500)
        if method == "initialize":
            self.initialized += 1
        if "id" not in body:
            return _Resp(status=202)                      # a notification
        self.calls.append((method, body.get("params")))
        if method == "tools/list":
            if self.fail_list:
                return _Resp(status=500)
            result = {"tools": [{"name": n, "description": t.get("description", ""),
                                 "inputSchema": t.get("schema", {"type": "object", "properties": {}})}
                                for n, t in self.tools.items()]}
        elif method == "tools/call":
            p = body.get("params") or {}
            t = self.tools.get(p.get("name"))
            if not t:
                return _Resp(body={"jsonrpc": "2.0", "id": body["id"],
                                   "error": {"code": -32602, "message": "no such tool"}})
            a = p.get("arguments") or {}
            # `raw` returns the tool result verbatim, for pinning how we adapt a shape that
            # is not the common single-JSON-text-block one.
            result = t["raw"](a) if "raw" in t else \
                {"content": [{"type": "text", "content": json.dumps(t["fn"](a))}]}
        else:
            return _Resp(body={"jsonrpc": "2.0", "id": body["id"],
                               "error": {"code": -32601, "message": f"unknown {method}"}})
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:      # the streamable-HTTP alternative framing
            return _Resp(text=f"event: message\ndata: {json.dumps(msg)}\n\n",
                         headers={"Content-Type": "text/event-stream"})
        return _Resp(body=msg)


class _Resp:
    def __init__(self, body=None, status=200, text="", headers=None):
        self._body = body
        self.status_code = status
        self.text = text
        self.headers = headers or {"Content-Type": "application/json"}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def install_fake(monkeypatch, servers_by_url):
    """`servers_by_url` maps url -> FakeMCP, so one test can run two servers."""
    class FakeRequests:
        def post(self, url, json=None, headers=None, timeout=None):
            # `json=` is the keyword `requests` uses, so it has to be accepted here; it is
            # passed on positionally to keep the real payload out of a name collision.
            fake = servers_by_url.get(url)
            if not fake:
                return _Resp(status=404)
            return fake.post(url, json, headers, timeout)
    monkeypatch.setattr(mcpclient, "requests", FakeRequests())


# ------------------------------------------------------------------------ client

def test_a_servers_tools_appear_prefixed_and_callable(monkeypatch, tmp_path):
    fake = FakeMCP({"forecast": {"description": "Weather for a city",
                                 "schema": {"type": "object",
                                            "properties": {"city": {"type": "string"}}},
                                 "fn": lambda a: {"temp": 21, "city": a.get("city")}}})
    install_fake(monkeypatch, {"http://x/mcp": fake})
    use_config(monkeypatch, write_servers(tmp_path, [
        {"name": "weather", "url": "http://x/mcp", "enabled": True}]))

    reg = chattools.build_registry()
    merged = mcpclient.merge(reg)
    assert "mcp_weather_forecast" in merged
    assert "weather" in merged["mcp_weather_forecast"].description.lower() or \
        merged["mcp_weather_forecast"].description == "Weather for a city"
    out = chattools.dispatch(merged, "mcp_weather_forecast", {"city": "Paris"})
    assert out["ok"] is True
    assert out["data"]["temp"] == 21


def test_the_native_registry_is_not_mutated_by_the_merge(monkeypatch, tmp_path):
    """`/mcp` serves the native registry. If external tools were merged in place, Rom-Com
    would re-advertise another server's tools as its own — a loop that grows a level per hop."""
    install_fake(monkeypatch, {"http://x/mcp": FakeMCP({"t": {"fn": lambda a: 1}})})
    use_config(monkeypatch, write_servers(tmp_path,
                                          [{"name": "s", "url": "http://x/mcp"}]))
    native = chattools.build_registry()
    before = set(native)
    merged = mcpclient.merge(native)
    assert set(native) == before
    assert "mcp_s_t" not in native and "mcp_s_t" in merged


def test_two_servers_offering_the_same_tool_name_do_not_collide(monkeypatch, tmp_path):
    """The prefix is the server name, so `search` on two servers is two distinct tools. A
    bare-name merge would silently drop one of them."""
    a = FakeMCP({"search": {"fn": lambda x: {"from": "a"}}})
    b = FakeMCP({"search": {"fn": lambda x: {"from": "b"}}})
    install_fake(monkeypatch, {"http://a/mcp": a, "http://b/mcp": b})
    use_config(monkeypatch, write_servers(tmp_path, [
        {"name": "alpha", "url": "http://a/mcp"}, {"name": "beta", "url": "http://b/mcp"}]))
    merged = mcpclient.merge(chattools.build_registry())
    assert chattools.dispatch(merged, "mcp_alpha_search", {})["data"] == {"from": "a"}
    assert chattools.dispatch(merged, "mcp_beta_search", {})["data"] == {"from": "b"}


def test_a_dotted_server_name_is_folded_to_underscores(monkeypatch, tmp_path):
    """Ollama's tool-name parser dislikes the sister app's dotted names, so `acme.tools`
    becomes a legal tool name rather than one the model cannot call."""
    install_fake(monkeypatch, {"http://x/mcp": FakeMCP({"ping": {"fn": lambda a: "pong"}})})
    use_config(monkeypatch, write_servers(tmp_path,
                                          [{"name": "acme.tools", "url": "http://x/mcp"}]))
    assert "mcp_acme_tools_ping" in mcpclient.merge(chattools.build_registry())


def test_a_disabled_server_contributes_nothing(monkeypatch, tmp_path):
    install_fake(monkeypatch, {"http://x/mcp": FakeMCP({"t": {"fn": lambda a: 1}})})
    use_config(monkeypatch, write_servers(tmp_path,
                                          [{"name": "s", "url": "http://x/mcp",
                                            "enabled": False}]))
    assert mcpclient.external_tools(force=True) == {}


def test_mcp_disabled_means_no_external_tools_at_all(monkeypatch, tmp_path):
    install_fake(monkeypatch, {"http://x/mcp": FakeMCP({"t": {"fn": lambda a: 1}})})
    use_config(monkeypatch, write_servers(tmp_path,
                                          [{"name": "s", "url": "http://x/mcp"}]),
               enabled=False)
    merged = mcpclient.merge(chattools.build_registry())
    assert not [n for n in merged if n.startswith("mcp_")]


def test_an_unreachable_server_costs_only_its_own_tools(monkeypatch, tmp_path):
    """One broken MCP server must not cost the assistant its own tools for the turn, and must
    not raise out of the turn."""
    good = FakeMCP({"ok": {"fn": lambda a: "fine"}})
    bad = FakeMCP(fail_all=True)
    install_fake(monkeypatch, {"http://good/mcp": good, "http://bad/mcp": bad})
    use_config(monkeypatch, write_servers(tmp_path, [
        {"name": "good", "url": "http://good/mcp"}, {"name": "bad", "url": "http://bad/mcp"}]))
    merged = mcpclient.merge(chattools.build_registry())
    assert "mcp_good_ok" in merged
    assert not [n for n in merged if n.startswith("mcp_bad_")]
    assert "library_summary" in merged          # the native tools all survived


def test_an_external_tool_that_fails_reports_like_a_native_tool_error(monkeypatch, tmp_path):
    """Shaped as `{ok: False, error}` so the agent loop treats someone else's server failing
    exactly like one of its own tools failing — the model corrects course and the turn lives."""
    broken = FakeMCP({"boom": {"fn": lambda a: 1 / 0}})
    install_fake(monkeypatch, {"http://x/mcp": broken})
    use_config(monkeypatch, write_servers(tmp_path, [{"name": "s", "url": "http://x/mcp"}]))
    out = chattools.dispatch(mcpclient.merge(chattools.build_registry()), "mcp_s_boom", {})
    assert out["ok"] is False and "MCP s/boom failed" in out["data"]["error"]


def test_disabling_a_server_takes_effect_on_the_next_turn_with_no_restart(monkeypatch, tmp_path):
    """Hot reload is an mtime stat, not a filesystem watcher — a deliberate departure from
    the sister app's `watchdog` dependency."""
    install_fake(monkeypatch, {"http://x/mcp": FakeMCP({"t": {"fn": lambda a: 1}})})
    path = write_servers(tmp_path, [{"name": "s", "url": "http://x/mcp"}])
    use_config(monkeypatch, path)
    assert "mcp_s_t" in mcpclient.merge(chattools.build_registry())

    # Rewrite the same path with the server disabled, and bump the mtime explicitly — a
    # filesystem's timestamp granularity is not something a test should depend on.
    write_servers(tmp_path, [{"name": "s", "url": "http://x/mcp", "enabled": False}])
    import os
    st = os.stat(path)
    os.utime(path, (st.st_atime + 5, st.st_mtime + 5))
    assert "mcp_s_t" not in mcpclient.merge(chattools.build_registry())


def test_tool_definitions_come_from_cache_not_a_request_per_turn(monkeypatch, tmp_path):
    """Re-listing every server on every turn would add an HTTP round trip to each turn and
    make latency depend on someone else's server being up."""
    fake = FakeMCP({"t": {"fn": lambda a: 1}})
    install_fake(monkeypatch, {"http://x/mcp": fake})
    use_config(monkeypatch, write_servers(tmp_path, [{"name": "s", "url": "http://x/mcp"}]))
    mcpclient.merge(chattools.build_registry())
    listed_once = [c for c in fake.calls if c[0] == "tools/list"]
    mcpclient.merge(chattools.build_registry())
    mcpclient.merge(chattools.build_registry())
    assert len([c for c in fake.calls if c[0] == "tools/list"]) == len(listed_once)
    assert len(listed_once) == 1


def test_an_sse_framed_reply_is_parsed_like_a_plain_json_one(monkeypatch, tmp_path):
    """A streamable-HTTP server may answer either way. This is where SSE framing genuinely
    applies — unlike Ollama, whose native endpoint is always NDJSON."""
    install_fake(monkeypatch, {"http://x/mcp": FakeMCP({"t": {"fn": lambda a: "sse ok"}}, sse=True)})
    use_config(monkeypatch, write_servers(tmp_path, [{"name": "s", "url": "http://x/mcp"}]))
    out = chattools.dispatch(mcpclient.merge(chattools.build_registry()), "mcp_s_t", {})
    assert out["ok"] is True and out["data"] == "sse ok"


def test_a_single_text_block_is_unwrapped_but_anything_else_is_left_alone(monkeypatch, tmp_path):
    """Unwrapping only the one case that carries no information. A multi-block or non-text
    result is passed through — guessing at it would drop content the model may need."""
    fake = FakeMCP({
        "plain": {"fn": lambda a: "just prose, not json"},
        "multi": {"raw": lambda a: {"content": [{"type": "text", "content": "one"},
                                                {"type": "text", "content": "two"}]}},
    })
    install_fake(monkeypatch, {"http://x/mcp": fake})
    use_config(monkeypatch, write_servers(tmp_path, [{"name": "s", "url": "http://x/mcp"}]))
    merged = mcpclient.merge(chattools.build_registry())

    assert chattools.dispatch(merged, "mcp_s_plain", {})["data"] == "just prose, not json"
    kept = chattools.dispatch(merged, "mcp_s_multi", {})["data"]
    assert kept["content"][1]["content"] == "two"


def test_the_client_does_the_handshake_before_calling(monkeypatch, tmp_path):
    fake = FakeMCP({"t": {"fn": lambda a: 1}})
    install_fake(monkeypatch, {"http://x/mcp": fake})
    use_config(monkeypatch, write_servers(tmp_path, [{"name": "s", "url": "http://x/mcp"}]))
    mcpclient.call("s", "t", {})
    assert fake.initialized >= 1


def test_calling_an_unknown_server_says_so(monkeypatch, tmp_path):
    use_config(monkeypatch, write_servers(tmp_path, []))
    with pytest.raises(RuntimeError) as e:
        mcpclient.call("nope", "t", {})
    assert "no enabled MCP server" in str(e.value)


def test_a_missing_config_file_is_not_an_error(monkeypatch, tmp_path):
    """A fresh install has no MCP servers; a chat turn must never fail over that."""
    use_config(monkeypatch, str(tmp_path / "does-not-exist.json"))
    assert mcpclient.servers(force=True) == []
    assert mcpclient.merge(chattools.build_registry())


def test_status_reports_a_broken_server_rather_than_hiding_it(monkeypatch, tmp_path):
    install_fake(monkeypatch, {"http://good/mcp": FakeMCP({"t": {"fn": lambda a: 1}}),
                               "http://bad/mcp": FakeMCP(fail_list=True)})
    use_config(monkeypatch, write_servers(tmp_path, [
        {"name": "good", "url": "http://good/mcp"}, {"name": "bad", "url": "http://bad/mcp"},
        {"name": "off", "url": "http://off/mcp", "enabled": False}]))
    by_name = {s["name"]: s for s in mcpclient.status()}
    assert by_name["good"]["state"] == "ok" and by_name["good"]["tools"] == 1
    assert by_name["bad"]["state"] == "error" and by_name["bad"]["error"]
    assert by_name["off"]["state"] == "disabled"


def test_a_server_can_be_raised_to_high_risk_by_config(monkeypatch, tmp_path):
    """Whether a server can do something irreversible is the owner's call, not ours to
    assume — so risk is configurable per server."""
    install_fake(monkeypatch, {"http://x/mcp": FakeMCP({"danger": {"fn": lambda a: 1}})})
    use_config(monkeypatch, write_servers(tmp_path, [
        {"name": "s", "url": "http://x/mcp", "risk": "high"}]))
    tool = mcpclient.external_tools(force=True)["mcp_s_danger"]
    assert tool.risk == "high"


# ------------------------------------------------------------------------ server

def mcp_client(monkeypatch, tmp_path, key="secret-key", items=None):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    if key:
        monkeypatch.setenv("ROMCOM_MCP_KEY", key)
    from romcom.config import invalidate
    invalidate()
    db = connect()
    with db:
        for r in items or []:
            db.execute("INSERT INTO items(id,title,system,authorized,wanted,status)"
                       " VALUES(?,?,?,1,1,?)",
                       (r["id"], r.get("title", r["id"]), r.get("system"), r.get("status", "MISSING")))
    return create_app().test_client()


def rpc(c, method, params=None, rid=1, headers=None, key="secret-key"):
    h = {"Authorization": f"Bearer {key}"} if key else {}
    h.update(headers or {})
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": rid, "method": method,
                                "params": params}, headers=h)


def test_the_served_catalog_matches_the_in_app_registry_exactly(monkeypatch, tmp_path):
    """**The anti-drift pin.** `tools/list` is serialized from `chattools.build_registry(ctx)`
    — the same artifact the assistant runs against — so an external client sees exactly what
    the in-app assistant can do. A tool added to one and not the other fails here."""
    c = mcp_client(monkeypatch, tmp_path)
    served = {t["name"] for t in rpc(c, "tools/list").get_json()["result"]["tools"]}
    native = set(chattools.build_registry())
    assert served == native
    assert "library_summary" in served and "library_audit" in served


def test_initialize_reports_the_protocol_and_server_name(monkeypatch, tmp_path):
    c = mcp_client(monkeypatch, tmp_path)
    r = rpc(c, "initialize").get_json()["result"]
    assert r["protocolVersion"] == mcpserver.PROTOCOL
    assert r["serverInfo"]["name"] == "rom-com"
    assert "tools" in r["capabilities"]


def test_initialized_notification_gets_a_204_with_no_body(monkeypatch, tmp_path):
    """A notification has no id and must not be answered with a result."""
    c = mcp_client(monkeypatch, tmp_path)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers={"Authorization": "Bearer secret-key"})
    assert r.status_code == 204 and not r.get_data()


def test_tools_call_returns_live_data_end_to_end(monkeypatch, tmp_path):
    c = mcp_client(monkeypatch, tmp_path, items=[{"id": "a", "system": "NES"},
                                                 {"id": "b", "system": "NES"}])
    r = rpc(c, "tools/call", {"name": "list_items", "arguments": {}}).get_json()
    assert "error" not in r
    payload = json.loads(r["result"]["content"][0]["content"])
    assert payload["ok"] is True and payload["data"]["total"] == 2
    assert r["result"]["isError"] is False


def test_a_high_risk_tool_is_listed_but_refuses_without_confirmation(monkeypatch, tmp_path):
    """Discoverable but not silently executable. The refusal names the requirement rather
    than just failing, so a client author can see what to send."""
    c = mcp_client(monkeypatch, tmp_path, items=[{"id": "a", "status": "VERIFIED"}])
    listed = {t["name"]: t for t in rpc(c, "tools/list").get_json()["result"]["tools"]}
    assert listed["mark_all_owned"]["description"].count("confirm") >= 1

    r = rpc(c, "tools/call", {"name": "mark_all_owned", "arguments": {}}).get_json()
    assert r["error"]["code"] == -32000
    assert r["error"]["data"]["requires_confirmation"] is True
    assert "Nothing has run" in r["error"]["data"]["hint"]
    assert connect().execute("SELECT wanted FROM items WHERE id='a'").fetchone()["wanted"] == 1


def test_a_high_risk_tool_runs_when_the_caller_confirms(monkeypatch, tmp_path):
    c = mcp_client(monkeypatch, tmp_path,
                   items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    db = connect()
    with db:
        db.execute("UPDATE items SET wanted=0,authorized=0 WHERE id='a'")
    r = rpc(c, "tools/call", {"name": "mark_all_owned",
                              "arguments": {"confirm": True}}).get_json()
    assert "error" not in r
    assert json.loads(r["result"]["content"][0]["content"])["ok"] is True
    assert connect().execute("SELECT wanted FROM items WHERE id='a'").fetchone()["wanted"] == 1


def test_the_confirm_flag_never_reaches_the_tool(monkeypatch, tmp_path):
    """It is not part of any tool's schema, and an unknown key flowing into an UPDATE is
    exactly the kind of surprise confirmation exists to prevent."""
    c = mcp_client(monkeypatch, tmp_path)
    seen = {}
    orig = chattools._invoke

    def spy(tool, args, session_id, name=None):
        seen["args"] = dict(args)
        return orig(tool, args, session_id, name)
    monkeypatch.setattr(chattools, "_invoke", spy)
    rpc(c, "tools/call", {"name": "bulk_update_items",
                          "arguments": {"confirm": True, "filters": {}, "field": "wanted",
                                        "value": 1}})
    assert "confirm" not in seen["args"]


def test_a_low_risk_tool_needs_no_confirmation(monkeypatch, tmp_path):
    c = mcp_client(monkeypatch, tmp_path)
    r = rpc(c, "tools/call", {"name": "library_summary", "arguments": {}}).get_json()
    assert "error" not in r and r["result"]["isError"] is False


def test_without_a_key_every_call_is_401(monkeypatch, tmp_path):
    """Unlike the UI gate, this is never open: the endpoint exists to be driven
    programmatically, so an unset key must mean nobody gets in rather than everybody."""
    c = mcp_client(monkeypatch, tmp_path, key=None)
    assert rpc(c, "tools/list", key=None).status_code == 401
    assert rpc(c, "tools/list", key="wrong").status_code == 401
    assert rpc(c, "tools/list", key=None, headers={"X-Romcom-Key": "guess"}).status_code == 401


def test_the_key_is_accepted_as_a_bearer_or_a_header(monkeypatch, tmp_path):
    c = mcp_client(monkeypatch, tmp_path, key="k3y")
    assert rpc(c, "ping", key="k3y").status_code == 200
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
               headers={"X-Romcom-Key": "k3y"})
    assert r.status_code == 200


def test_a_ui_session_token_also_works_so_one_path_serves_both(monkeypatch, tmp_path):
    """The in-app assistant and a user's script should not need two different credentials."""
    monkeypatch.setenv("ROMCOM_WEB_USER", "zaphod")
    monkeypatch.setenv("ROMCOM_WEB_PASS", "hunter2-correct-horse")
    c = mcp_client(monkeypatch, tmp_path, key="k3y")
    token = webauth.issue_token("zaphod")
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_an_unknown_method_is_a_jsonrpc_error_not_a_crash(monkeypatch, tmp_path):
    c = mcp_client(monkeypatch, tmp_path)
    r = rpc(c, "resources/list").get_json()
    assert r["error"]["code"] == -32601 and "resources/list" in r["error"]["message"]


def test_an_unknown_tool_lists_what_is_available(monkeypatch, tmp_path):
    c = mcp_client(monkeypatch, tmp_path)
    r = rpc(c, "tools/call", {"name": "nope", "arguments": {}}).get_json()
    assert r["error"]["code"] == -32602
    assert "library_summary" in r["error"]["data"]["available"]


def test_a_bad_body_is_reported_as_a_parse_error(monkeypatch, tmp_path):
    c = mcp_client(monkeypatch, tmp_path)
    h = {"Authorization": "Bearer secret-key", "Content-Type": "application/json"}
    assert c.post("/mcp", data="not json", headers=h).get_json()["error"]["code"] == -32700
    batch = c.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
                   headers=h).get_json()
    assert batch["error"]["code"] == -32600


def test_non_object_arguments_are_refused(monkeypatch, tmp_path):
    c = mcp_client(monkeypatch, tmp_path)
    r = rpc(c, "tools/call", {"name": "list_items", "arguments": "nonsense"}).get_json()
    assert r["error"]["code"] == -32602


def test_a_missing_id_is_treated_as_a_notification(monkeypatch, tmp_path):
    """No id means no reply — answering with a result the client is not waiting for is how
    a client's request/response matching gets confused."""
    c = mcp_client(monkeypatch, tmp_path)
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list"},
               headers={"Authorization": "Bearer secret-key"})
    assert r.status_code == 204


def test_the_mcp_endpoint_is_reachable_while_the_login_gates_the_ui(monkeypatch, tmp_path):
    """`/mcp` is deliberately NOT in PUBLIC_PATHS — it carries its own credential, and the
    UI gate must not be what decides whether it is reachable."""
    monkeypatch.setenv("ROMCOM_WEB_USER", "zaphod")
    monkeypatch.setenv("ROMCOM_WEB_PASS", "hunter2-correct-horse")
    c = mcp_client(monkeypatch, tmp_path, key="k3y")
    assert rpc(c, "ping", key="k3y").status_code == 200          # key, not a UI session
    assert c.get("/api/chat/tools").status_code == 401            # UI still gated
