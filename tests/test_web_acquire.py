import threading
from romcom.db import connect
from romcom.web import create_app


def make_client(monkeypatch, tmp_path, rows):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    db = connect()
    with db:
        for r in rows:
            db.execute("INSERT INTO items(id,title,series,authorized,wanted,status) VALUES(?,?,?,?,?,?)",
                       (r["id"], r.get("title", r["id"]), r.get("series"), r.get("authorized", 0),
                        r.get("wanted", 1), r.get("status", "CATALOGED")))
    # Block the pipeline on an Event so "running" is observable from the request thread.
    started, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def stub(progress=None, **kwargs):
        calls["n"] += 1
        calls["last_kwargs"] = kwargs
        started.set()
        release.wait(timeout=30)
        return {"queued": 0, "downloaded": 0, "download_failed": 0, "failed": 0, "skipped": 0,
                "skipped_by_reason": {}, "failed_items": [], "still_pending": 0,
                "wait_note": None, "elapsed_min": 0, "scan": None, "scan_note": None}
    monkeypatch.setattr("romcom.acquirer.auto_acquire", stub)
    app = create_app()
    return app.test_client(), started, release, calls


def test_watch_health_and_watchdog_relaunch(monkeypatch, tmp_path):
    """Health reflects the watcher's real state, and the watchdog relaunches it when the
    toggle is on but no thread is running — the self-heal that keeps downloads flowing."""
    from romcom import acquirer
    from romcom.config import invalidate
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    db = connect()
    started, release = threading.Event(), threading.Event()

    def stub(progress=None, **kwargs):
        started.set(); release.wait(timeout=30); return {}
    monkeypatch.setattr("romcom.acquirer.auto_acquire", stub)
    acquirer.STOP.clear()
    app = create_app()
    c = app.test_client()
    try:
        assert c.get("/api/acquire/health").get_json()["state"] == "off"
        with db:  # arm the always-on watcher, but nothing is running yet
            db.execute("INSERT INTO app_settings(key,value) VALUES('acquire_watch','true')")
        invalidate()
        h = c.get("/api/acquire/health").get_json()
        assert h["watch_on"] is True and h["running"] is False and h["state"] == "recovering"
        assert app.watchdog_tick() is True         # the watchdog notices and relaunches
        assert started.wait(2)
        assert c.get("/api/acquire/health").get_json()["state"] in ("alive", "stale")
    finally:
        release.set()
        acquirer.STOP.clear()


def test_watchdog_leaves_stopped_watcher_alone(monkeypatch, tmp_path):
    """When the user turns the watcher off (STOP set), the watchdog must not fight them
    and relaunch it, even if the persisted toggle hasn't flipped yet."""
    from romcom import acquirer
    from romcom.config import invalidate
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    db = connect()
    monkeypatch.setattr("romcom.acquirer.auto_acquire", lambda progress=None, **k: {})
    app = create_app()
    try:
        with db:
            db.execute("INSERT INTO app_settings(key,value) VALUES('acquire_watch','true')")
        invalidate()
        acquirer.STOP.set()                        # user is stopping it
        assert app.watchdog_tick() is False        # so the watchdog stands down
    finally:
        acquirer.STOP.clear()


def test_toggle_arms_auto_acquire(monkeypatch, tmp_path):
    c, started, release, _ = make_client(monkeypatch, tmp_path, [{"id": "i1", "title": "Game"}])
    try:
        r = c.post("/api/items/i1", json={"field": "authorized", "value": 1})
        assert r.status_code == 200
        assert r.get_json()["auto_acquire_started"] is True
        assert started.wait(2)
    finally:
        release.set()


def test_unrelated_field_does_not_trigger(monkeypatch, tmp_path):
    c, started, release, _ = make_client(monkeypatch, tmp_path,
                                         [{"id": "i1", "title": "Game", "authorized": 1}])
    try:
        r = c.post("/api/items/i1", json={"field": "notes", "value": "check later"})
        assert r.status_code == 200
        assert "auto_acquire_started" not in r.get_json()
        assert not started.is_set()
    finally:
        release.set()


def test_disarming_does_not_trigger(monkeypatch, tmp_path):
    c, started, release, _ = make_client(monkeypatch, tmp_path,
                                         [{"id": "i1", "title": "Game", "authorized": 1}])
    try:
        r = c.post("/api/items/i1", json={"field": "authorized", "value": 0})
        assert r.status_code == 200
        assert "auto_acquire_started" not in r.get_json()
        assert not started.is_set()
    finally:
        release.set()


def test_bulk_triggers(monkeypatch, tmp_path):
    c, started, release, _ = make_client(monkeypatch, tmp_path,
                                         [{"id": "i1"}, {"id": "i2"}, {"id": "i3", "wanted": 0}])
    try:
        r = c.post("/api/items/bulk", json={"field": "authorized", "value": 1, "filters": {}})
        assert r.status_code == 200 and r.get_json()["updated"] == 3
        assert started.wait(2)
    finally:
        release.set()


def test_series_triggers(monkeypatch, tmp_path):
    c, started, release, _ = make_client(monkeypatch, tmp_path,
                                         [{"id": "i1", "series": "Saga", "title": "Saga 1"}])
    try:
        r = c.post("/api/series/Saga", json={"field": "authorized", "value": "true"})
        assert r.status_code == 200 and r.get_json()["updated"] == 1
        assert started.wait(2)
    finally:
        release.set()


def test_manual_endpoint_and_409(monkeypatch, tmp_path):
    c, started, release, _ = make_client(monkeypatch, tmp_path, [{"id": "i1"}])
    try:
        assert c.post("/api/auto-acquire").status_code == 200
        assert started.wait(2)
        r = c.post("/api/auto-acquire")
        assert r.status_code == 409 and "already running" in r.get_json()["error"]
    finally:
        release.set()


