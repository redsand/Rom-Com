import json
from pathlib import Path
import threading
import time
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


def _fake_fetch(name):
    """A stand-in download that really writes a file.

    The acquirer inspects what landed before accepting it, so a fake that returns a
    path without creating anything is no longer a faithful stand-in for a fetch."""
    def _fetch(pick, dest):
        out = Path(dest) / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"ROM" * 64)
        return out
    return _fetch


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


def test_skips_and_failures_report_progress(monkeypatch, tmp_path):
    """A run where nothing is queueable must not look dead: every attempt start,
    skip, and failure reaches the progress channel (paced direct searches make
    a single item take minutes — silence reads as a hang)."""
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "i1", "title": "Game One"}])
    search, _ = fake_search([])  # nothing findable
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    seen = []
    auto_acquire(progress=lambda i, t, name, stats: seen.append((name, dict(stats))),
                 poll_interval=0)
    assert seen[0][0] == "search: Game One"      # the attempt is visible as it starts
    assert seen[-1][0].startswith("skipped:")    # and the skip is reported immediately
    assert seen[-1][1]["skipped"] == 1


def test_skip_cooldown_holds_recently_skipped(monkeypatch, tmp_path):
    """A watching loop must not re-search an item that just turned up nothing:
    skips leave an 'acquire-skip' event that cools the item out of eligible()."""
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "i1"}, {"id": "i2"}])
    search, seen = fake_search([])  # nothing findable
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    r = auto_acquire(poll_interval=0)
    assert r["skipped"] == 2 and sorted(seen) == ["i1", "i2"]
    assert eligible() == []  # both sit inside the cooldown window
    assert db.execute("SELECT COUNT(*) c FROM events WHERE event='acquire-skip'").fetchone()["c"] == 2


def test_watch_mode_sweeps_again_until_stopped(monkeypatch, tmp_path):
    """Watch mode repeats cycles forever (no idle rest with a 0 interval) and
    re-queries eligibility each cycle; a stop event ends it promptly."""
    db = client_db(monkeypatch, tmp_path)
    monkeypatch.setenv("ROMCOM_ACQUIRE_INTERVAL", "0")
    from romcom.config import invalidate
    invalidate()
    seed(db, [{"id": "i1"}])
    search, seen = fake_search([{"title": "Game", "url": "nzb://1", "size": 1, "score": 90}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.sab.add_url", lambda *a, **k: {"nzo_ids": ["n1"]})
    stop = threading.Event()

    def fake_sync(dbc):
        with dbc:
            dbc.execute("UPDATE jobs SET status='DOWNLOADED',completed_at='x' WHERE status='QUEUED'")
            dbc.execute("UPDATE items SET status='DOWNLOADED' WHERE status='QUEUED'")
    monkeypatch.setattr("romcom.actions.sync", fake_sync)

    # End the watch after two full sweeps — robust to the cycle's internal sync count.
    seen_cycles = {"n": 0}
    orig_cycle = acquirer._cycle
    def counting_cycle(*a, **k):
        seen_cycles["n"] += 1
        r = orig_cycle(*a, **k)
        if seen_cycles["n"] >= 2:
            stop.set()
        return r
    monkeypatch.setattr("romcom.acquirer._cycle", counting_cycle)

    r = auto_acquire(poll_interval=0, watch=True, stop=stop)
    assert r["cycles"] == 2          # it swept again instead of returning after cycle 1
    assert seen == ["i1"]            # and did not re-search the finished item
    assert r["queued"] == 1 and r["downloaded"] == 1


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
    # no download dir here, so the reason names the missing fallback rather than the floor
    assert any("no usable" in k for k in r["skipped_by_reason"])
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
    assert r["sweep_capped"] is True and r["armed_remaining"] == 1


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


def test_watch_bounds_each_sweep_and_resweeps(monkeypatch, tmp_path):
    """A watcher must not grind through the whole library in one cycle: each sweep
    attempts at most ROMCOM_ACQUIRE_WATCH_BATCH items, and the next sweep continues
    where it left off (the finished items are inside their search cooldown)."""
    db = client_db(monkeypatch, tmp_path)
    monkeypatch.setenv("ROMCOM_ACQUIRE_WATCH_BATCH", "2")
    monkeypatch.setenv("ROMCOM_ACQUIRE_INTERVAL", "0")
    monkeypatch.setenv("ROMCOM_ACQUIRE_SWEEP_PAUSE", "0")
    from romcom.config import invalidate
    invalidate()
    seed(db, [{"id": f"i{n}"} for n in range(1, 6)])
    stop = threading.Event()

    def search(dbc, kind, ident):
        seen.append(ident)
        if len(seen) == 5:  # all five attempted — end the watch from inside the attempt
            stop.set()
        return [], dbc.execute("SELECT * FROM items WHERE id=?", (ident,)).fetchone()
    seen = []
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)

    r = auto_acquire(poll_interval=0, watch=True, stop=stop)
    assert sorted(seen) == ["i1", "i2", "i3", "i4", "i5"]  # every item, exactly once
    assert r["cycles"] == 3                        # 2 + 2 + 1: the cap bounded each sweep
    assert r["skipped"] == 5 and r["queued"] == 0
    assert r["armed_remaining"] == 0 and r["cooling"] == 5  # all five are cooling now
    assert r["batch_note"] is None                 # the final sweep was not capped


def test_expired_items_queue_behind_untried_ones(monkeypatch, tmp_path):
    """Sweeps are bounded, so the order they work in decides whether the library ever
    drains: an item whose cooldown expired must not jump back in front of titles that
    have never been tried, or the same head of the queue is searched forever."""
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "a"}, {"id": "b"}, {"id": "c"}])
    with db:
        db.execute("INSERT INTO events(item_id,event,detail,created_at) "
                   "VALUES('a','acquire-skip','old',datetime('now','-2 hours'))")
    assert [r["id"] for r in eligible()] == ["b", "c", "a"]  # expired retry goes last
    assert acquirer.cooling_count() == 0                     # nothing is inside its window


