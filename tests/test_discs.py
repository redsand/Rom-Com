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
