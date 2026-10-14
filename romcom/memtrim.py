"""Forget big in-process caches nobody has used for a while.

The service runs for weeks. Parsed MAME/FBNeo/2003-Plus dats (~150 MB of Python objects
for the three), catver.ini, archive listings from an export — each is cached because one
job asks for it many times, and each stayed resident forever after, which is how an idle
service sat at ~800 MB. A cache registered here is cleared once it has gone `idle`
seconds untouched; the next use simply rebuilds it.
"""
import gc
import threading
import time

_LOCK = threading.Lock()
_CACHES = {}   # name -> the cache dict (cleared in place, so holders keep their reference)
_USED = {}     # name -> monotonic time of last use

IDLE = 900


def use(name, cache):
    """Note that `cache` was just used; returns it for convenience."""
    with _LOCK:
        _CACHES[name] = cache
        _USED[name] = time.monotonic()
    return cache


def trim(idle=IDLE, now=None):
    """Clear every registered cache idle for `idle` seconds. Returns the names cleared."""
    now = time.monotonic() if now is None else now
    cleared = []
    with _LOCK:
        for name, cache in _CACHES.items():
            if cache and now - _USED.get(name, 0) >= idle:
                cache.clear()
                cleared.append(name)
    if cleared:
        gc.collect()
    return cleared
