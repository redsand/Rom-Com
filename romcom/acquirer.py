"""Automatic acquisition: a Sonarr-style rolling pipeline with an always-on watcher.

Armed means authorized=1 and wanted=1. Only CATALOGED/MISSING items are attempted:
FOUND items are already waiting in the download directory (usually from a completed
volume), and FAILED downloads are deliberately not retried automatically — re-queueing
them on every toggle would hammer the indexer. Use the manual search drawer to retry.

Each cycle keeps up to ROMCOM_ACQUIRE_PARALLEL downloads in flight (SABnzbd downloads
them concurrently; 0 = queue everything, no cap): whenever a slot frees up, the next
armed item is searched and queued, and newly completed files are scanned into the
library while the run continues. With `ROMCOM_ACQUIRE_WATCH` on (or the web UI's
watcher toggle), cycles repeat forever — newly armed items and expiring search
cooldowns are picked up on every sweep, like a download manager on duty rather than
a one-shot batch.
"""
import threading
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
COOLDOWN_MIN = 60               # an item that turned up nothing waits this long before re-searching

STOP = threading.Event()        # the web UI sets this to cancel the watcher promptly


def eligible():
    """Armed items the pipeline will attempt this sweep. Items skipped recently
    (an 'acquire-skip' event inside the cooldown window) are held back so a
    watching loop re-searches them at a civil pace instead of every sweep."""
    db = connect()
    cooled = {r["item_id"] for r in db.execute(
        "SELECT DISTINCT item_id FROM events WHERE event='acquire-skip' "
        "AND created_at > datetime('now', ?)", (f"-{COOLDOWN_MIN} minutes",))}
    return [r for r in next_individuals()
            if r["status"] in QUEUEABLE and r["id"] not in cooled]


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


def auto_acquire(progress=None, poll_interval=None, max_wait_minutes=None, batch_max=None,
                 parallel=None, watch=False, stop=None):
    """Search, queue, wait, and import — continuously, with capped parallel downloads.

    Each item is searched at most once per cycle (the `attempted` set), so every
    cycle terminates. `batch_max` (from ROMCOM_ACQUIRE_BATCH_MAX; 0 = unlimited)
    caps how many items one cycle will attempt (searched — queued, skipped, or
    failed), so a stray toggle can't dump the whole library into SABnzbd or hammer
    the indexer's API. `parallel` (from ROMCOM_ACQUIRE_PARALLEL; 0 = unlimited)
    caps how many downloads may be in flight at once. Files that land in the
    download directory mid-cycle are scanned in right away instead of waiting
    for the cycle to end.

    With `watch` (or ROMCOM_ACQUIRE_WATCH saved on), the cycle repeats forever,
    resting `ROMCOM_ACQUIRE_INTERVAL` seconds between sweeps — the always-on
    "download manager" mode. Setting `stop` cancels the current cycle and ends
    the watch.
    """
    s = settings()
    poll = poll_interval if poll_interval is not None else s["acquire_poll"]
    max_wait = (max_wait_minutes if max_wait_minutes is not None else s["acquire_max_wait_min"]) * 60
    batch = batch_max if batch_max is not None else s["acquire_batch_max"]
    slots = parallel if parallel is not None else s["acquire_parallel"]
    interval = s["acquire_interval"]
    started_at = time.monotonic()
    totals = None
    cycles = 0
    while True:
        result = _cycle(progress, poll, max_wait, batch, slots, stop)
        cycles += 1
        # Watch mode reports what the WHOLE watch did, not just its last cycle.
        if totals is None:
            totals = result
        else:
            for k in ("queued", "downloaded", "download_failed", "failed", "skipped", "direct"):
                totals[k] += result[k]
            for reason, n in result["skipped_by_reason"].items():
                totals["skipped_by_reason"][reason] = totals["skipped_by_reason"].get(reason, 0) + n
            if result["failed_items"]:
                totals["failed_items"] = (totals["failed_items"] + result["failed_items"])[:50]
            totals.update(still_pending=result["still_pending"], wait_note=result["wait_note"],
                          batch_note=result["batch_note"], elapsed_min=round(
                              (time.monotonic() - started_at) / 60, 1))
            if result["scan"]:
                totals["scan"], totals["scan_note"] = result["scan"], result["scan_note"]
        totals["cycles"] = cycles
        watching = watch or settings()["acquire_watch"]
        if not watching or (stop and stop.is_set()):
            return totals
        # Idle rest between sweeps: newly armed items, expired cooldowns, and
        # retriable skips are all picked up on the next cycle.
        slept = 0.0
        while slept < interval:
            if stop and stop.is_set():
                return totals
            if progress:
                progress(0, 0, f"watching — next sweep in ~{int(interval - slept)}s",
                        {k: v for k, v in totals.items() if isinstance(v, (int, float, dict))})
            nap = min(5.0, interval - slept)
            time.sleep(nap)
            slept += nap


