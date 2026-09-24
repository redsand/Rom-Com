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

def test_zip_crc_collision_rejected(tmp_path, monkeypatch):
    """A lone CRC-only hit inside a many-member zip (MAME-style) must not count as a match."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    payload = b"colliding chunk"
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES('c1','Some DOS Game','dos')")
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('c1','crc',?)",
                   (f"{__import__('zlib').crc32(payload) & 0xffffffff:08x}",))
    roms = tmp_path / "roms"; roms.mkdir()
    with zipfile.ZipFile(roms / "mamegame.zip", "w") as z:
        z.writestr("chunk1.bin", payload)          # CRC collides, no md5/sha1 in catalog
        for i in range(2, 9):
            z.writestr(f"chunk{i}.bin", bytes([i]) * 64)
    r = scan(roms, name_match=False, adopt=False)
    assert r["matched"] == 0
    assert db.execute("SELECT status FROM items WHERE id='c1'").fetchone()["status"] == "CATALOGED"

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


def test_loose_mame_chip_files_are_not_adopted_as_arcade_games(monkeypatch, tmp_path):
    """A MAME game on disk is a set — one .zip/.7z, or a .chd. A flat ROM dump is loose chip
    images, and each one adopted separately becomes its own fake arcade title: `115b101`,
    `109740-001`, `1203,101-01`. Hundreds were sitting at the top of the library, sorting
    ahead of every real game."""
    from romcom.db import connect
    from romcom.scanner import adopt_unmatched
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "a.db"))
    db = connect()
    mame = tmp_path / "Mame_0_260_Roms_Fullset"
    mame.mkdir()
    with db:
        for name in ("115b101.u25", "109740-001.bin", "1203,101-01", "sf2.zip", "area51.chd"):
            (mame / name).write_bytes(b"x")
            db.execute("INSERT INTO files(path,bytes,sha1) VALUES(?,?,?)",
                       (str(mame / name), 1, "sha" + name))
    r = adopt_unmatched(root=str(mame))
    titles = sorted(x["title"] for x in db.execute("SELECT title FROM items"))
    assert titles == ["area51", "sf2"], titles
    assert r["adopted"] == 2 and r["skipped"] == 3
