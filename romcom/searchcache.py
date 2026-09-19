"""A small TTL cache for search results, keyed by (source, query).

The always-on watcher re-searches items every sweep once their cooldown expires, and an
item's title + aliases each hit the indexer separately. The paced direct sources
(romsgames, Vimm) cost 20-45s per request. Caching the raw fetched results for a while
means the same title isn't re-fetched from the same source on every pass.

The cache stores the RAW (pre-rank) results, so improving the ranker takes effect on
cached queries immediately — only the network fetch is memoized, not the scoring. Every
cache op is wrapped so a cache failure silently falls back to a live fetch: the cache is
an optimization, never a dependency.
"""
import json
from .config import settings
from .db import connect


def _ttl_minutes(ttl_minutes):
    if ttl_minutes is not None:
        return ttl_minutes
    try:
        return float(settings()["search_cache_ttl"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def cached(source, key, fetch, ttl_minutes=None):
    """Return cached results for (source, key) if fresh, else call fetch(), store, return.

    `fetch` is a zero-arg callable returning a JSON-serialisable list. ttl<=0 disables the
    cache (always fetch live). A fetch that raises propagates (nothing is cached)."""
    ttl = _ttl_minutes(ttl_minutes)
    if ttl <= 0:
        return fetch()
    seconds = int(ttl * 60)
    try:
        db = connect()
        row = db.execute(
            "SELECT results FROM search_cache WHERE source=? AND cache_key=? "
            "AND fetched_at > datetime('now', ?)", (source, key, f"-{seconds} seconds")).fetchone()
        if row:
            return json.loads(row["results"])
    except Exception:
        pass  # cache read failed — fall through to a live fetch
    results = fetch()
    try:
        db = connect()
        with db:
            db.execute(
                "INSERT INTO search_cache(source,cache_key,results,fetched_at) "
                "VALUES(?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(source,cache_key) DO UPDATE SET results=excluded.results,"
                "fetched_at=CURRENT_TIMESTAMP", (source, key, json.dumps(results)))
    except Exception:
        pass  # cache write failed — the caller still gets its results
    return results


def stats(db=None):
    """Cache size and freshness per source, for the CLI / Settings tab."""
    db = db or connect()
    ttl = _ttl_minutes(None)
    fresh_cut = f"-{int(ttl * 60)} seconds" if ttl > 0 else "-0 seconds"
    rows = db.execute(
        "SELECT source, COUNT(*) total, "
        "SUM(CASE WHEN fetched_at > datetime('now', ?) THEN 1 ELSE 0 END) fresh "
        "FROM search_cache GROUP BY source ORDER BY source", (fresh_cut,)).fetchall()
    return {"ttl_minutes": ttl, "by_source": [dict(r) for r in rows],
            "total": sum(r["total"] for r in rows)}


def clear(source=None, db=None):
    """Drop cached searches (all, or one source). Returns rows removed."""
    db = db or connect()
    with db:
        cur = db.execute("DELETE FROM search_cache WHERE (?1 IS NULL OR source=?1)", (source,))
    return cur.rowcount
