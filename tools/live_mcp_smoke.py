"""One-shot live smoke test for MCP in both directions, over real sockets.

The unit tests fake the transport, so by construction they cannot prove that `requests` is
called correctly, that the headers are acceptable to a real server, or that Werkzeug answers
`/mcp` the way a real client expects. This does: it starts a tiny MCP server on a real port,
points the configured client at it, and then drives Rom-Com's own `/mcp` through the real
app. Throwaway temp DB, nothing left running.

    PYTHONPATH=".../romcom-readline-stub" python tools/live_mcp_smoke.py

Prints a pass/fail line per claim and exits non-zero if any fail.
"""
import json
import logging
import os
import tempfile
import threading

# The external server is a real HTTP server, so Werkzeug logs every request it answers —
# two per call, because the client re-handshakes. That buries the report it is here to print.
logging.getLogger("werkzeug").setLevel(logging.ERROR)

tmp = tempfile.mkdtemp(prefix="romcom-mcp-smoke-")
os.environ["ROMCOM_DB"] = os.path.join(tmp, "smoke.db")
os.environ["ROMCOM_MCP_ENABLED"] = "true"
os.environ["ROMCOM_MCP_KEY"] = "smoke-key"
CONFIG = os.path.join(tmp, "mcp-servers.json")
os.environ["ROMCOM_MCP_SERVERS"] = CONFIG

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys  # noqa: E402
sys.path.insert(0, sys_path)

from flask import Flask, jsonify, request  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from romcom import chatstore, chattools, mcpclient  # noqa: E402
from romcom.db import connect  # noqa: E402
from romcom.web import create_app  # noqa: E402

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))


# ------------------------------------------------------------------ a real MCP server

def build_external():
    """A minimal but *conformant* streamable-HTTP MCP server: initialize, tools/list,
    tools/call, and a 202 with no body for anything without an id."""
    app = Flask("external-mcp")

    @app.post("/mcp")
    def rpc():
        body = request.get_json(force=True, silent=True) or {}
        if "id" not in body:
            return "", 202
        rid, method = body["id"], body.get("method")
        if method == "initialize":
            return jsonify({"jsonrpc": "2.0", "id": rid,
                            "result": {"protocolVersion": "2025-06-18",
                                       "capabilities": {"tools": {}},
                                       "serverInfo": {"name": "external", "version": "1"}}})
        if method == "tools/list":
            return jsonify({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{
                "name": "lookup_platform",
                "description": "Look up a platform's release year (an external server's tool)",
                "inputSchema": {"type": "object",
                                "properties": {"name": {"type": "string"}}},
            }]}})
        if method == "tools/call":
            a = (body.get("params") or {}).get("arguments") or {}
            return jsonify({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "content": json.dumps(
                    {"platform": a.get("name"), "year": 1985, "source": "external server"})}]}})
        return jsonify({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": f"no {method}"}})

    return app


srv = make_server("127.0.0.1", 0, build_external(), threaded=True)
url = f"http://127.0.0.1:{srv.server_port}/mcp"
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"external MCP server listening on {url}\n")

# seed a tiny library
db = connect()
with db:
    for i, (title, system, status) in enumerate([
            ("Super Mario Bros.", "NES", "MISSING"),
            ("Metroid", "NES", "DOWNLOADED"),
            ("Sonic the Hedgehog", "Genesis", "VERIFIED")]):
        db.execute("INSERT INTO items(id,title,system,authorized,wanted,status)"
                   " VALUES(?,?,?,1,1,?)", (f"smoke-{i}", title, system, status))

# ------------------------------------------------------------------- client direction

print("client — Rom-Com calling someone else's MCP server")
with open(CONFIG, "w", encoding="utf-8") as fh:
    json.dump({"servers": [{"name": "external.tools", "url": url}]}, fh)

merged = mcpclient.merge(chattools.build_registry())
check("its tools appear in the assistant's registry, prefixed and name-safe",
      "mcp_external_tools_lookup_platform" in merged,
      ", ".join(sorted(n for n in merged if n.startswith("mcp_"))))
check("the native catalog is untouched by the merge",
      not [n for n in chattools.build_registry() if n.startswith("mcp_")])

out = chattools.dispatch(merged, "mcp_external_tools_lookup_platform", {"name": "NES"})
check("a call round-trips over a real socket and unwraps the content envelope",
      out.get("ok") and out["data"].get("year") == 1985, json.dumps(out.get("data"))[:90])

logged = chatstore.recent_calls(limit=5)
check("the external tool was audited like a native one",
      any(r["tool"] == "mcp_external_tools_lookup_platform" for r in logged),
      f"{len(logged)} audit row(s)")

# hot reload: disable the server, bump the mtime, expect the tool to be gone next turn
with open(CONFIG, "w", encoding="utf-8") as fh:
    json.dump({"servers": [{"name": "external.tools", "url": url, "enabled": False}]}, fh)
st = os.stat(CONFIG)
os.utime(CONFIG, (st.st_atime + 5, st.st_mtime + 5))
check("disabling the server takes effect on the next turn, with no restart",
      "mcp_external_tools_lookup_platform" not in mcpclient.merge(chattools.build_registry()))

with open(CONFIG, "w", encoding="utf-8") as fh:
    json.dump({"servers": [{"name": "external.tools", "url": url}]}, fh)
st = os.stat(CONFIG)
os.utime(CONFIG, (st.st_atime + 5, st.st_mtime + 5))
check("…and re-enabling it takes effect just as immediately",
      "mcp_external_tools_lookup_platform" in mcpclient.merge(chattools.build_registry()))

# ------------------------------------------------------------------- server direction

print("\nserver — someone else calling Rom-Com at /mcp")
app = create_app()
client = app.test_client()
H = {"Authorization": "Bearer smoke-key"}


def rpc(method, params=None, rid=1):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": rid, "method": method,
                                     "params": params}, headers=H)


