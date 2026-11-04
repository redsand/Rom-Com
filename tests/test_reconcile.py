from romcom.db import connect
from romcom.reconcile import reconcile, reacquire_mismatches, wrong_downloads


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


def test_a_wrong_download_let_go_also_retires_its_page(monkeypatch, tmp_path):
    """When the audit lets a wrong download go, the job row that said DOWNLOADED goes
    FAILED with it. Left as it was, the spent-page ledger kept calling the url
    'already downloaded' forever, and the game's one real source stayed blocked —
    2026-11-04: 2,391 such rows, and the last new game had arrived 10-30."""
    from pathlib import Path
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    db = connect()
    wrong = tmp_path / "2006 FIFA World Cup.7z"
    wrong.write_bytes(b"715 MB of not a ds game")
    with db:
        db.execute("INSERT INTO items(id,title,system,status) "
                   "VALUES('f','2006 FIFA World Cup','nds','DOWNLOADED')")
        db.execute("INSERT INTO jobs(entity_type,entity_id,nzo_id,result_title,result_url,"
                   "bytes,status,source) VALUES('item','f',NULL,?,?,'715000000','DOWNLOADED',"
                   "'romsgames')", (wrong.name, "https://r/fifa/"))
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) "
                   "VALUES(?,?,?,1)", (str(wrong), 9, "f"))
    r = wrong_downloads(apply=True)
    assert r["count"] == 1 and r["applied"]
    assert db.execute("SELECT status FROM items WHERE id='f'").fetchone()["status"] == "MISSING"
    job = db.execute("SELECT status FROM jobs WHERE result_url='https://r/fifa/'").fetchone()
    assert job["status"] == "FAILED"
