from romcom import llm
from romcom.config import invalidate


def env(monkeypatch, tmp_path, enabled="true"):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ROMCOM_LLM_ENABLED", enabled)
    monkeypatch.setenv("ROMCOM_SEARCH_CACHE_TTL", "360")
    invalidate()


class FakeResp:
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p
    def raise_for_status(self):
        pass


def _reply(idx):
    return FakeResp({"message": {"content": '{"index": %d}' % idx}})


def test_choose_returns_selected_and_caches(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    cand = [{"title": "Wrong Game", "url": "a"}, {"title": "Nancy Drew (USA) v1.1", "url": "b"}]
    calls = {"n": 0}
    def fake_post(url, timeout=None, json=None):
        calls["n"] += 1
        return _reply(1)
    monkeypatch.setattr("romcom.llm.requests.post", fake_post)
    assert llm.choose("Nancy Drew", "nds", cand)["url"] == "b"
    llm.choose("Nancy Drew", "nds", cand)          # served from cache
    assert calls["n"] == 1


def test_choose_none_when_no_match(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    monkeypatch.setattr("romcom.llm.requests.post", lambda *a, **k: _reply(-1))
    assert llm.choose("X", "nes", [{"title": "y", "url": "u"}]) is None


def test_choose_disabled_is_noop(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, enabled="false")
    called = []
    monkeypatch.setattr("romcom.llm.requests.post", lambda *a, **k: called.append(1))
    assert llm.choose("X", "nes", [{"title": "y", "url": "u"}]) is None
    assert not called                               # the model is never contacted


def test_choose_fails_open_on_error(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    def boom(*a, **k):
        raise RuntimeError("ollama down")
    monkeypatch.setattr("romcom.llm.requests.post", boom)
    assert llm.choose("X", "nes", [{"title": "y", "url": "u"}]) is None  # never raises


def test_choose_ignores_out_of_range_index(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    monkeypatch.setattr("romcom.llm.requests.post", lambda *a, **k: _reply(9))
    assert llm.choose("X", "nes", [{"title": "y", "url": "u"}]) is None