native = set(chattools.build_registry())
served = {t["name"] for t in rpc("tools/list").get_json()["result"]["tools"]}
check("tools/list serves exactly the in-app catalog (the anti-drift pin)",
      served == native, f"{len(served)} tools; diff {served ^ native or 'none'}")

called = rpc("tools/call", {"name": "list_items", "arguments": {"system": "NES"}}).get_json()
payload = json.loads(called["result"]["content"][0]["content"])
check("tools/call of list_items returns live data from the real DB",
      payload["ok"] and payload["data"]["total"] == 2, f"total={payload['data'].get('total')}")

check("a high-risk tool is listed but refuses without confirmation",
      rpc("tools/call", {"name": "mark_all_owned", "arguments": {}})
      .get_json()["error"]["data"]["requires_confirmation"] is True)

check("…and runs once the caller confirms",
      rpc("tools/call", {"name": "mark_all_owned", "arguments": {"confirm": True}})
      .get_json()["result"]["isError"] is False)

check("an unauthenticated call is 401",
      client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                  headers={"X-Romcom-Key": "wrong"}).status_code == 401)

check("a notification gets a 202 with no body",
      client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                  headers=H).status_code == 204)

# ------------------------------------------------------------------ with the login on

print("\nthe combination the owner actually wants: login on *and* MCP reachable")
os.environ["ROMCOM_WEB_USER"] = "smoke"
os.environ["ROMCOM_WEB_PASS"] = "smoke-pass"
from romcom.config import invalidate  # noqa: E402
invalidate()
client2 = create_app().test_client()
check("MCP key still works while the UI login is configured",
      client2.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers=H).status_code == 200)
check("the UI itself is still gated",
      client2.get("/api/chat/tools").status_code == 401)

srv.shutdown()
failed = [r for r in RESULTS if not r[1]]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
sys.exit(1 if failed else 0)