def test_plan_eligible_count(monkeypatch, tmp_path):
    c, _, release, _ = make_client(monkeypatch, tmp_path,
                                   [{"id": "i1", "authorized": 1}, {"id": "i2", "authorized": 1},
                                    {"id": "i3", "authorized": 1, "status": "FOUND"}])
    try:
        d = c.get("/api/plan").get_json()
        assert d["eligible"] == 2 and d["cooling"] == 0
        # picks are what the pipeline would actually attempt: the FOUND item is already
        # on disk (it needs a scan, not a download), so it is not a pick
        assert len(d["items"]) == 2 and all(i["status"] != "FOUND" for i in d["items"])
    finally:
        release.set()


def test_restart_recovery_relaunches_acquire(monkeypatch, tmp_path):
    """A server killed mid-run leaves the job journaled as 'running'; a fresh server
    must flip it to 'interrupted' and relaunch the acquire pipeline."""
    from romcom.db import connect as _connect
    c, started, release, calls = make_client(monkeypatch, tmp_path, [{"id": "i1"}])
    try:
        assert c.post("/api/auto-acquire").status_code == 200
        assert started.wait(2)
        assert calls["n"] == 1
        # "crash": abandon the first server (its blocked thread) and boot a new one
        app2 = create_app()
        app2.recover_interrupted()
        deadline_calls, deadline_started = calls["n"], calls["n"]  # wait for the relaunch
        import time as _time
        for _ in range(40):
            if calls["n"] > deadline_calls:
                break
            _time.sleep(0.05)
        assert calls["n"] == 2, "recovered server must relaunch the acquire job"
        db = _connect()
        statuses = [r["status"] for r in db.execute(
            "SELECT status FROM web_jobs WHERE kind='acquire' ORDER BY id")]
        assert "interrupted" in statuses  # the stale row was marked, not left dangling
    finally:
        release.set()  # let both blocked stub threads finish their cleanup

def test_watch_toggle_starts_persists_and_resumes(monkeypatch, tmp_path):
    """The continuous-watcher toggle: on → launches immediately with watch=True;
    the flag persists in app_settings, so a fresh server resumes the watcher."""
    import time as _time
    c, started, release, calls = make_client(monkeypatch, tmp_path, [{"id": "i1"}])
    try:
        d = c.post("/api/acquire/watch", json={"on": True}).get_json()
        assert d["on"] is True and d["launched"] is True
        assert started.wait(2)
        assert calls["last_kwargs"].get("watch") is True
        assert c.get("/api/acquire/watch").get_json()["on"] is True

        # "crash": a fresh server boots with the flag on and must resume watching
        app2 = create_app()
        n = calls["n"]
        app2.start_watcher_if_on()
        for _ in range(40):
            if calls["n"] > n:
                break
            _time.sleep(0.05)
        assert calls["n"] > n, "watcher flag must resume the pipeline on server start"

        d = c.post("/api/acquire/watch", json={"on": False}).get_json()
        assert d["on"] is False
        assert c.get("/api/acquire/watch").get_json()["on"] is False
    finally:
        release.set()


def test_mark_owned_endpoint(monkeypatch, tmp_path):
    """One sweep flags everything already on disk. It must not arm the downloader:
    an owned item is never CATALOGED/MISSING, so nothing new becomes eligible."""
    c, started, release, _ = make_client(monkeypatch, tmp_path, [
        {"id": "f", "status": "FOUND", "wanted": 0, "authorized": 0},
        {"id": "v", "status": "VERIFIED", "wanted": 0, "authorized": 0},
        {"id": "x", "status": "EXCLUDED", "wanted": 0, "authorized": 0},
        {"id": "cat", "status": "CATALOGED", "wanted": 1, "authorized": 1},
    ])
    try:
        r = c.post("/api/library/own")
        assert r.status_code == 200 and r.get_json()["updated"] == 2
        assert r.get_json()["eligible"] == 1  # only the real to-do item
        assert not started.is_set()           # nothing new to acquire, so no run started
        db = connect()
        flags = {x["id"]: (x["wanted"], x["authorized"]) for x in db.execute("SELECT * FROM items")}
        assert flags == {"f": (1, 1), "v": (1, 1), "x": (0, 0), "cat": (1, 1)}
        assert c.post("/api/library/own").get_json()["updated"] == 0  # idempotent
    finally:
        release.set()


def test_next_picks_filter_and_paging(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    db = connect()
    with db:
        db.executemany(
            "INSERT INTO items(id,title,system,authorized,wanted,status) VALUES(?,?,?,?,?,?)",
            [(f"i{n}", f"Game {n}", "nes" if n % 2 else "snes", 1, 1, "CATALOGED") for n in range(12)])
    c = create_app().test_client()

    d = c.get("/api/next").get_json()  # default page returns everything eligible
    assert d["total"] == 12 and len(d["items"]) == 12

    d = c.get("/api/next?limit=5").get_json()  # paging
    assert len(d["items"]) == 5 and d["total"] == 12
    d2 = c.get("/api/next?limit=5&offset=5").get_json()
    assert len(d2["items"]) == 5
    assert {i["id"] for i in d["items"]} & {i["id"] for i in d2["items"]} == set()

    d = c.get("/api/next?q=game+3").get_json()  # title/id filter, case-insensitive
    assert d["total"] == 1 and d["items"][0]["title"] == "Game 3"

    d = c.get("/api/next?system=nes").get_json()
    assert d["total"] == 6 and all(i["system"] == "nes" for i in d["items"])
