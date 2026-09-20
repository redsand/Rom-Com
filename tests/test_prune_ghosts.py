"""Adopted items outlive their files; a scan must not leave the corpses behind."""
from romcom.db import connect
from romcom.scanner import prune_adopted_ghosts


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "g.db"))
    return connect()


def _item(db, iid, src="local", status="FOUND"):
    db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id)"
               " VALUES(?,?,'arcade',?,?,?)", (iid, iid, status, src, iid))


def test_an_adopted_item_with_no_file_is_removed(monkeypatch, tmp_path):
    """The exact shape that built up 65,768 times: adopt a loose file, later import the dat
    that describes it, and the next scan re-points the file at the real entry."""
    db = _db(monkeypatch, tmp_path)
    with db:
        _item(db, "local-arcade-ghost")
        _item(db, "antopisa-arcade-real", src="antopisa", status="VERIFIED")
        db.execute("INSERT INTO files(path,matched_item_id) VALUES('H:/x.bin','antopisa-arcade-real')")
    assert prune_adopted_ghosts(db) == 1
    assert [r["id"] for r in db.execute("SELECT id FROM items")] == ["antopisa-arcade-real"]


def test_an_adopted_item_that_still_owns_its_file_is_kept(monkeypatch, tmp_path):
    db = _db(monkeypatch, tmp_path)
    with db:
        _item(db, "local-arcade-live")
        db.execute("INSERT INTO files(path,matched_item_id) VALUES('H:/y.bin','local-arcade-live')")
    assert prune_adopted_ghosts(db) == 0
    assert db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 1


def test_catalog_items_are_never_touched(monkeypatch, tmp_path):
    """Only adopted rows represent a file. A catalogued item with no file is the normal
    case -- it is simply something you do not own yet, and 203,194 of them are."""
    db = _db(monkeypatch, tmp_path)
    with db:
        _item(db, "nointro-arcade-missing", src="nointro", status="CATALOGED")
    assert prune_adopted_ghosts(db) == 0
    assert db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 1


def test_excluded_adopted_items_survive(monkeypatch, tmp_path):
    """Exclusion is a deliberate decision; deleting the row would let the next scan re-adopt
    the same file and undo it."""
    db = _db(monkeypatch, tmp_path)
    with db:
        _item(db, "local-arcade-excluded", status="EXCLUDED")
    assert prune_adopted_ghosts(db) == 0
    assert db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 1
