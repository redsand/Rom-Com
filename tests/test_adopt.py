import hashlib, zipfile
from romcom.db import connect
from romcom.scanner import scan, adopt_unmatched

def test_zip_member_match(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    payload = b"zipped rom payload"
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES('z1','Zipped Game','snes')")
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('z1','crc',?)",
                   (f"{__import__('zlib').crc32(payload) & 0xffffffff:08x}",))
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('z1','sha1',?)",
                   (hashlib.sha1(payload).hexdigest(),))
    roms = tmp_path / "roms"; roms.mkdir()
    with zipfile.ZipFile(roms / "Zipped Game (USA).zip", "w") as z:
        z.writestr("Zipped Game (USA).sfc", payload)

    r = scan(roms)
    assert r["matched"] == 1 and r["verified"] == 1
    assert db.execute("SELECT status FROM items WHERE id='z1'").fetchone()["status"] == "VERIFIED"
    row = db.execute("SELECT match_method FROM files").fetchone()
    assert row["match_method"] == "hash-zip"

def test_adopt_unmatched(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    roms = tmp_path / "roms"
    (roms / "nes").mkdir(parents=True)
    (roms / "nes" / "Homebrew Game (World).bin").write_bytes(b"folder-detected")
    (roms / "misc").mkdir()
    (roms / "misc" / "Portable Thing.gba").write_bytes(b"ext-detected")
    (roms / "misc" / "notes.txt").write_bytes(b"junk")

    # Adoption happens as part of the scan itself
    r = scan(roms, name_match=False)
    assert r["adopted"] == 2 and r["adopt_skipped"] == 1
    assert r["adopted_by_system"] == {"nes": 1, "gba": 1}
    assert ".txt" in r["skipped_exts"]
    db = connect()
    item = db.execute("SELECT * FROM items WHERE system='nes'").fetchone()
    assert item["catalog_source"] == "local" and item["status"] == "FOUND"
    assert item["title"] == "Homebrew Game (World)"
    assert db.execute("SELECT COUNT(*) c FROM files WHERE match_method='adopted'").fetchone()["c"] == 2
    # Idempotent: nothing left to adopt
    assert adopt_unmatched()["adopted"] == 0
