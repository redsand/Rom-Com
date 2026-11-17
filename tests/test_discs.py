"""Disc games kept once: the CHD becomes the game's file, the archive goes."""
import zipfile
from pathlib import Path

from romcom import discs, esde
from romcom.db import connect


def _env(monkeypatch, tmp_path, ok=True):
    cache = tmp_path / "F" / "romcom-chd-cache"
    monkeypatch.setattr(esde, "chd_cache_dir", lambda: cache)
    monkeypatch.setattr(esde, "chdman", lambda: "chdman.exe")
    monkeypatch.setattr(discs, "settings", lambda: {})
    monkeypatch.setattr(discs, "verify", lambda p: ok)

    def convert(src, names, main, target, system):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"CHD" * 100)
    monkeypatch.setattr(esde, "_convert_chd", convert)
    games = tmp_path / "H" / "Games"
    games.mkdir(parents=True)
    a = games / "Shenmue (USA) (Disc 1).7z"
    with zipfile.ZipFile(a, "w") as z:          # .7z by name; zipfile reads it the same
        z.writestr("Shenmue (USA) (Disc 1).gdi", "3\n")
        z.writestr("Shenmue (USA) (Disc 1) (Track 1).bin", b"t" * 2352)
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('s','Shenmue (USA) (Disc 1)','dreamcast','VERIFIED')")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,?,'s',1)",
                   (str(a), a.stat().st_size))
    monkeypatch.setattr(esde, "members", lambda p: [(n.filename, n.file_size)
                                                     for n in zipfile.ZipFile(p).infolist()])
    return db, a, tmp_path / "F" / "Games CHD"


def test_a_converted_disc_keeps_its_chd_and_loses_its_archive(monkeypatch, tmp_path):
    db, a, lib = _env(monkeypatch, tmp_path)
    assert discs.compact(apply=False)["chds"] == 0           # not converted yet: nothing
    r = discs.compact(apply=True, convert=True)
    chd = lib / "dreamcast" / "Shenmue (USA) (Disc 1).chd"
    assert r["chds"] == 1 and r["converted"] == 1 and chd.exists() and not a.exists()
    rows = {r[0]: r[1] for r in db.execute("SELECT path, matched_item_id FROM files")}
    assert rows == {str(chd): "s"}
    steps, entries, _ = esde.plan("dreamcast", [(str(chd), chd.stat().st_size)], chd=True)
    assert steps[0][0] == "copy" and entries == ["Shenmue (USA) (Disc 1).chd"]


def test_a_chd_that_does_not_verify_keeps_the_archive(monkeypatch, tmp_path):
    db, a, lib = _env(monkeypatch, tmp_path, ok=False)
    r = discs.compact(apply=True, convert=True)
    assert r["chds"] == 0 and a.exists() and "did not verify" in next(iter(r["skipped"].values()))


def test_a_rescan_keeps_a_converted_chd_on_its_game(monkeypatch, tmp_path):
    from romcom.scanner import scan
    db, a, lib = _env(monkeypatch, tmp_path)
    discs.compact(apply=True, convert=True)
    scan(lib, adopt=False)
    assert db.execute("SELECT matched_item_id FROM files WHERE path LIKE '%.chd'").fetchone()[0] == "s"


def test_a_corrupt_archive_goes_and_its_game_is_fetched_again(monkeypatch, tmp_path):
    db, a, lib = _env(monkeypatch, tmp_path)

    def broken(src, names, main, target, system):
        raise OSError("7-Zip could not unpack Shenmue.7z: ERROR: Data Error : Track 3.bin")
    monkeypatch.setattr(esde, "_convert_chd", broken)
    r = discs.compact(apply=True, convert=True)
    assert r["corrupt"] == [a.name] and not a.exists()
    assert db.execute("SELECT status, wanted FROM items WHERE id='s'").fetchone()[:] == ("MISSING", 1)


