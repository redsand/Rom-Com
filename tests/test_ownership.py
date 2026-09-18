"""Anything already on disk is part of the intended collection: wanted + authorized.

That keeps the collection state honest (coverage, the missing list, and the SD-card
export all agree with what is on the shelf) without ever re-downloading it — the
acquisition pipeline only searches items with nothing on disk yet.
"""
import hashlib
from romcom.db import connect
from romcom.status import own_all
from romcom.scanner import scan
from romcom.planner import bulk_plan, next_individuals

OWNED = ("FOUND", "DOWNLOADED", "VERIFIED", "NORMALIZED", "INSTALLED", "TESTED")


def db_with(rows, monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    db = connect()
    with db:
        for r in rows:
            db.execute("INSERT INTO items(id,title,status,wanted,authorized) VALUES(?,?,?,?,?)",
                       (r[0], r[0], r[1], r[2], r[3]))
    return db


def test_own_all_flags_what_we_have(monkeypatch, tmp_path):
    db = db_with([(f"o{n}", st, 0, 0) for n, st in enumerate(OWNED)]
                 + [("cat", "CATALOGED", 0, 0), ("mis", "MISSING", 0, 0),
                    ("x", "EXCLUDED", 0, 0), ("half", "VERIFIED", 0, 1)],
                 monkeypatch, tmp_path)
    with db:
        assert own_all(db) == len(OWNED) + 1  # every owned status + the authorized-only one
    flags = {r["id"]: (r["wanted"], r["authorized"]) for r in db.execute("SELECT * FROM items")}
    for n in range(len(OWNED)):
        assert flags[f"o{n}"] == (1, 1)
    assert flags["cat"] == (0, 0) and flags["mis"] == (0, 0)  # nothing on disk — still to find
    assert flags["x"] == (0, 0)                               # an explicit exclusion is respected
    assert flags["half"] == (1, 1)
    with db:  # idempotent: pressing the button twice changes nothing
        assert own_all(db) == 0


def test_statuses_stay_put(monkeypatch, tmp_path):
    """Marking ownership must never move an item along (or back along) the lifecycle."""
    db = db_with([("f", "FOUND", 0, 0), ("d", "DOWNLOADED", 0, 0)], monkeypatch, tmp_path)
    with db:
        own_all(db)
    assert {r["id"]: r["status"] for r in db.execute("SELECT * FROM items")} == \
        {"f": "FOUND", "d": "DOWNLOADED"}


def test_scanning_a_file_marks_its_item_owned(monkeypatch, tmp_path):
    content = b"owned payload"
    db = db_with([("g", "CATALOGED", 0, 0)], monkeypatch, tmp_path)
    with db:
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('g','sha1',?)",
                   (hashlib.sha1(content).hexdigest(),))
    root = tmp_path / "lib"; root.mkdir(); (root / "anything.bin").write_bytes(content)
    scan(root)
    row = db.execute("SELECT status,wanted,authorized FROM items WHERE id='g'").fetchone()
    assert (row["status"], row["wanted"], row["authorized"]) == ("VERIFIED", 1, 1)


def test_adopted_files_are_owned(monkeypatch, tmp_path):
    db = db_with([], monkeypatch, tmp_path)
    roms = tmp_path / "roms" / "nes"; roms.mkdir(parents=True)
    (roms / "Homebrew (World).bin").write_bytes(b"homebrew")
    scan(tmp_path / "roms", name_match=False)
    row = db.execute("SELECT status,wanted,authorized FROM items WHERE system='nes'").fetchone()
    assert (row["status"], row["wanted"], row["authorized"]) == ("FOUND", 1, 1)


def test_owned_items_are_never_picks_or_missing(monkeypatch, tmp_path):
    """The flags change the bookkeeping, not the pipeline: an item on disk is never a
    download candidate, and a bundle is not worth grabbing for titles we already have."""
    db = db_with([("have", "FOUND", 0, 0), ("want", "CATALOGED", 1, 1)], monkeypatch, tmp_path)
    with db:
        own_all(db)
    assert [r["id"] for r in next_individuals()] == ["want"]
    with db:
        db.execute("INSERT INTO volumes(id,title,authorized,estimated_bytes) VALUES('v','Bundle',1,1073741824)")
        for i in ("have", "want"):
            db.execute("INSERT INTO volume_covers(volume_id,item_id) VALUES('v',?)", (i,))
    assert bulk_plan()[0]["missing"] == 1  # the covered title we already have doesn't count
