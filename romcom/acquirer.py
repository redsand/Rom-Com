"""Automatic acquisition: a Sonarr-style rolling pipeline with an always-on watcher.

Armed means authorized=1 and wanted=1. Only CATALOGED/MISSING items are attempted:
FOUND items are already waiting in the download directory (usually from a completed
volume), and FAILED downloads are deliberately not retried automatically — re-queueing
them on every toggle would hammer the indexer. Use the manual search drawer to retry.

Each cycle searches and queues armed items across a pool of ROMCOM_ACQUIRE_PARALLEL
workers (default 4) — that many indexer searches in flight at once, feeding SABnzbd,
which downloads them concurrently. Items the indexer can't satisfy are handed to a
single background direct worker (romsgames, then Vimm) that downloads one game at a
time without ever blocking the NZB pool — the "other provider" is a single lane, the
NZB path is a highway. Newly completed files are scanned into the library while the run
continues. With `ROMCOM_ACQUIRE_WATCH` on (or the web UI's watcher toggle), cycles
repeat forever — newly armed items and expiring search cooldowns are picked up on every
sweep, like a download manager on duty rather than a one-shot batch.

Every sweep is bounded (`ROMCOM_ACQUIRE_WATCH_BATCH`, default 50 items): a watcher
re-sweeps by itself, so grinding through a whole library in one cycle only starves the
UI of feedback. Items a sweep passes over are dated in the `events` table and held back
until their cooldown expires — an hour for a transient failure, six for "nothing found
anywhere", which rarely changes between sweeps. That trail is what lets a re-sweep skip
the thousands of titles that turned up nothing and spend its budget on fresh ones, and
the pile is worked least-recently-tried first so a sweep never re-searches the head of
the queue while unattempted titles wait behind it.
"""
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .config import settings
from .db import connect
from .planner import next_individuals
from .status import MISSING
from . import indexer, actions, webdl, vimm, llm, archive
import sqlite3
from .scanner import scan


def _retry_write(db, sql, params, attempts=4, delay=2.0):
    """Run a write, riding out a transient `database is locked`.

    busy_timeout already covers ordinary contention; this covers what it does not -- one
    long write transaction elsewhere in the process holding the lock for longer than the
    timeout. Re-raises once the attempts are spent, so a real failure stays a failure."""
    for i in range(attempts):
        try:
            with db:
                db.execute(sql, params)
            return True
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))
    return False

MIN_SCORE = 20.0                # rank() floor: % of query tokens that must appear in the release title
QUEUEABLE = MISSING             # the only statuses worth searching for
SYNC_FAILURE_LIMIT = 10         # consecutive failed syncs before the wait phase gives up
# Search cooldowns, in minutes, keyed by the event a skip leaves behind. A transient
# failure (indexer hiccup, SABnzbd down, no download dir yet) is worth retrying soon;
# "nothing found anywhere" is a property of the sources, so those items cool for hours.
COOLDOWN_EVENTS = {"acquire-skip": 60, "acquire-miss": 360}
EVENT_KEEP_DAYS = 2             # cooldown-trail rows older than the longest window are pruned

STOP = threading.Event()        # the web UI sets this to cancel the watcher promptly
# Direct downloads run ROMCOM_ACQUIRE_DIRECT_PARALLEL at a time (romsgames transfers
# overlap; its request pacing still spaces the HTTP calls). Vimm self-limits to one at a
# time via its own module semaphore regardless of this. SABnzbd is the multi-lane highway
# alongside; the direct workers are the (now multi-lane) side road.
DEFAULT_NZB_WORKERS = 4         # parallel indexer searches/queues when ROMCOM_ACQUIRE_PARALLEL is unset/0


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


def armed_summary():
    """Both counts from a SINGLE _armed() pass. The Acquire tab needs eligible + cooling
    together; calling the two functions above would scan the (millions-of-rows) events
    table twice for the same answer."""
    ready, cooling = _armed(connect())
    return {"eligible": len(ready), "cooling": cooling}


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


