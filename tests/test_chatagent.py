"""The agent loop and its three guardrails. A local model looping is the normal failure
mode, so each bound is pinned by a test rather than assumed."""
import json

from fake_llm import FakeOllama

from romcom import chatagent, chatstore, chattools
from romcom.db import connect


def setup_db(monkeypatch, tmp_path, items=None):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ROMCOM_CHAT_MODEL", "qwen3:14b")
    db = connect()
    with db:
        for r in items or []:
            db.execute("INSERT INTO items(id,title,system,authorized,wanted,status) VALUES(?,?,?,?,?,?)",
                       (r["id"], r.get("title", r["id"]), r.get("system"), r.get("authorized", 1),
                        r.get("wanted", 1), r.get("status", "CATALOGED")))
    return db


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, name, payload):
        self.events.append((name, payload))

    def names(self):
        return [n for n, _ in self.events]

    def payloads(self, name):
        return [p for n, p in self.events if n == name]

    def text(self):
        return "".join(p["text"] for p in self.payloads("token"))


def ping_registry(**extra):
    """A tiny registry so guardrail tests are deterministic and independent of the database."""
    tools = {"ping": chattools.Tool(
        "ping", "A tool used by the guardrail tests to echo a value back.",
        chattools._obj({"n": chattools._INT}), lambda a, c: {"pong": a.get("n", 0)})}
    for name, fn in extra.items():
        tools[name] = chattools.Tool(name, f"Test tool {name} for the guardrail tests.",
                                     chattools._obj({}), fn)
    return tools


# ---------------------------------------------------------------------- the happy path

def test_a_tool_using_turn_reaches_a_grounded_answer(monkeypatch, tmp_path):
    """The whole point: the model asks for a number, the tool supplies it from the database,
    and the final answer is streamed. The tool call is not decorative."""
    setup_db(monkeypatch, tmp_path, items=[
        {"id": "a", "title": "A", "system": "NES"}, {"id": "b", "title": "B", "system": "NES"}])
    FakeOllama(turns=[
        {"thinking": ["I should count them. "],
         "tool_calls": [{"name": "library_summary", "arguments": {}}]},
        {"tokens": ["You have ", "2", " items cataloged."],
         "usage": {"eval_count": 11, "total_duration": 999}},
    ]).install(monkeypatch)
    rec = Recorder()
    reg = chattools.build_registry()
    out = chatagent.run_turn(None, "how many items do I have?", reg, rec)

    assert rec.names() == ["start", "thinking", "tool_start", "tool_result", "token", "token",
                           "token", "done"]
    assert rec.text() == "You have 2 items cataloged."
    assert rec.payloads("tool_start")[0]["name"] == "library_summary"
    result = rec.payloads("tool_result")[0]
    assert result["ok"] is True and result["data"]["cataloged"] == 2
    assert out["usage"]["eval_count"] == 11
    assert out["stopped"] is None

    # and the exchange is persisted so the next turn has the context
    roles = [m["role"] for m in chatstore.history(out["session_id"])]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_the_tool_catalog_is_advertised_to_the_model(monkeypatch, tmp_path):
    """A tool the model was never told about does not exist, however well it is implemented."""
    setup_db(monkeypatch, tmp_path)
    fake = FakeOllama(turns=[{"tokens": ["ok"]}]).install(monkeypatch)
    chatagent.run_turn(None, "hi", chattools.build_registry(), Recorder())
    offered = fake.tool_names_sent(0)
    assert "library_summary" in offered and "library_audit" in offered
    assert len(offered) == len(chattools.build_registry())


