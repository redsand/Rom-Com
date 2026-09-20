"""The chat HTTP surface, including the SSE framing. The frame format is a contract with
app.js, so it is asserted literally rather than approximately."""
import json

import pytest
from fake_llm import FakeOllama

from romcom import chatstore, webchat
from romcom.db import connect
from romcom.web import create_app


@pytest.fixture(autouse=True)
def _clear_running():
    """The in-flight-turn registry is module state, and every test's session ids start at 1
    again — without this, one test's claim would 409 the next test's turn."""
    webchat._RUNNING.clear()
    yield
    webchat._RUNNING.clear()


def make_app(monkeypatch, tmp_path, chat=True, items=None):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ROMCOM_CHAT_MODEL", "qwen3:14b")
    monkeypatch.setenv("ROMCOM_CHAT_ENABLED", "true" if chat else "false")
    from romcom.config import invalidate
    invalidate()
    db = connect()
    with db:
        for r in items or []:
            db.execute("INSERT INTO items(id,title,system,authorized,wanted,status) VALUES(?,?,?,?,?,?)",
                       (r["id"], r.get("title", r["id"]), r.get("system"),
                        r.get("authorized", 1), r.get("wanted", 1),
                        r.get("status", "CATALOGED")))
    return create_app()


def make_client(monkeypatch, tmp_path, chat=True, items=None):
    return make_app(monkeypatch, tmp_path, chat, items).test_client()


def parse_sse(body):
    """`event: <name>\\ndata: <json>\\n\\n` frames, exactly as app.js splits them."""
    out = []
    for chunk in body.strip().split("\n\n"):
        if not chunk.strip():
            continue
        name, data = None, None
        for line in chunk.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((name, data))
    return out


def stream(client, message, **extra):
    r = client.post("/api/chat/stream", json={"message": message, **extra})
    return r, (parse_sse(r.get_data(as_text=True)) if r.status_code == 200 else None)


# ---------------------------------------------------------------------- the stream

def test_the_stream_is_unbuffered_event_stream(monkeypatch, tmp_path):
    """If this ever buffers, the tab shows nothing until the whole turn finishes — which for
    a tool-heavy local turn is most of a minute. Asserted on the Response object itself: the
    test client's wrapper rebuffers the body, so anything read off *it* says nothing about
    what the view returned. (`Response.buffered` no longer exists in Werkzeug 3; `is_streamed`
    is the equivalent check.)

    `direct_passthrough` must stay *off*. It sounds like the stronger guarantee, but it hands
    the raw iterable to the WSGI server unencoded while the headers still promise chunked
    framing — and the frames are `str`, which WSGI rejects. The server then wrote nothing,
    closed the connection, and the tab reported a fetch-level network error on a 200. Werkzeug
    encoding the generator costs no buffering and is what actually reaches the browser."""
    app = make_app(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["hi"]}]).install(monkeypatch)
    with app.test_request_context("/api/chat/stream", method="POST", json={"message": "hello"}):
        resp = app.view_functions["api_chat_stream"]()
        try:
            assert resp.mimetype == "text/event-stream"
            assert resp.is_streamed is True
            assert resp.direct_passthrough is False    # see the docstring: off, deliberately
            assert resp.headers["X-Accel-Buffering"] == "no"
            assert resp.headers["Cache-Control"] == "no-cache"
        finally:
            resp.close()          # also exercises the generator's cleanup path


def test_the_body_arriving_over_the_wire_is_well_formed_sse(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["hi"]}]).install(monkeypatch)
    r, frames = stream(c, "hello")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/event-stream")
    body = r.get_data(as_text=True)
    assert body.startswith("event: start\ndata: {") and body.endswith("\n\n")
    assert frames[-1][0] == "done"


def test_a_full_turn_emits_start_tokens_then_done(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path, items=[{"id": "a", "system": "NES"},
                                                  {"id": "b", "system": "NES"}])
    FakeOllama(turns=[{"tool_calls": [{"name": "library_summary", "arguments": {}}]},
                      {"tokens": ["Two ", "items."], "usage": {"eval_count": 5}}]).install(monkeypatch)
    r, frames = stream(c, "how many items?")
    names = [n for n, _ in frames]
    assert names == ["start", "tool_start", "tool_result", "token", "token", "done"]
    assert frames[0][1]["session_id"] == 1
    assert frames[2][1]["data"]["cataloged"] == 2
    assert "".join(p["text"] for n, p in frames if n == "token") == "Two items."
    assert frames[-1][1]["usage"]["eval_count"] == 5
    assert frames[-1][1]["stopped"] is None


def test_the_session_is_persisted_and_reusable(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["first"]}, {"tokens": ["second"]}]).install(monkeypatch)
    _, first = stream(c, "one")
    sid = first[0][1]["session_id"]
    _, second = stream(c, "two", session_id=sid)
    assert second[0][1]["session_id"] == sid
    detail = c.get(f"/api/chat/session/{sid}").get_json()
    assert [m["content"] for m in detail["messages"]] == ["one", "first", "two", "second"]


