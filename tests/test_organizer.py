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
    files = [x for x in order if x.endswith(".nes")]  # phase labels are not file copies
    assert files[0] == "Aardvark.nes" and files[1] == "Zebra.nes"  # rating 9 -> 90 beats 80


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
    assert (tmp_path / "sd" / "nes" / "High.nes").exists()
    assert not (tmp_path / "sd" / "nes" / "Low.nes").exists()


def test_unranked_games_sort_last(tmp_path, monkeypatch):
    """No rating and no community score is a score of 0: unknowns go to the back."""
    _one_rom(tmp_path, monkeypatch, name="Scored.nes")
    _one_rom(tmp_path, monkeypatch, name="Mystery.nes")
    db = connect()
    with db:
        db.execute("UPDATE items SET community_score=5 WHERE id='nes-Scored.nes'")
    order = []
    organize(tmp_path / "sd", progress=lambda i, n, name: order.append(name))
    files = [x for x in order if x.endswith(".nes")]  # phase labels are not file copies
    assert files.index("Scored.nes") < files.index("Mystery.nes")


def test_export_narrates_its_phases(tmp_path, monkeypatch):
    """The job status must tell the story of an export — selecting, measuring, the arcade
    build — instead of sitting on 'starting…' for the whole silent stretch before the
    first copy. (The arcade build is the longest phase and the one that used to be mute.)"""
    _one_rom(tmp_path, monkeypatch)
    db = connect()
    with db:
        db.execute("UPDATE items SET external_id='mame/galaga', system='arcade' "
                   "WHERE id='nes-Game (USA).nes'")
    datfile = tmp_path / "dat.dat"
    datfile.write_text('<?xml version="1.0"?><datafile>\n</datafile>', encoding="utf-8")  # truthy but empty
    monkeypatch.setattr("romcom.mameset.dat_path", lambda profile="mame": datfile)
    calls = []
    def fake_build(setnames, dest, db=None, dry_run=False, stop=None, room_for=None, progress=None,
                   fresh=False, zipped=False, profile="mame", dat=None):
        if progress: progress(0, len(setnames), "galaga")
        calls.append(dry_run)
        return {"sets": {}, "copied": 0, "bytes": 0, "bytes_needed": 0,
                "complete": 0, "incomplete": 0}
    monkeypatch.setattr("romcom.mameset.build", fake_build)
    seen = []
    organize(tmp_path / "sd", progress=lambda i, n, name: seen.append(name))
    assert seen[0] == "selecting items…"
    assert "measuring what's left to copy…" in seen
    assert "measuring arcade: galaga" in seen and "arcade: galaga" in seen
    assert calls == [True, False]   # the sizing pass, then the real build
    assert seen[-1] == "done"


def test_a_wipe_sizes_the_arcade_rebuild_in_full(tmp_path, monkeypatch):
    """The sizing pass runs BEFORE the wipe, against the old sets still on the card — so
    under a wipe it must ask mameset for the full build, not the delta over the sets it
    is about to delete. The delta once passed a 70 GB rebuild the card could not hold,
    and the run stopped at the free-space reserve mid-copy (ps3 got 12 files, psp 8)."""
    _one_rom(tmp_path, monkeypatch, system="arcade", name="Game (USA).nes")
    db = connect()
    with db:
        db.execute("UPDATE items SET external_id='mame/galaga' WHERE id='arcade-Game (USA).nes'")
    datfile = tmp_path / "dat.dat"
    datfile.write_text('<?xml version="1.0"?><datafile>\n</datafile>', encoding="utf-8")  # truthy but empty
    monkeypatch.setattr("romcom.mameset.dat_path", lambda profile="mame": datfile)
    calls = []
    def fake_build(setnames, dest, db=None, dry_run=False, stop=None, room_for=None, progress=None,
                   fresh=False, zipped=False, profile="mame", dat=None):
        calls.append((dry_run, fresh, zipped))
        return {"sets": {}, "copied": 0, "bytes": 0, "bytes_needed": 0,
                "complete": 0, "incomplete": 0}
    monkeypatch.setattr("romcom.mameset.build", fake_build)
    organize(tmp_path / "sd", systems=["arcade"], wipe=True)
    # size the FULL rebuild as zips, then build the zips
    assert calls == [(True, True, True), (False, False, True)]
    calls.clear()
    organize(tmp_path / "sd2", systems=["arcade"])
    assert calls == [(True, False, True), (False, False, True)]  # no wipe: the delta is the truth


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
    assert len(list((tmp_path / "sd" / "nes").iterdir())) == 1


