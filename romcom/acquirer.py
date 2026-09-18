"""Automatic acquisition: queue every armed item, wait out SABnzbd, scan the download dir.

Armed means authorized=1 and wanted=1. Only CATALOGED/MISSING items are attempted:
FOUND items are already waiting in the download directory (usually from a completed
volume), and FAILED downloads are deliberately not retried automatically — re-queueing
them on every toggle would hammer the indexer. Use the manual search drawer to retry.
"""
import time
from pathlib import Path
from .config import settings
from .db import connect
from .planner import next_individuals
from . import indexer, actions, webdl
from .scanner import scan

MIN_SCORE = 20.0                # rank() floor: % of query tokens that must appear in the release title
QUEUEABLE = ("CATALOGED", "MISSING")
SYNC_FAILURE_LIMIT = 10         # consecutive failed syncs before the wait phase gives up


def eligible():
    """Armed items the pipeline will attempt this run."""
    return [r for r in next_individuals() if r["status"] in QUEUEABLE]


def eligible_count():
    return len(eligible())


def _pick(results, min_score=MIN_SCORE):
    """First usable result (rank() sorts descending) or None."""
    for r in results or []:
        if r.get("url") and r.get("score", 0) >= min_score:
            return r
    return None


def _pending(db):
    """Non-terminal download jobs sync() can actually resolve (a NULL nzo_id never will)."""
    return db.execute("""SELECT COUNT(*) c FROM jobs
      WHERE status IN ('QUEUED','DOWNLOADING') AND nzo_id IS NOT NULL""").fetchone()["c"]


