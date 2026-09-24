"""Local web UI: `romcom web` serves this Flask app on 127.0.0.1."""
import json, os, string, threading, time
from flask import Flask, jsonify, request, send_from_directory
from pathlib import Path
from .config import load_yaml, settings, ENV_VARS, invalidate as invalidate_settings
from .db import connect
from .catalog import import_dats
from .scanner import scan, adopt_unmatched
from .organizer import organize
from .report import summary
from .planner import bulk_plan, next_individuals, next_picks
from .doctor import run as doctor_run
from .catalog_status import catalog_status
from .manage import set_series
from .status import LIFECYCLE, MISSING, SATISFIED, own_all
from . import (indexer, actions, acquirer, sab, webdl, webauth, chattools, webchat,
               mcpclient, mcpserver)

STATIC = Path(__file__).resolve().parent / "webui"

ITEM_FIELDS = {"authorized", "status", "wanted", "preferred_runtime", "notes", "play_status", "system", "region", "language", "keep"}
VOLUME_FIELDS = {"authorized", "status"}

def _entity(db, ident):
    i = db.execute("SELECT * FROM items WHERE id=?", (ident,)).fetchone()
    if i: return "item", i
    v = db.execute("SELECT * FROM volumes WHERE id=?", (ident,)).fetchone()
    if v: return "volume", v
    return None, None