def _already_fetched(db, url):
    """True if this exact source page has already produced a file, for any item.

    Source pages are shared: one generic result ("dragon ball z 4 in 1") can be the
    top pick for several different items, and without this check each of them pays
    the full paced fetch cost — four HTTP requests, ~150 s — to download a file that
    is already on disk. The ledger is the record of what has been pulled, so a URL
    with a DOWNLOADED job is a duplicate whoever asked for it.
    """
    if not url:
        return False
    return db.execute("SELECT 1 FROM jobs WHERE result_url=? AND status='DOWNLOADED' LIMIT 1",
                      (url,)).fetchone() is not None


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
    the indexer's API. `parallel` (from ROMCOM_ACQUIRE_PARALLEL; 0 = default 4) is
    how many indexer searches run concurrently; direct downloads always run one at a
    time on a separate background worker. Files that land in the download directory
    mid-cycle are scanned in right away instead of waiting for the cycle to end.

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
    cycle_errors = 0
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
        try:
            result = _cycle(progress, poll, max_wait, batch, slots, stop)
            cycle_errors = 0
        except Exception as ex:
            # A one-shot run surfaces the error to its caller as before. A watching run
            # must NEVER die on a single bad sweep (a DB hiccup, a scan blowup, a source
            # outage): record it, rest, and come back — the whole point of an always-on
            # download manager is that it stays on.
            if not watching:
                raise
            cycle_errors += 1
            if totals is not None:
                totals["cycle_error"] = f"{type(ex).__name__}: {ex}"
                totals["cycle_errors"] = cycle_errors
            if progress:
                progress(0, 0, f"sweep failed, recovering (#{cycle_errors}): {ex}",
                         {k: v for k, v in (totals or {}).items() if isinstance(v, (int, float))})
            slept = 0.0
            rest = s["acquire_interval"]
            while slept < rest:
                if stop and stop.is_set():
                    return totals or {"cycles": cycles, "cycle_errors": cycle_errors}
                nap = min(5.0, rest - slept)
                time.sleep(nap)
                slept += nap
            continue
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
    """One parallel fill → wait → import pass over the armed items.

    NZB searching/queueing runs across a pool of `slots` workers (default
    DEFAULT_NZB_WORKERS) — up to that many indexer searches in flight at once. Items the
    indexer can't satisfy are handed to a single background direct worker (romsgames, then
    Vimm) that downloads one game at a time without ever blocking the NZB pool. All state
    (attempted set, stats) lives and dies with the cycle; shared state is guarded by `lock`
    because the workers touch it concurrently, and every worker uses its own DB connection
    (SQLite connections are single-thread)."""
    s = settings()
    db = connect()                 # main thread only: feed, sync, refresh, import
    start = time.monotonic()
    _prune_events(db)

    lock = threading.Lock()
    attempted = set()
    processed = [0]  # items attempted this cycle (queued, skipped, or failed) — capped per run
    stats = {"queued": 0, "skipped": 0, "failed": 0, "waiting": 0,
             "downloaded": 0, "download_failed": 0, "direct": 0}
    skipped_by_reason = {}
    failed_items = []
    nzb_workers = int(slots) if slots and int(slots) > 0 else DEFAULT_NZB_WORKERS
    # Count only SABnzbd jobs here (nzo_id set): direct downloads are journaled straight
    # to DOWNLOADED and counted separately as stats["direct"], so scoping to nzo_id keeps
    # the two from double-counting the same file.
    base_dl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='DOWNLOADED' AND nzo_id IS NOT NULL").fetchone()["c"]
    base_fl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='FAILED' AND nzo_id IS NOT NULL").fetchone()["c"]

    def _emit(msg):
        """Surface a step to the progress channel with a consistent stats snapshot."""
        if progress:
            with lock:
                snap = dict(stats)
            progress(processed[0], processed[0] + max(1, snap["waiting"]), msg, snap)

    def _skip(item, reason, wdb, miss=False):
        """Record a skip and start its cooldown. `miss` marks the reasons that mean "the
        sources don't have this" (vs "not right now"), which cool for hours."""
        with lock:
            stats["skipped"] += 1
            skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
        try:
            with wdb:
                wdb.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",
                            (item["id"], "acquire-miss" if miss else "acquire-skip", reason))
        except Exception:
            pass
        _emit(f"skipped: {item['title']}")

    def _fail(item, reason):
        with lock:
            stats["failed"] += 1
            if len(failed_items) < 50:
                failed_items.append({"id": item["id"], "title": item["title"], "reason": reason})
        _emit(f"failed: {item['title']}")

    def _refresh():
        w = _pending(db)
        dl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='DOWNLOADED' AND nzo_id IS NOT NULL").fetchone()["c"]
        fl = db.execute("SELECT COUNT(*) c FROM jobs WHERE status='FAILED' AND nzo_id IS NOT NULL").fetchone()["c"]
        with lock:
            stats["waiting"] = w
            stats["downloaded"] = max(0, dl - base_dl)
            stats["download_failed"] = max(0, fl - base_fl)

    # --- background direct workers (romsgames/vimm), off the NZB path ---
    direct_q = queue.Queue()
    DONE = object()
    direct_inflight = set()      # source urls being fetched right now, to dedup across workers

    def _direct_worker():
        wdb = connect()
        while True:
            c = direct_q.get()
            try:
                if c is DONE:
                    return
                if stop and stop.is_set():
                    continue
                sources = [("romsgames", webdl)]
                if s["vimm_enabled"]:
                    sources.append(("vimm", vimm))
                if s["archive_enabled"]:
                    sources.append(("archive", archive))
                picked, had_error = None, False
                for sname, mod in sources:
                    _emit(f"{sname} search: {c['title']}")
                    try:
                        cand = _pick(mod.search(c["title"], c["system"]))
                    except Exception:
                        had_error = True; continue
                    if cand:
                        picked = (sname, mod, cand); break
                if picked is None:
                    _skip(c, "no usable result anywhere (indexer or direct)", wdb, miss=not had_error)
                    continue
                sname, mod, direct = picked
                url = direct.get("url")
                # Dedup across workers AND across sweeps: the same source page can be the top
                # pick for several items (a multicart). Claim the url under the lock — if the
                # ledger already has it, or another worker is fetching it right now, skip.
                with lock:
                    dup = url in direct_inflight or _already_fetched(wdb, url)
                    if not dup:
                        direct_inflight.add(url)
                if dup:
                    _skip(c, f"already downloaded from {url}", wdb, miss=True)
                    continue
                _emit(f"{sname} download: {c['title']}")
                try:
                    # romsgames runs several in parallel (transfers overlap; its request
                    # pacing still spaces the HTTP calls). Vimm self-serializes to exactly
                    # one at a time via its own module semaphore, regardless of worker count.
                    path = mod.fetch(direct, s["download_dir"])
                except Exception as ex:
                    with lock:
                        direct_inflight.discard(url)   # failed — let it be retried later
                    _fail(c, f"{sname} download failed: {ex}"); continue
                # The file is already on disk here. Losing this write to a transient lock
                # means the item is fetched all over again on a later sweep, so it is worth
                # retrying past a long-running writer: a bulk catalog edit can hold the
                # write lock for longer than busy_timeout on its own.
                _retry_write(wdb, "UPDATE items SET status='DOWNLOADED' WHERE id=?", (c["id"],))
                try:
                    actions.journal_direct(wdb, c["id"], sname, direct, path)
                except Exception:
                    pass  # the file is on disk; a ledger hiccup must not fail the item
                with lock:
                    stats["direct"] += 1
                    direct_inflight.discard(url)        # the ledger now covers this url
                _emit(c["title"])
            except Exception as ex:
                # Without this the thread dies. It happened: three workers were killed by
                # a `database is locked` on the status write and nothing reported it --
                # the sweep just stopped making progress with items still queued, which
                # looks like a stall rather than a crash. One lost item is recoverable on
                # the next sweep; a lost worker is not.
                try:
                    _fail(c, f"direct worker error: {type(ex).__name__}: {ex}")
                except Exception:
                    pass
            finally:
                direct_q.task_done()

    n_direct = max(1, int(s["acquire_direct_parallel"]))
    direct_threads = [threading.Thread(target=_direct_worker, daemon=True) for _ in range(n_direct)]
    for _t in direct_threads:
        _t.start()

    # --- NZB search + queue, run concurrently across the worker pool ---
    def _nzb_attempt(c):
        wdb = connect()             # each worker its own connection
        _emit(f"search: {c['title']}")
        try:
            results, e = indexer.search_entity(wdb, "item", c["id"])
        except Exception as ex:
            # The indexer is down or erroring — the direct sources (romsgames, Vimm) may
            # still have the ROM, so fall through to them instead of giving up on the item.
            if s["download_dir"]:
                direct_q.put(c)
            else:
                _skip(c, f"indexer search failed, no download dir for the direct fallback: {ex}", wdb)
            return
        top = _pick(results)
        if top is None and llm.enabled():
            # The strict ranker found nothing confident. Ask the LLM to salvage a real
            # match from the below-floor candidates before falling back to direct sources.
            cand = [r for r in results if r.get("url")]
            if cand:
                _emit(f"llm review: {c['title']}")
                try:
                    top = llm.choose(c["title"], c.get("system"), cand)
                except Exception:
                    top = None
        if top is None:
            if not s["download_dir"]:
                _skip(c, "no usable indexer result, and no ROMCOM_DOWNLOAD_DIR for the direct fallback", wdb); return
            direct_q.put(c)          # hand off to the single direct worker; don't block the pool
            return
        try:
            nzo = actions.queue_result(wdb, "item", e, top)
        except Exception as ex:
            _fail(c, f"SABnzbd error: {ex}"); return
        if nzo is None:
            with wdb:
                wdb.execute("UPDATE items SET status='FAILED' WHERE id=?", (c["id"],))
            _fail(c, "SABnzbd returned no nzo id"); return
        with lock:
            stats["queued"] += 1
        _emit(c["title"])

    def _claim(limit):
        """Atomically take up to `limit` armed, never-attempted items (respecting the batch
        cap). Short-circuits once the cap is hit so the heavy eligibility query is skipped."""
        with lock:
            if batch and processed[0] >= batch:
                return []
        picked = []
        for c in eligible(db):
            with lock:
                if c["id"] in attempted:
                    continue
                if batch and processed[0] >= batch:
                    break
                attempted.add(c["id"]); processed[0] += 1
                picked.append(c)
            if len(picked) >= limit:
                break
        return picked

    ddir = s["download_dir"]
    ddir_ok = bool(ddir) and Path(ddir).exists()
    imported = {"last": None, "adopted": 0, "last_result": None}  # incremental-scan state

    def import_new():
        """Scan the download directory when files have landed since the last pass.
        Unchanged files are resumed via the files table, so re-scans are cheap; 'adopted'
        accumulates across passes (each pass only adopts files new to it)."""
        nonlocal imported
        with lock:
            done_now = stats["downloaded"] + stats["direct"]
        if not ddir_ok or done_now == 0 or imported["last"] == done_now:
            return
        imported["last"] = done_now

        def prog(i, total, name, scan_stats=None):
            if progress: progress(i, total, f"scan: {name}", _snapshot() | (scan_stats or {}))
        r = scan(ddir, name_match=True, adopt=True, progress=prog)
        imported["adopted"] += r.get("adopted", 0)
        imported["last_result"] = r
        if "adopted" in r:
            r = dict(r); r["adopted"] = imported["adopted"]
            imported["last_result"] = r

    def _snapshot():
        with lock:
            return dict(stats)

    # The rolling loop: feed a round of armed items across the NZB pool, sync + import what
    # SABnzbd finished, and keep going until nothing is left to feed, nothing is downloading,
    # and the direct worker's queue is drained. The direct worker runs the whole time.
    note = None
    sync_failures = 0
    with ThreadPoolExecutor(max_workers=nzb_workers) as pool:
        while True:
            if stop and stop.is_set():
                note = "cancelled"; break
            round_items = _claim(nzb_workers * 4)
            if round_items:
                list(pool.map(_nzb_attempt, round_items))
            try:
                actions.sync(db); sync_failures = 0
            except Exception:
                sync_failures += 1
                if sync_failures >= SYNC_FAILURE_LIMIT:
                    note = f"gave up waiting after {SYNC_FAILURE_LIMIT} failed syncs"; break
            _refresh(); import_new()
            with lock:
                waiting = stats["waiting"]
            directs_pending = direct_q.unfinished_tasks
            if not round_items and waiting == 0 and directs_pending == 0:
                break
            if not round_items:
                # Waiting phase: nothing new to feed, but SABnzbd and/or the direct worker
                # are still going — show progress and poll for completions.
                snap = _snapshot()
                _emit(f"{snap['queued']} queued · {waiting} downloading · "
                      f"{directs_pending} direct pending · {snap['downloaded']} done")
                if time.monotonic() - start > max_wait:
                    note = f"wait cap reached; {waiting} download(s) still pending"; break
                time.sleep(poll)

    # Tell each direct worker to stop after its current item. A straggler mid-download keeps
    # running as a daemon (Vimm self-serializes; romsgames is request-paced) and won't
    # overlap the next sweep meaningfully.
    for _t in direct_threads:
        direct_q.put(DONE)
    for _t in direct_threads:
        _t.join(timeout=poll if poll else 1.0)

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