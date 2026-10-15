"""RomCom tidies itself: the incoming folder and the upkeep steps."""
import zipfile

from romcom import config, incoming, maintain


def _env(monkeypatch, tmp_path):
    games = tmp_path / "Games"
    inc = tmp_path / "Games Incoming"
    games.mkdir(); inc.mkdir()
    monkeypatch.setattr(incoming, "settings", lambda: {"download_dir": str(games)})
    monkeypatch.setattr(maintain, "STATE", tmp_path / "upkeep.json")
    return games, inc


def test_only_the_games_in_an_archive_are_unpacked_and_only_once(monkeypatch, tmp_path):
    games, inc = _env(monkeypatch, tmp_path)
    with zipfile.ZipFile(inc / "psp-001.zip", "w") as z:
        z.writestr("psp/Daxter.iso", b"iso" * 100)
        z.writestr("psp/images/Daxter-thumb.png", b"png")
        z.writestr("psp/videos/Daxter.mp4", b"mp4")
        z.writestr("psp/gamelist.xml", "<gameList/>")
    with zipfile.ZipFile(inc / "xbox-007.zip", "w") as z:
        z.writestr("xbox/Halo 2.iso", b"x" * 100)          # no handheld runs it
        z.writestr("xbox/images/Halo 2-image.png", b"png")
    assert incoming.incoming_dir() == inc
    r = incoming.unpack()
    assert r["archives"] == 2 and r["files"] == 1
    assert sorted(p.name for p in (games / "incoming" / "psp-001").iterdir()) == ["Daxter.iso"]
    assert "xbox-007.zip" in r["skipped"] and not (games / "incoming" / "xbox-007").exists()
    assert incoming.pending() == []                        # remembered: never again
    assert incoming.unpack()["archives"] == 0


def test_loose_files_dropped_in_are_noticed(monkeypatch, tmp_path):
    games, inc = _env(monkeypatch, tmp_path)
    (inc / "Turtles in Time.sfc").write_bytes(b"s" * 64)
    assert incoming.loose_changed()
    st = incoming._load(inc); st["_loose"] = sorted(incoming._loose(inc)); incoming._save(inc, st)
    assert not incoming.loose_changed()
    (inc / "Another.gba").write_bytes(b"g")
    assert incoming.loose_changed()


def test_upkeep_skips_deleting_while_an_export_runs_and_survives_a_failing_step(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    ran = []

    def fake(name, progress, stop):
        ran.append(name)
        if name == "labels":
            raise RuntimeError("catver unreadable")
        return {"ok": name}
    monkeypatch.setattr(maintain, "_step", fake)
    r = maintain.run(busy=lambda: True)
    assert r["steps"]["dupes"]["skipped"].startswith("a card export")
    assert r["steps"]["prune"]["skipped"]
    assert "dupes" not in ran and "prune" not in ran
    assert r["steps"]["labels"]["error"].startswith("RuntimeError")
    assert r["steps"]["artwork"] == {"ok": "artwork"}       # after the failure, still ran
    assert maintain.last_full() > 0 and maintain.last()["full"]
    maintain.run(steps=["claim"], busy=lambda: False)
    assert maintain.last()["full"] is False                 # a partial run is not a full one


def test_an_archive_of_hundreds_of_games_unpacks(monkeypatch, tmp_path):
    """828 names on one 7-Zip command line is past Windows' limit (WinError 206)."""
    import shutil
    import pytest
    from romcom import esde
    if not esde.seven_zip():
        pytest.skip("7-Zip not installed")
    src = tmp_path / "src"; (src / "nes").mkdir(parents=True)
    names = []
    for i in range(300):
        n = f"nes/Some Rather Long Game Title Number {i:04d} (USA) [T-Spa1.0v_Wave].nes"
        (src / n).write_bytes(b"N" * 16)
        names.append(n)
    archive = tmp_path / "nes.7z"
    import subprocess
    subprocess.run([esde.seven_zip(), "a", str(archive), "nes"], cwd=src, capture_output=True)
    esde.extract(archive, [n.replace("/", "\\") for n in names], tmp_path / "out")
    assert len(list((tmp_path / "out").iterdir())) == 300


def _incoming_env(monkeypatch, tmp_path, remove="true"):
    games, inc = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(incoming, "settings",
                        lambda: {"download_dir": str(games), "incoming_remove": remove})
    with zipfile.ZipFile(inc / "snes-001.zip", "w") as z:
        z.writestr("snes/Turtles in Time (USA).sfc", b"S" * 2048)
        z.writestr("snes/images/Turtles-thumb.png", b"png")
    with zipfile.ZipFile(inc / "xbox-007.zip", "w") as z:
        z.writestr("xbox/Halo 2.iso", b"x" * 100)
    with zipfile.ZipFile(inc / "art-only.zip", "w") as z:
        z.writestr("wii/images/Kirby-marquee.png", b"png")
    (inc / "Pocket Game (USA).gba").write_bytes(b"G" * 4096)
    (inc / "mystery.xyz").write_bytes(b"?")
    return games, inc


def test_imported_games_leave_the_incoming_folder(monkeypatch, tmp_path):
    """A large import must not be kept twice: an unpacked archive is deleted, a loose game
    moves into the library; what RomCom left out or could not place stays for the owner."""
    games, inc = _incoming_env(monkeypatch, tmp_path)
    r = incoming.run()
    left = sorted(p.name for p in inc.iterdir() if p.name != incoming.STATE)
    assert left == ["art-only.zip", "mystery.xyz", "xbox-007.zip"]
    assert r["tidied"]["deleted"] == ["snes-001.zip"] and r["tidied"]["moved"] == [
        "Pocket Game (USA).gba"]
    assert (games / "incoming" / "snes-001" / "Turtles in Time (USA).sfc").exists()
    moved = games / "incoming" / "loose" / "Pocket Game (USA).gba"
    assert moved.exists()
    from romcom.db import connect
    row = connect().execute("SELECT matched_item_id FROM files WHERE path=?",
                            (str(moved),)).fetchone()
    assert row and row[0]                              # the library follows the move


def test_with_incoming_remove_off_nothing_leaves(monkeypatch, tmp_path):
    games, inc = _incoming_env(monkeypatch, tmp_path, remove="false")
    r = incoming.run()
    assert r["tidied"] == {"deleted": [], "moved": [], "already_had": 0, "bytes": 0}
    assert (inc / "snes-001.zip").exists() and (inc / "Pocket Game (USA).gba").exists()


def test_a_game_already_in_the_library_is_not_kept_twice(monkeypatch, tmp_path):
    """Importing a pile that overlaps the library: the archive goes, and so does the
    unpacked copy of a game the library already holds byte for byte."""
    from romcom.scanner import scan
    games, inc = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(incoming, "settings", lambda: {"download_dir": str(games)})
    lib = games / "snes"
    lib.mkdir()
    (lib / "Turtles in Time (USA).sfc").write_bytes(b"S" * 2048)          # already had it
    scan(lib, adopt=True)
    with zipfile.ZipFile(inc / "snes-002.zip", "w") as z:
        z.writestr("snes/Turtles in Time (USA).sfc", b"S" * 2048)
        z.writestr("snes/Brand New (USA).sfc", b"N" * 2048)
    r = incoming.run()
    out = games / "incoming" / "snes-002"
    assert not (inc / "snes-002.zip").exists()
    assert not (out / "Turtles in Time (USA).sfc").exists() and (out / "Brand New (USA).sfc").exists()
    assert (lib / "Turtles in Time (USA).sfc").exists()
    assert r["tidied"]["already_had"] == 1
