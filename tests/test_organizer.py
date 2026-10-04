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
    assert (dest / "nes" / "0" / "Example Game (USA).nes").read_bytes() == payload

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


def test_export_copies_best_rated_games_first(tmp_path, monkeypatch):
    """Fill order is a ranking, not an accident of the alphabet: a game ranks by the higher
    of the crowd's score and the owner's rating x10."""
    _one_rom(tmp_path, monkeypatch, name="Zebra.nes", payload=b"12345")
    _one_rom(tmp_path, monkeypatch, name="Aardvark.nes", payload=b"123")
    db = connect()
    with db:
        db.execute("UPDATE items SET community_score=80 WHERE id='nes-Zebra.nes'")
        db.execute("UPDATE items SET rating=9 WHERE id='nes-Aardvark.nes'")
    order = []
    organize(tmp_path / "sd", progress=lambda i, n, name: order.append(name))
    assert order[0] == "Aardvark.nes" and order[1] == "Zebra.nes"  # rating 9 -> 90 beats 80


def test_fill_mode_keeps_top_games_when_space_runs_out(tmp_path, monkeypatch):
    """When a fill run runs out of room it must be the low end of the library left off the
    card, not whatever sorted first alphabetically."""
    _one_rom(tmp_path, monkeypatch, name="Low.nes")
    _one_rom(tmp_path, monkeypatch, name="High.nes")
    db = connect()
    with db:
        db.execute("UPDATE items SET community_score=10 WHERE id='nes-Low.nes'")
        db.execute("UPDATE items SET community_score=90 WHERE id='nes-High.nes'")
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
    r = organize(tmp_path / "sd", fill=True)
    assert r["copied"] == 1 and r["stopped"]
    assert (tmp_path / "sd" / "nes" / "90" / "High.nes").exists()
    assert not (tmp_path / "sd" / "nes" / "10" / "Low.nes").exists()


def test_unranked_games_sort_last(tmp_path, monkeypatch):
    """No rating and no community score is a score of 0: unknowns go to the back."""
    _one_rom(tmp_path, monkeypatch, name="Scored.nes")
    _one_rom(tmp_path, monkeypatch, name="Mystery.nes")
    db = connect()
    with db:
        db.execute("UPDATE items SET community_score=5 WHERE id='nes-Scored.nes'")
    order = []
    organize(tmp_path / "sd", progress=lambda i, n, name: order.append(name))
    assert order.index("Scored.nes") < order.index("Mystery.nes")


def _files_for(tmp_path, monkeypatch, item, system, title, copies):
    """One catalog entry owning `copies` files: (name, bytes, sha1, match_method) tuples."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    roms = tmp_path / "roms"; roms.mkdir(exist_ok=True)
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES(?,?,?)", (item, title, system))
        for name, size, sha1, method in copies:
            (roms / name).write_bytes(b"x" * size)
            db.execute("INSERT INTO files(path,bytes,matched_item_id,sha1,match_method) "
                       "VALUES(?,?,?,?,?)", (str(roms / name), size, item, sha1, method))
    return roms


def test_export_skips_same_content_copies(tmp_path, monkeypatch):
    """One game fetched twice is two files with different names and the same bytes (every
    download source names files differently); the card only needs one of them."""
    _files_for(tmp_path, monkeypatch, "c1", "nes", "Cars", [
        ("Cars (vimm).zip", 9, "aa", None), ("Cars (romsgames).zip", 9, "aa", None),
        ("Cars (archive).zip", 9, "aa", None)])
    r = organize(tmp_path / "sd")
    assert r["copied"] == 1 and r["dupes_skipped"] == 2
    assert len(list((tmp_path / "sd" / "nes" / "0").iterdir())) == 1


def test_export_prefers_the_hash_verified_copy(tmp_path, monkeypatch):
    """Two identical-content copies: the hash match is proof, the name match is a guess."""
    _files_for(tmp_path, monkeypatch, "c1", "nes", "Cars", [
        ("guess.zip", 9, "aa", "filename-exact"), ("proof.zip", 9, "aa", "hash")])
    organize(tmp_path / "sd")
    assert (tmp_path / "sd" / "nes" / "0" / "proof.zip").exists()
    assert not (tmp_path / "sd" / "nes" / "0" / "guess.zip").exists()


def test_export_keeps_distinct_content_under_one_item(tmp_path, monkeypatch):
    """A multi-file game (.cue + .bin) is not a duplicate: distinct content is kept."""
    _files_for(tmp_path, monkeypatch, "c1", "psx", "Game", [
        ("Game.cue", 9, "bb", None), ("Game.bin", 9, "cc", None)])
    r = organize(tmp_path / "sd")
    assert r["copied"] == 2 and r["dupes_skipped"] == 0


def test_export_keeps_hashless_files(tmp_path, monkeypatch):
    """A file with no strong hash cannot be proven identical to anything, so it is
    never dropped."""
    _files_for(tmp_path, monkeypatch, "c1", "nes", "Cars", [
        ("a.zip", 9, None, None), ("b.zip", 9, None, None)])
    r = organize(tmp_path / "sd")
    assert r["copied"] == 2 and r["dupes_skipped"] == 0


def test_distinct_games_collapses_variants(tmp_path, monkeypatch):
    """'Cars (USA)' and 'Cars (Germany)' are separate catalog entries but one game; with
    distinct_games the card gets the best variant only."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    _files_for(tmp_path, monkeypatch, "usa", "nes", "Cars (USA)",
               [("Cars (USA).zip", 9, "aa", "hash")])
    db = connect()
    with db:
        db.execute("UPDATE items SET community_score=90 WHERE id='usa'")
    _files_for(tmp_path, monkeypatch, "ger", "nes", "Cars (Germany)",
               [("Cars (Germany).zip", 9, "bb", None)])
    with db:
        db.execute("UPDATE items SET community_score=10 WHERE id='ger'")
    r = organize(tmp_path / "sd", distinct_games=True)
    assert r["copied"] == 1 and r["dupes_skipped"] == 1
    assert (tmp_path / "sd" / "nes" / "90" / "Cars (USA).zip").exists()
    assert not (tmp_path / "sd" / "nes" / "10" / "Cars (Germany).zip").exists()


