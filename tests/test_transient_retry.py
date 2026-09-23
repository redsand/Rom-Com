"""A transient upstream failure must not end the turn — but a retry must never duplicate
output that already reached the browser."""
import pytest
import romcom.llmclient as lc


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(lc.time, "sleep", lambda _s: None)
    monkeypatch.setattr(lc, "resolve_model", lambda: "test-model")


def test_an_upstream_500_is_retried(monkeypatch):
    """The reported failure: a cloud-hosted model returned 'Internal Server Error (ref: ...)'
    mid-stream and the turn died in the UI with an opaque reference and no way forward."""
    calls = []
    def run(body, on_event, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Internal Server Error (ref: dc10f0db-82fc-4877-a423)")
        return {"content": "recovered", "tool_calls": []}
    monkeypatch.setattr(lc, "_run", run)
    assert lc.chat([{"role": "user", "content": "hi"}])["content"] == "recovered"
    assert len(calls) == 2


def test_a_retry_never_duplicates_output_already_streamed(monkeypatch):
    """Once tokens have reached the browser, re-running the turn would append a second
    answer to the first. Past that point the error has to surface instead."""
    def run(body, on_event, timeout):
        on_event("token", {"text": "half an answer"})
        raise RuntimeError("Internal Server Error (ref: abc)")
    monkeypatch.setattr(lc, "_run", run)
    seen = []
    with pytest.raises(RuntimeError, match="Internal Server Error"):
        lc.chat([{"role": "user", "content": "hi"}], on_event=lambda n, p: seen.append(n))
    assert seen == ["token"]          # emitted once, not twice


def test_a_real_error_is_not_retried(monkeypatch):
    """Retrying a permanent failure just makes the user wait three times as long for it."""
    calls = []
    def run(body, on_event, timeout):
        calls.append(1)
        raise RuntimeError("model 'nope' not found")
    monkeypatch.setattr(lc, "_run", run)
    with pytest.raises(RuntimeError, match="not found"):
        lc.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 1


def test_it_gives_up_rather_than_retrying_forever(monkeypatch):
    calls = []
    def run(body, on_event, timeout):
        calls.append(1)
        raise RuntimeError("503 Service Unavailable")
    monkeypatch.setattr(lc, "_run", run)
    with pytest.raises(RuntimeError, match="503"):
        lc.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == lc._TRANSIENT_ATTEMPTS


def test_context_overflow_still_prunes_and_is_not_treated_as_transient(monkeypatch):
    """The pre-existing fail-soft path has to survive the new wrapper around it."""
    calls = []
    def run(body, on_event, timeout):
        calls.append(len(body["messages"]))
        if len(calls) == 1:
            raise lc._ContextOverflow("context length exceeded")
        return {"content": "pruned ok", "tool_calls": []}
    monkeypatch.setattr(lc, "_run", run)
    monkeypatch.setattr(lc, "prune", lambda msgs, aggressive=False: [{"role": "user", "content": "x"}])
    msgs = [{"role": "user", "content": "a"}, {"role": "tool", "content": "b" * 100}]
    assert lc.chat(msgs)["content"] == "pruned ok"
    assert calls == [2, 1]            # full, then pruned
