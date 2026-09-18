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

Every sweep is bounded (`ROMCOM_ACQUIRE_WATCH_BATCH`, default 50 items): a watcher
re-sweeps by itself, so grinding through a whole library in one cycle only starves the
UI of feedback. Items a sweep passes over are dated in the `events` table and held back
until their cooldown expires — an hour for a transient failure, six for "nothing found
anywhere", which rarely changes between sweeps. That trail is what lets a re-sweep skip
the thousands of titles that turned up nothing and spend its budget on fresh ones, and
the pile is worked least-recently-tried first so a sweep never re-searches the head of
the queue while unattempted titles wait behind it.
"""
import threading
import time
from pathlib import Path
from .config import settings
from .db import connect
from .planner import next_individuals
from .status import MISSING
from . import indexer, actions, webdl
from .scanner import scan

MIN_SCORE = 20.0                # rank() floor: % of query tokens that must appear in the release title
QUEUEABLE = MISSING             # the only statuses worth searching for
SYNC_FAILURE_LIMIT = 10         # consecutive failed syncs before the wait phase gives up
# Search cooldowns, in minutes, keyed by the event a skip leaves behind. A transient
# failure (indexer hiccup, SABnzbd down, no download dir yet) is worth retrying soon;
# "nothing found anywhere" is a property of the sources, so those items cool for hours.
COOLDOWN_EVENTS = {"acquire-skip": 60, "acquire-miss": 360}
EVENT_KEEP_DAYS = 2             # cooldown-trail rows older than the longest window are pruned

STOP = threading.Event()        # the web UI sets this to cancel the watcher promptly


def _cooled(db):
    """item_id -> the event holding it back, for every skip inside its cooldown window."""
    out = {}
    for event, minutes in COOLDOWN_EVENTS.items():
        for r in db.execute("SELECT DISTINCT item_id FROM events WHERE event=? "
                            "AND created_at > datetime('now', ?)", (event, f"-{minutes} minutes")):
            out[r["item_id"]] = event
    return out


def _armed(db):
    """The armed pile the pipeline can act on, split into (ready, cooling).

    Ready items come back least-recently-tried first. Ordering matters once sweeps are
    bounded: with thousands of armed titles, sorting by anything else lets an item whose
    cooldown expires early re-take the front of the queue on the next sweep and starve
    everything behind it. Never-tried items sort ahead of retried ones, so sweeps make
    forward progress and the pile drains before anything is searched twice.
    """
    cooled = _cooled(db)
    armed = [r for r in next_individuals() if r["status"] in QUEUEABLE]
    tried = {r["item_id"]: r["t"] for r in db.execute(
        f"SELECT item_id, MAX(created_at) t FROM events WHERE event IN {tuple(COOLDOWN_EVENTS)} "
        "AND item_id IS NOT NULL GROUP BY item_id")}
    ready = [r for r in armed if r["id"] not in cooled]
    ready.sort(key=lambda r: (tried.get(r["id"]) or "", r["system"] or "", r["title"]))
    return ready, len(armed) - len(ready)


def eligible(db=None):
    """Armed items the pipeline will attempt this sweep."""
    return _armed(db or connect())[0]


def eligible_count():
    return len(eligible())


def cooling_count():
    """Armed items held back by an unexpired cooldown — reported alongside the eligible
    count so a quiet sweep ("3 armed, 4,000 cooling") is explicable instead of looking
    stuck, and so the effect of the cooldown is visible in the UI."""
    return _armed(connect())[1]


def _prune_events(db):
    """Cooldown rows exist only to date a skip and the longest window is hours: drop what
    nothing can still be reading, so a watcher running for weeks doesn't grow the trail
    without bound."""
    events = tuple(COOLDOWN_EVENTS)
    try:
        with db:
            db.execute(f"DELETE FROM events WHERE event IN {events} AND created_at < datetime('now', ?)",
                       (f"-{EVENT_KEEP_DAYS} days",))
    except Exception:
        pass  # housekeeping must never take a sweep down (a busy DB is not a failure)


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

    With `watch` (or ROMCOM_ACQUIRE_WATCH saved on), the cycle repeats forever —
    the always-on "download manager" mode. A watching cycle is bounded by
    `ROMCOM_ACQUIRE_WATCH_BATCH` (0 = unlimited): sweeps end, they don't run away
    with the whole library. A sweep that ended early because it hit that cap rests
    only `ROMCOM_ACQUIRE_SWEEP_PAUSE` seconds, since the next one picks up where it
    left off; a sweep with nothing left to do rests the full `ROMCOM_ACQUIRE_INTERVAL`.
    Setting `stop` cancels the current cycle and ends the watch.
    """
    started_at = time.monotonic()
    totals = None
    cycles = 0
    while True:
        # Re-read settings each cycle: the watcher is long-lived, and the UI can retune
        # it (or turn it off) while it runs.
        s = settings()
        watching = watch or s["acquire_watch"]
        poll = poll_interval if poll_interval is not None else s["acquire_poll"]
        max_wait = (max_wait_minutes if max_wait_minutes is not None else s["acquire_max_wait_min"]) * 60
        slots = parallel if parallel is not None else s["acquire_parallel"]
        batch = batch_max if batch_max is not None else (
            s["acquire_watch_batch"] if watching else s["acquire_batch_max"])
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
                          batch_note=result["batch_note"], sweep_capped=result["sweep_capped"],
                          armed_remaining=result["armed_remaining"], cooling=result["cooling"],
                          elapsed_min=round((time.monotonic() - started_at) / 60, 1))
            if result["scan"]:
                totals["scan"], totals["scan_note"] = result["scan"], result["scan_note"]
        totals["cycles"] = cycles
        if not watching or (stop and stop.is_set()):
            return totals
        # Rest between sweeps: newly armed items, expired cooldowns, and retriable skips
        # are all picked up on the next one. A sweep that stopped at the cap still has a
        # queue of untried items behind it, so it comes straight back — the pause only
        # keeps back-to-back sweeps from hammering the indexer.
        pause = min(s["acquire_interval"], s["acquire_sweep_pause"]) if result["sweep_capped"] \
            else s["acquire_interval"]
        slept = 0.0
        while slept < pause:
            if stop and stop.is_set():
                return totals
            if progress:
                more = f", {totals['armed_remaining']:,} armed left" if totals.get("armed_remaining") else ""
                progress(0, 0, f"watching — {cycles} sweep(s){more}, next in ~{int(pause - slept)}s",
                        {k: v for k, v in totals.items() if isinstance(v, (int, float, dict))})
            nap = min(5.0, pause - slept)
            time.sleep(nap)
            slept += nap