def _cycle(progress, poll, max_wait, batch, slots, stop):
    """One fill → wait → import pass over the armed items. Returns the cycle's
    counters; all state (attempted set, stats) lives and dies with the cycle."""
    s = settings()
    db = connect()
    start = time.monotonic()

    attempted = set()
    processed = [0]  # items searched this cycle (queued, skipped, or failed) — capped per run
    stats = {"queued": 0, "skipped": 0, "failed": 0, "waiting": 0,
             "downloaded": 0, "download_failed": 0, "direct": 0}
    skipped_by_reason = {}
    failed_items = []
    base_dl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='DOWNLOADED'").fetchone()["c"]
    base_fl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='FAILED'").fetchone()["c"]

    def _skip(item, reason):
        stats["skipped"] += 1
        skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
        # The cooldown trail: a watching loop must not re-search this every sweep.
        try:
            with db:
                db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,'acquire-skip',?)",
                           (item["id"], reason))
        except Exception:
            pass
        if progress:
            progress(processed[0], processed[0] + max(1, stats["waiting"]),
                     f"skipped: {item['title']}", dict(stats))

    def _fail(item, reason):
        stats["failed"] += 1
        if len(failed_items) < 50:
            failed_items.append({"id": item["id"], "title": item["title"], "reason": reason})
        if progress:
            progress(processed[0], processed[0] + max(1, stats["waiting"]),
                     f"failed: {item['title']}", dict(stats))

    def _refresh():
        stats["waiting"] = _pending(db)
        dl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='DOWNLOADED'").fetchone()["c"]
        fl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='FAILED'").fetchone()["c"]
        stats["downloaded"] = max(0, dl - base_dl)
        stats["download_failed"] = max(0, fl - base_fl)

    def attempt(c):
        """Search one armed item and hand it to SABnzbd (or the direct source)."""
        attempted.add(c["id"])
        processed[0] += 1

        def _say(msg):
            """Surface every step — a paced direct download can take minutes, and
            skips would otherwise leave the job's stats blank the whole run."""
            if progress:
                progress(processed[0], processed[0] + max(1, stats["waiting"]), msg, dict(stats))

        def _step():
            _refresh()
            _say(c["title"])

        _say(f"search: {c['title']}")
        try:
            results, e = indexer.search_entity(db, "item", c["id"])
        except Exception as ex:
            _skip(c, f"search failed: {ex}"); return
        top = _pick(results)
        if top is None:
            # The NZB indexer had nothing usable — fall back to the direct-download
            # source (romsgames.net) before giving up on this item.
            if not s["download_dir"]:
                _skip(c, "no usable result (low score or no url)"); return
            _say(f"direct search: {c['title']}")
            try:
                direct = _pick(webdl.search(c["title"], c["system"]))
            except Exception as ex:
                _skip(c, f"direct search failed: {ex}"); return
            if direct is None:
                _skip(c, "no usable result anywhere (indexer or direct)"); return
            _say(f"direct download: {c['title']}")
            try:
                webdl.fetch(direct, s["download_dir"])
            except Exception as ex:
                _fail(c, f"direct download failed: {ex}"); return
            with db:
                db.execute("UPDATE items SET status='DOWNLOADED' WHERE id=?", (c["id"],))
            stats["direct"] += 1
            _step()
            return
        try:
            nzo = actions.queue_result(db, "item", e, top)
        except Exception as ex:
            _fail(c, f"SABnzbd error: {ex}"); return
        if nzo is None:
            # queue_result journaled a job SABnzbd can never report on — mark it dead
            # instead of leaving an unresolvable row that would hang the wait phase.
            with db:
                db.execute("UPDATE items SET status='FAILED' WHERE id=?", (c["id"],))
            _fail(c, "SABnzbd returned no nzo id"); return
        stats["queued"] += 1
        _step()

    def fill():
        """Attempt armed items never tried this cycle until the download slots are
        full (or nothing eligible remains / the run cap is hit). Slots free up as
        SABnzbd finishes, so the loop keeps queueing the next item all cycle long.
        Called every loop turn, so items armed mid-run join on the next pass."""
        todo = [c for c in eligible() if c["id"] not in attempted]
        for c in todo:
            if batch and processed[0] >= batch:
                return
            if slots and _pending(db) >= slots:
                return
            if stop and stop.is_set():
                return
            attempt(c)

    ddir = s["download_dir"]
    ddir_ok = bool(ddir) and Path(ddir).exists()
    imported = {"last": None, "adopted": 0, "last_result": None}  # incremental-scan state

    def import_new():
        """Scan the download directory when files have landed since the last pass.
        Unchanged files are resumed via the files table, so re-scans are cheap;
        totals (files/matched/…) come from the latest pass, while 'adopted'
        accumulates across passes (each pass only adopts files new to it)."""
        nonlocal imported
        done_now = stats["downloaded"] + stats["direct"]
        if not ddir_ok or done_now == 0 or imported["last"] == done_now:
            return
        imported["last"] = done_now

        def prog(i, total, name, scan_stats=None):
            if progress: progress(i, total, f"scan: {name}", dict(stats) | (scan_stats or {}))
        r = scan(ddir, name_match=True, adopt=True, progress=prog)
        imported["adopted"] += r.get("adopted", 0)
        # Pin to the completion count at scan start: anything that finished DURING
        # the scan is a different count and triggers another (cheap, resumed) pass.
        imported["last_result"] = r
        if "adopted" in r:
            r = dict(r); r["adopted"] = imported["adopted"]
            imported["last_result"] = r

    # The rolling loop: fill every free download slot, wait a tick for SABnzbd,
    # import whatever finished, and repeat — until nothing is in flight and
    # nothing armed is left unattempted.
    note = None
    sync_failures = 0
    while True:
        fill()
        _refresh()
        if stats["waiting"] == 0:
            break
        import_new()
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
        if stop and stop.is_set():
            note = "cancelled"; break
        if time.monotonic() - start > max_wait:
            _refresh()
            note = f"wait cap reached; {stats['waiting']} download(s) still pending"; break

    # Final import of whatever completed since the last pass.
    try: actions.sync(db)
    except Exception: pass
    _refresh()
    import_new()
    result = {"queued": stats["queued"], "downloaded": stats["downloaded"],
              "download_failed": stats["download_failed"], "failed": len(failed_items),
              "skipped": stats["skipped"], "skipped_by_reason": skipped_by_reason,
              "failed_items": failed_items, "still_pending": stats["waiting"],
              "direct": stats["direct"],
              "wait_note": note, "elapsed_min": round((time.monotonic() - start) / 60, 1),
              "batch_note": None, "scan": imported.get("last_result"), "scan_note": None}
    if batch and processed[0] >= batch:
        remaining = eligible_count()
        if remaining:
            result["batch_note"] = (f"capped at {batch} attempted; {remaining} armed item(s) "
                                    "left for the next run")
    if not ddir:
        result["scan_note"] = "ROMCOM_DOWNLOAD_DIR is not set — files not imported"
    elif not ddir_ok:
        result["scan_note"] = f"ROMCOM_DOWNLOAD_DIR does not exist: {ddir}"
    elif stats["downloaded"] == 0 and stats["direct"] == 0:
        result["scan_note"] = "no downloads completed this run"
    return result