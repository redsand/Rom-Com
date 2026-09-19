"""Chat persistence. The load-bearing property is that a stored thread round-trips back into
exactly the message shape Ollama needs, because a turn that reloads a subtly different
conversation than the model saw is a bug that only shows up as confused answers."""
import json

from romcom import chatstore
from romcom.db import connect


def setup_db(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    return connect()


def test_a_new_session_is_empty_and_listed(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session("Find the ghosts")
    s = chatstore.session(sid)
    assert s["title"] == "Find the ghosts"
    assert chatstore.history(sid) == []
    assert [x["id"] for x in chatstore.sessions()] == [sid]


def test_an_untitled_session_gets_a_title_from_its_first_message(monkeypatch, tmp_path):
    """A session list of seven rows all called "New chat" is useless; the first user turn is
    what the owner will recognize it by."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    assert chatstore.session(sid)["title"] is None
    chatstore.touch(sid, title="what am I missing on the NES?")
    assert chatstore.session(sid)["title"] == "what am I missing on the NES?"


def test_messages_come_back_oldest_first_and_page_from_the_end(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    ids = [chatstore.append(sid, "user", f"m{i}") for i in range(10)]
    assert [m["content"] for m in chatstore.history(sid)] == [f"m{i}" for i in range(10)]
    tail = chatstore.history(sid, limit=3)
    assert [m["content"] for m in tail] == ["m7", "m8", "m9"]
    assert [m["id"] for m in tail] == ids[-3:]


def test_a_thread_round_trips_into_ollamas_message_shape(monkeypatch, tmp_path):
    """This is the contract. A tool result is matched back to its call BY NAME, so the name
    must survive; the assistant's own tool_calls must come back as structured calls, not as
    text the model has to re-parse."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    chatstore.append(sid, "user", "how many are missing?")
    calls = [{"id": "c1", "name": "list_items", "arguments": {"view": "missing"}}]
    chatstore.append(sid, "assistant", "", thinking="I should count them", tool_args=calls)
    chatstore.append(sid, "tool", '{"ok": true, "data": {"total": 3}}', tool_name="list_items")
    chatstore.append(sid, "assistant", "Three are missing.")
    msgs = chatstore.to_messages(chatstore.history(sid))
    assert msgs[0] == {"role": "user", "content": "how many are missing?"}
    assert msgs[1]["role"] == "assistant" and msgs[1]["tool_calls"] == calls
    assert msgs[2]["role"] == "tool" and msgs[2]["tool_name"] == "list_items"
    assert msgs[3] == {"role": "assistant", "content": "Three are missing."}


def test_reasoning_is_stored_but_never_replayed_into_context(monkeypatch, tmp_path):
    """`thinking` is for the UI's reasoning bubble. Re-feeding a local model its own prior
    reasoning degrades it and inflates the prompt for nothing, so it must not appear in the
    rebuilt messages."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    chatstore.append(sid, "assistant", "The answer is 3.", thinking="secret chain of thought")
    stored = chatstore.history(sid)[0]
    assert stored["thinking"] == "secret chain of thought"
    msgs = chatstore.to_messages(chatstore.history(sid))
    assert "thinking" not in msgs[0] and "secret" not in json.dumps(msgs)


def test_tool_results_are_capped_at_insert(monkeypatch, tmp_path):
    """A 40 KB library_summary replayed on every subsequent turn is what makes a tool-heavy
    thread blow the context window. The in-flight turn still sees the full result."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    chatstore.append(sid, "tool", "Z" * 9000, tool_name="library_summary")
    stored = chatstore.history(sid)[0]["content"]
    assert len(stored) < chatstore.TOOL_RESULT_CAP + 100
    assert "truncated" in stored and "9000" in stored


def test_a_short_tool_result_is_not_annotated(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    chatstore.append(sid, "tool", '{"ok":true}', tool_name="doctor")
    assert chatstore.history(sid)[0]["content"] == '{"ok":true}'


def test_thinking_can_be_backfilled_after_a_stream_finishes(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    mid = chatstore.append(sid, "assistant", "answer")
    chatstore.set_thinking(mid, "reasoned about it")
    assert chatstore.history(sid)[0]["thinking"] == "reasoned about it"


def test_drop_after_removes_a_half_written_turn(monkeypatch, tmp_path):
    """If a turn is abandoned mid-stream, its partial assistant reply must not be replayed
    as context on the next turn."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session()
    keep = chatstore.append(sid, "user", "question")
    chatstore.append(sid, "assistant", "half an ans")
    chatstore.append(sid, "tool", "{}", tool_name="facets")
    assert chatstore.drop_after(sid, keep) == 2
    assert [m["content"] for m in chatstore.history(sid)] == ["question"]


def test_deleting_a_session_takes_its_messages_but_not_the_audit_trail(monkeypatch, tmp_path):
    """An audit trail that vanishes when someone tidies their chat list is worthless."""
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session("doomed")
    chatstore.append(sid, "user", "hi")
    chatstore.log_call(sid, "list_items", {"view": "missing"}, True, "low", "ok")
    assert chatstore.delete_session(sid) == 1
    assert chatstore.session(sid) is None
    assert chatstore.history(sid) == []
    assert [r["tool"] for r in chatstore.recent_calls(sid)] == ["list_items"]


def test_the_session_list_reports_message_counts_and_a_preview(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    sid = chatstore.new_session("thread")
    chatstore.append(sid, "user", "the first thing I asked")
    chatstore.append(sid, "assistant", "an answer")
    row = chatstore.sessions()[0]
    assert row["messages"] == 2 and row["first_user"] == "the first thing I asked"


def test_sessions_are_ordered_by_most_recent_activity(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    a = chatstore.new_session("older")
    b = chatstore.new_session("newer")
    db = connect()
    with db:
        db.execute("UPDATE chat_sessions SET updated_at='2020-01-01 00:00:00' WHERE id=?", (a,))
        db.execute("UPDATE chat_sessions SET updated_at='2030-01-01 00:00:00' WHERE id=?", (b,))
    assert [s["id"] for s in chatstore.sessions()] == [b, a]


def test_the_audit_log_records_arguments_and_outcome(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    chatstore.log_call(3, "list_items", {"system": "NES", "limit": 50}, True, "low", "12 rows")
    chatstore.log_call(3, "get_item", {"ident": "nope"}, False, "low", "not found")
    rows = chatstore.recent_calls(3)
    assert rows[0]["tool"] == "get_item" and rows[0]["ok"] == 0
    assert json.loads(rows[1]["arguments"])["system"] == "NES"
    assert rows[1]["result_summary"] == "12 rows"


# ---------------------------------------------------------------------------- approvals

def test_a_request_starts_pending_and_lists_against_its_session(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    aid = chatstore.request_approval(4, "mark_all_owned", {}, "Mark everything owned")
    row = chatstore.approval(aid)
    assert row["status"] == "pending" and row["session_id"] == 4
    assert json.loads(row["arguments"]) == {}
    assert [a["id"] for a in chatstore.pending_approvals(4)] == [aid]
    assert chatstore.pending_approvals(9) == []


def test_a_decision_is_recorded_once_and_only_from_pending(monkeypatch, tmp_path):
    """Idempotence is enforced by the UPDATE's WHERE clause, not by the caller remembering
    to check — a double-click must not be able to approve twice."""
    setup_db(monkeypatch, tmp_path)
    aid = chatstore.request_approval(1, "watcher_toggle", {"on": True}, "Turn the watcher on")
    assert chatstore.decide_approval(aid, True)["status"] == "approved"
    assert chatstore.decide_approval(aid, True) is None
    assert chatstore.decide_approval(aid, False) is None
    assert chatstore.approval(aid)["decided_at"]


def test_spending_an_approval_consumes_it(monkeypatch, tmp_path):
    setup_db(monkeypatch, tmp_path)
    aid = chatstore.request_approval(1, "mark_all_owned", {}, "s")
    assert chatstore.spend_approval(aid) is None            # not approved yet
    chatstore.decide_approval(aid, True)
    assert chatstore.spend_approval(aid)["status"] == "used"
    assert chatstore.spend_approval(aid) is None            # and not twice
    assert chatstore.approval(aid)["status"] == "used"