def test_a_disabled_assistant_returns_a_json_error_not_a_stream(monkeypatch, tmp_path):
    """The tab renders a disabled state from this, so it must be legible JSON rather than an
    empty stream that looks like a broken connection."""
    c = make_client(monkeypatch, tmp_path, chat=False)
    r = c.post("/api/chat/stream", json={"message": "hi"})
    assert r.status_code == 400
    assert "disabled" in r.get_json()["error"]
    assert not r.headers["Content-Type"].startswith("text/event-stream")


def test_an_empty_message_is_refused(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    assert c.post("/api/chat/stream", json={"message": "   "}).status_code == 400


def test_an_unknown_session_is_refused(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    assert c.post("/api/chat/stream", json={"message": "hi", "session_id": 999}).status_code == 404


def test_a_model_failure_still_produces_a_terminal_done(monkeypatch, tmp_path):
    """Otherwise the composer stays disabled forever after one Ollama hiccup."""
    c = make_client(monkeypatch, tmp_path)
    FakeOllama(turns=[{"status": 500, "error": "model runner crashed"}]).install(monkeypatch)
    _, frames = stream(c, "hello")
    names = [n for n, _ in frames]
    assert "error" in names and names[-1] == "done"
    assert "crashed" in [p["message"] for n, p in frames if n == "error"][0]


# ---------------------------------------------------------------- concurrency/cancel

def test_only_one_turn_runs_per_session_at_a_time(monkeypatch, tmp_path):
    """Two concurrent turns would interleave writes into one history and produce a
    conversation the model never actually saw."""
    c = make_client(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["first"]}, {"tokens": ["second"]}]).install(monkeypatch)
    _, frames = stream(c, "one")
    sid = frames[0][1]["session_id"]
    assert webchat._claim(sid) is True            # simulate a turn still in flight
    try:
        r = c.post("/api/chat/stream", json={"message": "two", "session_id": sid})
        assert r.status_code == 409
    finally:
        webchat._release(sid)
    assert c.post("/api/chat/stream", json={"message": "three", "session_id": sid}).status_code == 200


def test_the_claim_is_released_when_a_turn_finishes(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["x"]}]).install(monkeypatch)
    _, frames = stream(c, "hi")
    assert webchat._cancel_event(frames[0][1]["session_id"]) is None


def test_cancel_sets_the_running_turns_flag(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    assert c.post("/api/chat/cancel", json={"session_id": 5}).get_json() == {"cancelled": False}
    import threading
    webchat._RUNNING[5] = threading.Event()
    assert c.post("/api/chat/cancel", json={"session_id": 5}).get_json() == {"cancelled": True}
    assert webchat._RUNNING[5].is_set()


def test_cancel_requires_a_session_id(monkeypatch, tmp_path):
    assert make_client(monkeypatch, tmp_path).post("/api/chat/cancel", json={}).status_code == 400


# ------------------------------------------------------------------- other routes

def test_session_list_detail_and_delete(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["answer"]}]).install(monkeypatch)
    _, frames = stream(c, "a question")
    sid = frames[0][1]["session_id"]

    listing = c.get("/api/chat/sessions").get_json()
    assert listing[0]["id"] == sid and listing[0]["messages"] == 2
    assert c.get(f"/api/chat/session/{sid}").get_json()["session"]["title"] == "a question"
    assert c.delete(f"/api/chat/session/{sid}").get_json() == {"deleted": 1}
    assert c.get(f"/api/chat/session/{sid}").status_code == 404


def test_status_reports_the_resolved_model_and_the_installed_list(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    FakeOllama().install(monkeypatch)
    d = c.get("/api/chat/status").get_json()
    assert d["enabled"] is True and d["reachable"] is True
    assert d["model"] == "qwen3:14b" and {m["name"] for m in d["models"]} >= {"qwen3:14b"}


def test_status_degrades_gracefully_when_ollama_is_down(monkeypatch, tmp_path):
    """A status endpoint that 500s because the model server is down is useless exactly when
    it is most needed."""
    c = make_client(monkeypatch, tmp_path)
    monkeypatch.setenv("ROMCOM_CHAT_MODEL", "")
    import requests
    from romcom import llmclient
    monkeypatch.setattr(llmclient.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("refused")))
    d = c.get("/api/chat/status").get_json()
    assert d["reachable"] is False and "refused" in d["error"] and d["model"] is None


