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
    calls = []
    def fake_build(setnames, dest, db=None, dry_run=False, stop=None, room_for=None, progress=None,
                   fresh=False, zipped=False):
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
    calls = []
    def fake_build(setnames, dest, db=None, dry_run=False, stop=None, room_for=None, progress=None,
                   fresh=False, zipped=False):
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
    assert (dest / "nes" / "0" / "New.nes").exists()
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
    assert (dest / "nes" / "0" / "New.nes").exists()


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
<machine name="neogeo">
<rom name="bios.bin" size="16" crc="aaaa1111"/>
</machine>
</datafile>"""


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
    r = romset("fbneo", dest)
    assert r["profile"] == "fbneo" and r["assemblable"] == 2
    assert (dest / "neogeo.zip").exists()                    # the bios parent, first
    assert (dest / "mslug.zip").exists() and (dest / "kof97.zip").exists()
    assert r["bios_built"] == ["neogeo"]
    assert r["bios_unavailable"] == {"ghost": 1}             # kof97's parent: not in the dump


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
