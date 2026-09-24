"""Writing RetroArch playlists from the catalog rather than from its scanner."""
import json

from romcom import playlists
from romcom.db import connect


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "p.db"))
    from romcom.config import invalidate
    invalidate()
    monkeypatch.setattr(playlists, "_core_for",
                        lambda system: (r"C:\ra\cores\snes9x_libretro.dll", "snes9x"))
    return connect()


def test_one_entry_per_game_not_per_file(monkeypatch, tmp_path):
    """The same rom sits in several folders here — a set, its -processed copy, and a
    per-system tree. A playlist listing each shows the same game three times with nothing to
    tell them apart, which is how 29,181 GBA games first came out as 64,718 entries."""
    db = _setup(monkeypatch, tmp_path)
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('g','Chrono Trigger','snes','VERIFIED')")
        for folder in ("a", "b", "c"):
            d = tmp_path / folder
            d.mkdir()
            rom = d / "Chrono Trigger.smc"
            rom.write_bytes(b"x" * 16)
            db.execute("INSERT INTO files(path,bytes,crc32,matched_item_id,content)"
                       " VALUES(?,16,'ABC','g',1)", (str(rom),))
    r = playlists.build(systems=["snes"], dest=tmp_path / "pl", db=db)
    assert r["written"]["snes"] == 1
    data = json.loads((tmp_path / "pl" / "Rom-Com - Nintendo - Super Nintendo Entertainment System.lpl").read_text())
    assert len(data["items"]) == 1
    assert data["items"][0]["label"] == "Chrono Trigger"


def test_retroarchs_own_playlists_are_not_overwritten(monkeypatch, tmp_path):
    """An archive tool has no business destroying another program's work unasked."""
    db = _setup(monkeypatch, tmp_path)
    dest = tmp_path / "pl"
    dest.mkdir()
    theirs = dest / "Nintendo - Super Nintendo Entertainment System.lpl"
    theirs.write_text("ORIGINAL", encoding="utf-8")
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('g','G','snes','VERIFIED')")
        rom = tmp_path / "g.smc"; rom.write_bytes(b"x")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,1,'g',1)", (str(rom),))
    playlists.build(systems=["snes"], dest=dest, db=db)
    assert theirs.read_text() == "ORIGINAL"
    assert (dest / "Rom-Com - Nintendo - Super Nintendo Entertainment System.lpl").exists()


def test_replace_is_opt_in(monkeypatch, tmp_path):
    db = _setup(monkeypatch, tmp_path)
    dest = tmp_path / "pl"; dest.mkdir()
    theirs = dest / "Nintendo - Super Nintendo Entertainment System.lpl"
    theirs.write_text("ORIGINAL", encoding="utf-8")
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('g','G','snes','VERIFIED')")
        rom = tmp_path / "g.smc"; rom.write_bytes(b"x")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,1,'g',1)", (str(rom),))
    playlists.build(systems=["snes"], dest=dest, db=db, replace=True)
    assert json.loads(theirs.read_text())["items"][0]["label"] == "G"


def test_the_db_name_stays_canonical_so_thumbnails_keep_working(monkeypatch, tmp_path):
    """RetroArch matches thumbnail packs on db_name, not on the file name. Renaming the
    playlist file but not the db_name is what keeps artwork working."""
    db = _setup(monkeypatch, tmp_path)
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('g','G','snes','VERIFIED')")
        rom = tmp_path / "g.smc"; rom.write_bytes(b"x")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,1,'g',1)", (str(rom),))
    playlists.build(systems=["snes"], dest=tmp_path / "pl", db=db)
    data = json.loads((tmp_path / "pl" / "Rom-Com - Nintendo - Super Nintendo Entertainment System.lpl").read_text())
    assert data["items"][0]["db_name"] == "Nintendo - Super Nintendo Entertainment System.lpl"


def test_a_decoy_named_rom_is_listed_but_reported(monkeypatch, tmp_path):
    """We do hold the game, so it belongs in the playlist — but RetroArch filters on the
    visible extension and will refuse it, and pretending otherwise is the dishonest part."""
    db = _setup(monkeypatch, tmp_path)
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('g','G','snes','VERIFIED')")
        rom = tmp_path / "G.smc.wmf"; rom.write_bytes(b"x")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,1,'g',1)", (str(rom),))
    r = playlists.build(systems=["snes"], dest=tmp_path / "pl", db=db)
    assert r["written"]["snes"] == 1
    assert r["unloadable"]["snes"] == 1