def test_export_prefers_the_hash_verified_copy(tmp_path, monkeypatch):
    """Two identical-content copies: the hash match is proof, the name match is a guess."""
    _files_for(tmp_path, monkeypatch, "c1", "nes", "Cars", [
        ("guess.zip", 9, "aa", "filename-exact"), ("proof.zip", 9, "aa", "hash")])
    organize(tmp_path / "sd")
    assert (tmp_path / "sd" / "nes" / "proof.zip").exists()
    assert not (tmp_path / "sd" / "nes" / "guess.zip").exists()


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
    assert (tmp_path / "sd" / "nes" / "Cars (USA).zip").exists()
    assert not (tmp_path / "sd" / "nes" / "Cars (Germany).zip").exists()


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
    assert (tmp_path / "sd" / "nes" / "Cars (USA).zip").exists()


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
    organize(tmp_path / "sd", layout="ranked")
    assert sorted(p.name for p in (tmp_path / "sd" / "nes").iterdir()) == ["0", "70", "80"]
    assert (tmp_path / "sd" / "nes" / "80" / "Golden.zip").exists()   # 87 -> 80
    assert (tmp_path / "sd" / "nes" / "70" / "Silver.zip").exists()   # rating 7 -> 70
    assert (tmp_path / "sd" / "nes" / "0" / "Mystery.zip").exists()


def test_wipe_clears_the_ticked_platforms_folder(tmp_path, monkeypatch):
    """A top-up silently kept games that had fallen out of the recipe (and a layout
    change left the old copy beside the new one). Wipe makes a clean card first — but
    only the ticked platforms' own folders go."""
    _one_rom(tmp_path, monkeypatch, system="nes", name="New.nes")
    dest = tmp_path / "sd"
    (dest / "nes").mkdir(parents=True)
    (dest / "nes" / "Stale.nes").write_bytes(b"stale!")
    (dest / "snes").mkdir(parents=True)
    (dest / "snes" / "Keepme.sfc").write_bytes(b"keep")
    (dest / "notes.txt").write_bytes(b"x")
    r = organize(dest, systems=["nes"], wipe=True)
    assert r["copied"] == 1 and not r["skipped"]
    assert r["wiped"]["folders"] == ["nes"] and r["wiped"]["files"] == 1
    assert not (dest / "nes" / "Stale.nes").exists()
    assert (dest / "nes" / "New.nes").exists()
    assert (dest / "snes" / "Keepme.sfc").exists()    # unticked: untouched
    assert (dest / "notes.txt").exists()              # loose file: untouched


def test_wipe_without_a_platform_list_is_refused(tmp_path, monkeypatch):
    """"Clear the card" with no filter would mean deleting folders the request never
    named — refused rather than guessed at."""
    _one_rom(tmp_path, monkeypatch)
    dest = tmp_path / "sd"
    (dest / "nes").mkdir(parents=True)
    (dest / "nes" / "Old.nes").write_bytes(b"old")
    r = organize(dest, wipe=True)
    assert "error" in r and (dest / "nes" / "Old.nes").exists()


def test_a_wipe_dry_run_counts_without_deleting(tmp_path, monkeypatch):
    """Dry run never touches the destination, wipe included: it says what would be
    cleared, and clears nothing."""
    _one_rom(tmp_path, monkeypatch, name="New.nes")
    dest = tmp_path / "sd"
    (dest / "nes").mkdir(parents=True)
    (dest / "nes" / "Stale.nes").write_bytes(b"stale!")
    r = organize(dest, systems=["nes"], wipe=True, dry_run=True)
    assert r["wiped"]["files"] == 1 and (dest / "nes" / "Stale.nes").exists()


def test_an_export_too_big_for_even_a_cleared_card_refuses_before_wiping(tmp_path, monkeypatch):
    """The clear-first order mattered: a selection that cannot fit even with the ticked
    platforms empty used to wipe the card and THEN refuse — an empty card and nothing
    copied. The refusal must come first, with the card's contents intact."""
    _one_rom(tmp_path, monkeypatch, name="New.nes")
    dest = tmp_path / "sd"
    (dest / "nes").mkdir(parents=True)
    (dest / "nes" / "Stale.nes").write_bytes(b"stale!")
    _fake_disk(monkeypatch, free=1024 ** 3)  # exactly the reserve: no room for anything
    r = organize(dest, systems=["nes"], wipe=True)
    assert "error" in r and r["copied"] == 0
    assert "still not enough" in r["error"]
    assert (dest / "nes" / "Stale.nes").exists()   # refused before clearing anything


def test_a_wipe_that_makes_it_fit_clears_and_copies(tmp_path, monkeypatch):
    """The point of clearing first is making room: 8 bytes of headroom cannot hold a
    9-byte rom, but the ticked platform's 300 stale bytes can — the wipe is what makes
    the export fit, and then the old copy goes and the new one lands."""
    _one_rom(tmp_path, monkeypatch, name="New.nes")
    dest = tmp_path / "sd"
    (dest / "nes").mkdir(parents=True)
    (dest / "nes" / "Stale.nes").write_bytes(b"x" * 300)
    from romcom import organizer
    state = {"free": 1024 ** 3 + 8}
    monkeypatch.setattr(organizer, "_reserve_bytes", lambda d: 1024 ** 3)
    monkeypatch.setattr(organizer, "_free_bytes", lambda d: state["free"])
    real_rmtree = organizer.shutil.rmtree
    def rmtree(p, **kw):
        real_rmtree(p, **kw)
        state["free"] += 300      # clearing the folder really does free its bytes
    monkeypatch.setattr(organizer.shutil, "rmtree", rmtree)
    r = organize(dest, systems=["nes"], wipe=True)
    assert r["copied"] == 1 and r["wiped"]["files"] == 1
    assert not (dest / "nes" / "Stale.nes").exists()
    assert (dest / "nes" / "New.nes").exists()


