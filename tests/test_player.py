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


# ---------------------------------------------------- the session-0 handoff

def test_a_launch_is_queued_not_spawned(monkeypatch, tmp_path):
    """The server must never spawn. A Windows service runs in session 0 whatever account it
    uses, and session 0 has no desktop — the emulator window would exist and be invisible,
    holding the ROM open with nothing to show for it."""
    db, rom = setup(monkeypatch, tmp_path, {"snes": 'emu.exe "{rom}"'})
    monkeypatch.setattr(player.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    r = player.request_launch("snes-ct", db=db)
    assert r["status"] == "PENDING"
    row = db.execute("SELECT * FROM launch_requests").fetchone()
    assert row["status"] == "PENDING" and row["command"] == f'emu.exe "{rom}"'


def test_a_bad_request_fails_where_someone_is_looking(monkeypatch, tmp_path):
    """Resolved up front, so an unconfigured emulator is an error in the UI rather than a
    row the agent quietly chokes on later."""
    db, _ = setup(monkeypatch, tmp_path, {})
    with pytest.raises(LookupError, match="emulators.yaml"):
        player.request_launch("snes-ct", db=db)
    assert db.execute("SELECT COUNT(*) c FROM launch_requests").fetchone()["c"] == 0


def test_the_agent_runs_the_queue_and_marks_it_played(monkeypatch, tmp_path):
    db, _ = setup(monkeypatch, tmp_path, {"snes": 'emu.exe "{rom}"'})
    spawned = []
    class Alive:
        def poll(self): return None
    monkeypatch.setattr(player.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a[0]) or Alive())
    player.request_launch("snes-ct", db=db)
    assert player.agent_once(db) == 1
    assert len(spawned) == 1
    assert db.execute("SELECT status FROM launch_requests").fetchone()["status"] == "RUNNING"
    assert db.execute("SELECT play_status FROM items WHERE id='snes-ct'").fetchone()["play_status"] == "PLAYED"


def test_the_same_request_is_never_launched_twice(monkeypatch, tmp_path):
    """Claimed before spawning, so a second agent tick — or a second agent — cannot start
    the same game again."""
    db, _ = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    class Alive:
        def poll(self): return None
    monkeypatch.setattr(player.subprocess, "Popen", lambda *a, **k: Alive())
    player.request_launch("snes-ct", db=db)
    assert player.agent_once(db) == 1
    assert player.agent_once(db) == 0


def test_a_failed_spawn_is_recorded_not_retried_forever(monkeypatch, tmp_path):
    db, _ = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    def boom(*a, **k):
        raise FileNotFoundError("emu.exe not found")
    monkeypatch.setattr(player.subprocess, "Popen", boom)
    player.request_launch("snes-ct", db=db)
    player.agent_once(db)
    row = db.execute("SELECT status,error FROM launch_requests").fetchone()
    assert row["status"] == "FAILED" and "not found" in row["error"]
    assert player.agent_once(db) == 0


def test_agent_status_reports_whether_anyone_is_listening(monkeypatch, tmp_path):
    """So the UI can say 'start the agent' instead of queuing launches into a void."""
    db, _ = setup(monkeypatch, tmp_path, {"snes": '{rom}'})
    assert player.agent_status(db)["running"] is False
    player.agent_once(db)                      # a tick beats
    assert player.agent_status(db)["running"] is True


def test_arcade_export_is_delegated_to_the_set_builder(monkeypatch, tmp_path):
    """Arcade is built from the dat, not copied from the match table.

    One physical file belongs to many sets while files.matched_item_id records a single
    owner, so copying matched files left every set but one incomplete — galaga came out
    missing prom-2.5c exactly that way. mameset.build owns the layout now; this pins that
    organize hands arcade over to it and does not copy those rows itself."""
    from romcom.organizer import organize
    from romcom import mameset
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "arc.db"))
    from romcom.config import invalidate
    invalidate()
    db = connect()
    src = tmp_path / "flat"; src.mkdir()
    with db:
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id,keep)"
                   " VALUES('a','Galaga','arcade','VERIFIED','antopisa','arcade/galaga',1)")
        (src / "gg1.3p").write_bytes(b"x")
        db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,1,'a',1)",
                   (str(src / "gg1.3p"),))
    seen = {}
    monkeypatch.setattr(mameset, "build",
                        lambda sets, dest, db=None, dry_run=False:
                        seen.update(sets=list(sets), dest=str(dest)) or
                        {"sets": {}, "copied": 3, "bytes": 0, "complete": 1, "incomplete": 0})
    r = organize(str(tmp_path / "card"), keep_only=True)
    assert seen["sets"] == ["galaga"]
    assert seen["dest"].endswith("arcade")
    assert r["arcade"]["complete"] == 1
    assert r["copied"] == 3        # the builder's count, not a second copy of the same files


