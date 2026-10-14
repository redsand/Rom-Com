"""Idle caches let go: the service sat at ~800 MB holding parsed dats nobody had asked
for since the last export."""
from romcom import memtrim


def test_an_idle_cache_is_cleared_and_a_busy_one_kept():
    busy, idle = {"k": 1}, {"k": 2}
    memtrim.use("t.idle", idle)
    memtrim._USED["t.idle"] -= 2000
    memtrim.use("t.busy", busy)
    assert "t.idle" in memtrim.trim(idle=900)
    assert idle == {} and busy == {"k": 1}