def auto_acquire(progress=None, poll_interval=None, max_wait_minutes=None, batch_max=None):
    """Queue the best indexer hit for every armed item, wait for SABnzbd, then import.

    Runs on the caller's thread with its own DB connection. Each item is searched at
    most once per run (the `attempted` set), so the sweep terminates; the wait phase
    exits when nothing is pending, the wall-clock cap expires, or sync keeps failing.
    `batch_max` (from ROMCOM_ACQUIRE_BATCH_MAX; 0 = unlimited) caps how many items
    one run will attempt (searched — queued, skipped, or failed), so a stray toggle
    can't dump the whole library into SABnzbd or hammer the indexer's API.
    """
    s = settings()
    poll = poll_interval if poll_interval is not None else s["acquire_poll"]
    max_wait = (max_wait_minutes if max_wait_minutes is not None else s["acquire_max_wait_min"]) * 60
    batch = batch_max if batch_max is not None else s["acquire_batch_max"]
    db = connect()
    start = time.monotonic()

    attempted = set()
    processed = [0]  # items searched this run (queued, skipped, or failed) — capped per run
    stats = {"queued": 0, "skipped": 0, "failed": 0, "waiting": 0,
             "downloaded": 0, "download_failed": 0, "direct": 0}
    skipped_by_reason = {}
    failed_items = []
    base_dl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='DOWNLOADED'").fetchone()["c"]
    base_fl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='FAILED'").fetchone()["c"]

    def _skip(item, reason):
        stats["skipped"] += 1
        skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1

    def _fail(item, reason):
        stats["failed"] += 1
        if len(failed_items) < 50:
            failed_items.append({"id": item["id"], "title": item["title"], "reason": reason})

    def _refresh():
        stats["waiting"] = _pending(db)
        dl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='DOWNLOADED'").fetchone()["c"]
        fl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='FAILED'").fetchone()["c"]
        stats["downloaded"] = max(0, dl - base_dl)
        stats["download_failed"] = max(0, fl - base_fl)

    def sweep():
        """One pass over armed items never attempted this run. Marking ids in
        `attempted` before doing any work guarantees each item is visited at most once.
        Stops early once the run's attempt cap is reached; unvisited items stay armed
        for the next run."""
        todo = [c for c in eligible() if c["id"] not in attempted]
        for i, c in enumerate(todo):
            if batch and processed[0] >= batch:
                break
            attempted.add(c["id"])
            processed[0] += 1
            try:
                results, e = indexer.search_entity(db, "item", c["id"])
            except Exception as ex:
                _skip(c, f"search failed: {ex}"); continue
            top = _pick(results)
            if top is None:
                # The NZB indexer had nothing usable — fall back to the direct-download
                # source (romsgames.net) before giving up on this item.
                if not s["download_dir"]:
                    _skip(c, "no usable result (low score or no url)"); continue
                try:
                    direct = _pick(webdl.search(c["title"], c["system"]))
                except Exception as ex:
                    _skip(c, f"direct search failed: {ex}"); continue
                if direct is None:
                    _skip(c, "no usable result anywhere (indexer or direct)"); continue
                try:
                    webdl.fetch(direct, s["download_dir"])
                except Exception as ex:
                    _fail(c, f"direct download failed: {ex}"); continue
                with db:
                    db.execute("UPDATE items SET status='DOWNLOADED' WHERE id=?", (c["id"],))
                stats["direct"] += 1
                if progress: progress(i + 1, len(todo), c["title"], dict(stats))
                continue
            try:
                nzo = actions.queue_result(db, "item", e, top)
            except Exception as ex:
                _fail(c, f"SABnzbd error: {ex}"); continue
            if nzo is None:
                # queue_result journaled a job SABnzbd can never report on — mark it dead
                # instead of leaving an unresolvable row that would hang the wait phase.
                with db:
                    db.execute("UPDATE items SET status='FAILED' WHERE id=?", (c["id"],))
                _fail(c, "SABnzbd returned no nzo id"); continue
            stats["queued"] += 1
            _refresh()
            if progress: progress(i + 1, len(todo), c["title"], dict(stats))
        return len(todo)

    # Phase 1: queue everything armed.
    sweep()

    # Phase 2: wait out SABnzbd, picking up items armed mid-run.
    note = None
    sync_failures = 0
    while _pending(db) > 0:
        _refresh()
        done = stats["downloaded"] + stats["download_failed"]
        if progress:
            progress(done, done + stats["waiting"],
                     f"waiting for {stats['waiting']} download(s)…", dict(stats))
        time.sleep(poll)
        try:
            actions.sync(db); sync_failures = 0
        except Exception:
            sync_failures += 1
            if sync_failures >= SYNC_FAILURE_LIMIT:
                note = f"gave up waiting after {SYNC_FAILURE_LIMIT} failed syncs"; break
        sweep()
        if _pending(db) == 0: break
        if time.monotonic() - start > max_wait:
            _refresh()
            note = f"wait cap reached; {stats['waiting']} download(s) still pending"; break

    # Phase 3: import whatever landed in the download directory.
    try: actions.sync(db)
    except Exception: pass
    _refresh()
    result = {"queued": stats["queued"], "downloaded": stats["downloaded"],
              "download_failed": stats["download_failed"], "failed": len(failed_items),
              "skipped": stats["skipped"], "skipped_by_reason": skipped_by_reason,
              "failed_items": failed_items, "still_pending": stats["waiting"],
              "direct": stats["direct"],
              "wait_note": note, "elapsed_min": round((time.monotonic() - start) / 60, 1),
              "batch_note": None, "scan": None, "scan_note": None}
    if batch and processed[0] >= batch:
        remaining = eligible_count()
        if remaining:
            result["batch_note"] = (f"capped at {batch} attempted; {remaining} armed item(s) "
                                    "left for the next run")
    ddir = s["download_dir"]
    if not ddir:
        result["scan_note"] = "ROMCOM_DOWNLOAD_DIR is not set — files not imported"
    elif not Path(ddir).exists():
        result["scan_note"] = f"ROMCOM_DOWNLOAD_DIR does not exist: {ddir}"
    elif stats["downloaded"] == 0 and stats["direct"] == 0:
        result["scan_note"] = "no downloads completed this run"
    else:
        def prog(i, total, name, scan_stats=None):
            if progress: progress(i, total, f"scan: {name}", dict(stats) | (scan_stats or {}))
        result["scan"] = scan(ddir, name_match=True, adopt=True, progress=prog)
    return result