def _second_game(db, games, iid, title, inner="disc.gdi", archive=None):
    a = games / (archive or f"[GDI] {title}.7z")
    with zipfile.ZipFile(a, "w") as z:
        z.writestr(inner, "3\n")
        z.writestr("track01.bin", b"t" * 2352)
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES(?,?,'dreamcast','VERIFIED')",
                   (iid, title))
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,?,?,1)",
                   (str(a), a.stat().st_size, iid))
    return a


def test_archives_that_all_hold_disc_gdi_each_keep_their_own_chd(monkeypatch, tmp_path):
    """Naming the CHD after the disc inside the archive sent every `disc.gdi` game to one
    `disc.chd`, each overwriting the last and losing its archive: one CHD per game, by title."""
    db, a, lib = _env(monkeypatch, tmp_path)
    games = a.parent
    b = _second_game(db, games, "redump-dreamcast-aaa", "Boku Doraemon (Japan)")
    c = _second_game(db, games, "redump-dreamcast-bbb", "Pop'n Music 3 - Append Disc (Japan)")
    r = discs.compact(apply=True, convert=True)
    assert r["chds"] == 3 and not b.exists() and not c.exists()
    rows = dict(db.execute("SELECT matched_item_id, path FROM files").fetchall())
    d = lib / "dreamcast"
    assert rows == {"s": str(d / "Shenmue (USA) (Disc 1).chd"),
                    "redump-dreamcast-aaa": str(d / "Boku Doraemon (Japan).chd"),
                    "redump-dreamcast-bbb": str(d / "Pop'n Music 3 - Append Disc (Japan).chd")}
    assert not (d / "disc.chd").exists()


def test_a_name_another_game_owns_is_never_taken_over(monkeypatch, tmp_path):
    db, a, lib = _env(monkeypatch, tmp_path)
    d = lib / "dreamcast"
    d.mkdir(parents=True)
    theirs = d / "Shenmue (USA) (Disc 1).chd"
    theirs.write_bytes(b"OTHER")
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES('redump-dreamcast-zzz','Other','dreamcast')")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,5,'redump-dreamcast-zzz',1)",
                   (str(theirs),))
    discs.compact(apply=True, convert=True)
    assert theirs.read_bytes() == b"OTHER"
    mine = db.execute("SELECT path FROM files WHERE matched_item_id='s'").fetchone()[0]
    assert mine != str(theirs) and mine.endswith("Shenmue (USA) (Disc 1) [s].chd")


def test_a_title_is_made_safe_for_a_windows_file_name():
    assert discs._stem("Star Wars: Episode I <Racer>?") == "Star Wars - Episode I _Racer__"
    assert discs._stem("  Game.  ") == "Game"


def test_a_loose_psp_iso_compacts_to_its_chd(monkeypatch, tmp_path):
    """A loose .iso is a dump like an archived one: converted (a UMD's padding is half its
    bytes), verified, made the game's library file — and the iso goes. 46 of them, 33 GB,
    sat outside compaction because only archives qualified."""
    db, a, lib = _env(monkeypatch, tmp_path)
    games = tmp_path / "H" / "Games"
    iso = games / "Crisis Core (USA).iso"
    iso.write_bytes(b"u" * 2048 * 100)
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('p','Crisis Core (USA)','psp','VERIFIED')")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,?,'p',1)",
                   (str(iso), iso.stat().st_size))
    r = discs.compact(apply=True, convert=True)
    chd = lib / "psp" / "Crisis Core (USA).chd"
    assert r["chds"] >= 1 and chd.exists() and not iso.exists()
    rows = {x[0]: x[1] for x in db.execute(
        "SELECT path, matched_item_id FROM files WHERE matched_item_id='p'")}
    assert rows == {str(chd): "p"}


