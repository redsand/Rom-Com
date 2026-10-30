from pathlib import Path
import hashlib
from romcom.db import connect
from romcom.scanner import scan

def test_hash_scan_promotes_verified(tmp_path,monkeypatch):
    monkeypatch.setenv("ROMCOM_DB",str(tmp_path/"test.db")); db=connect()
    content=b"hello romcom"; sha1=hashlib.sha1(content).hexdigest()
    with db:
        db.execute("INSERT INTO items(id,title,status) VALUES('g','Game','CATALOGED')")
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('g','sha1',?)",(sha1,))
    root=tmp_path/"lib"; root.mkdir(); (root/"anything.bin").write_bytes(content)
    result=scan(root)
    assert result["verified"]==1
    assert connect().execute("SELECT status FROM items WHERE id='g'").fetchone()["status"]=="VERIFIED"


def test_scan_non_recursive_skips_subfolders(tmp_path, monkeypatch):
    """recursive=False scans only the top level — for when a folder holds one system's
    roms and you don't want to sweep every nested subfolder."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db")); db = connect()
    top, sub = b"top rom", b"sub rom"
    with db:
        db.execute("INSERT INTO items(id,title,status) VALUES('a','A','CATALOGED')")
        db.execute("INSERT INTO items(id,title,status) VALUES('b','B','CATALOGED')")
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('a','sha1',?)", (hashlib.sha1(top).hexdigest(),))
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('b','sha1',?)", (hashlib.sha1(sub).hexdigest(),))
    root = tmp_path / "roms"; root.mkdir()
    (root / "top.bin").write_bytes(top)
    (root / "nested").mkdir(); (root / "nested" / "sub.bin").write_bytes(sub)

    scan(root, recursive=False)
    assert connect().execute("SELECT status FROM items WHERE id='a'").fetchone()["status"] == "VERIFIED"
    assert connect().execute("SELECT status FROM items WHERE id='b'").fetchone()["status"] == "CATALOGED"  # subfolder skipped
    scan(root, recursive=True)
    assert connect().execute("SELECT status FROM items WHERE id='b'").fetchone()["status"] == "VERIFIED"  # now found


def test_scan_accepts_a_single_file(tmp_path, monkeypatch):
    """scan() on a bare file path scans just that file instead of silently
    matching nothing (rglob on a file yields no entries)."""
    import hashlib, zipfile
    from romcom.db import connect
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    db = connect()
    rom = tmp_path / "10-Yard Fight (USA, Europe).nes"
    rom.write_bytes(b"NES ROM DATA")
    z = tmp_path / "10-Yard Fight.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(rom, arcname=rom.name)
    with db:
        db.execute("INSERT INTO items(id,title,system,authorized,wanted,status) VALUES(?,?,?,?,?,?)",
                   ("i1", "10-Yard Fight (USA, Europe)", "nes", 1, 1, "CATALOGED"))
        # register the member's CRC exactly as import-dat would
        with zipfile.ZipFile(z) as zf:
            member_crc = zf.infolist()[0].CRC
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES(?,?,?)",
                   ("i1", "crc", f"{member_crc:08x}"))
    r = scan(str(z))
    assert r["matched"] == 1


def test_a_name_match_is_refused_when_the_extension_contradicts_it(monkeypatch, tmp_path):
    """A name match is a guess and the extension is evidence against it.

    "Donkey Kong (JU).nes" is not the arcade machine `dkong` however well the titles line up,
    yet 1,254 arcade items had acquired nes/smc/gba files exactly that way — enough that the
    rom handed to an emulator could be for the wrong console entirely. Hashes are proof and
    are never second-guessed; only this fallback is.
    """
    from romcom.db import connect
    from romcom.scanner import scan
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "fn.db"))
    from romcom.config import invalidate
    invalidate()
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id)"
                   " VALUES('arc','Donkey Kong','arcade','CATALOGED','antopisa','arcade/dkong')")
        db.execute("INSERT INTO aliases(item_id,alias) VALUES('arc','Donkey Kong')")
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id)"
                   " VALUES('nesitem','Track and Field','nes','CATALOGED','nointro','nes/tnf')")
        db.execute("INSERT INTO aliases(item_id,alias) VALUES('nesitem','Track and Field')")
    d = tmp_path / "roms"; d.mkdir()
    (d / "Donkey Kong.nes").write_bytes(b"x" * 32)      # .nes cannot be an arcade machine
    (d / "Track and Field.nes").write_bytes(b"y" * 32)  # .nes matching a nes item is fine
    scan(str(d), adopt=False)
    got = {Path(r["path"]).name: r["matched_item_id"]
           for r in db.execute("SELECT path, matched_item_id FROM files")}
    assert got["Donkey Kong.nes"] is None, "a .nes file must not be matched to an arcade item"
    assert got["Track and Field.nes"] == "nesitem"


def _zip_db(tmp_path, monkeypatch, owners):
    """Items whose catalogs hold the given member contents: {item_id: [bytes, ...]}."""
    import zlib
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "z.db")); db = connect()
    with db:
        for iid, blobs in owners.items():
            db.execute("INSERT INTO items(id,title,system) VALUES(?,?,'dreamcast')", (iid, iid))
            for b in blobs:
                db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES(?,'crc',?)",
                           (iid, f"{zlib.crc32(b) & 0xffffffff:08x}"))
                db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES(?,'sha1',?)",
                           (iid, hashlib.sha1(b).hexdigest()))
    return db


def test_a_track_every_disc_shares_does_not_name_the_zip(tmp_path, monkeypatch):
    """Dreamcast's standard audio track is on 176 discs: a demo disc holding it was filed
    as Sonic Adventure, the first owner of that hash. Shared tracks are no evidence."""
    import zipfile
    from romcom.scanner import _zip_member_matches
    audio, demo = b"standard audio" * 10, b"demo data" * 10
    owners = {f"disc-{n}": [audio] for n in range(12)}           # the shared track
    owners.update({f"demo-{n}": [demo] for n in range(3)})       # three demo discs share data
    db = _zip_db(tmp_path, monkeypatch, owners)
    z = tmp_path / "Dreamon Volume 10 (France).zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Track 2.bin", audio)
        f.writestr("Track 4.bin", demo)
    assert _zip_member_matches(db, z) == []                      # no single game is named


def test_members_that_agree_on_one_game_still_name_it(tmp_path, monkeypatch):
    import zipfile
    from romcom.scanner import _zip_member_matches
    t1, t3 = b"data one" * 10, b"data three" * 10
    db = _zip_db(tmp_path, monkeypatch, {"usa": [t1, t3], "eur": [t1], "jpn": [t3]})
    z = tmp_path / "Game.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Track 1.bin", t1)
        f.writestr("Track 3.bin", t3)
    assert _zip_member_matches(db, z) == ["usa"]


def test_the_zips_own_name_settles_a_game_two_catalogs_list(tmp_path, monkeypatch):
    import zipfile
    from romcom.scanner import _zip_member_matches
    iso = b"the iso" * 10
    db = _zip_db(tmp_path, monkeypatch, {"redump-cars": [iso], "nointro-cars": [iso]})
    with db:
        db.execute("UPDATE items SET title='Disney-Pixar Cars - Race-O-Rama (USA)' WHERE id='redump-cars'")
        db.execute("UPDATE items SET title='Cars - Race-O-Rama (USA) (En,Fr)' WHERE id='nointro-cars'")
    z = tmp_path / "Disney-Pixar Cars - Race-O-Rama (USA).zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Disney-Pixar Cars - Race-O-Rama (USA).iso", iso)
    assert _zip_member_matches(db, z) == ["redump-cars"]


def test_a_track_many_discs_share_does_not_verify_a_game(tmp_path, monkeypatch):
    """A loose 'Virtua Tennis 2 (Track 2).bin' was Sonic Adventure's file: the standard
    audio track is on 176 Dreamcast discs, and the first owner of the hash took it."""
    audio = b"standard audio" * 10
    db = _zip_db(tmp_path, monkeypatch, {f"disc-{n}": [audio] for n in range(12)})
    root = tmp_path / "lib"; root.mkdir()
    (root / "Virtua Tennis 2 (Track 2).bin").write_bytes(audio)
    scan(root, adopt=False)
    assert connect().execute("SELECT matched_item_id FROM files").fetchone()[0] is None


def test_a_crc_alone_does_not_beat_the_catalogs_sha1(tmp_path, monkeypatch):
    import zlib
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "c.db")); db = connect()
    data = b"some gba rom" * 10
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES('vita','Vita DLC','vita')")
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('vita','crc',?)",
                   (f"{zlib.crc32(data) & 0xffffffff:08x}",))
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('vita','sha1','00')")
    root = tmp_path / "lib"; root.mkdir(); (root / "Game.gba").write_bytes(data)
    scan(root, adopt=False)
    assert connect().execute("SELECT matched_item_id FROM files").fetchone()[0] is None


def test_an_empty_file_is_no_game(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "e.db")); db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES('dlc','Empty DLC','psp')")
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('dlc','sha1',?)",
                   (hashlib.sha1(b"").hexdigest(),))
    root = tmp_path / "lib"; root.mkdir(); (root / "stderr.txt").write_bytes(b"")
    scan(root, adopt=False)
    assert connect().execute("SELECT status FROM items WHERE id='dlc'").fetchone()[0] != "VERIFIED"


def test_a_saved_error_page_is_never_a_game(tmp_path, monkeypatch):
    """A download site's HTML error page saved as 'BC Racers.zip' matched the game by
    name and went onto a card as a 32X game."""
    from romcom.verify import check_download
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "w.db")); db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES('bc','BC Racers','32x')")
        db.execute("INSERT INTO aliases(item_id,alias) VALUES('bc','BC Racers')")
    root = tmp_path / "lib"; root.mkdir()
    page = root / "BC Racers.zip"
    page.write_bytes(b"<!DOCTYPE html><html><head><title>Not found</title></head></html>")
    scan(root, adopt=False)
    assert connect().execute("SELECT matched_item_id FROM files").fetchone()[0] is None
    ok, why = check_download(page, "32x")
    assert not ok and "web page" in why
