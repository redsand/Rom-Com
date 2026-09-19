from romcom import searchcache
from romcom.config import invalidate


def env(monkeypatch, tmp_path, ttl="360"):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ROMCOM_SEARCH_CACHE_TTL", ttl)
    invalidate()


def test_cached_miss_then_hit(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    calls = {"n": 0}
    def fetch():
        calls["n"] += 1
        return [{"title": "x", "url": "u"}]
    r1 = searchcache.cached("nzb", "mario|50", fetch)
    r2 = searchcache.cached("nzb", "mario|50", fetch)   # served from cache
    assert r1 == r2 == [{"title": "x", "url": "u"}]
    assert calls["n"] == 1                              # fetch ran once


def test_distinct_keys_are_separate(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    searchcache.cached("nzb", "a", lambda: [{"a": 1}])
    searchcache.cached("nzb", "b", lambda: [{"b": 2}])
    assert searchcache.cached("nzb", "a", lambda: [{"nope": 0}]) == [{"a": 1}]
    assert searchcache.stats()["total"] == 2


def test_ttl_zero_disables_cache(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, ttl="0")
    calls = {"n": 0}
    def fetch():
        calls["n"] += 1
        return [1]
    searchcache.cached("nzb", "k", fetch)
    searchcache.cached("nzb", "k", fetch)
    assert calls["n"] == 2                              # always live
    assert searchcache.stats()["total"] == 0           # nothing stored


def test_expired_entry_refetches(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, ttl="360")
    searchcache.cached("nzb", "k", lambda: ["old"])
    # age the entry past a 1-minute view of the ttl
    from romcom.db import connect
    db = connect()
    with db:
        db.execute("UPDATE search_cache SET fetched_at=datetime('now','-2 hours') WHERE cache_key='k'")
    env(monkeypatch, tmp_path, ttl="60")               # ttl now 60 min; entry is 120 min old
    assert searchcache.cached("nzb", "k", lambda: ["new"]) == ["new"]


def test_clear(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    searchcache.cached("nzb", "a", lambda: [1])
    searchcache.cached("webdl", "b", lambda: [2])
    assert searchcache.clear("nzb") == 1               # only the nzb entry
    assert searchcache.stats()["total"] == 1
    assert searchcache.clear() == 1                    # the rest
    assert searchcache.stats()["total"] == 0
