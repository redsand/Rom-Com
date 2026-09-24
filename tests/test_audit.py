"""The catalog's claims must match the evidence — without losing anything."""
from romcom import audit
from romcom.db import connect


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "a.db"))
    from romcom.config import invalidate
    invalidate()
    return connect()


def _item(db, iid, status, system="snes", playable=0):
    db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id,playable)"
               " VALUES(?,?,?,?,'nointro',?,?)", (iid, iid, system, status, f"{system}/{iid}", playable))


def test_a_claim_with_nothing_behind_it_is_demoted_not_deleted(monkeypatch, tmp_path):
    """An archive that overstates itself corrupts every count built on it — but a game we do
    not currently hold is still knowledge worth keeping. CATALOGED is exactly that."""
    db = _db(monkeypatch, tmp_path)
    with db:
        _item(db, "ghost", "VERIFIED")
    r = audit.audit(fix=True, db=db)
    row = db.execute("SELECT status FROM items WHERE id='ghost'").fetchone()
    assert row["status"] == "CATALOGED"
    assert db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 1
    assert r["items_deleted"] == 0


def test_a_hash_match_is_what_verified_means(monkeypatch, tmp_path):
    """A file with the right name proves nothing; a file with the right hash proves the
    content is the dump the catalog describes."""
    db = _db(monkeypatch, tmp_path)
    rom = tmp_path / "g.smc"; rom.write_bytes(b"x")
    with db:
        _item(db, "proven", "CATALOGED")
        db.execute("INSERT INTO files(path,bytes,sha1,matched_item_id) VALUES(?,1,'abc','proven')",
                   (str(rom),))
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('proven','sha1','ABC')")
    audit.audit(fix=True, db=db)
    assert db.execute("SELECT status FROM items WHERE id='proven'").fetchone()["status"] == "VERIFIED"


def test_a_file_without_a_hash_match_is_only_found(monkeypatch, tmp_path):
    db = _db(monkeypatch, tmp_path)
    rom = tmp_path / "g.smc"; rom.write_bytes(b"x")
    with db:
        _item(db, "unproven", "VERIFIED")
        db.execute("INSERT INTO files(path,bytes,sha1,matched_item_id) VALUES(?,1,'zzz','unproven')",
                   (str(rom),))
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('unproven','sha1','abc')")
    audit.audit(fix=True, db=db)
    assert db.execute("SELECT status FROM items WHERE id='unproven'").fetchone()["status"] == "FOUND"


def test_a_record_of_a_file_that_is_gone_is_the_lie(monkeypatch, tmp_path):
    """The file record goes; the catalog entry it pointed at does not."""
    db = _db(monkeypatch, tmp_path)
    with db:
        _item(db, "stale", "VERIFIED")
        db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,1,'stale')",
                   (str(tmp_path / "vanished.smc"),))
    r = audit.audit(fix=True, db=db)
    assert r["stale_file_records"] == 1
    assert db.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 1


def test_arcade_is_judged_by_whether_the_set_can_be_built(monkeypatch, tmp_path):
    """A MAME set is assembled on demand from chips scattered through a flat dump, so
    ownership is "can this be built" rather than "does this row own a file"."""
    db = _db(monkeypatch, tmp_path)
    with db:
        _item(db, "buildable", "CATALOGED", system="arcade", playable=1)
        _item(db, "not-buildable", "VERIFIED", system="arcade", playable=0)
    audit.audit(fix=True, db=db)
    got = {r["id"]: r["status"] for r in db.execute("SELECT id,status FROM items")}
    assert got["buildable"] == "VERIFIED"
    assert got["not-buildable"] == "CATALOGED"


def test_a_partial_arcade_set_is_found_not_nothing(monkeypatch, tmp_path):
    """Saying CATALOGED for a machine whose chips are half present throws away the fact that
    we hold some of it."""
    db = _db(monkeypatch, tmp_path)
    chip = tmp_path / "gg1.3p"; chip.write_bytes(b"x")
    with db:
        _item(db, "partial", "VERIFIED", system="arcade", playable=0)
        db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,1,'partial')", (str(chip),))
    audit.audit(fix=True, db=db)
    assert db.execute("SELECT status FROM items WHERE id='partial'").fetchone()["status"] == "FOUND"


def test_deliberate_states_are_left_alone(monkeypatch, tmp_path):
    """EXCLUDED is the owner's decision; FAILED and MANUAL record history. A status sweep has
    no business rewriting any of them."""
    db = _db(monkeypatch, tmp_path)
    with db:
        for status in ("EXCLUDED", "FAILED", "MANUAL"):
            _item(db, status.lower(), status)
    audit.audit(fix=True, db=db)
    got = {r["id"]: r["status"] for r in db.execute("SELECT id,status FROM items")}
    assert got == {"excluded": "EXCLUDED", "failed": "FAILED", "manual": "MANUAL"}


def test_a_dry_run_changes_nothing(monkeypatch, tmp_path):
    db = _db(monkeypatch, tmp_path)
    with db:
        _item(db, "ghost", "VERIFIED")
        db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,1,'ghost')",
                   (str(tmp_path / "gone.smc"),))
    r = audit.audit(fix=False, db=db)
    assert r["status_changes"] == 1 and r["applied"] is False
    assert db.execute("SELECT status FROM items WHERE id='ghost'").fetchone()["status"] == "VERIFIED"
    assert db.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 1
