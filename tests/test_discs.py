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
