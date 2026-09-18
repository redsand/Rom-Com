import json
from datetime import datetime, timedelta
from romcom.db import connect
from romcom import actions, acquirer
from romcom.acquirer import eligible, _pick, auto_acquire


def seed(db, rows):
    with db:
        for r in rows:
            db.execute("INSERT INTO items(id,title,authorized,wanted,status) VALUES(?,?,?,?,?)",
                       (r["id"], r.get("title", r["id"]), r.get("authorized", 1),
                        r.get("wanted", 1), r.get("status", "CATALOGED")))


def client_db(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("ROMCOM_DOWNLOAD_DIR", raising=False)
    return connect()


def fake_search(results):
    """search_entity stub that returns fixed results and records which items were searched."""
    seen = []
    def _search(db, kind, ident):
        seen.append(ident)
        row = db.execute("SELECT * FROM items WHERE id=?", (ident,)).fetchone()
        return results, row
    return _search, seen


def test_eligible_filters_status(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)
    seed(db, [
        {"id": "cat", "status": "CATALOGED"}, {"id": "mis", "status": "MISSING"},
        {"id": "fnd", "status": "FOUND"}, {"id": "fld", "status": "FAILED"},
        {"id": "que", "status": "QUEUED"}, {"id": "noauth", "authorized": 0},
        {"id": "nowant", "wanted": 0},
    ])
    assert {r["id"] for r in eligible()} == {"cat", "mis"}


def test_pick_requires_score_and_url():
    assert _pick([]) is None
    assert _pick([{"title": "x", "size": 1}]) is None                    # no url
    assert _pick([{"title": "x", "url": "u", "score": 5}]) is None        # below floor
    top = {"title": "good", "url": "u2", "score": 80}
    assert _pick([{"title": "x", "url": "u1", "score": 5}, top]) == top


def test_sync_reaps_vanished_jobs(monkeypatch, tmp_path):
    """A download deleted from SABnzbd (absent from queue and history) is marked
    FAILED after the grace window instead of staying QUEUED forever."""
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "old"}, {"id": "new"}, {"id": "nonzo"}])
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    now = datetime.now().isoformat(timespec="seconds")
    with db:
        db.execute("INSERT INTO jobs(entity_type,entity_id,nzo_id,status,queued_at) VALUES('item','old','n_gone','QUEUED',?)", (past,))
        db.execute("INSERT INTO jobs(entity_type,entity_id,nzo_id,status,queued_at) VALUES('item','new','n_fresh','QUEUED',?)", (now,))
        db.execute("INSERT INTO jobs(entity_type,entity_id,nzo_id,status,queued_at) VALUES('item','nonzo',NULL,'QUEUED',?)", (past,))
        db.execute("UPDATE items SET status='QUEUED' WHERE id IN ('old','new','nonzo')")
    monkeypatch.setattr("romcom.sab.queue", lambda: [])
    monkeypatch.setattr("romcom.sab.history", lambda: [])
    actions.sync(db)
    assert db.execute("SELECT status FROM jobs WHERE nzo_id='n_gone'").fetchone()["status"] == "FAILED"
    assert db.execute("SELECT status FROM items WHERE id='old'").fetchone()["status"] == "FAILED"
    assert db.execute("SELECT status FROM jobs WHERE nzo_id IS NULL").fetchone()["status"] == "FAILED"
    # a job inside the grace window is left alone — it may just race the queue snapshot
    assert db.execute("SELECT status FROM jobs WHERE nzo_id='n_fresh'").fetchone()["status"] == "QUEUED"


