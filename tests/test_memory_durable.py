"""Long-term memory must survive the embedding model being down."""
import romcom.chatstore as cs
import romcom.llmclient as _llm
from romcom.db import connect


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "m.db"))
    from romcom.config import invalidate
    invalidate()
    return connect()


def _break_embeddings(monkeypatch):
    def down(*_a, **_k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(cs, "embed_cached", down)


def _fake_embeddings(monkeypatch, dim=4):
    monkeypatch.setattr(cs, "embed_cached",
                        lambda texts, **_k: ([[0.5] * dim for _ in texts], dim, "fake-embed"))


def test_an_outage_queues_the_chunk_instead_of_dropping_it(monkeypatch, tmp_path):
    """The bug: summarization advances its watermark as soon as the summary text is saved,
    so a swallowed embedding failure meant that stretch of conversation could never become
    recallable memory again — a silent, permanent hole whenever Ollama was down."""
    db = _db(monkeypatch, tmp_path)
    _break_embeddings(monkeypatch)
    assert cs.store_chunk_durable("session_summary", "we decided to collect PS3", ref_id=1) is None
    assert cs.pending_chunks() == 1
    row = db.execute("SELECT kind,ref_id,text,last_error FROM chat_memory_pending").fetchone()
    assert row["text"] == "we decided to collect PS3" and row["ref_id"] == "1"
    assert "connection refused" in row["last_error"]


def test_the_queue_drains_once_the_model_returns(monkeypatch, tmp_path):
    db = _db(monkeypatch, tmp_path)
    _break_embeddings(monkeypatch)
    cs.store_chunk_durable("session_summary", "we decided to collect PS3", ref_id=1)
    _fake_embeddings(monkeypatch)
    assert cs.flush_pending_chunks() == 1
    assert cs.pending_chunks() == 0
    assert db.execute("SELECT text FROM chat_memory_chunks").fetchone()["text"] == "we decided to collect PS3"


def test_a_still_broken_model_keeps_the_work_and_records_why(monkeypatch, tmp_path):
    """A failed flush must not consume the backlog, and must leave a reason behind."""
    db = _db(monkeypatch, tmp_path)
    _break_embeddings(monkeypatch)
    cs.store_chunk_durable("session_summary", "a", ref_id=1)
    assert cs.flush_pending_chunks() == 0
    assert cs.pending_chunks() == 1
    assert db.execute("SELECT attempts FROM chat_memory_pending").fetchone()["attempts"] == 1


def test_the_flush_stops_at_the_first_failure(monkeypatch, tmp_path):
    """If one embedding call fails the model is down and the rest fail identically —
    grinding the whole backlog just makes a slow turn slower."""
    _db(monkeypatch, tmp_path)
    _break_embeddings(monkeypatch)
    for i in range(5):
        cs.store_chunk_durable("session_summary", f"note {i}", ref_id=i)
    calls = []
    def one_then_fail(texts, **_k):
        calls.append(texts)
        if len(calls) > 1:
            raise RuntimeError("still down")
        return ([[0.5] * 4], 4, "fake-embed")
    monkeypatch.setattr(cs, "embed_cached", one_then_fail)
    assert cs.flush_pending_chunks() == 1
    assert len(calls) == 2            # one success, one failure, then it stopped
    assert cs.pending_chunks() == 4


def test_empty_text_is_not_queued(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    _break_embeddings(monkeypatch)
    assert cs.store_chunk_durable("session_summary", "   ", ref_id=1) is None
    assert cs.pending_chunks() == 0


def test_a_healthy_model_stores_directly_and_queues_nothing(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    _fake_embeddings(monkeypatch)
    assert cs.store_chunk_durable("session_summary", "fine", ref_id=1) is not None
    assert cs.pending_chunks() == 0


# ------------------------------------------- short threads must still reach memory

def test_a_short_finished_thread_is_summarized_so_it_is_recallable(monkeypatch, tmp_path):
    """Recall is built from session summaries, and summarization only fires once a thread is
    long enough to be worth compressing — so a two-message exchange left no trace at all,
    ever. Four sessions in the real library are exactly that and are unrecallable."""
    _db(monkeypatch, tmp_path)
    _fake_embeddings(monkeypatch)
    sid = cs.new_session("short one")
    cs.append(sid, "user", "the SNES folder is H:/Games/snes")
    cs.append(sid, "assistant", "noted")
    # Backdate it so it counts as finished rather than in-flight.
    with cs.connect() as db:
        db.execute("UPDATE chat_messages SET created_at='2000-01-01T00:00:00' WHERE session_id=?", (sid,))
    monkeypatch.setattr(_llm, "chat", lambda *a, **k: {"content": "Owner's SNES folder: H:/Games/snes"})
    assert cs.summarize_stale_sessions() == [sid]
    assert "SNES folder" in (cs.session(sid)["summary"] or "")
    assert cs.recall("where do snes roms live", k=3, min_score=0.0), "should now be recallable"


def test_an_active_thread_is_left_alone(monkeypatch, tmp_path):
    """Idleness stands in for a session-close event, so a thread still being typed into must
    not be compressed out from under the person using it.

    Timezone-sensitive, and that is the point. This is the test that failed only on CI:
    the cutoff was built with datetime.now().isoformat() ('2026-09-23T19:56:12', local)
    and compared against CURRENT_TIMESTAMP ('2026-09-23 15:51:18', UTC). Since ' ' sorts
    before 'T', every session read as stale — but a negative UTC offset moved the date
    forward enough to hide it locally. Running in UTC, it fired every time."""
    _db(monkeypatch, tmp_path)
    sid = cs.new_session("live one")
    cs.append(sid, "user", "still talking")
    called = []
    monkeypatch.setattr(_llm, "chat", lambda *a, **k: called.append(1) or {"content": "x"})
    assert cs.summarize_stale_sessions() == []
    assert not called


def test_it_is_bounded_per_turn(monkeypatch, tmp_path):
    """This runs on an ordinary turn and each session costs a model round trip."""
    _db(monkeypatch, tmp_path)
    _fake_embeddings(monkeypatch)
    for i in range(6):
        sid = cs.new_session(f"s{i}")
        cs.append(sid, "user", f"note {i}")
    with cs.connect() as db:
        db.execute("UPDATE chat_messages SET created_at='2000-01-01T00:00:00'")
    monkeypatch.setattr(_llm, "chat", lambda *a, **k: {"content": "summary"})
    assert len(cs.summarize_stale_sessions(limit=2)) == 2
