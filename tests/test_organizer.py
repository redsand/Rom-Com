import hashlib, zlib
from romcom.db import connect
from romcom.scanner import scan
from romcom.organizer import organize

def test_scan_then_organize(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    payload = b"rom-bytes"
    roms = tmp_path / "roms"; roms.mkdir()
    (roms / "Example Game (USA).nes").write_bytes(payload)
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES('x1','Example Game','nes')")
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('x1','sha1',?)",
                   (hashlib.sha1(payload).hexdigest(),))

    r = scan(roms)
    assert (r["files"], r["matched"], r["verified"], r["reused"]) == (1, 1, 1, 0)
    assert db.execute("SELECT status FROM items WHERE id='x1'").fetchone()["status"] == "VERIFIED"

    # Unchanged file on re-scan: hashes reused, match still found
    r2 = scan(roms)
    assert r2["reused"] == 1 and r2["matched"] == 1

    dest = tmp_path / "sd"
    o = organize(dest)
    assert o["copied"] == 1 and not o["errors"]
    assert (dest / "nes" / "Example Game (USA).nes").read_bytes() == payload

    # Re-run: already present, nothing recopied
    o2 = organize(dest)
    assert o2["copied"] == 0 and o2["skipped"] == 1

    # System filter excludes everything else
    o3 = organize(tmp_path / "sd2", systems=["snes"])
    assert o3["matched_files"] == 0