def test_a_loose_cue_takes_its_own_tracks_only(monkeypatch, tmp_path):
    """A .cue's tracks go with it; a second dump of the same disc (.cdi beside the .cue)
    keeps its place — which dump a card carries is the export's business, not this's."""
    db, a, lib = _env(monkeypatch, tmp_path)
    games = tmp_path / "H" / "Games"
    cue, binf = games / "Loose (USA).cue", games / "Loose (USA) (Track 1).bin"
    cue.write_text('FILE "Loose (USA) (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n',
                   encoding="utf-8")
    binf.write_bytes(b"t" * 2352 * 4)
    cdi = games / "Loose (USA).cdi"
    cdi.write_bytes(b"d" * 900)
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('l','Loose (USA)','psx','VERIFIED')")
        for f in (cue, binf, cdi):
            db.execute("INSERT INTO files(path,bytes,matched_item_id,content) "
                       "VALUES(?,?,?,1)", (str(f), f.stat().st_size, "l"))
    r = discs.compact(apply=True, convert=True)
    chd = lib / "psx" / "Loose (USA).chd"
    assert r["chds"] >= 1 and chd.exists()
    assert not cue.exists() and not binf.exists()
    assert cdi.exists()                    # the other dump of the disc stays
    paths = {x[0] for x in db.execute("SELECT path FROM files WHERE matched_item_id='l'")}
    assert paths == {str(chd), str(cdi)}


def _later_source(monkeypatch, db, a, name, payload):
    """A second archive for game 's', arriving after the first was compacted. The fake
    converter writes `payload`, and a CHD's hash is its bytes."""
    monkeypatch.setattr(esde, "_convert_chd", lambda src, names, main, target, system: (
        Path(target).parent.mkdir(parents=True, exist_ok=True),
        Path(target).write_bytes(payload)))
    monkeypatch.setattr(discs, "_sha", lambda p: Path(p).read_bytes().hex())
    b = a.parent / name
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("disc.gdi", "3\n")
        z.writestr("track01.bin", b"u" * 2352)
    with db:
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,?,'s',1)",
                   (str(b), b.stat().st_size))
    return b


def test_a_second_disc_arriving_later_is_converted_not_deleted(monkeypatch, tmp_path):
    """Disc 1 was compacted to `<title>.chd`. Disc 2, filed under the same game, found
    that name on disk, was taken for already converted and deleted unconverted."""
    db, a, lib = _env(monkeypatch, tmp_path)
    discs.compact(apply=True, convert=True)
    b = _later_source(monkeypatch, db, a, "Shenmue (USA) (Disc 2).7z", b"DISC2")
    r = discs.compact(apply=True, convert=True)
    d = lib / "dreamcast"
    second = d / "Shenmue (USA) (Disc 1) - Shenmue (USA) (Disc 2).chd"
    assert r["converted"] == 1 and not b.exists()
    assert second.read_bytes() == b"DISC2"
    assert (d / "Shenmue (USA) (Disc 1).chd").read_bytes() == b"CHD" * 100
    assert {x[0] for x in db.execute("SELECT path FROM files WHERE matched_item_id='s'")} == {
        str(d / "Shenmue (USA) (Disc 1).chd"), str(second)}


def test_the_same_disc_downloaded_again_adds_no_second_chd(monkeypatch, tmp_path):
    db, a, lib = _env(monkeypatch, tmp_path)
    discs.compact(apply=True, convert=True)
    b = _later_source(monkeypatch, db, a, "Shenmue again.7z", b"CHD" * 100)
    discs.compact(apply=True, convert=True)
    assert not b.exists()
    assert [p.name for p in (lib / "dreamcast").iterdir()] == ["Shenmue (USA) (Disc 1).chd"]
    assert not list((tmp_path / "F" / "romcom-chd-cache").rglob("*.chd"))


def test_a_later_source_is_kept_until_it_may_be_converted(monkeypatch, tmp_path):
    db, a, lib = _env(monkeypatch, tmp_path)
    discs.compact(apply=True, convert=True)
    b = _later_source(monkeypatch, db, a, "Shenmue (USA) (Disc 2).7z", b"DISC2")
    r = discs.compact(apply=True, convert=False)
    assert b.exists() and r["chds"] == 0