def test_missing_items_cool_longer_than_transient_failures(monkeypatch, tmp_path):
    """'Nothing found anywhere' is a property of the sources, so it holds the item back
    for hours; a transient failure (indexer/SABnzbd/network) comes back within the hour."""
    db = client_db(monkeypatch, tmp_path)
    ddir = tmp_path / "downloads"; ddir.mkdir()
    monkeypatch.setenv("ROMCOM_DOWNLOAD_DIR", str(ddir))
    from romcom.config import invalidate
    invalidate()
    seed(db, [{"id": "i1", "title": "Nowhere Game"}])
    search, _ = fake_search([{"title": "unrelated", "url": "nzb://x", "size": 1, "score": 0}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.acquirer.webdl.search", lambda *a: [])  # site has nothing either
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)

    r = auto_acquire(poll_interval=0)
    assert r["skipped"] == 1 and r["cooling"] == 1
    assert db.execute("SELECT event FROM events").fetchone()["event"] == "acquire-miss"
    with db:  # two hours on: still cooling (miss window is six hours)
        db.execute("UPDATE events SET created_at=datetime('now','-2 hours')")
    assert eligible() == [] and acquirer.cooling_count() == 1

    with db:  # the same age on a transient skip is already expired
        db.execute("UPDATE events SET event='acquire-skip' WHERE event='acquire-miss'")
    assert [x["id"] for x in eligible()] == ["i1"]


def test_cooldown_trail_is_pruned_but_history_is_kept(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": "i1"}])
    with db:
        db.execute("INSERT INTO events(item_id,event,detail,created_at) "
                   "VALUES('old','acquire-skip','stale',datetime('now','-5 days'))")
        db.execute("INSERT INTO events(item_id,event,detail,created_at) "
                   "VALUES('old2','scan-match','x',datetime('now','-5 days'))")
    search, _ = fake_search([])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    auto_acquire(poll_interval=0)
    kinds = {r["event"] for r in db.execute("SELECT event FROM events")}
    assert "scan-match" in kinds            # real history is left alone
    assert db.execute("SELECT COUNT(*) c FROM events WHERE item_id='old'").fetchone()["c"] == 0


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
    monkeypatch.setattr("romcom.acquirer.webdl.search", lambda *a: [])  # direct source empty too
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    def fail_scan(*a, **k): raise AssertionError("scan must not run")
    monkeypatch.setattr("romcom.acquirer.scan", fail_scan)
    r = auto_acquire(poll_interval=0)
    assert r["scan"] is None and r["scan_note"] == "no downloads completed this run"

def test_direct_fallback_when_indexer_has_nothing(monkeypatch, tmp_path):
    """Indexer below the score floor → romsgames fallback downloads the file
    directly, the item lands DOWNLOADED, and the scan phase still runs."""
    from pathlib import Path
    from romcom.config import invalidate
    db = client_db(monkeypatch, tmp_path)
    ddir = tmp_path / "games"; ddir.mkdir()
    monkeypatch.setenv("ROMCOM_DOWNLOAD_DIR", str(ddir))
    invalidate()
    seed(db, [{"id": "i1", "title": "Super Mario Land"}])
    search, _ = fake_search([{"title": "unrelated junk", "url": "nzb://x", "size": 1, "score": 5}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.acquirer.webdl.search",
                        lambda q, sys: [{"title": "super mario land", "console": "gameboy",
                                         "url": "https://r.example/gameboy-rom-super-mario-land/", "score": 100}])
    monkeypatch.setattr("romcom.acquirer.webdl.fetch", _fake_fetch("Super Mario Land (World).zip"))
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    monkeypatch.setattr("romcom.acquirer.scan", lambda *a, **k: {"files": 1})

    r = auto_acquire(poll_interval=0)
    assert r["queued"] == 0 and r["direct"] == 1 and r["skipped"] == 0
    assert db.execute("SELECT status FROM items WHERE id='i1'").fetchone()["status"] == "DOWNLOADED"
    assert r["scan"] == {"files": 1}  # runs even though SAB downloaded nothing


def test_direct_download_is_journaled(monkeypatch, tmp_path):
    """A direct (romsgames) download lands in the SAME jobs ledger SABnzbd uses, tagged
    with its source and no nzo_id — so the Activity feed represents it instead of the
    file appearing on disk with nothing in the feed to show for it."""
    from pathlib import Path
    from romcom.config import invalidate
    db = client_db(monkeypatch, tmp_path)
    ddir = tmp_path / "games"; ddir.mkdir()
    monkeypatch.setenv("ROMCOM_DOWNLOAD_DIR", str(ddir))
    invalidate()
    seed(db, [{"id": "i1", "title": "Super Mario Land"}])
    search, _ = fake_search([{"title": "junk", "url": "nzb://x", "size": 1, "score": 5}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.acquirer.webdl.search",
                        lambda q, sys: [{"title": "super mario land", "console": "gameboy",
                                         "url": "https://r.example/gameboy-rom-super-mario-land/", "score": 100}])
    monkeypatch.setattr("romcom.acquirer.webdl.fetch", _fake_fetch("Super Mario Land (World).zip"))
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    monkeypatch.setattr("romcom.acquirer.scan", lambda *a, **k: {"files": 1})

    r = auto_acquire(poll_interval=0)
    assert r["direct"] == 1
    row = db.execute("SELECT entity_id,status,source,nzo_id,result_title FROM jobs").fetchone()
    assert row["entity_id"] == "i1" and row["status"] == "DOWNLOADED"
    assert row["source"] == "romsgames" and row["nzo_id"] is None
    assert row["result_title"] == "super mario land"


def test_direct_duplicate_url_is_not_refetched(monkeypatch, tmp_path):
    """Two items whose best pick is the same source page — the multicart case, where
    "dragon ball z 4 in 1" was the top result for three different items. The first
    downloads it; the rest must not pay ~150 s of paced fetching for a file already
    on disk (the ledger is the record of what has been pulled)."""
    from pathlib import Path
    from romcom.config import invalidate
    db = client_db(monkeypatch, tmp_path)
    ddir = tmp_path / "games"; ddir.mkdir()
    monkeypatch.setenv("ROMCOM_DOWNLOAD_DIR", str(ddir))
    invalidate()
    seed(db, [{"id": "i1", "title": "4-in-1 (SN 406)"},
              {"id": "i2", "title": "4-in-1 (AP009)"}])
    search, _ = fake_search([{"title": "junk", "url": "nzb://x", "size": 1, "score": 5}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    same = "https://r.example/nintendo-rom-dragon-ball-z-4-in-1/"
    monkeypatch.setattr("romcom.acquirer.webdl.search",
                        lambda q, sys: [{"title": "dragon ball z 4 in 1", "console": "nintendo",
                                         "url": same, "score": 100}])
    fetches = []
    def _dup_fetch(pick, dest):
        fetches.append(pick["url"])
        return _fake_fetch("dup.zip")(pick, dest)
    monkeypatch.setattr("romcom.acquirer.webdl.fetch", _dup_fetch)
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    monkeypatch.setattr("romcom.acquirer.scan", lambda *a, **k: {"files": 1})

    r = auto_acquire(poll_interval=0)
    assert fetches == [same], "the same source page must be fetched once, not per item"
    assert r["direct"] == 1 and r["skipped"] == 1


def test_watch_survives_cycle_error(monkeypatch, tmp_path):
    """A watching run must not die when a sweep raises: it records the failure, rests, and
    sweeps again. Here the first _cycle explodes; the second ends the watch cleanly."""
    client_db(monkeypatch, tmp_path)
    monkeypatch.setenv("ROMCOM_ACQUIRE_INTERVAL", "0")
    from romcom.config import invalidate
    invalidate()
    stop = threading.Event()
    calls = {"n": 0}

    def boom(progress, poll, max_wait, batch, slots, stopev):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sweep exploded")
        stop.set()
        return {"queued": 0, "downloaded": 0, "download_failed": 0, "failed": 0, "skipped": 0,
                "skipped_by_reason": {}, "failed_items": [], "still_pending": 0, "direct": 0,
                "wait_note": None, "elapsed_min": 0, "batch_note": None, "scan": None,
                "scan_note": None, "sweep_capped": False, "armed_remaining": 0, "cooling": 0}
    monkeypatch.setattr("romcom.acquirer._cycle", boom)

    r = auto_acquire(poll_interval=0, watch=True, stop=stop)  # must return, not raise
    assert calls["n"] == 2          # it came back for a second sweep after the crash
    assert r["cycles"] == 1          # the crashed sweep is recovered, not counted as done


def test_one_shot_run_still_raises_on_cycle_error(monkeypatch, tmp_path):
    """The per-sweep recovery is only for watching runs — a one-shot call still surfaces
    the failure to its caller instead of swallowing it."""
    import pytest
    client_db(monkeypatch, tmp_path)
    monkeypatch.setattr("romcom.acquirer._cycle",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        auto_acquire(poll_interval=0)


def test_llm_salvages_a_below_floor_result(monkeypatch, tmp_path):
    """When enabled, the LLM reviews below-floor indexer candidates and can salvage a real
    match the strict ranker turned away — which then gets queued to SABnzbd normally."""
    from romcom.config import invalidate
    db = client_db(monkeypatch, tmp_path)
    monkeypatch.setenv("ROMCOM_LLM_ENABLED", "true")
    invalidate()
    seed(db, [{"id": "i1", "title": "Nancy Drew"}])
    search, _ = fake_search([{"title": "Nancy Drew Secrets v1.1 [xyz]", "url": "nzb://1", "size": 10, "score": 5}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.acquirer.llm.choose", lambda title, sys, cand: cand[0])
    monkeypatch.setattr("romcom.sab.add_url", lambda *a, **k: {"nzo_ids": ["n1"]})

    def fake_sync(dbc):
        with dbc:
            dbc.execute("UPDATE jobs SET status='DOWNLOADED',completed_at='x' WHERE status='QUEUED'")
            dbc.execute("UPDATE items SET status='DOWNLOADED' WHERE status='QUEUED'")
    monkeypatch.setattr("romcom.actions.sync", fake_sync)
    monkeypatch.setattr("romcom.acquirer.scan", lambda *a, **k: None)

    r = auto_acquire(poll_interval=0)
    assert r["queued"] == 1
    assert db.execute("SELECT result_url FROM jobs").fetchone()["result_url"] == "nzb://1"


def test_indexer_error_falls_through_to_direct(monkeypatch, tmp_path):
    """An indexer outage must not stop the direct sources from filling the library: when
    search_entity *raises* (not just returns empty), the item still goes to the direct
    worker (romsgames → Vimm)."""
    from pathlib import Path
    from romcom.config import invalidate
    db = client_db(monkeypatch, tmp_path)
    ddir = tmp_path / "games"; ddir.mkdir()
    monkeypatch.setenv("ROMCOM_DOWNLOAD_DIR", str(ddir))
    invalidate()
    seed(db, [{"id": "i1", "title": "Super Mario Land"}])

    def boom(dbc, kind, ident):
        raise RuntimeError("indexer 503")
    monkeypatch.setattr("romcom.indexer.search_entity", boom)
    grabbed = {}
    def webdl_search(q, sys):
        grabbed["q"] = q
        return [{"title": "super mario land", "url": "https://r/x/", "score": 100, "source": "romsgames"}]
    monkeypatch.setattr("romcom.acquirer.webdl.search", webdl_search)
    monkeypatch.setattr("romcom.acquirer.webdl.fetch", _fake_fetch("sml.zip"))
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    monkeypatch.setattr("romcom.acquirer.scan", lambda *a, **k: {"files": 1})

    r = auto_acquire(poll_interval=0)
    assert r["direct"] == 1 and grabbed["q"] == "Super Mario Land"
    row = db.execute("SELECT source,status FROM jobs").fetchone()
    assert row["source"] == "romsgames" and row["status"] == "DOWNLOADED"


def test_direct_fallback_skipped_when_no_download_dir(monkeypatch, tmp_path):
    db = client_db(monkeypatch, tmp_path)  # client_db unsets ROMCOM_DOWNLOAD_DIR
    seed(db, [{"id": "i1", "title": "Game One"}])
    search, _ = fake_search([])  # indexer has literally nothing
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    called = []
    monkeypatch.setattr("romcom.acquirer.webdl.search", lambda *a: called.append(a) or [])

    r = auto_acquire(poll_interval=0)
    assert r["direct"] == 0 and r["skipped"] == 1
    assert not called  # never hits the direct site without somewhere to put files


def test_vimm_used_when_enabled_and_romsgames_misses(monkeypatch, tmp_path):
    """With Vimm enabled and romsgames empty, the item is fetched from Vimm and journaled
    with source='vimm' — the second direct source, tried after the first."""
    from pathlib import Path
    from romcom.config import invalidate
    db = client_db(monkeypatch, tmp_path)
    ddir = tmp_path / "games"; ddir.mkdir()
    monkeypatch.setenv("ROMCOM_DOWNLOAD_DIR", str(ddir))
    monkeypatch.setenv("ROMCOM_VIMM_ENABLED", "true")
    invalidate()
    seed(db, [{"id": "i1", "title": "Super Mario World"}])
    search, _ = fake_search([{"title": "junk", "url": "nzb://x", "size": 1, "score": 5}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.acquirer.webdl.search", lambda q, sys: [])  # romsgames has nothing
    monkeypatch.setattr("romcom.acquirer.vimm.search",
                        lambda q, sys: [{"title": "super mario world", "score": 100,
                                         "url": "https://vimm.example/vault/1652", "source": "vimm"}])
    monkeypatch.setattr("romcom.acquirer.vimm.fetch", _fake_fetch("Super Mario World (USA).zip"))
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)
    monkeypatch.setattr("romcom.acquirer.scan", lambda *a, **k: {"files": 1})

    r = auto_acquire(poll_interval=0)
    assert r["direct"] == 1
    row = db.execute("SELECT source,status,nzo_id FROM jobs").fetchone()
    assert row["source"] == "vimm" and row["status"] == "DOWNLOADED" and row["nzo_id"] is None


def test_vimm_not_tried_when_disabled(monkeypatch, tmp_path):
    """Vimm is opt-in: with the toggle off (default), the direct fallback never touches it
    even when romsgames comes up empty."""
    from romcom.config import invalidate
    db = client_db(monkeypatch, tmp_path)
    ddir = tmp_path / "games"; ddir.mkdir()
    monkeypatch.setenv("ROMCOM_DOWNLOAD_DIR", str(ddir))
    invalidate()
    seed(db, [{"id": "i1", "title": "Game"}])
    search, _ = fake_search([])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.acquirer.webdl.search", lambda *a: [])
    called = []
    monkeypatch.setattr("romcom.acquirer.vimm.search", lambda *a: called.append(a) or [])
    monkeypatch.setattr("romcom.actions.sync", lambda db: None)

    r = auto_acquire(poll_interval=0)
    assert r["skipped"] == 1 and not called


def test_nzb_searches_run_in_parallel_up_to_worker_count(monkeypatch, tmp_path):
    """`parallel` is now the number of concurrent NZB search/queue workers: with parallel=2
    and a blocking search, at most two searches run at once — and all items still get
    queued (no artificial one-in-flight cap; SABnzbd downloads them in parallel)."""
    db = client_db(monkeypatch, tmp_path)
    seed(db, [{"id": f"i{n}"} for n in range(1, 6)])   # 5 items
    live = {"now": 0, "max": 0}
    clock = threading.Lock()

    def search(dbc, kind, ident):
        with clock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.05)                # hold the search so real overlap is observable
        with clock:
            live["now"] -= 1
        return [{"title": "Game", "url": "nzb://1", "size": 1, "score": 90}], \
            dbc.execute("SELECT * FROM items WHERE id=?", (ident,)).fetchone()
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.sab.add_url", lambda *a, **k: {"nzo_ids": ["n1"]})

    def fake_sync(dbc):
        with dbc:
            dbc.execute("UPDATE jobs SET status='DOWNLOADED',completed_at='x' WHERE status='QUEUED'")
            dbc.execute("UPDATE items SET status='DOWNLOADED' WHERE status='QUEUED'")
    monkeypatch.setattr("romcom.actions.sync", fake_sync)
    monkeypatch.setattr("romcom.acquirer.scan", lambda *a, **k: None)

    r = auto_acquire(poll_interval=0, parallel=2)
    assert live["max"] == 2      # never more than two indexer searches at once
    assert r["queued"] == 5      # but every item still gets queued


def test_rolling_fill_and_incremental_import(monkeypatch, tmp_path):
    """A completed download frees its slot for the next item, and each finished
    file is scanned into the library while the run continues — 'adopted'
    accumulates across passes instead of only counting the last one."""
    db = client_db(monkeypatch, tmp_path)
    ddir = tmp_path / "downloads"; ddir.mkdir()
    monkeypatch.setenv("ROMCOM_DOWNLOAD_DIR", str(ddir))
    from romcom.config import invalidate
    invalidate()
    seed(db, [{"id": "i1"}, {"id": "i2"}])
    search, seen = fake_search([{"title": "Game", "url": "nzb://1", "size": 1, "score": 90}])
    monkeypatch.setattr("romcom.indexer.search_entity", search)
    monkeypatch.setattr("romcom.sab.add_url", lambda *a, **k: {"nzo_ids": ["n" + str(len(seen))]})

    def fake_sync(dbc):
        with dbc:  # completes ONE queued job per tick, so imports land incrementally
            row = dbc.execute("SELECT id,entity_id FROM jobs WHERE status='QUEUED' ORDER BY id LIMIT 1").fetchone()
            if row:
                dbc.execute("UPDATE jobs SET status='DOWNLOADED',completed_at='x' WHERE id=?", (row["id"],))
                dbc.execute("UPDATE items SET status='DOWNLOADED' WHERE id=?", (row["entity_id"],))
    monkeypatch.setattr("romcom.actions.sync", fake_sync)
    calls = []
    def fake_scan(root, name_match=True, adopt=True, progress=None):
        calls.append(str(root))
        return {"files": 1, "matched": 0, "adopted": 1}
    monkeypatch.setattr("romcom.acquirer.scan", fake_scan)

    r = auto_acquire(poll_interval=0, parallel=1)
    assert sorted(seen) == ["i1", "i2"]
    assert r["queued"] == 2 and r["downloaded"] == 2 and r["still_pending"] == 0
    # i2 was only queued after i1 finished (1 slot), and each completion was
    # imported mid-run: two scan passes, both against the download dir.
    assert calls == [str(ddir), str(ddir)]
    assert r["scan"]["files"] == 1 and r["scan"]["adopted"] == 2  # accumulated, not overwritten