def _cycle(progress, poll, max_wait, batch, slots, stop):
    """One fill → wait → import pass over the armed items. Returns the cycle's
    counters; all state (attempted set, stats) lives and dies with the cycle."""
    s = settings()
    db = connect()
    start = time.monotonic()
    _prune_events(db)

    attempted = set()
    processed = [0]  # items searched this cycle (queued, skipped, or failed) — capped per run
    stats = {"queued": 0, "skipped": 0, "failed": 0, "waiting": 0,
             "downloaded": 0, "download_failed": 0, "direct": 0}
    skipped_by_reason = {}
    failed_items = []
    base_dl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='DOWNLOADED'").fetchone()["c"]
    base_fl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='FAILED'").fetchone()["c"]

    def _skip(item, reason, miss=False):
        """Record a skip and start its cooldown. `miss` marks the reasons that mean "the
        sources don't have this" (as opposed to "not right now"), which cool for hours."""
        stats["skipped"] += 1
        skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
        # The cooldown trail: a watching loop must not re-search this every sweep.
        try:
            with db:
                db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",
                           (item["id"], "acquire-miss" if miss else "acquire-skip", reason))
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
                _skip(c, "no usable indexer result, and no ROMCOM_DOWNLOAD_DIR for the direct fallback"); return
            _say(f"direct search: {c['title']}")
            try:
                direct = _pick(webdl.search(c["title"], c["system"]))
            except Exception as ex:
                _skip(c, f"direct search failed: {ex}"); return
            if direct is None:
                _skip(c, "no usable result anywhere (indexer or direct)", miss=True); return
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
    # What the sweep leaves behind, for the UI and for the watcher's decision to come
    # straight back: ready items are freshly armed or finished cooling, cooling ones are
    # dated in the events table and will rejoin after their window.
    ready, cooling = _armed(db)
    capped = bool(batch) and processed[0] >= batch
    result = {"queued": stats["queued"], "downloaded": stats["downloaded"],
              "download_failed": stats["download_failed"], "failed": len(failed_items),
              "skipped": stats["skipped"], "skipped_by_reason": skipped_by_reason,
              "failed_items": failed_items, "still_pending": stats["waiting"],
              "direct": stats["direct"],
              "wait_note": note, "elapsed_min": round((time.monotonic() - start) / 60, 1),
              "batch_note": None, "scan": imported.get("last_result"), "scan_note": None,
              "sweep_capped": capped, "armed_remaining": len(ready), "cooling": cooling}
    if capped:
        result["batch_note"] = (f"sweep capped at {batch} attempted; {len(ready)} armed item(s) left"
                                f"{f' ({cooling:,} held back by search cooldowns)' if cooling else ''}")
    if not ddir:
        result["scan_note"] = "ROMCOM_DOWNLOAD_DIR is not set — files not imported"
    elif not ddir_ok:
        result["scan_note"] = f"ROMCOM_DOWNLOAD_DIR does not exist: {ddir}"
    elif stats["downloaded"] == 0 and stats["direct"] == 0:
        result["scan_note"] = "no downloads completed this run"
    return result