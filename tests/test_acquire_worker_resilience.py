"""A direct-download worker must outlive the items it fails on."""
import sqlite3
import pytest
import romcom.acquirer as acq


def test_a_locked_write_is_retried_then_succeeds(monkeypatch):
    """busy_timeout covers ordinary contention; this covers what it does not — one long
    write transaction elsewhere holding the lock for longer than the timeout."""
    monkeypatch.setattr(acq.time, "sleep", lambda _s: None)
    attempts = []

    class DB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params):
            attempts.append(1)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")
    assert acq._retry_write(DB(), "UPDATE items SET status='DOWNLOADED' WHERE id=?", ("x",)) is True
    assert len(attempts) == 3


def test_a_permanently_locked_write_still_raises(monkeypatch):
    """Retrying forever would hide a real problem; the caller's handler must still see it."""
    monkeypatch.setattr(acq.time, "sleep", lambda _s: None)

    class DB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params):
            raise sqlite3.OperationalError("database is locked")
    with pytest.raises(sqlite3.OperationalError):
        acq._retry_write(DB(), "UPDATE x SET y=1", ())


def test_a_non_lock_error_is_not_retried(monkeypatch):
    """A syntax or schema error will fail identically every time."""
    monkeypatch.setattr(acq.time, "sleep", lambda _s: None)
    calls = []

    class DB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params):
            calls.append(1)
            raise sqlite3.OperationalError("no such column: nope")
    with pytest.raises(sqlite3.OperationalError):
        acq._retry_write(DB(), "UPDATE x SET nope=1", ())
    assert len(calls) == 1


def test_the_worker_loop_catches_everything(monkeypatch):
    """The regression, pinned at the source level because the worker is a closure inside
    `run`. Three workers were killed by an uncaught `database is locked` on the status
    write; the sweep then stopped making progress with items still queued, which looks like
    a stall rather than a crash. The loop body must be guarded, not just followed by a
    `finally` that only marks the queue item done."""
    import inspect
    src = inspect.getsource(acq)
    start = src.index("def _direct_worker")
    body = src[start:src.index("n_direct = max(", start)]
    assert "except Exception as ex:" in body, "the worker loop has no except — one bad item kills it"
    assert body.index("except Exception as ex:") < body.index("finally:\n                direct_q.task_done()")
