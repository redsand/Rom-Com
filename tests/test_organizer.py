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


def _one_rom(tmp_path, monkeypatch, system="nes", name="Game (USA).nes", payload=b"rom-bytes"):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    roms = tmp_path / "roms"; roms.mkdir(exist_ok=True)
    f = roms / name; f.write_bytes(payload)
    db = connect()
    iid = f"{system}-{name}"
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES(?,?,?)", (iid, name, system))
        db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,?,?)",
                   (str(f), len(payload), iid))
    return f


def _fake_disk(monkeypatch, free, total=100 * 1024 ** 3):
    from collections import namedtuple
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr("romcom.organizer.shutil.disk_usage",
                        lambda p: Usage(total, total - free, free))


def test_refuses_an_export_that_would_eat_the_reserve(tmp_path, monkeypatch):
    """The card that prompted this filled to the brim. A run that will not fit with the
    reserve intact must refuse before writing a byte."""
    _one_rom(tmp_path, monkeypatch)
    _fake_disk(monkeypatch, free=1024 ** 3)  # exactly the reserve: nothing is usable
    dest = tmp_path / "sd"
    r = organize(dest)
    assert "error" in r and r["copied"] == 0
    assert not dest.exists()


def test_stops_mid_run_when_the_reserve_is_reached(tmp_path, monkeypatch):
    """Free space can vanish during a run (another writer, a low estimate). The per-file
    check stops before the file that would cross the reserve."""
    _one_rom(tmp_path, monkeypatch)
    from romcom import organizer
    frees = iter([10 * 1024 ** 3, 1024 ** 3])  # preflight sees room; the copy check does not
    monkeypatch.setattr(organizer, "_free_bytes", lambda d: next(frees))
    monkeypatch.setattr(organizer, "_reserve_bytes", lambda d: 1024 ** 3)
    r = organize(tmp_path / "sd")
    assert r["stopped"] and r["copied"] == 0
    assert any("reserve" in e["error"] for e in r["errors"])


def test_stop_event_ends_the_run(tmp_path, monkeypatch):
    import threading
    _one_rom(tmp_path, monkeypatch)
    stop = threading.Event(); stop.set()
    r = organize(tmp_path / "sd", stop=stop)
    assert r["stopped"] and r["copied"] == 0


def test_a_systems_filter_without_arcade_does_not_build_arcade(tmp_path, monkeypatch):
    """`export snes` used to build every arcade set too: the set build ran before the
    systems filter was applied."""
    from romcom import mameset
    _one_rom(tmp_path, monkeypatch, system="snes", name="Game.sfc")
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system,external_id) "
                   "VALUES('a','Galaga','arcade','arcade/galaga')")
        db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES('x:/gg1',1,'a')")
    called = []
    monkeypatch.setattr(mameset, "build", lambda *a, **k: called.append(1) or {})
    organize(tmp_path / "sd", systems=["snes"])
    assert not called


def test_export_systems_counts_what_an_export_would_copy(tmp_path, monkeypatch):
    from romcom.organizer import export_systems
    _one_rom(tmp_path, monkeypatch, system="nes", name="A.nes", payload=b"12345")
    _one_rom(tmp_path, monkeypatch, system="nes", name="B.nes", payload=b"123")
    assert export_systems() == [{"system": "nes", "files": 2, "bytes": 8}]
    assert export_systems(keep_only=True) == []


def test_fill_copies_until_the_reserve_instead_of_refusing(tmp_path, monkeypatch):
    """A card given over to one platform bigger than it (arcade on this owner's card) must
    still be usable: fill waives the refusal but never the reserve."""
    _one_rom(tmp_path, monkeypatch, name="A.nes")
    _one_rom(tmp_path, monkeypatch, name="B.nes")
    from romcom import organizer
    # 15 bytes usable above the reserve: one 9-byte rom fits, the second does not
    state = {"free": 2 * 1024 ** 3 - 5}
    monkeypatch.setattr(organizer, "_reserve_bytes", lambda d: 2 * 1024 ** 3 - 20)
    monkeypatch.setattr(organizer, "_free_bytes", lambda d: state["free"])
    real_copy = organizer.shutil.copy2
    def copy(src, dst):
        state["free"] -= 9
        return real_copy(src, dst)
    monkeypatch.setattr(organizer.shutil, "copy2", copy)
    assert "error" in organize(tmp_path / "sd")            # 18 bytes needed, 15 usable: refused
    r = organize(tmp_path / "sd2", fill=True)
    assert r["copied"] == 1 and r["stopped"]


def test_an_unmounted_card_is_refused(tmp_path, monkeypatch):
    _one_rom(tmp_path, monkeypatch)
    from romcom import organizer
    monkeypatch.setattr(organizer, "_free_bytes", lambda d: None)
    r = organize(tmp_path / "sd")
    assert "mounted" in r["error"] and r["copied"] == 0