def test_the_tool_catalog_is_exposed_for_the_ui(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    tools = {t["name"]: t["risk"] for t in c.get("/api/chat/tools").get_json()}
    assert tools["library_audit"] == "low" and tools["search_title"] == "medium"


# ------------------------------------------------------------------------- approvals

def pause_on_a_gated_call(c, message="mark everything owned"):
    """Drive a turn into the pause and hand back (session_id, approval_id) — the state the UI
    is in when its confirm card is on screen."""
    _, frames = stream(c, message)
    card = [p for n, p in frames if n == "approval_required"][0]
    sid = [p for n, p in frames if n == "start"][0]["session_id"]
    return sid, card["approval_id"]


def test_a_gated_call_pauses_the_stream_without_erroring(monkeypatch, tmp_path):
    """The pause is a normal end to a turn, not a failure: `done` still arrives, so the
    composer is re-enabled and the tab is never left waiting on nothing."""
    c = make_client(monkeypatch, tmp_path, items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    FakeOllama(turns=[{"tool_calls": [{"name": "mark_all_owned", "arguments": {}}]}]).install(monkeypatch)
    r, frames = stream(c, "mark everything owned")
    names = [n for n, _ in frames]
    assert r.status_code == 200
    assert names[-1] == "done" and "error" not in names
    assert frames[-1][1]["paused"] is True
    card = [p for n, p in frames if n == "approval_required"][0]
    assert card["tool"] == "mark_all_owned" and card["summary"]
    assert connect().execute("SELECT wanted FROM items WHERE id='a'").fetchone()["wanted"] == 0


def test_approving_over_http_runs_the_call_and_streams_the_continuation(monkeypatch, tmp_path):
    """The whole point of the gate, driven the way app.js drives it."""
    c = make_client(monkeypatch, tmp_path, items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    FakeOllama(turns=[{"tool_calls": [{"name": "mark_all_owned", "arguments": {}}]},
                      {"tokens": ["All set."]}]).install(monkeypatch)
    sid, aid = pause_on_a_gated_call(c)
    assert chatstore.approval(aid)["status"] == "pending"

    r = c.post(f"/api/chat/approve/{aid}", json={"approve": True})
    frames = parse_sse(r.get_data(as_text=True))
    assert r.status_code == 200
    # The resumed turn reuses the session and streams its own `done`.
    assert [p for n, p in frames if n == "start"][0] == {"session_id": sid, "user_message_id": None}
    assert [p for n, p in frames if n == "tool_result"][0]["approved"] is True
    assert frames[-1][0] == "done"
    assert connect().execute("SELECT wanted FROM items WHERE id='a'").fetchone()["wanted"] == 1


def test_declining_over_http_changes_nothing(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path, items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    FakeOllama(turns=[{"tool_calls": [{"name": "mark_all_owned", "arguments": {}}]},
                      {"tokens": ["Left them alone."]}]).install(monkeypatch)
    _, aid = pause_on_a_gated_call(c)
    r = c.post(f"/api/chat/approve/{aid}", json={"approve": False})
    frames = parse_sse(r.get_data(as_text=True))
    assert [p for n, p in frames if n == "tool_result"][0]["approved"] is False
    assert connect().execute("SELECT wanted FROM items WHERE id='a'").fetchone()["wanted"] == 0
    assert chatstore.approval(aid)["result"]


def test_an_approval_cannot_be_resolved_twice_over_http(monkeypatch, tmp_path):
    """A double-click on the confirm card, or two open tabs. The second one is refused rather
    than running the gated call a second time."""
    c = make_client(monkeypatch, tmp_path, items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    FakeOllama(turns=[{"tool_calls": [{"name": "mark_all_owned", "arguments": {}}]},
                      {"tokens": ["done"]}]).install(monkeypatch)
    _, aid = pause_on_a_gated_call(c)
    assert c.post(f"/api/chat/approve/{aid}", json={"approve": True}).status_code == 200
    second = c.post(f"/api/chat/approve/{aid}", json={"approve": True})
    assert second.status_code == 409 and "already" in second.get_json()["error"]


def test_an_unknown_approval_is_a_404_not_a_500(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    assert c.post("/api/chat/approve/999", json={"approve": True}).status_code == 404


def test_pending_approvals_can_be_listed_so_a_reload_can_re_render_the_card(monkeypatch, tmp_path):
    """A paused turn survives a page refresh because the card is rebuilt from this route."""
    c = make_client(monkeypatch, tmp_path, items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    FakeOllama(turns=[{"tool_calls": [{"name": "mark_all_owned", "arguments": {}}]}]).install(monkeypatch)
    sid, aid = pause_on_a_gated_call(c)
    rows = c.get(f"/api/chat/approvals/{sid}?status=pending").get_json()
    assert [r["id"] for r in rows] == [aid] and json.loads(rows[0]["arguments"]) == {}
    assert c.get("/api/chat/approvals/999").get_json() == []


def test_the_approve_route_is_behind_the_login_too(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ROMCOM_WEB_USER", "zaphod")
    monkeypatch.setenv("ROMCOM_WEB_PASS", "hunter2-correct-horse")
    connect()
    c = create_app().test_client()
    assert c.post("/api/chat/approve/1", json={"approve": True}).status_code == 401
    assert c.get("/api/chat/approvals/1").status_code == 401


# ------------------------------------------------------------------------- auth

def test_the_chat_routes_are_behind_the_login_too(monkeypatch, tmp_path):
    """A new /api surface is exactly where an auth gate gets forgotten. The gate is a
    before_request hook, so this proves it covers the chat routes rather than assuming."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ROMCOM_WEB_USER", "zaphod")
    monkeypatch.setenv("ROMCOM_WEB_PASS", "hunter2-correct-horse")
    connect()
    c = create_app().test_client()
    assert c.post("/api/chat/stream", json={"message": "hi"}).status_code == 401
    assert c.get("/api/chat/sessions").status_code == 401
    assert c.get("/api/chat/status").status_code == 401