def test_pipeline_queues_top_result(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "i1", "title": "Game One"}])
    search, seen = fake_search([{"title": "Game One", "url": "nzb://1", "size": 100, "score": 90}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.sab.add_url", lambda *a, **k: {"nzo_ids": ["n1"]})

    def fake_sync(db):
        with db:  # SABnzbd finished the download
            db.execute("UPDATE jobs SET status='DOWNLOADED',completed_at='x' WHERE nzo_id='n1'")
            db.execute("UPDATE items SET status='DOWNLOADED' WHERE id='i1'")
    monkeypatch.setattr("romcom.actions.sync", fake_sync)
    monkeypatch.setattr("romcom.acquirer.scan", lambda *a, **k: {"files": 0})

    r = auto_acquire(poll_interval=0)
    assert seen == ["i1"]
    assert r["queued"] == 1 and r["downloaded"] == 1
    assert db.execute("SELECT status FROM items WHERE id='i1'").fetchone()["status"] == "DOWNLOADED"
    assert db.execute("SELECT nzo_id FROM jobs").fetchone()["nzo_id"] == "n1"
    assert "ROMCOM_DOWNLOAD_DIR" in r["scan_note"]
    assert r["still_pending"] == 0


def test_pipeline_skips_low_score(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "i1"}])
    search, _ = fake_search([{"title": "Unrelated Junk", "url": "nzb://1", "size": 1, "score": 0}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    r = auto_acquire(poll_interval=0)
    assert r["queued"] == 0 and r["skipped"] == 1
    assert any("no usable result" in k for k in r["skipped_by_reason"])
    assert db.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0
    assert db.execute("SELECT status FROM items WHERE id='i1'").fetchone()["status"] == "CATALOGED"


def test_null_nzo_marks_failed(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "i1"}])
    search, _ = fake_search([{"title": "Game", "url": "nzb://1", "size": 1, "score": 90}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.sab.add_url", lambda *a, **k: {})
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    r = auto_acquire(poll_interval=0)
    assert r["queued"] == 0 and r["failed"] == 1
    assert "nzo" in r["failed_items"][0]["reason"]
    assert db.execute("SELECT status FROM items WHERE id='i1'").fetchone()["status"] == "FAILED"
    assert r["still_pending"] == 0  # the unresolvable row must not hang the wait phase


def test_wait_cap_terminates(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "i1"}])
    search, _ = fake_search([{"title": "Game", "url": "nzb://1", "size": 1, "score": 90}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.sab.add_url", lambda *a, **k: {"nzo_ids": ["n1"]})
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)  # download never completes
    r = auto_acquire(poll_interval=0, max_wait_minutes=0.001)
    assert r["queued"] == 1 and r["still_pending"] == 1
    assert "wait cap" in r["wait_note"]


def test_sweep_picks_up_newly_armed_items(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "i1", "title": "Game One"}])
    search, seen = fake_search([{"title": "Game", "url": "nzb://1", "size": 1, "score": 90}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.sab.add_url", lambda *a, **k: {"nzo_ids": ["n" + str(len(seen))]})
    calls = {"n": 0}

    def fake_sync(dbc):
        with dbc:
            if calls["n"] == 0:  # armed by the user while the run is already waiting
                dbc.execute("INSERT INTO items(id,title,authorized,wanted,status) VALUES('i2','Game Two',1,1,'CATALOGED')")
            dbc.execute("UPDATE jobs SET status='DOWNLOADED',completed_at='x' WHERE status='QUEUED'")
            dbc.execute("UPDATE items SET status='DOWNLOADED' WHERE status='QUEUED'")
        calls["n"] += 1
    monkeypatch.setattr("romcom.actions.sync", fake_sync)
    monkeypatch.setattr("romcom.acquirer.scan", lambda *a, **k: None)

    r = auto_acquire(poll_interval=0)
    assert sorted(seen) == ["i1", "i2"]
    assert r["queued"] == 2 and r["downloaded"] == 2 and r["still_pending"] == 0


def test_batch_cap_limits_queue(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "i1"}, {"id": "i2"}, {"id": "i3"}])
    search, seen = fake_search([{"title": "Game", "url": "nzb://1", "size": 1, "score": 90}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.sab.add_url", lambda *a, **k: {"nzo_ids": ["n1"]})
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    r = auto_acquire(poll_interval=0, batch_max=2, max_wait_minutes=0.001)
    assert r["queued"] == 2 and len(seen) == 2      # the third item was never searched
    assert "i3" not in seen
    assert db.execute("SELECT status FROM items WHERE id='i3'").fetchone()["status"] == "CATALOGED"
    assert "capped at 2 attempted" in r["batch_note"]
    assert "1 armed item(s)" in r["batch_note"]


def test_batch_cap_counts_skipped_searches(monkeypatch, tmp_path):
    """Unfindable items consume the cap too — the indexer API is the resource being protected."""
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "i1"}, {"id": "i2"}, {"id": "i3"}])
    search, seen = fake_search([])  # nothing findable
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    r = auto_acquire(poll_interval=0, batch_max=2)
    assert len(seen) == 2 and r["skipped"] == 2 and r["queued"] == 0
    assert "capped at 2 attempted" in r["batch_note"]


def test_scan_phase_runs(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)
    ddir = tmp_path / "downloads"; ddir.mkdir()
    (ddir / "game.zip").write_bytes(b"data")
    monkeypatch.setenv("ROMCOM_DOWNLOAD_DIR", str(ddir))
    seed(db, [{"id": "i1"}])
    search, _ = fake_search([{"title": "Game", "url": "nzb://1", "size": 1, "score": 90}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.sab.add_url", lambda *a, **k: {"nzo_ids": ["n1"]})

    def fake_sync(dbc):
        with dbc:
            dbc.execute("UPDATE jobs SET status='DOWNLOADED',completed_at='x' WHERE nzo_id='n1'")
            dbc.execute("UPDATE items SET status='DOWNLOADED' WHERE id='i1'")
    monkeypatch.setattr("romcom.actions.sync", fake_sync)
    calls = {}
    def fake_scan(root, name_match=True, adopt=True, progress=None):
        calls.update(root=str(root), name_match=name_match, adopt=adopt)
        return {"files": 1, "matched": 1}
    monkeypatch.setattr("romcom.acquirer.scan", fake_scan)

    r = auto_acquire(poll_interval=0)
    assert calls == {"root": str(ddir), "name_match": True, "adopt": True}
    assert r["scan"] == {"files": 1, "matched": 1}


def test_scan_skipped_when_nothing_downloaded(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)
    ddir = tmp_path / "downloads"; ddir.mkdir()
    monkeypatch.setenv("ROMCOM_DOWNLOAD_DIR", str(ddir))
    seed(db, [{"id": "i1"}])
    search, _ = fake_search([])  # nothing found, nothing downloaded
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    def fail_scan(*a, **k): raise AssertionError("scan must not run")
    monkeypatch.setattr("romcom.acquirer.scan", fail_scan)
    r = auto_acquire(poll_interval=0)
    assert r["scan"] is None and r["scan_note"] == "no downloads completed this run"