def test_non_arcade_systems_keep_the_flat_layout(monkeypatch, tmp_path):
    """One file is one game everywhere else; nesting those would just add a pointless level."""
    from romcom.organizer import organize
    db, rom = setup(monkeypatch, tmp_path, {})
    player.set_keep("snes-ct", True, db=db)
    dest = tmp_path / "card2"
    organize(str(dest), keep_only=True)
    assert (dest / "snes" / rom.name).exists()


def test_play_stages_the_arcade_set_on_demand(monkeypatch, tmp_path):
    """Clicking a game should play it. Before this, Play only worked for sets someone had
    already exported by hand, and the failure was silent in the worst way: MAME printed
    `NOT FOUND (tried in progolf)` to a console nobody sees, exited 0, and the UI said it
    was running."""
    from romcom import mameset
    db, _ = setup(monkeypatch, tmp_path, {"arcade": 'mame.exe -rompath "{}" {{set}}'.format(tmp_path / "rp")})
    monkeypatch.setattr(player, "rompath", lambda: str(tmp_path / "rp"))
    with db:
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id)"
                   " VALUES('a','Donkey King','arcade','VERIFIED','antopisa','arcade/dking')")
    built = []
    monkeypatch.setattr(mameset, "build", lambda sets, dest, db=None, dry_run=False:
                        built.append(list(sets)) or
                        {"sets": {s: {"complete": True, "missing": [], "roms": 1, "found": 1} for s in sets},
                         "copied": 1, "bytes": 1, "complete": 1, "incomplete": 0})
    r = player.request_launch("a", db=db)
    assert built == [["dking"]]
    assert r["staged"]["built"] is True
    assert r["command"].endswith("dking")


def test_an_unassemblable_set_refuses_before_queuing(monkeypatch, tmp_path):
    """Better to say which roms are missing than to queue a launch that dies invisibly."""
    from romcom import mameset
    db, _ = setup(monkeypatch, tmp_path, {"arcade": 'mame.exe {set}'})
    monkeypatch.setattr(player, "rompath", lambda: str(tmp_path / "rp"))
    with db:
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id)"
                   " VALUES('a','Broken','arcade','VERIFIED','antopisa','arcade/broken')")
    monkeypatch.setattr(mameset, "build", lambda sets, dest, db=None, dry_run=False:
                        {"sets": {"broken": {"complete": False, "missing": ["a.rom"], "roms": 2, "found": 1}},
                         "copied": 0, "bytes": 0, "complete": 0, "incomplete": 1})
    with pytest.raises(LookupError, match="a.rom"):
        player.request_launch("a", db=db)
    assert db.execute("SELECT COUNT(*) c FROM launch_requests").fetchone()["c"] == 0


def test_an_emulator_that_quits_instantly_is_reported_as_failed(monkeypatch, tmp_path):
    """A successful spawn proves nothing. MAME with a missing romset prints NOT FOUND, exits
    0 and vanishes; that reported as RUNNING with no error while nothing happened on screen."""
    db, _ = setup(monkeypatch, tmp_path, {"snes": 'emu.exe "{rom}"'})
    monkeypatch.setattr(player, "EARLY_EXIT_SECS", 0.2)

    class Dead:
        def poll(self): return 0
    monkeypatch.setattr(player.subprocess, "Popen", lambda *a, **k: Dead())
    player.request_launch("snes-ct", db=db)
    player.agent_once(db)
    row = db.execute("SELECT status,error FROM launch_requests").fetchone()
    assert row["status"] == "FAILED" and "exited" in row["error"]


def test_a_surviving_emulator_stays_running(monkeypatch, tmp_path):
    db, _ = setup(monkeypatch, tmp_path, {"snes": 'emu.exe "{rom}"'})
    monkeypatch.setattr(player, "EARLY_EXIT_SECS", 0.2)

    class Alive:
        def poll(self): return None
    monkeypatch.setattr(player.subprocess, "Popen", lambda *a, **k: Alive())
    player.request_launch("snes-ct", db=db)
    assert player.agent_once(db) == 1
    assert db.execute("SELECT status FROM launch_requests").fetchone()["status"] == "RUNNING"
