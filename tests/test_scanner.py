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
