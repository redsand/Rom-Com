from romcom.db import connect
from romcom.reconcile import reconcile, reacquire_mismatches


def test_reconcile_classifies_found_items(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    db = connect()
    with db:
        # FOUND with a catalog hash the file didn't match -> mismatch (re-acquirable)
        db.execute("INSERT INTO items(id,title,system,status) VALUES('m','M','nes','FOUND')")
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('m','sha1','abc')")
        # FOUND with no catalog hash -> unverifiable (terminal, correct)
        db.execute("INSERT INTO items(id,title,system,status) VALUES('u','U','dos','FOUND')")
        # VERIFIED is not part of the gap
        db.execute("INSERT INTO items(id,title,system,status) VALUES('v','V','nes','VERIFIED')")

    r = reconcile()
    assert r["found_total"] == 2
    assert r["mismatch_total"] == 1 and r["unverifiable_total"] == 1
    assert r["mismatch_by_system"] == [{"system": "nes", "count": 1}]
    assert r["unverifiable_by_system"] == [{"system": "dos", "count": 1}]


def test_reacquire_only_flips_mismatches(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('m','M','nes','FOUND')")
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('m','sha1','abc')")
        db.execute("INSERT INTO items(id,title,system,status) VALUES('u','U','dos','FOUND')")

    assert reacquire_mismatches() == 1
    assert connect().execute("SELECT status FROM items WHERE id='m'").fetchone()["status"] == "MISSING"
    assert connect().execute("SELECT status FROM items WHERE id='u'").fetchone()["status"] == "FOUND"  # untouched