def test_a_turn_starts_a_session_and_titles_it(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tokens": ["ok"]}]).install(monkeypatch)
    rec = Recorder()
    out = chatagent.run_turn(None, "  what am I missing on the NES?  ",
                             chattools.build_registry(), rec)
    assert rec.payloads("start")[0]["session_id"] == out["session_id"]
    assert chatstore.session(out["session_id"])["title"] == "what am I missing on the NES?"


def test_an_existing_session_continues_rather_than_starting_a_new_one(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session("existing")
    chatstore.append(sid, "user", "earlier")
    chatstore.append(sid, "assistant", "earlier answer")
    fake = FakeOllama(turns=[{"tokens": ["ok"]}]).install(monkeypatch)
    chatagent.run_turn(sid, "follow up", chattools.build_registry(), Recorder())
    sent = fake.calls[0][1]["messages"]
    assert sent[0]["role"] == "system"
    assert [m["content"] for m in sent[1:]] == ["earlier", "earlier answer", "follow up"]


def test_history_is_capped_to_the_configured_window(monkeypatch, tmp_path):
    """Old turns fall out of the verbatim window; without the cap a long thread would exceed
    the local model's context on every single request."""
    setup_db(monkeypatch, tmp_path)
    monkeypatch.setenv("ROMCOM_CHAT_HISTORY_MAX", "4")
    from romcom.config import invalidate
    invalidate()
    sid = chatstore.new_session("long")
    for i in range(10):
        chatstore.append(sid, "user", f"q{i}")
        chatstore.append(sid, "assistant", f"a{i}")
    fake = FakeOllama(turns=[{"tokens": ["ok"]}]).install(monkeypatch)
    chatagent.run_turn(sid, "latest", chattools.build_registry(), Recorder())
    sent = [m["content"] for m in fake.calls[0][1]["messages"] if m["role"] != "system"]
    # The window is the last 4 stored messages — which lands mid-exchange at a8 — so it is
    # trimmed forward to the nearest user turn rather than opening on a dangling answer.
    assert sent == ["q9", "a9", "latest"]


# -------------------------------------------------------------------------- guardrails

def test_the_iteration_cap_stops_a_runaway_tool_loop(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    # Distinct arguments each time, so the stuck guard is NOT what stops this — the cap is.
    FakeOllama(turns=[{"tool_calls": [{"name": "ping", "arguments": {"n": i}}]}
                      for i in range(chatagent.MAX_ITERATIONS + 3)]).install(monkeypatch)
    rec = Recorder()
    out = chatagent.run_turn(None, "loop forever", ping_registry(), rec)
    assert out["stopped"] and "without a final answer" in out["stopped"]
    assert rec.names().count("tool_start") == chatagent.MAX_ITERATIONS
    assert rec.names()[-1] == "done"          # a terminal event still arrives


def test_the_same_call_with_the_same_arguments_three_times_stops_the_turn(monkeypatch, tmp_path):
    """The characteristic local-model failure: re-issuing a call that returned something
    unhelpful, forever."""
    setup_db(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tool_calls": [{"name": "ping", "arguments": {"n": 1}}]}
                      for _ in range(5)]).install(monkeypatch)
    rec = Recorder()
    out = chatagent.run_turn(None, "stuck", ping_registry(), rec)
    assert "repeated" in out["stopped"] and "ping" in out["stopped"]
    assert rec.names().count("tool_start") == chatagent.STUCK_REPEAT - 1
    assert rec.names()[-1] == "done"


def test_a_repeated_call_with_different_arguments_is_allowed(monkeypatch, tmp_path):
    """Paging a list calls the same tool repeatedly on purpose. Counting *consecutive
    identical* calls rather than total calls is what keeps that legitimate."""
    setup_db(monkeypatch, tmp_path)
    FakeOllama(turns=[
        {"tool_calls": [{"name": "ping", "arguments": {"n": 1}}]},
        {"tool_calls": [{"name": "ping", "arguments": {"n": 2}}]},
        {"tool_calls": [{"name": "ping", "arguments": {"n": 3}}]},
        {"tool_calls": [{"name": "ping", "arguments": {"n": 4}}]},
        {"tokens": ["done paging"]},
    ]).install(monkeypatch)
    out = chatagent.run_turn(None, "page through", ping_registry(), Recorder())
    assert out["stopped"] is None and out["content"] == "done paging"


def test_a_tool_error_is_fed_back_and_the_turn_continues(monkeypatch, tmp_path):
    """A tool error is information the model can act on — ending the turn instead would make
    every recoverable mistake fatal."""
    setup_db(monkeypatch, tmp_path)
    FakeOllama(turns=[
        {"tool_calls": [{"name": "explode", "arguments": {}}]},
        {"tokens": ["That failed, so here is what I know."]},
    ]).install(monkeypatch)
    rec = Recorder()
    out = chatagent.run_turn(None, "try it", ping_registry(explode=lambda a, c: 1 / 0), rec)
    assert rec.payloads("tool_result")[0]["ok"] is False
    assert "ZeroDivisionError" in rec.payloads("tool_result")[0]["error"]
    assert out["content"] == "That failed, so here is what I know."
    assert out["stopped"] is None


def test_an_unknown_tool_is_reported_back_to_the_model(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    FakeOllama(turns=[
        {"tool_calls": [{"name": "list_itemz", "arguments": {}}]},
        {"tokens": ["corrected"]},
    ]).install(monkeypatch)
    rec = Recorder()
    out = chatagent.run_turn(None, "typo tool", ping_registry(), rec)
    assert rec.payloads("tool_result")[0]["ok"] is False
    assert out["content"] == "corrected"


def test_a_model_that_fails_entirely_still_ends_the_turn(monkeypatch, tmp_path):
    """Ollama being down must leave the UI usable, not hang it."""
    setup_db(monkeypatch, tmp_path)
    FakeOllama(turns=[{"status": 500, "error": "connection refused"}]).install(monkeypatch)
    rec = Recorder()
    out = chatagent.run_turn(None, "hello?", ping_registry(), rec)
    assert "connection refused" in out["stopped"]
    assert rec.names()[0] == "start" and rec.names()[-1] == "done"
    assert rec.payloads("error")[0]["message"]


def test_cancelling_stops_the_turn_at_the_next_boundary(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    import threading
    FakeOllama(turns=[{"tokens": ["never sent"]}]).install(monkeypatch)
    cancel = threading.Event()
    cancel.set()
    rec = Recorder()
    out = chatagent.run_turn(None, "stop", ping_registry(), rec, cancel=cancel)
    assert out["stopped"] == "cancelled"
    assert rec.payloads("error")[0]["message"] == "cancelled"
    assert rec.names()[-1] == "done"      # the UI's composer is always re-enabled


def test_an_abandoned_turn_leaves_no_empty_assistant_message(monkeypatch, tmp_path):
    """An empty assistant row would be replayed as context and read as 'the model said
    nothing here'."""
    setup_db(monkeypatch, tmp_path)
    import threading
    FakeOllama(turns=[]).install(monkeypatch)
    cancel = threading.Event(); cancel.set()
    out = chatagent.run_turn(None, "cancelled", ping_registry(), Recorder(), cancel=cancel)
    roles = [m["role"] for m in chatstore.history(out["session_id"])]
    assert roles == ["user"]


# ------------------------------------------------------------------ context assembly

def test_the_system_prompt_teaches_the_lifecycle_distinctions():
    """The model's answers depend on knowing that DOWNLOADED is not VERIFIED. If this drifts,
    the assistant starts confidently telling the owner that unverified files are fine."""
    p = chatagent.system_prompt()
    assert "DOWNLOADED" in p and "VERIFIED" in p
    assert "does not mean" in p.lower() or "DOES NOT mean" in p
    assert "library_audit" in p and "facets" in p
    assert "total" in p            # the anti-false-completeness instruction


def test_the_verbatim_window_is_the_only_history_injected(monkeypatch, tmp_path):
    """Stage 2 has no memory yet; this pins the injection order so Stage 4 has a defined
    place to add to rather than a free-for-all."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session("s")
    chatstore.append(sid, "user", "hello")
    msgs = chatagent._assemble(sid, chattools.build_registry())
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "hello"}


# ------------------------------------------------------------------- approvals

def gated_registry(monkeypatch, tmp_path, **ctx):
    """A registry with one gated tool over a real temp DB."""
    setup_db(monkeypatch, tmp_path,
             items=[{"id": "a", "status": "VERIFIED", "wanted": 0, "authorized": 0}])
    r = chattools.build_registry(chattools.Ctx(**ctx))
    return r


def test_a_gated_call_pauses_the_turn_instead_of_failing_it(monkeypatch, tmp_path):
    """The pause is not an error: the owner is being asked, and the UI needs `paused` to
    distinguish "waiting on you" from "something broke"."""
    r = gated_registry(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tool_calls": [{"name": "mark_all_owned", "arguments": {}}]}]).install(monkeypatch)
    rec = Recorder()
    out = chatagent.run_turn(None, "mark everything owned", r, rec)
    assert "approval_required" in rec.names()
    assert out["paused"] is True and out["stopped"] is None
    assert "error" not in rec.names()          # nothing went wrong
    card = rec.payloads("approval_required")[0]
    assert card["tool"] == "mark_all_owned" and card["approval_id"]
    assert "Mark every item" in card["summary"]


def test_a_paused_turn_records_no_outcome_for_the_call(monkeypatch, tmp_path):
    """Nothing may claim the call ran, and nothing may claim it failed: the owner has not
    answered. The model's call is left open so `resolve_approval` can answer it with the real
    outcome — one call, one reply, rather than a "not run" row followed by a second one."""
    r = gated_registry(monkeypatch, tmp_path)
    FakeOllama(turns=[{"tool_calls": [{"name": "mark_all_owned", "arguments": {}}]}]).install(monkeypatch)
    rec = Recorder()
    sid = chatagent.run_turn(None, "mark everything owned", r, rec)["session_id"]
    assert [m["role"] for m in chatstore.history(sid)] == ["user", "assistant"]
    assert connect().execute("SELECT wanted FROM items WHERE id='a'").fetchone()["wanted"] == 0


def test_approving_runs_the_call_and_continues_the_conversation(monkeypatch, tmp_path):
    """One continuous reply: the decision lands in the history where the model left its call
    waiting, and the loop carries on from there."""
    r = gated_registry(monkeypatch, tmp_path)
    FakeOllama(turns=[
        {"tool_calls": [{"name": "mark_all_owned", "arguments": {}}]},
        {"tokens": ["Done — everything on disk is now wanted."]},
    ]).install(monkeypatch)
    rec = Recorder()
    sid = chatagent.run_turn(None, "mark everything owned", r, rec)["session_id"]
    aid = rec.payloads("approval_required")[0]["approval_id"]

    # `resolve_approval` is what decides — the route hands it the answer, not a pre-flipped
    # row, so there is only one place a decision can be made.
    rec2 = Recorder()
    out = chatagent.resolve_approval(aid, True, r, rec2)
    # The continuation reuses the same session and streams the model's answer.
    assert out["session_id"] == sid
    assert rec2.payloads("start")[0] == {"session_id": sid, "user_message_id": None}
    assert rec2.names().count("done") == 1
    assert "wanted" in rec2.text()
    assert connect().execute("SELECT wanted FROM items WHERE id='a'").fetchone()["wanted"] == 1
    # And the transcript is now: question, the model's call, the tool result, the answer —
    # four rows, because the pause deliberately wrote no reply of its own.
    roles = [m["role"] for m in chatstore.history(sid)]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_the_resumed_turn_sees_the_tool_result_as_a_tool_message(monkeypatch, tmp_path):
    """Not as a user message: the owner's 'yes' is a fact about the call, and feeding it in
    as something the owner typed would make the model answer the wrong question."""
    r = gated_registry(monkeypatch, tmp_path)
    FakeOllama(turns=[
        {"tool_calls": [{"name": "mark_all_owned", "arguments": {}}]},
        {"tokens": ["ok"]},
    ]).install(monkeypatch)
    rec = Recorder()
    sid = chatagent.run_turn(None, "mark everything owned", r, rec)["session_id"]
    aid = rec.payloads("approval_required")[0]["approval_id"]
    chatagent.resolve_approval(aid, True, r, Recorder())

    msgs = chatagent._assemble(sid, r)
    assert msgs[-2]["role"] == "tool" and msgs[-2]["tool_name"] == "mark_all_owned"
    assert msgs[-1]["role"] == "assistant"
    assert not any(m["role"] == "user" and "approv" in (m["content"] or "").lower() for m in msgs)


def test_declining_is_told_to_the_model_so_it_can_respond(monkeypatch, tmp_path):
    """A refusal is information. Left as a dangling tool call, the model would either invent
    an outcome or retry — both worse than being told no."""
    r = gated_registry(monkeypatch, tmp_path)
    FakeOllama(turns=[
        {"tool_calls": [{"name": "mark_all_owned", "arguments": {}}]},
        {"tokens": ["Understood — I'll leave them alone."]},
    ]).install(monkeypatch)
    rec = Recorder()
    sid = chatagent.run_turn(None, "mark everything owned", r, rec)["session_id"]
    aid = rec.payloads("approval_required")[0]["approval_id"]

    rec2 = Recorder()
    chatagent.resolve_approval(aid, False, r, rec2)
    assert "leave them alone" in rec2.text()
    assert connect().execute("SELECT wanted FROM items WHERE id='a'").fetchone()["wanted"] == 0
    assert chatstore.approval(aid)["result"]


def test_an_already_decided_approval_cannot_be_resolved_again(monkeypatch, tmp_path):
    r = gated_registry(monkeypatch, tmp_path)
    aid = chatstore.request_approval(1, "mark_all_owned", {}, "s")
    chatagent.resolve_approval(aid, True, r, Recorder())
    rec = Recorder()
    assert chatagent.resolve_approval(aid, True, r, rec) is None
    assert "already used" in rec.payloads("error")[0]["message"]
    assert "done" not in rec.names()      # no second turn was started


def test_an_unknown_approval_reports_rather_than_raises(monkeypatch, tmp_path):
    r = gated_registry(monkeypatch, tmp_path)
    rec = Recorder()
    assert chatagent.resolve_approval(999, True, r, rec) is None
    assert "no such approval" in rec.payloads("error")[0]["message"]
