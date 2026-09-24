"""Auditioning a library: launch a game, then mark whether it earned a slot on the card."""
import pytest
from romcom.db import connect
from romcom import player


def setup(monkeypatch, tmp_path, emulators=None):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "p.db"))
    from romcom.config import invalidate
    invalidate()
    monkeypatch.setattr(player, "emulators", lambda: emulators or {})
    db = connect()
    rom = tmp_path / "Chrono Trigger (USA).sfc"
    rom.write_bytes(b"x" * 4096)
    manual = tmp_path / "Chrono Trigger (USA).txt"
    manual.write_bytes(b"y" * 90000)      # bigger, but not content
    with db:
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id)"
                   " VALUES('snes-ct','Chrono Trigger (USA)','snes','VERIFIED','nointro','snes/CT')")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,?,?,1)",
                   (str(rom), 4096, "snes-ct"))
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,?,?,0)",
                   (str(manual), 90000, "snes-ct"))
    return db, rom


def test_it_resolves_the_command_without_launching_anything(monkeypatch, tmp_path):
    """command_for is what the web UI calls. The service runs as LocalSystem in session 0,
    which cannot put a window on the desktop, so it must never spawn."""
    db, rom = setup(monkeypatch, tmp_path, {"snes": 'emu.exe "{rom}"'})
    monkeypatch.setattr(player.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    plan = player.command_for("snes-ct", db=db)
    assert plan["command"] == f'emu.exe "{rom}"'
    assert plan["rom"] == str(rom)


def test_the_rom_is_the_largest_content_file_not_the_largest_file(monkeypatch, tmp_path):
    """A set carries manuals and cue sheets beside the ROM. Picking the biggest file would
    hand the emulator a 90 KB text file over the 4 KB cartridge."""
    db, rom = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    assert player.rom_for(db, db.execute("SELECT * FROM items").fetchone()) == str(rom)


def test_an_unconfigured_system_says_how_to_configure_it(monkeypatch, tmp_path):
    db, _ = setup(monkeypatch, tmp_path, {})
    with pytest.raises(LookupError, match="emulators.yaml"):
        player.command_for("snes-ct", db=db)


def test_an_item_with_nothing_on_disk_cannot_be_launched(monkeypatch, tmp_path):
    db, rom = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    with db:
        db.execute("DELETE FROM files")
    with pytest.raises(LookupError, match="no file on disk"):
        player.command_for("snes-ct", db=db)


def test_an_ambiguous_title_lists_the_candidates(monkeypatch, tmp_path):
    """Typing full ids is miserable, so titles are accepted — but silently picking one of
    several matches would launch the wrong game."""
    db, _ = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('x','Chrono Trigger (JP)','snes','VERIFIED')")
    with pytest.raises(LookupError, match="matches several"):
        player.command_for("Chrono", db=db)


def test_launching_records_that_it_was_played(monkeypatch, tmp_path):
    db, _ = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    monkeypatch.setattr(player.subprocess, "Popen", lambda *a, **k: None)
    player.launch("snes-ct", db=db)
    row = db.execute("SELECT play_status, last_played FROM items WHERE id='snes-ct'").fetchone()
    assert row["play_status"] == "PLAYED" and row["last_played"]


def test_keep_marks_for_export_and_survives_a_later_launch(monkeypatch, tmp_path):
    """A verdict must not be overwritten by simply replaying the game."""
    db, _ = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    monkeypatch.setattr(player.subprocess, "Popen", lambda *a, **k: None)
    player.set_keep("snes-ct", True, db=db)
    assert db.execute("SELECT keep,play_status FROM items").fetchone()["play_status"] == "KEEP"
    player.launch("snes-ct", db=db)
    row = db.execute("SELECT keep,play_status FROM items").fetchone()
    assert row["keep"] == 1 and row["play_status"] == "KEEP"


def test_unkeep_reverses_it(monkeypatch, tmp_path):
    db, _ = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    player.set_keep("snes-ct", True, db=db)
    player.set_keep("snes-ct", False, db=db)
    row = db.execute("SELECT keep,play_status FROM items").fetchone()
    assert row["keep"] == 0 and row["play_status"] == "SKIP"


def test_kept_reports_the_size_of_the_export(monkeypatch, tmp_path):
    """The number that decides whether it fits on the card."""
    db, _ = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    player.set_keep("snes-ct", True, db=db)
    k = player.kept(db=db)
    assert k["count"] == 1 and k["bytes"] == 94096


def test_organize_can_export_only_what_is_kept(monkeypatch, tmp_path):
    from romcom.organizer import organize
    db, rom = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    with db:
        db.execute("INSERT INTO items(id,title,system,status,keep) VALUES('other','Other','snes','VERIFIED',0)")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,1,'other',1)",
                   (str(tmp_path / "other.sfc"),))
    (tmp_path / "other.sfc").write_bytes(b"z")
    player.set_keep("snes-ct", True, db=db)
    r = organize(str(tmp_path / "card"), keep_only=True, dry_run=True)
    assert r["keep_only"] is True and r["would_copy"] == 2   # the kept item's rom + its manual
    assert (tmp_path / "card").exists() is False             # dry run wrote nothing
