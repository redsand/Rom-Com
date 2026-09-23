"""The verbatim window, and what it must never forget."""
import romcom.chatagent as ca
import romcom.chatstore as cs
from romcom.db import connect


def _session(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "h.db"))
    from romcom.config import invalidate
    invalidate()
    connect()
    return cs.new_session() if hasattr(cs, "new_session") else cs.start_session()


def _turn(sid, question, tool_rows=20, answer="Here you go.", payload=900):
    """One realistic exchange: a question, then the tool storm a real turn produces."""
    cs.append(sid, "user", question)
    cs.append(sid, "assistant", "")
    for i in range(tool_rows):
        cs.append(sid, "tool", "x" * payload, tool_name=f"list_items_{i}")
    cs.append(sid, "assistant", answer)


def test_earlier_exchanges_survive_a_tool_heavy_thread(monkeypatch, tmp_path):
    """The regression this fixes. Counted in rows, one tool-heavy turn filled the whole
    window, the trim-to-a-user-turn step then discarded everything up to the newest
    question, and the model entered the turn having forgotten the conversation. Real
    sessions of 126 and 32 messages both produced a zero-row window."""
    sid = _session(monkeypatch, tmp_path)
    _turn(sid, "first question")
    _turn(sid, "second question")
    cs.append(sid, "user", "third question")

    rows = ca._window(sid)
    said = [r["content"] for r in rows if r["role"] == "user"]
    assert said == ["first question", "second question", "third question"], said


def test_the_window_starts_on_a_question(monkeypatch, tmp_path):
    """A window opening mid-exchange leaves an answer with no question, which reads to the
    model as something it volunteered."""
    sid = _session(monkeypatch, tmp_path)
    _turn(sid, "only question")
    cs.append(sid, "user", "next")
    assert ca._window(sid)[0]["role"] == "user"


def test_bulky_tool_output_is_trimmed_before_anything_said_is_touched(monkeypatch, tmp_path):
    """Tool output is bulky and reconstructible; what was actually said is neither."""
    sid = _session(monkeypatch, tmp_path)
    _turn(sid, "q1", tool_rows=40, answer="A distinctive answer worth keeping.", payload=2000)
    cs.append(sid, "user", "q2")

    rows = ca._window(sid)
    assert sum(len(r["content"] or "") for r in rows) <= ca.HISTORY_BUDGET
    assert any("[trimmed]" in (r["content"] or "") for r in rows if r["role"] == "tool")
    # Nothing either party said was truncated.
    for r in rows:
        if r["role"] in ("user", "assistant") and r["content"]:
            assert "[trimmed]" not in r["content"]
    assert "A distinctive answer worth keeping." in [r["content"] for r in rows]


def test_the_budget_is_actually_enforced(monkeypatch, tmp_path):
    """Trimming tool payloads alone does not bound a long thread — most of it is prose. The
    window drops whole oldest exchanges rather than mangling sentences, and the rolling
    summary covers what falls off."""
    sid = _session(monkeypatch, tmp_path)
    for i in range(8):
        _turn(sid, f"question {i}", tool_rows=10, answer="z" * 4000, payload=1500)
    cs.append(sid, "user", "latest")

    rows = ca._window(sid)
    assert sum(len(r["content"] or "") for r in rows) <= ca.HISTORY_BUDGET
    said = [r["content"] for r in rows if r["role"] == "user"]
    assert said[-1] == "latest"
    assert len(said) >= 2, "must never collapse to just the new question"


def test_a_short_thread_is_kept_whole(monkeypatch, tmp_path):
    sid = _session(monkeypatch, tmp_path)
    _turn(sid, "q1", tool_rows=2, payload=50)
    cs.append(sid, "user", "q2")
    said = [r["content"] for r in ca._window(sid) if r["role"] == "user"]
    assert said == ["q1", "q2"]