def create_app():
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="")

    @app.errorhandler(Exception)
    def on_error(e):
        code = getattr(e, "code", None)
        if isinstance(code, int):  # Flask/HTTP errors keep their status
            return jsonify({"error": str(e)}), code
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    # Master login. Inert unless ROMCOM_WEB_USER/ROMCOM_WEB_PASS are set, so this is a
    # no-op for every existing test and for anyone running the app as before. The gate is a
    # before_request hook, so it only ever sees HTTP requests — the watcher, watchdog, and
    # checkpointer run in threads that call functions directly and are unaffected.
    webauth.install(app)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/summary")
    def api_summary():
        db = connect()
        active = db.execute("SELECT COUNT(*) c FROM jobs WHERE status IN ('QUEUED','DOWNLOADING')").fetchone()["c"]
        satisfied = db.execute(f"SELECT COUNT(*) c FROM items WHERE wanted=1 AND status IN {SATISFIED}").fetchone()["c"]
        return jsonify(summary() | {"active_jobs": active, "satisfied": satisfied})

    @app.get("/api/doctor")
    def api_doctor():
        return jsonify([{"name": n, "ok": ok, "detail": d} for n, ok, d in doctor_run()])

    SETTABLE = {"nzb_url", "nzb_key", "sab_url", "sab_key", "sab_category", "sab_verify_ssl",
                "download_dir", "acquire_poll", "acquire_max_wait_min", "acquire_batch_max",
                "acquire_parallel", "acquire_direct_parallel", "acquire_watch", "acquire_interval",
                "acquire_watch_batch", "acquire_sweep_pause",
                "webdl_base", "webdl_delay", "webdl_jitter", "webdl_timeout",
                "vimm_enabled", "vimm_base", "vimm_dl_base", "vimm_delay", "vimm_jitter", "vimm_timeout",
                "archive_enabled", "archive_base", "archive_delay", "archive_timeout",
                "search_cache_ttl", "llm_enabled", "llm_base", "llm_model", "llm_timeout",
                "chat_enabled", "chat_model", "chat_embed_model", "chat_history_max",
                "mcp_enabled", "mcp_servers_path"}

    def _settings_payload(db):
        s = settings()
        rows = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM app_settings")}
        sources = {}
        for k in SETTABLE:
            if rows.get(k):
                sources[k] = "ui"    # saved via the Settings tab (overrides .env)
            elif os.getenv(ENV_VARS.get(k, "")):
                sources[k] = "env"  # set in .env (load_dotenv) or the process environment
            else:
                sources[k] = "default"
        return {"settings": {k: s[k] for k in sorted(SETTABLE)},
                "overrides": sorted(k for k in SETTABLE if rows.get(k)),
                "sources": sources}

    @app.get("/api/settings")
    def api_settings():
        db = connect()  # first call on an old DB migrates the app_settings table in
        return jsonify(_settings_payload(db))

    @app.post("/api/settings")
    def api_save_settings():
        body = request.get_json(force=True) or {}
        for k in body:
            if k not in SETTABLE:
                return jsonify({"error": f"unknown setting: {k}"}), 400
        db = connect()
        with db:
            for k, v in body.items():
                db.execute("""INSERT INTO app_settings(key,value) VALUES(?,?)
                  ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""",
                           (k, "" if v is None else str(v).strip()))
        invalidate_settings()
        return jsonify(_settings_payload(db))

    @app.post("/api/settings/test")
    def api_settings_test():
        """Live connectivity check for the Settings tab. API keys never appear in
        error details — request errors echo the URL they hit, key included."""
        def _safe(e):
            msg = str(e)
            for k in (settings()["nzb_key"], settings()["sab_key"]):
                if k:
                    msg = msg.replace(k, "***")
            return msg
        probes = [("indexer", lambda: indexer.ping()),
                  ("sabnzbd", lambda: sab.queue()),
                  ("romsgames", lambda: webdl.test())]
        if settings()["vimm_enabled"]:
            from . import vimm
            probes.append(("vimm", lambda: vimm.test()))
        if settings()["archive_enabled"]:
            from . import archive
            probes.append(("archive.org", lambda: archive.test()))
        if settings()["llm_enabled"]:
            from . import llm
            probes.append(("ollama", lambda: llm.test()))
        out = {}
        for name, fn in probes:
            try:
                fn()
                out[name] = {"ok": True, "detail": ""}
            except Exception as e:
                out[name] = {"ok": False, "detail": _safe(e)}
        return jsonify(out)

    @app.get("/api/catalog-status")
    def api_catalog_status():
        return jsonify(catalog_status())

    @app.get("/api/facets")
    def api_facets():
        db = connect()
        systems = [r["system"] for r in db.execute("SELECT DISTINCT system FROM items WHERE system IS NOT NULL ORDER BY system")]
        cfg = load_yaml("catalogs.yaml")
        known = {s for meta in cfg.get("catalogs", {}).values() for s in meta.get("systems", [])}
        known |= set(cfg.get("custom", {}).get("systems", []))
        return jsonify({"systems": systems, "statuses": LIFECYCLE, "all_systems": sorted(known | set(systems))})

    def _item_filter(args):
        q = "SELECT * FROM items WHERE 1=1"; p = []
        if args.get("system"): q += " AND system=?"; p.append(args["system"])
        if args.get("status"): q += " AND status=?"; p.append(args["status"])
        if args.get("q"):
            q += " AND (title LIKE ? OR series LIKE ? OR id LIKE ?)"
            like = f"%{args['q']}%"; p += [like, like, like]
        view = args.get("view", "all")
        if view == "missing":
            q += f" AND wanted=1 AND status IN {MISSING}"
        elif view == "wanted":
            q += " AND wanted=1"
        elif view == "satisfied":
            q += f" AND status IN {SATISFIED}"
        return q, p

    @app.get("/api/items")
    def api_items():
        db = connect()
        q, p = _item_filter(request.args)
        total = db.execute(f"SELECT COUNT(*) c FROM ({q})", p).fetchone()["c"]
        limit = min(int(request.args.get("limit", 200)), 1000)
        offset = int(request.args.get("offset", 0))
        q += " ORDER BY system,series,series_number,title LIMIT ? OFFSET ?"; p += [limit, offset]
        return jsonify({"total": total, "items": [dict(r) for r in db.execute(q, p)]})

    @app.post("/api/items/<ident>")
    def api_set(ident):
        body = request.get_json(force=True)
        field, value = body.get("field"), body.get("value")
        db = connect()
        kind, e = _entity(db, ident)
        if not kind:
            return jsonify({"error": "unknown item/volume"}), 404
        allowed = ITEM_FIELDS if kind == "item" else VOLUME_FIELDS
        if field not in allowed:
            return jsonify({"error": f"field must be one of: {', '.join(sorted(allowed))}"}), 400
        if field in ("authorized", "wanted", "keep"):
            value = 1 if str(value).lower() in ("1", "true", "yes", "on") else 0
        table = "items" if kind == "item" else "volumes"
        with db:
            db.execute(f"UPDATE {table} SET {field}=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (value, ident))
        _, updated = _entity(db, ident)
        out = dict(updated)
        if kind == "item" and field in ("authorized", "wanted") and _maybe_auto_acquire():
            out["auto_acquire_started"] = True
        return jsonify(out)

    @app.post("/api/items/bulk")
    def api_bulk():
        body = request.get_json(force=True)
        field, value = body.get("field"), body.get("value")
        if field not in ("wanted", "authorized", "status"):
            return jsonify({"error": "field must be wanted, authorized, or status"}), 400
        if field in ("wanted", "authorized"):
            value = 1 if str(value).lower() in ("1", "true", "yes", "on") else 0
        q, p = _item_filter(body.get("filters") or {})
        db = connect()
        with db:
            cur = db.execute(f"UPDATE items SET {field}=?,updated_at=CURRENT_TIMESTAMP "
                             f"WHERE id IN (SELECT id FROM ({q}))", [value] + p)
        if field in ("wanted", "authorized"):
            _maybe_auto_acquire()
        return jsonify({"updated": cur.rowcount})

    @app.post("/api/series/<name>")
    def api_set_series(name):
        body = request.get_json(force=True)
        try:
            count = set_series(name, body.get("field"), body.get("value"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if body.get("field") in ("authorized", "wanted"):
            _maybe_auto_acquire()
        return jsonify({"updated": count})

    @app.post("/api/library/own")
    def api_mark_owned():
        """Everything already on disk counts as wanted & authorized, so coverage, the
        missing list and the SD-card export agree with the collection itself. Safe to
        press while a watcher runs: the pipeline only ever searches CATALOGED/MISSING
        items, and this never lowers a status. EXCLUDED items are left alone."""
        db = connect()
        with db:
            updated = own_all(db)
        return jsonify({"updated": updated, "eligible": acquirer.eligible_count()})

    @app.get("/api/plan")
    def api_plan():
        summ = acquirer.armed_summary()   # one events scan for both counts, not two
        return jsonify({"volumes": bulk_plan(), "items": next_individuals(50),
                        "eligible": summ["eligible"], "cooling": summ["cooling"]})

    @app.get("/api/next")
    def api_next():
        """Paged/filterable next individual picks for the Acquire tab."""
        items, total = next_picks(
            search=request.args.get("q", "").strip() or None,
            system=request.args.get("system", "").strip() or None,
            limit=request.args.get("limit", 50, type=int) or 50,
            offset=max(request.args.get("offset", 0, type=int) or 0, 0))
        return jsonify({"items": items, "total": total})

    @app.get("/api/search/<ident>")
    def api_search(ident):
        db = connect()
        kind, e = _entity(db, ident)
        if not kind:
            return jsonify({"error": "unknown item/volume"}), 404
        try:
            results, e = indexer.search_entity(db, kind, ident)
        except PermissionError as ex:
            return jsonify({"error": str(ex)}), 403
        return jsonify({"kind": kind, "entity": dict(e), "results": results})

    @app.post("/api/acquire/skip")
    def api_acquire_skip():
        """Defer an item to the back of the queue after a failed manual search — record an
        acquire-skip event so next_picks (least-recently-tried first) sinks it, and the
        auto-watcher cools it briefly rather than re-hitting the same empty search."""
        body = request.get_json(force=True) or {}
        db = connect()
        kind, e = _entity(db, body.get("ident", ""))
        if kind != "item":
            return jsonify({"error": "unknown item"}), 404
        with db:
            db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,'acquire-skip',?)",
                       (e["id"], body.get("reason") or "manual search: no usable result"))
        return jsonify({"deferred": e["id"]})

    @app.post("/api/acquire")
    def api_acquire():
        body = request.get_json(force=True)
        db = connect()
        kind, e = _entity(db, body.get("ident", ""))
        if not kind:
            return jsonify({"error": "unknown item/volume"}), 404
        if not e["authorized"]:
            return jsonify({"error": f"{kind} is not marked authorized"}), 403
        if not body.get("url"):
            return jsonify({"error": "missing result url"}), 400
        nzo = actions.queue_result(db, kind, e,
                                   {"url": body["url"], "title": body.get("title", ""), "size": body.get("size", 0)})
        return jsonify({"queued": e["title"], "nzo_id": nzo})

    @app.post("/api/sync")
    def api_sync():
        return jsonify(actions.sync())

    @app.post("/api/auto-acquire")
    def api_auto_acquire():
        return start_job("acquire", {})

    @app.post("/api/acquire/watch")
    def api_watch_toggle():
        """The always-on watcher toggle (Sonarr-style). Persisting the flag is what
        matters: a run in flight keeps going and picks the flag up at cycle end."""
        body = request.get_json(force=True) or {}
        on = str(body.get("on", "")).strip().lower() in ("1", "true", "yes", "on")
        return jsonify(_set_watch(on))

    @app.get("/api/acquire/watch")
    def api_watch_state():
        return jsonify({"on": settings()["acquire_watch"], "running": jobs["acquire"]["running"]})

    @app.get("/api/acquire/health")
    def api_watch_health():
        """Liveness of the always-on watcher: is the toggle on, is the thread actually
        running, how long since its last heartbeat, and does the self-healing watchdog
        consider it healthy. 'stale' means it's running but hasn't beat in a while (a long
        SABnzbd wait is normal; a very old beat suggests a hang the watchdog can't kill)."""
        j = jobs["acquire"]
        beat = j.get("last_beat")
        secs = round(time.monotonic() - beat, 1) if beat else None
        watch_on = bool(settings()["acquire_watch"])
        # Idle rest beats every ~5s and the wait phase every poll interval; allow generous
        # slack before calling a running watcher stale.
        stale = bool(j["running"] and secs is not None and secs > max(120.0, settings()["acquire_poll"] * 3))
        if not watch_on:
            state = "off"
        elif j["running"]:
            state = "stale" if stale else "alive"
        else:
            state = "recovering"  # watch on but thread down — the watchdog will relaunch it
        return jsonify({"watch_on": watch_on, "running": j["running"], "state": state,
                        "stale": stale, "last_beat_secs": secs, "error": j.get("error"),
                        "current": j.get("current"), "stats": j.get("stats")})

    # One background job per kind at a time; state polled by the UI.
    jobs = {k: {"running": False, "done": 0, "total": 0, "current": "", "stats": None,
                "result": None, "error": None, "last_beat": None}
            for k in ("import", "scan", "organize", "adopt", "acquire")}
    job_lock = threading.Lock()

    def _job_fn(kind, params):
        if kind == "import":
            return lambda prog: import_dats(params["path"], system=params.get("system") or None,
                                            source=params.get("source") or None,
                                            wanted=bool(params.get("wanted")), progress=prog)
        if kind == "scan":
            return lambda prog: scan(params["path"], name_match=params.get("name_match", True),
                                     adopt=params.get("adopt", True),
                                     recursive=params.get("recursive", True), progress=prog)
        if kind == "organize":
            return lambda prog: organize(params["path"], systems=params.get("systems") or None, progress=prog)
        if kind == "acquire":
            return lambda prog: acquirer.auto_acquire(progress=prog, watch=bool(params.get("watch")),
                                                      stop=acquirer.STOP)
        return lambda prog: adopt_unmatched(root=params.get("path") or None, progress=prog)

    def _launch(kind, params):
        """Start a job thread; the request is journaled so an interrupted job resumes on server start."""
        j = jobs[kind]
        with job_lock:
            if j["running"]:
                return False
            if kind == "acquire":
                acquirer.STOP.clear()
            j.update(running=True, done=0, total=0, current="starting…", stats=None,
                     result=None, error=None, last_beat=time.monotonic())
        db = connect()
        with db:
            cur = db.execute("INSERT INTO web_jobs(kind,params,status) VALUES(?,?,'running')",
                             (kind, json.dumps(params)))
        jid = cur.lastrowid
        fn = _job_fn(kind, params)

        def run():
            status = "done"
            try:
                j["result"] = fn(lambda i, total, name, stats=None:
                                 j.update(done=i, total=total, current=name, stats=stats,
                                          last_beat=time.monotonic()))
            except Exception as e:
                j["error"] = f"{type(e).__name__}: {e}"; status = "error"
            finally:
                j["running"] = False
                jdb = connect()
                with jdb:
                    jdb.execute("UPDATE web_jobs SET status=?,finished_at=CURRENT_TIMESTAMP WHERE id=?", (status, jid))

        threading.Thread(target=run, daemon=True).start()
        return True

    def start_job(kind, params):
        if not _launch(kind, params):
            return jsonify({"error": f"a {kind} job is already running"}), 409
        return jsonify({"started": True})

    def _set_watch(on):
        """Flip the always-on watcher. Shared by the Acquire tab's toggle and the
        assistant's watcher tool so the two can never diverge — one implementation, one
        behaviour."""
        db = connect()
        with db:
            db.execute("""INSERT INTO app_settings(key,value) VALUES('acquire_watch',?)
              ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""",
                       ("true" if on else "false",))
        invalidate_settings()
        launched = False
        if on:
            acquirer.STOP.clear()
            launched = _launch("acquire", {"watch": True})
        else:
            acquirer.STOP.set()  # the in-flight cycle exits at its next stop check
        return {"on": on, "launched": launched, "running": jobs["acquire"]["running"]}

    def _set_settings(values):
        """The assistant's set_setting tool. Routed through here rather than writing
        app_settings itself so the SETTABLE whitelist is enforced in exactly one place — a
        tool must not be able to persist a key the Settings tab would reject."""
        bad = sorted(k for k in values if k not in SETTABLE)
        if bad:
            return {"error": f"unknown setting(s): {', '.join(bad)}"}
        db = connect()
        with db:
            for k, v in values.items():
                db.execute("""INSERT INTO app_settings(key,value) VALUES(?,?)
                  ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP""",
                           (k, "" if v is None else str(v).strip()))
        invalidate_settings()
        return {"saved": sorted(values)}

    def _maybe_auto_acquire():
        """Marking an item approved & wanted arms it — start the acquire pipeline.

        Silently no-ops when nothing is eligible or a run is already in flight
        (the running job's sweeps pick the item up on their next pass).
        """
        try:
            if acquirer.eligible_count() == 0:
                return False
        except Exception:
            return False
        try:
            return _launch("acquire", {})
        except Exception:
            return False

    def recover_interrupted():
        """Re-launch jobs that were mid-flight when the server last stopped."""
        db = connect()
        stale = db.execute("SELECT * FROM web_jobs WHERE status='running' ORDER BY id").fetchall()
        with db:
            db.execute("UPDATE web_jobs SET status='interrupted',finished_at=CURRENT_TIMESTAMP WHERE status='running'")
        latest = {}
        for r in stale:
            latest[r["kind"]] = json.loads(r["params"])
        for kind, params in latest.items():
            if kind not in jobs: continue
            if kind != "organize" and params.get("path") and not Path(params["path"]).exists(): continue
            _launch(kind, params)

    def _checked_path(body, must_exist=True):
        path = (body.get("path") or "").strip().strip('"')
        if not path or (must_exist and not Path(path).exists()):
            return None
        return path

    @app.post("/api/import-dats")
    def api_import_dats():
        body = request.get_json(force=True)
        path = _checked_path(body)
        if not path:
            return jsonify({"error": f"path not found: {body.get('path') or '(empty)'}"}), 400
        return start_job("import", {"path": path, "system": body.get("system") or None,
                                    "source": body.get("source") or None, "wanted": bool(body.get("wanted"))})

    @app.post("/api/scan")
    def api_scan():
        body = request.get_json(force=True)
        path = _checked_path(body)
        if not path:
            return jsonify({"error": f"path not found: {body.get('path') or '(empty)'}"}), 400
        return start_job("scan", {"path": path,
                                  "name_match": body.get("name_match", True) not in (False, "false", 0),
                                  "adopt": body.get("adopt", True) not in (False, "false", 0),
                                  "recursive": body.get("recursive", True) not in (False, "false", 0)})

    @app.post("/api/item/<path:item_id>/keep")
    def api_item_keep(item_id):
        """Mark or unmark an item for export. A database write, so the service can do it
        even though it cannot launch the emulator itself."""
        from . import player
        on = str((request.get_json(silent=True) or {}).get("keep", True)).lower() not in ("0", "false", "no")
        try:
            return jsonify(player.set_keep(item_id, on=on))
        except LookupError as e:
            return jsonify({"error": str(e)}), 404

    @app.post("/api/item/<path:item_id>/play")
    def api_item_play(item_id):
        """Queue a launch for the agent running in the owner's session.

        The server cannot spawn it: a Windows service lives in session 0 whatever account
        it runs as, and session 0 has no desktop, so the emulator window would exist and be
        invisible. Refuses up front when no agent is alive rather than queuing into a void."""
        from . import player
        st = player.agent_status()
        if not st["running"]:
            return jsonify({"error": "no launch agent is running — start it in your own "
                                     "session with: romcom agent", "agent": st}), 409
        try:
            return jsonify(player.request_launch(item_id))
        except LookupError as e:
            return jsonify({"error": str(e)}), 404

    @app.get("/api/play/<int:rid>")
    def api_play_status(rid):
        from . import player
        r = player.launch_request(rid)
        return (jsonify(r), 200) if r else (jsonify({"error": "no such request"}), 404)

    @app.get("/api/agent")
    def api_agent():
        from . import player
        return jsonify(player.agent_status())

    @app.get("/api/item/<path:item_id>/launch-command")
    def api_item_launch_command(item_id):
        """What to run to play this. Deliberately NOT a launch: the service runs as
        LocalSystem in session 0, which is isolated from the desktop, so anything it
        spawned would be an invisible process holding the ROM open. The UI shows the
        command; `romcom play` runs it in the owner's own session."""
        from . import player
        try:
            return jsonify(player.command_for(item_id))
        except LookupError as e:
            return jsonify({"error": str(e)}), 404

    @app.get("/api/keep")
    def api_keep_list():
        from . import player
        return jsonify(player.kept(system=request.args.get("system") or None))

    @app.post("/api/organize")
    def api_organize():
        body = request.get_json(force=True)
        path = _checked_path(body, must_exist=False)
        if not path:
            return jsonify({"error": "destination path is required"}), 400
        systems = [s for s in (body.get("systems") or []) if s]
        return start_job("organize", {"path": path, "systems": systems})

    @app.post("/api/adopt")
    def api_adopt():
        body = request.get_json(force=True, silent=True) or {}
        return start_job("adopt", {"path": (body.get("path") or "").strip()})

    @app.get("/api/job/<kind>/status")
    def api_job_status(kind):
        if kind not in jobs:
            return jsonify({"error": "unknown job"}), 404
        return jsonify(jobs[kind])

    @app.get("/api/jobs/active")
    def api_jobs_active():
        return jsonify({k: {"done": j["done"], "total": j["total"], "current": j["current"]}
                        for k, j in jobs.items() if j["running"]})

    app.recover_interrupted = recover_interrupted

    def start_watcher_if_on():
        """The watcher toggle survives restarts: flip it on once and every server
        start resumes the always-on loop."""
        try:
            if settings()["acquire_watch"] and not jobs["acquire"]["running"]:
                _launch("acquire", {"watch": True})
        except Exception:
            pass

    app.start_watcher_if_on = start_watcher_if_on

    WATCHDOG_INTERVAL = 15.0  # seconds between watcher liveness checks

    # Deliberately generous. A single large transfer can run for minutes without an
    # emit, so this must be far longer than any plausible quiet stretch -- it is a
    # wedge detector, not a progress meter.
    WATCHDOG_STALL_SECS = 900

    def _watchdog_tick():
        """One liveness check: if the watch toggle is on but the watcher is not actually
        working -- it crashed, was never started, or is wedged -- relaunch it. Does
        nothing while the watcher is being deliberately stopped (STOP set). Returns True
        if it relaunched.

        Checking `running` alone was not enough. When the direct-download workers died,
        the coordinating thread stayed blocked waiting on a queue nothing would ever
        drain, so the job remained `running: true` with a frozen heartbeat forever. The
        watchdog never fired, and because every start path refuses while a job claims to
        be running, acquisition could not be restarted either -- the assistant tried five
        times and was told "a acquire job is already running" each time. A stall has to
        count as death, or one hang costs a service restart.
        """
        try:
            if not settings()["acquire_watch"] or acquirer.STOP.is_set():
                return False
            j = jobs["acquire"]
            if j["running"]:
                beat = j.get("last_beat")
                if beat is None or (time.monotonic() - beat) < WATCHDOG_STALL_SECS:
                    return False
                # Wedged. Release the claim so the relaunch below is allowed to proceed;
                # the stuck thread is a daemon and cannot block shutdown.
                j["running"] = False
                j["error"] = (f"watchdog: no progress for {WATCHDOG_STALL_SECS}s -- "
                              "treated as stalled and restarted")
            acquirer.STOP.clear()
            return _launch("acquire", {"watch": True})
        except Exception:
            pass  # the watchdog itself must never die
        return False

    def _watchdog():
        """Keep the always-on watcher alive. Combined with auto_acquire's per-sweep error
        recovery, this is what makes 'always healthy' true in practice: no single failure
        can leave downloads silently stopped until someone notices."""
        while True:
            time.sleep(WATCHDOG_INTERVAL)
            _watchdog_tick()

    def start_watchdog():
        threading.Thread(target=_watchdog, daemon=True).start()

    CHECKPOINT_INTERVAL = 60.0  # seconds between WAL truncating checkpoints

    WAL_WARN_BYTES = 128 * 1024 * 1024

    def _checkpointer():
        """Fold the WAL back into the db on a cadence.

        The parallel watcher writes constantly and the server reads constantly, so a
        TRUNCATE checkpoint routinely comes back busy: it needs every reader to stand
        down at once. That failure used to be swallowed entirely, and the WAL grew to
        428 MB unnoticed, at which point every read scans it and the UI takes seconds to
        load. PASSIVE first (it reclaims what it can without waiting on anyone), then
        TRUNCATE to reset the file, and a log line when the WAL stays large anyway --
        silence is what let this get to 428 MB."""
        from .db import checkpoint, connect as _conn
        from pathlib import Path as _P
        warned = False
        while True:
            time.sleep(CHECKPOINT_INTERVAL)
            try:
                db = _conn()
                try:
                    db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                finally:
                    db.close()
                checkpoint()
            except Exception:
                pass  # a busy checkpoint is fine — the next tick tries again
            try:
                wal = _P(str(settings()["db"]) + "-wal")
                size = wal.stat().st_size if wal.exists() else 0
                if size > WAL_WARN_BYTES and not warned:
                    print(f" * WARNING: WAL is {size / 1048576:.0f} MB and not draining — "
                          "reads will be slow until it does", flush=True)
                    warned = True
                elif size <= WAL_WARN_BYTES:
                    warned = False
            except Exception:
                pass

    def start_checkpointer():
        threading.Thread(target=_checkpointer, daemon=True).start()

    app.jobs = jobs          # test seam: lets a test freeze a heartbeat
    app.watchdog_tick = _watchdog_tick
    app.start_watchdog = start_watchdog
    app.start_checkpointer = start_checkpointer

    # Back-compat alias used by earlier UI builds.
    @app.get("/api/import-dats/status")
    def api_import_status():
        return jsonify(jobs["import"])

    @app.get("/api/browse")
    def api_browse():
        p = (request.args.get("path") or "").strip().strip('"')
        if not p:
            drives = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
            return jsonify({"path": "", "parent": None, "dirs": drives, "files": []})
        base = Path(p)
        if not base.exists():
            return jsonify({"error": f"not found: {p}"}), 404
        if base.is_file():
            base = base.parent
        dirs, files = [], []
        try:
            for c in sorted(base.iterdir(), key=lambda x: x.name.lower()):
                try:
                    if c.is_dir():
                        dirs.append(c.name)
                    elif c.suffix.lower() in (".dat", ".xml", ".zip", ".gz"):
                        files.append(c.name)
                except OSError:
                    pass
        except PermissionError:
            return jsonify({"error": "permission denied"}), 403
        parent = "" if base.parent == base else str(base.parent)
        return jsonify({"path": str(base), "parent": parent, "dirs": dirs, "files": files})

    @app.get("/api/jobs")
    def api_jobs():
        db = connect()
        rows = db.execute("""SELECT j.*, COALESCE(i.title,v.title,j.entity_id) entity_title
          FROM jobs j
          LEFT JOIN items i ON j.entity_type='item' AND i.id=j.entity_id
          LEFT JOIN volumes v ON j.entity_type='volume' AND v.id=j.entity_id
          ORDER BY j.id DESC LIMIT 200""").fetchall()
        return jsonify([dict(r) for r in rows])

    # Assistant. `ctx` is how tools reach closure state that lives in here — the job dict,
    # the launcher, the watcher toggle — without chattools importing web.py (circular).
    # Everything else a tool needs it imports from a sibling at call time.
    chat_ctx = chattools.Ctx(
        job_status=lambda kind: dict(jobs.get(kind) or {}),
        jobs_active=lambda: {k: {"done": j["done"], "total": j["total"], "current": j["current"]}
                             for k, j in jobs.items() if j["running"]},
        launch_job=lambda kind, params: _launch(kind, params),
        launch_watcher=_set_watch,
        arm_acquire=_maybe_auto_acquire,
        set_settings=_set_settings)
    webchat.register(app, chat_ctx)

    # MCP server. Registered unconditionally: an unset ROMCOM_MCP_KEY already means every
    # call 401s, so gating registration on a flag would only make the failure mode less
    # legible (404 instead of 401).
    mcpserver.register(app, chat_ctx)

    return app

# One chat turn occupies a worker thread for as long as it runs -- tool-heavy turns are
# tens of seconds, and a turn paused on an approval card holds its thread until answered.
# Waitress defaults to 4, which a single open assistant panel plus the dashboard's
# once-a-second job polling can exhaust, and an exhausted pool looks exactly like a hung
# server. Threads are cheap here; starving the poller is not.
SERVE_THREADS = 16
# Default is 120s, measured from the last byte. An SSE stream waiting on an approval sends
# nothing meanwhile, so the default would drop precisely the connection the owner is
# looking at.
SERVE_CHANNEL_TIMEOUT = 1800


class _AccessLog:
    """Request logging, which waitress does not do and werkzeug did.

    Not cosmetic: the werkzeug access log is what identified the dead chat stream -- it
    showed the browser's polling arriving while no /api/chat/stream request ever did, which
    located the fault in the client rather than the agent. Losing that would be a real
    regression, so the same one-line-per-request format is kept."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        def _start(status, headers, exc_info=None):
            q = environ.get("QUERY_STRING")
            print(f'{environ.get("REMOTE_ADDR", "-")} - - '
                  f'[{time.strftime("%d/%b/%Y %H:%M:%S")}] '
                  f'"{environ.get("REQUEST_METHOD", "-")} {environ.get("PATH_INFO", "-")}'
                  f'{"?" + q if q else ""}" {str(status).split(" ")[0]} -', flush=True)
            return start_response(status, headers, exc_info)
        return self.app(environ, _start)


def serve(host="127.0.0.1", port=8927, open_browser=True, debug=False):
    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    app = create_app()
    app.recover_interrupted()          # first connect() here builds any new indexes
    from .db import connect, backfill_content
    n = backfill_content(connect())    # one-time, batched, BEFORE the watcher starts writing
    if n:
        print(f"backfilled files.content for {n} row(s)")
    # The only reliably quiet moment in the process: the watcher has not started and no
    # request is being served. A truncating checkpoint needs every reader to stand down,
    # and once the watcher is up that window may never come again -- the WAL was found at
    # 428 MB, which put simple reads at 6-16 seconds.
    try:
        from .db import checkpoint as _ckpt
        busy, pages, done = _ckpt()
        print(f" * WAL checkpoint at startup: {done}/{pages} pages folded in"
              f"{' (BUSY)' if busy else ''}", flush=True)
    except Exception as e:
        print(f" * WAL checkpoint at startup failed: {type(e).__name__}: {e}", flush=True)
    app.start_watcher_if_on()
    app.start_watchdog()
    app.start_checkpointer()
    # Werkzeug's server is for development and says so on every boot; this runs as a service
    # from boot. Waitress is kept optional so a checkout without it still starts, and debug
    # still means werkzeug because waitress has no reloader or debugger.
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        waitress_serve = None
    if waitress_serve and not debug:
        print(f" * Rom-Com on http://{host}:{port} (waitress, {SERVE_THREADS} threads)", flush=True)
        waitress_serve(_AccessLog(app), host=host, port=port, threads=SERVE_THREADS,
                       channel_timeout=SERVE_CHANNEL_TIMEOUT, ident="Rom-Com")
    else:
        app.run(host=host, port=port, debug=debug)