def _romset_env(monkeypatch, tmp_path, dats, disk):
    """One romset profile environment: a dump of `disk` {filename: crc} files in the
    scan table, and a dat_path that answers from the `dats` {profile: path} map."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    from romcom.config import invalidate
    invalidate()
    from romcom import mameset
    flat = tmp_path / "flat"
    flat.mkdir(exist_ok=True)
    db = connect()
    with db:
        for name, crc in disk.items():
            (flat / name).write_bytes(b"x" * 16)
            db.execute("INSERT INTO files(path,bytes,crc32,sha1) VALUES(?,?,?,?)",
                       (str(flat / name), 16, crc, chr(ord('a') + len(name)) * 40))
    monkeypatch.setattr(mameset, "dat_path", lambda profile="mame": dats.get(profile))
    monkeypatch.setattr(mameset, "dat_paths",
                        lambda profile="mame": [p for p in [dats.get(profile)] if p])
    return db


CORE_DAT = """<?xml version="1.0"?><datafile>
<game name="mslug" romof="neogeo">
<rom name="m1.bin" size="16" crc="aaaa1111"/>
</game>
<game name="kof97" romof="ghost">
<rom name="k.bin" size="16" crc="dddd4444"/>
</game>
</datafile>"""

MAME_DAT = """<?xml version="1.0"?><datafile>
<machine name="neogeo" isbios="yes">
<rom name="bios.bin" size="16" crc="aaaa1111"/>
</machine>
</datafile>"""


def test_export_builds_the_arcade_bios_parents_of_its_selection(tmp_path, monkeypatch):
    """The export's arcade phase builds what the recipe ticked — but mslug on a card
    without neogeo.zip beside it refuses to boot. The romof parents of the selection
    come along whether or not they were ticked, and a parent the dump cannot build is
    reported, not guessed at."""
    from romcom.organizer import organize
    # one dat, as the real mame one: the games with their romof parents, parents defined
    export_dat = tmp_path / "mame.dat"
    export_dat.write_text(CORE_DAT.replace("</datafile>",
                        '<machine name="neogeo" isbios="yes">\n'
                        '<rom name="bios.bin" size="16" crc="aaaa1111"/>\n'
                        '</machine>\n</datafile>'), encoding="utf-8")
    db = _romset_env(monkeypatch, tmp_path, {"mame": export_dat},
                     {"m1.bin": "aaaa1111", "k.bin": "dddd4444"})
    with db:      # the recipe ticked the games; neogeo was never ticked
        for iid, setname, chip in (("m", "mslug", "m1.bin"), ("k", "kof97", "k.bin")):
            db.execute("INSERT INTO items(id,title,system,external_id) "
                       f"VALUES('{iid}','{setname}','arcade','arcade/{setname}')")
            db.execute(f"UPDATE files SET matched_item_id='{iid}' WHERE path LIKE '%{chip}'")
    # 'all': kof97 cannot boot without its parent, and the default health gate would hold
    # it back (see test_arcadehealth) — this is about the parents, so build everything.
    r = organize(tmp_path / "sd", arcade_status="all")
    card = tmp_path / "sd" / "arcade"
    assert (card / "mslug.zip").exists() and (card / "kof97.zip").exists()
    assert (card / "neogeo.zip").exists()               # the parent, not ticked, still there
    assert r["arcade_bios_unavailable"] == {"ghost": 1}  # kof97's parent: not in the dump


def test_romset_builds_every_assemblable_set_and_the_bios_parents(tmp_path, monkeypatch):
    """A core's games romof-reference bios parents (neogeo, pgm…) its own dat never
    defines — the core looks for them as their own zips, so the build must supply them
    from the MAME dat before any game lands on the card. A parent the dump cannot build
    is reported, not guessed at."""
    from romcom.organizer import romset
    core = tmp_path / "core.dat"; core.write_text(CORE_DAT, encoding="utf-8")
    mame = tmp_path / "mame.dat"; mame.write_text(MAME_DAT, encoding="utf-8")
    _romset_env(monkeypatch, tmp_path, {"fbneo": core, "mame": mame},
                {"m1.bin": "aaaa1111", "k.bin": "dddd4444"})
    dest = tmp_path / "card"
    # 'all': kof97's parent is not in the dump, so the default health gate would hold it
    # back — this is about the parents, so build everything assemblable.
    r = romset("fbneo", dest, arcade_status="all")
    assert r["profile"] == "fbneo" and r["assemblable"] == 2
    assert (dest / "neogeo.zip").exists()                    # the bios parent, first
    assert (dest / "mslug.zip").exists() and (dest / "kof97.zip").exists()
    assert r["bios_built"] == ["neogeo"]
    assert r["bios_unavailable"] == {"ghost": 1}             # kof97's parent: not in the dump


def test_romset_builds_bios_parents_from_a_fallback_dat(tmp_path, monkeypatch):
    """The mame dat defines what it defines — a core's games may need bios sets of another
    romset era (MAME 2003-Plus's cvs, taitofx1…) that neither dat of its own defines. Extra
    dats in the profile's folder are the fallback: a parent the mame dat cannot build gets
    built from the era-matched one instead of being reported unavailable."""
    from romcom import mameset
    from romcom.organizer import romset
    core = tmp_path / "core.dat"; core.write_text(CORE_DAT, encoding="utf-8")
    mame = tmp_path / "mame.dat"; mame.write_text(MAME_DAT, encoding="utf-8")
    era = tmp_path / "MAME 0.78.dat"
    era.write_text('<?xml version="1.0"?><datafile>\n'
                   '<machine name="ghost">\n'
                   '<rom name="g.bin" size="16" crc="dddd4444"/>\n'
                   '</machine>\n</datafile>', encoding="utf-8")
    _romset_env(monkeypatch, tmp_path, {"fbneo": core, "mame": mame},
                {"m1.bin": "aaaa1111", "k.bin": "dddd4444"})
    monkeypatch.setattr(mameset, "dat_paths",
                        lambda profile="mame": [core, era])   # the profile's own dat, then its fallback
    r = romset("fbneo", tmp_path / "card")
    assert sorted(r["bios_built"]) == ["ghost", "neogeo"]      # mame dat, then 0.78 dat
    assert (tmp_path / "card" / "neogeo.zip").exists()
    assert (tmp_path / "card" / "ghost.zip").exists()
    assert r["bios_unavailable"] == {}                          # every gap was covered


def test_romset_fills_best_scored_games_first(tmp_path, monkeypatch):
    """A fill stops at the free-space reserve, and an alphabetical fill stopped halfway
    leaves a card without every famous M-Z game on it — the fill must run best-game-first
    so it is the obscure zero-score tail that drops."""
    from romcom.organizer import romset
    core = tmp_path / "core.dat"
    core.write_text(CORE_DAT.replace(' romof="neogeo"', '').replace(' romof="ghost"', ''),
                   encoding="utf-8")
    db = _romset_env(monkeypatch, tmp_path, {"fbneo": core},
                    {"m1.bin": "aaaa1111", "k.bin": "dddd4444"})
    with db:      # kof97 is the alphabetically-first set; mslug is the one anyone wants
        db.execute("INSERT INTO items(id,title,system,external_id,community_score) "
                   "VALUES('m','Metal Slug','arcade','arcade/mslug',90)")
    seen = []
    romset("fbneo", tmp_path / "card",
           progress=lambda i, total, name: seen.append(name))
    assert seen.index("mslug") < seen.index("kof97")


def test_romset_stops_at_the_leave_bytes_floor(tmp_path, monkeypatch):
    """`leave_bytes` is how three cores share one 64 GB card: the biggest runs first
    holding room back for the rest, and stops cleanly at the floor instead of eating it."""
    from romcom.organizer import romset
    core = tmp_path / "core.dat"
    core.write_text(CORE_DAT.replace(' romof="neogeo"', '').replace(' romof="ghost"', ''),
                   encoding="utf-8")
    _romset_env(monkeypatch, tmp_path, {"fbneo": core},
                {"m1.bin": "aaaa1111", "k.bin": "dddd4444"})
    r = romset("fbneo", tmp_path / "card", leave_bytes=1024 ** 5)   # a floor no disk clears
    assert r["stopped"] and r["out_of_room"]
    assert not (tmp_path / "card").exists() or not any((tmp_path / "card").iterdir())


def test_romset_without_a_dat_names_the_folder(tmp_path, monkeypatch):
    """A profile whose dat is not installed must say where it expected it, not just fail."""
    from romcom.organizer import romset
    _romset_env(monkeypatch, tmp_path, {"mame2003": None}, {"m1.bin": "aaaa1111"})
    r = romset("mame2003", tmp_path / "card")
    assert "DAT/MAME2003-Plus" in r["error"] and r["copied"] == 0


def test_distinct_games_reaches_the_arcade_build(tmp_path, monkeypatch):
    """'One copy per game' once stopped at arcade: the set list was drawn from the raw
    selection, so every region variant of a game built although the plan promised one."""
    dat = tmp_path / "mame.dat"
    dat.write_text('<?xml version="1.0"?><datafile>\n'
                   '<game name="mslug">\n<rom name="m1.bin" size="16" crc="aaaa1111"/>\n</game>\n'
                   '<game name="mslugu" cloneof="mslug" romof="mslug">\n<rom name="m1.bin" size="16" crc="aaaa1111"/>\n</game>\n'
                   '</datafile>', encoding="utf-8")
    db = _romset_env(monkeypatch, tmp_path, {"mame": dat},
                     {"m1.bin": "aaaa1111", "m1 (2).bin": "aaaa1111"})
    with db:        # two region variants of one title, each with its own dump of the chip
        for iid, setname, title, chip in (("a", "mslug", "Metal Slug (USA)", "m1.bin"),
                                          ("b", "mslugu", "Metal Slug (Germany)", "m1 (2).bin")):
            db.execute("INSERT INTO items(id,title,system,external_id) "
                       f"VALUES('{iid}','{title}','arcade','arcade/{setname}')")
            db.execute("UPDATE files SET matched_item_id=? WHERE path LIKE ?", (iid, f"%{chip}"))
    card = tmp_path / "sd" / "arcade"
    r = organize(tmp_path / "sd", distinct_games=True)
    assert (card / "mslug.zip").exists() and not (card / "mslugu.zip").exists()
    assert r["dupes_skipped"] == 1
    organize(tmp_path / "sd2")                     # off: both variants build
    assert (tmp_path / "sd2" / "arcade" / "mslugu.zip").exists()


def test_export_gets_the_arcade_core_it_is_asked_for(tmp_path, monkeypatch):
    """The export builds the chosen core's romset into the ES-DE system that launches it
    with that core: mame -> arcade/, fbneo -> fbneo/, mame2003 -> mame/."""
    two = {"mame": '<?xml version="1.0"?><datafile>\n'
                   '<game name="mslug" romof="neogeo">\n<rom name="m1.bin" size="16" crc="aaaa1111"/>\n</game>\n'
                   '<machine name="neogeo" isbios="yes">\n<rom name="bios.bin" size="16" crc="aaaa1111"/>\n</machine>\n'
                   '</datafile>',
           "fbneo": '<?xml version="1.0"?><datafile>\n'
                    '<game name="mslug">\n<rom name="m1.bin" size="16" crc="aaaa1111"/>\n</game>\n'
                    '<game name="kof97">\n<rom name="k.bin" size="16" crc="dddd4444"/>\n</game>\n'
                    '</datafile>'}
    dats = {k: (tmp_path / f"{k}.dat") for k in two}
    for k, text in two.items():
        dats[k].write_text(text, encoding="utf-8")
    db = _romset_env(monkeypatch, tmp_path, dats, {"m1.bin": "aaaa1111", "k.bin": "dddd4444"})
    with db:
        for iid, setname, chip in (("m", "mslug", "m1.bin"), ("k", "kof97", "k.bin")):
            db.execute("INSERT INTO items(id,title,system,external_id) "
                       f"VALUES('{iid}','{setname}','arcade','arcade/{setname}')")
            db.execute("UPDATE files SET matched_item_id=? WHERE path LIKE ?", (iid, f"%{chip}"))
    r = organize(tmp_path / "sd", systems=["arcade"], arcade_core="fbneo")
    card = tmp_path / "sd" / "fbneo"
    assert (card / "mslug.zip").exists() and (card / "kof97.zip").exists()
    assert r["arcade"]["complete"] == 2        # one core, its dat alone decides what builds
    alla = organize(tmp_path / "sd3", systems=["arcade"], arcade_core="all")
    assert (tmp_path / "sd3" / "arcade" / "mslug.zip").exists()
    assert (tmp_path / "sd3" / "arcade" / "neogeo.zip").exists()   # mame's bios parent
    assert (tmp_path / "sd3" / "fbneo" / "kof97.zip").exists()   # fbneo's own set
    assert set(alla["arcade"].keys()) == {"mame", "fbneo", "mame2003"}
    # mame builds mslug plus the neogeo parent before it; fbneo builds both of its sets.
    assert alla["arcade"]["mame"]["complete"] == 2 and alla["arcade"]["fbneo"]["complete"] == 2
    assert alla["arcade"]["mame2003"].get("error")            # no dat for that core installed


def test_rating_min_filters_the_export_on_its_own(tmp_path, monkeypatch):
    """A rating gate set alone has to do its own filtering. The gate used to be a clause of
    the keep switch, so 'rated >= 7' with no 'curated only' tick copied everything."""
    _one_rom(tmp_path, monkeypatch, system="nes", name="Great (USA).nes")
    _one_rom(tmp_path, monkeypatch, system="nes", name="Mediocre (Europe).nes")
    _one_rom(tmp_path, monkeypatch, system="nes", name="Crowd Favourite (Japan).nes")
    db = connect()
    with db:
        db.execute("UPDATE items SET rating=8 WHERE title='Great (USA).nes'")
        db.execute("UPDATE items SET rating=5 WHERE title='Mediocre (Europe).nes'")
        # Nobody rates it personally, but the crowd's 85 speaks the same scale as a 8.5.
        db.execute("UPDATE items SET community_score=85 WHERE title='Crowd Favourite (Japan).nes'")
    dest = tmp_path / "sd"
    r = organize(dest, rating_min=7)
    copied = list((dest / "nes").rglob("*.nes"))
    assert r["copied"] == 2
    assert {p.name for p in copied} == {"Great (USA).nes", "Crowd Favourite (Japan).nes"}

    # Both gates: keep is the wide one, rating widens it further, never narrows it.
    with db:
        db.execute("UPDATE items SET keep=1 WHERE title='Mediocre (Europe).nes'")
    r2 = organize(tmp_path / "sd2", keep_only=True, rating_min=7)
    assert r2["copied"] == 3 and r2["dupes_skipped"] == 0


def test_the_export_carries_console_bios_and_reports_the_gaps(tmp_path, monkeypatch):
    """A card full of Dreamcast/Saturn games plays nothing without their bios, and the
    library's query for matched items can never find them — they sit in `files` unmatched.
    The export copies the ones it finds into <dest>/system and names what is missing."""
    _one_rom(tmp_path, monkeypatch, system="dreamcast", name="Sonic Adventure (USA).dc")
    _one_rom(tmp_path, monkeypatch, system="saturn", name="Panzer Dragoon (USA).bin")
    dump = tmp_path / "dump"; dump.mkdir()
    boot = dump / "DC_BOOT.BIN"; boot.write_bytes(b"boot")   # dumps come in any case
    flash = dump / "dc_flash.bin"; flash.write_bytes(b"flash")
    db = connect()
    with db:
        for f in (boot, flash):
            db.execute("INSERT INTO files(path,bytes) VALUES(?,?)", (str(f), f.stat().st_size))
    dest = tmp_path / "sd"
    r = organize(dest)
    assert (dest / "system" / "dc_boot.bin").read_bytes() == b"boot"
    cb = r["console_bios"]
    assert cb["dir"] == str(dest / "system")
    assert cb["systems"]["dreamcast"]["copied"] == ["dc_boot.bin", "dc_flash.bin"]
    assert cb["systems"]["dreamcast"]["missing"] == []
    # Saturn has no bios anywhere in the dump: the report must say so, not stay silent.
    assert cb["systems"]["saturn"]["missing"] == ["sega_101.bin or sega_102.bin"]
    # One region's Sega CD bios is enough: the slot lists the three and asks for one.
    with db:
        (dump / "bios_CD_U.bin").write_bytes(b"segacd")
        db.execute("INSERT INTO files(path,bytes) VALUES(?,7)", (str(dump / "bios_CD_U.bin"),))
    r2 = organize(tmp_path / "sd2", dry_run=True, systems=["segacd"])
    cb2 = r2["console_bios"]
    assert set(cb2["systems"]) == {"segacd"}                  # dry run reports the same shape
    assert cb2["systems"]["segacd"]["copied"] == ["bios_CD_U.bin"]
    # Unticked consoles' bios are not this run's business.
    r3 = organize(tmp_path / "sd3", systems=["dreamcast"])
    assert set(r3["console_bios"]["systems"]) == {"dreamcast"}


def test_distinct_games_prunes_the_dats_clone_families(tmp_path, monkeypatch):
    """'One copy per game' goes by the dat's own clone book, not just catalog titles: two
    sets of one MAME family must not both reach the card, however much the titles differ
    (doa2 survived beside doa2m by title, with both sets' distinct chips). The family
    keeps its best-scored member."""
    DAT = ('<?xml version="1.0"?><datafile>\n'
           '<machine name="doa2">\n<rom name="a.bin" size="16" crc="aaaa1111"/>\n</machine>\n'
           '<machine name="doa2m" cloneof="doa2">\n<rom name="b.bin" size="16" crc="bbbb2222"/>\n</machine>\n'
           '</datafile>')
    dats = {"mame": tmp_path / "mame.dat"}
    dats["mame"].write_text(DAT, encoding="utf-8")
    db = _romset_env(monkeypatch, tmp_path, dats, {"a.bin": "aaaa1111", "b.bin": "bbbb2222"})
    with db:    # different titles AND different chips: the title dedupe cannot see this one
        db.execute("INSERT INTO items(id,title,system,external_id,community_score) "
                   "VALUES('a','Dead or Alive 2 (USA)','arcade','arcade/doa2',80)")
        db.execute("INSERT INTO items(id,title,system,external_id,community_score) "
                   "VALUES('b','Dead or Alive 2 Millennium (Japan)','arcade','arcade/doa2m',20)")
        for iid, chip in (("a", "a.bin"), ("b", "b.bin")):
            db.execute("UPDATE files SET matched_item_id=? WHERE path LIKE ?", (iid, f"%{chip}"))
    r = organize(tmp_path / "sd", distinct_games=True)
    arcade = tmp_path / "sd" / "arcade"
    assert (arcade / "doa2.zip").exists() and not (arcade / "doa2m.zip").exists()
    assert r["clones_pruned"] == {"mame": {"doa2m": "doa2"}}
    assert r["dupes_skipped"] == 1

    organize(tmp_path / "sd2")                       # off: the clone is asked for, it builds
    assert (tmp_path / "sd2" / "arcade" / "doa2m.zip").exists()


def test_clone_pruning_survives_chains_and_scores_the_family(tmp_path, monkeypatch):
    """A clone of a clone resolves to the family root, and the best-scored member wins even
    when that member is the root's own clone."""
    DAT = ('<?xml version="1.0"?><datafile>\n'
           '<machine name="sf2">\n<rom name="a.bin" size="16" crc="aaaa1111"/>\n</machine>\n'
           '<machine name="sf2ce" cloneof="sf2">\n<rom name="b.bin" size="16" crc="bbbb2222"/>\n</machine>\n'
           '<machine name="sf2ced" cloneof="sf2ce">\n<rom name="c.bin" size="16" crc="cccc3333"/>\n</machine>\n'
           '</datafile>')
    dats = {"mame": tmp_path / "mame.dat"}
    dats["mame"].write_text(DAT, encoding="utf-8")
    db = _romset_env(monkeypatch, tmp_path, dats,
                     {"a.bin": "aaaa1111", "b.bin": "bbbb2222", "c.bin": "cccc3333"})
    with db:
        db.execute("INSERT INTO items(id,title,system,external_id,community_score) "
                   "VALUES('a','Street Fighter II (USA)','arcade','arcade/sf2',50)")
        db.execute("INSERT INTO items(id,title,system,external_id,community_score) "
                   "VALUES('b','Street Fighter II Champion Edition (US)','arcade','arcade/sf2ce',60)")
        db.execute("INSERT INTO items(id,title,system,external_id,community_score) "
                   "VALUES('c','Street Fighter II CE (Japan)','arcade','arcade/sf2ced',50)")
        for iid, chip in (("a", "a.bin"), ("b", "b.bin"), ("c", "c.bin")):
            db.execute("UPDATE files SET matched_item_id=? WHERE path LIKE ?", (iid, f"%{chip}"))
    r = organize(tmp_path / "sd", distinct_games=True)
    arcade = tmp_path / "sd" / "arcade"
    # one family, one survivor: the Champion Edition, which the crowd rates highest
    assert (arcade / "sf2ce.zip").exists()
    assert not (arcade / "sf2.zip").exists() and not (arcade / "sf2ced.zip").exists()
    assert r["clones_pruned"] == {"mame": {"sf2": "sf2ce", "sf2ced": "sf2ce"}}


def test_a_set_whose_chips_belong_to_siblings_still_reaches_the_card(tmp_path, monkeypatch):
    """A chip shared by many sets records one owner, so mspacman — every chip owned by a
    sibling — had no file row and never reached a card, complete or not. A playable set
    with no file of its own is selected by its item."""
    dat = tmp_path / "mame.dat"
    dat.write_text('<?xml version="1.0"?><datafile>\n'
                   '<game name="pacman">\n<rom name="p.bin" size="16" crc="aaaa1111"/>\n</game>\n'
                   '<game name="mspacman">\n<rom name="p.bin" size="16" crc="aaaa1111"/>\n</game>\n'
                   '</datafile>', encoding="utf-8")
    db = _romset_env(monkeypatch, tmp_path, {"mame": dat}, {"p.bin": "aaaa1111"})
    with db:
        db.execute("INSERT INTO items(id,title,system,external_id,status) "
                   "VALUES('p','Pac-Man','arcade','arcade/pacman','VERIFIED')")
        db.execute("INSERT INTO items(id,title,system,external_id,status,playable) "
                   "VALUES('ms','Ms. Pac-Man','arcade','arcade/mspacman','VERIFIED',1)")
        db.execute("UPDATE files SET matched_item_id='p'")
    organize(tmp_path / "sd", arcade_status="all")
    assert (tmp_path / "sd" / "arcade" / "mspacman.zip").exists()


def test_a_bootleg_never_takes_its_familys_slot_from_the_real_game(tmp_path, monkeypatch):
    from romcom import labels
    dat = tmp_path / "mame.dat"
    dat.write_text('<?xml version="1.0"?><datafile>\n'
                   '<game name="mslug5">\n<rom name="a.bin" size="16" crc="aaaa1111"/>\n</game>\n'
                   '<game name="ms5plus" cloneof="mslug5" romof="mslug5">\n'
                   '<rom name="b.bin" size="16" crc="bbbb2222"/>\n</game>\n'
                   '</datafile>', encoding="utf-8")
    db = _romset_env(monkeypatch, tmp_path, {"mame": dat}, {"a.bin": "aaaa1111", "b.bin": "bbbb2222"})
    monkeypatch.setattr(labels, "catver_path", lambda: None)
    with db:
        for iid, s, title, chip, score in (
                ("a", "mslug5", "Metal Slug 5", "a.bin", 60),
                ("b", "ms5plus", "Metal Slug 5 Plus (bootleg)", "b.bin", 90)):
            db.execute("INSERT INTO items(id,title,system,external_id,community_score) "
                       "VALUES(?,?,'arcade',?,?)", (iid, title, f"arcade/{s}", score))
            db.execute("UPDATE files SET matched_item_id=? WHERE path LIKE ?", (iid, f"%{chip}"))
    labels.refresh(db)
    organize(tmp_path / "sd", distinct_games=True, arcade_status="all")
    built = sorted(p.stem for p in (tmp_path / "sd" / "arcade").glob("*.zip"))
    assert built == ["mslug5"]


FAMILY_DAT = """<?xml version="1.0"?><datafile>
<machine name="sf2">
<rom name="a.bin" size="16" crc="aaaa1111"/>
</machine>
<machine name="sf2j" cloneof="sf2" romof="sf2">
<rom name="b.bin" size="16" crc="bbbb2222"/>
</machine>
<machine name="sf2ua" cloneof="sf2" romof="sf2">
<rom name="c.bin" size="16" crc="cccc3333"/>
</machine>
<machine name="gtclub">
<rom name="d.bin" size="16" crc="dddd4444"/>
</machine>
</datafile>"""


def _family_env(monkeypatch, tmp_path, dats):
    db = _romset_env(monkeypatch, tmp_path, dats,
                     {"a.bin": "aaaa1111", "b.bin": "bbbb2222", "c.bin": "cccc3333",
                      "d.bin": "dddd4444"})
    with db:
        for s, title in (("sf2", "Street Fighter II: The World Warrior (Japan 911210)"),
                         ("sf2j", "Street Fighter II: The World Warrior (Japan 910522)"),
                         ("sf2ua", "Street Fighter II: The World Warrior (USA 910206)"),
                         ("gtclub", "GTI Club")):
            db.execute("INSERT INTO items(id,title,system,external_id) "
                       f"VALUES('{s}','{title}','arcade','arcade/{s}')")
    return db


def test_romset_keeps_the_best_version_of_each_game(tmp_path, monkeypatch):
    """Twenty Street Fighter IIs are a list to scroll past, not a choice: a romset keeps
    one version per game — an English release over the Japanese parent — and a re-run
    removes the versions an every-version build left in the folder."""
    from romcom.organizer import romset
    dat = tmp_path / "mame.dat"; dat.write_text(FAMILY_DAT, encoding="utf-8")
    _family_env(monkeypatch, tmp_path, {"mame": dat})
    card = tmp_path / "card" / "arcade"
    romset("mame", card, arcade_status="all", versions=0, artwork=False)
    assert {p.stem for p in card.glob("*.zip")} == {"sf2", "sf2j", "sf2ua", "gtclub"}
    (card / "mine.zip").write_bytes(b"not a set")             # the owner's own file
    r = romset("mame", card, arcade_status="all", artwork=False)
    assert {p.stem for p in card.glob("*.zip")} == {"sf2ua", "gtclub", "mine"}
    assert r["clones_dropped"] == 2 and r["pruned_files"] == 2
    romset("mame", card, arcade_status="all", versions=2, artwork=False, prune=False)
    assert len([p for p in card.glob("sf2*.zip")]) == 2      # the runner-up joins sf2ua


def test_romset_leaves_out_games_another_core_carries(tmp_path, monkeypatch):
    """MAME beside FBNeo on one card: a game FBNeo's build carries stays out of the MAME
    folder, so each game is on the card once and MAME holds what FBNeo cannot run."""
    from romcom.organizer import romset
    dat = tmp_path / "mame.dat"; dat.write_text(FAMILY_DAT, encoding="utf-8")
    fb = tmp_path / "fbneo.dat"
    fb.write_text(FAMILY_DAT.split('<machine name="gtclub">')[0] + "</datafile>",
                  encoding="utf-8")
    _family_env(monkeypatch, tmp_path, {"mame": dat, "fbneo": fb})
    card = tmp_path / "card" / "arcade"
    r = romset("mame", card, arcade_status="all", artwork=False, defer_to=["fbneo"])
    assert [p.stem for p in card.glob("*.zip")] == ["gtclub"]
    assert r["deferred"] == 1


def test_romset_writes_the_esde_gamelist(tmp_path, monkeypatch):
    """A bare mslug.zip is a blank tile: the build names its sets for ES-DE."""
    from romcom.organizer import romset
    dat = tmp_path / "mame.dat"; dat.write_text(FAMILY_DAT, encoding="utf-8")
    _family_env(monkeypatch, tmp_path, {"mame": dat})
    card = tmp_path / "card" / "arcade"
    r = romset("mame", card, arcade_status="all")
    gl = (tmp_path / "card" / "ES-DE" / "gamelists" / "arcade" / "gamelist.xml").read_text(
        encoding="utf-8")
    assert "./gtclub.zip" in gl and "GTI Club" in gl
    assert r["media"]["gamelist"] == 2


def test_arcade_card_puts_fbneo_first_and_mame_only_what_fbneo_lacks(tmp_path, monkeypatch):
    """The one-click arcade card: fbneo\ and arcade\ on the card root, each game once."""
    from romcom.organizer import arcade_card
    dat = tmp_path / "mame.dat"; dat.write_text(FAMILY_DAT, encoding="utf-8")
    fb = tmp_path / "fbneo.dat"
    fb.write_text(FAMILY_DAT.split('<machine name="gtclub">')[0] + "</datafile>",
                  encoding="utf-8")
    _family_env(monkeypatch, tmp_path, {"mame": dat, "fbneo": fb})
    card = tmp_path / "card"
    r = arcade_card(card / "arcade", arcade_status="all", artwork=False)   # folder typed
    assert [p.stem for p in (card / "fbneo").glob("*.zip")] == ["sf2ua"]
    assert [p.stem for p in (card / "arcade").glob("*.zip")] == ["gtclub"]
    assert r["cores"]["mame"]["deferred"] == 1 and not r["stopped"]