def test_distinct_games_off_keeps_variants(tmp_path, monkeypatch):
    """The toggle is the owner's call: off, each catalog entry gets its copy."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    _files_for(tmp_path, monkeypatch, "usa", "nes", "Cars (USA)",
               [("Cars (USA).zip", 9, "aa", None)])
    _files_for(tmp_path, monkeypatch, "ger", "nes", "Cars (Germany)",
               [("Cars (Germany).zip", 9, "bb", None)])
    r = organize(tmp_path / "sd")  # distinct_games defaults off
    assert r["copied"] == 2 and r["dupes_skipped"] == 0


def test_distinct_games_prefers_the_hash_verified_variant(tmp_path, monkeypatch):
    """A better-rated variant whose file is only a name match loses to a hash-verified
    dump: the copy on the card should be the one the catalog can vouch for."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    _files_for(tmp_path, monkeypatch, "usa", "nes", "Cars (USA)",
               [("Cars (USA).zip", 9, "aa", "hash")])
    db = connect()
    with db:
        db.execute("UPDATE items SET community_score=10 WHERE id='usa'")
    _files_for(tmp_path, monkeypatch, "ger", "nes", "Cars (Germany)",
               [("Cars (Germany).zip", 9, "bb", "filename-exact")])
    with db:
        db.execute("UPDATE items SET community_score=90 WHERE id='ger'")
    r = organize(tmp_path / "sd", distinct_games=True)
    assert r["copied"] == 1
    assert (tmp_path / "sd" / "nes" / "10" / "Cars (USA).zip").exists()


def test_planner_counts_match_a_deduped_export(tmp_path, monkeypatch):
    """The picker's numbers must be what an export copies: both dedupe the same way."""
    from romcom.organizer import export_systems
    _files_for(tmp_path, monkeypatch, "c1", "nes", "Cars", [
        ("Cars (vimm).zip", 9, "aa", None), ("Cars (romsgames).zip", 9, "aa", None),
        ("Other.zip", 9, "bb", None)])
    for dg in (False, True):
        assert export_systems(distinct_games=dg) == [
            {"system": "nes", "files": 2, "bytes": 18}]
    assert organize(tmp_path / "sd", dry_run=True)["would_copy_bytes"] == 18


def test_export_files_games_into_score_folders(tmp_path, monkeypatch):
    """RetroArch cannot show a rating, so the card shows it instead: a game lands in the
    folder of the score it ranks by, and unranked games land in 0."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    _files_for(tmp_path, monkeypatch, "a", "nes", "Golden", [("Golden.zip", 9, "aa", None)])
    db = connect()
    with db:
        db.execute("UPDATE items SET community_score=87 WHERE id='a'")
    _files_for(tmp_path, monkeypatch, "b", "nes", "Silver",
               [("Silver.zip", 9, "bb", None)])
    with db:
        db.execute("UPDATE items SET rating=7 WHERE id='b'")
    _files_for(tmp_path, monkeypatch, "c", "nes", "Mystery",
               [("Mystery.zip", 9, "cc", None)])
    organize(tmp_path / "sd")
    assert sorted(p.name for p in (tmp_path / "sd" / "nes").iterdir()) == ["0", "70", "80"]
    assert (tmp_path / "sd" / "nes" / "80" / "Golden.zip").exists()   # 87 -> 80
    assert (tmp_path / "sd" / "nes" / "70" / "Silver.zip").exists()   # rating 7 -> 70
    assert (tmp_path / "sd" / "nes" / "0" / "Mystery.zip").exists()
