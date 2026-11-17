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
        z.writestr("xbox360/Halo 3.iso", b"x" * 100)       # nothing here runs it
        z.writestr("xbox360/images/Halo 3-image.png", b"png")
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
        z.writestr("xbox360/Halo 3.iso", b"x" * 100)
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


def test_an_archive_that_would_fill_the_drive_waits(monkeypatch, tmp_path):
    games, inc = _env(monkeypatch, tmp_path)
    with zipfile.ZipFile(inc / "big.zip", "w") as z:
        z.writestr("ps2/Huge Game.iso", b"I" * 5000)
    from collections import namedtuple
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda p: Usage(10 ** 12, 10 ** 12, 1000))
    r = incoming.unpack()
    assert "not enough room yet" in r["skipped"]["big.zip"]
    assert incoming.pending() != []                       # tried again once space is back


def test_a_hand_unpacked_folder_beside_its_import_is_not_kept_twice(monkeypatch, tmp_path):
    """'nes/' (games plus cover art) dropped beside nes.rar: the games are the ones the
    import already unpacked, so those copies go; the art stays for the owner."""
    games, inc = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(incoming, "settings", lambda: {"download_dir": str(games)})
    with zipfile.ZipFile(inc / "nes.zip", "w") as z:
        z.writestr("nes/Some Game (USA).nes", b"N" * 1024)
    (inc / "nes" / "images").mkdir(parents=True)
    (inc / "nes" / "Some Game (USA).nes").write_bytes(b"N" * 1024)
    (inc / "nes" / "images" / "Some Game-thumb.png").write_bytes(b"png")
    r = incoming.run()
    assert (games / "incoming" / "nes" / "Some Game (USA).nes").exists()
    assert not (inc / "nes" / "Some Game (USA).nes").exists()
    assert (inc / "nes" / "images" / "Some Game-thumb.png").exists()
    assert r["tidied"]["already_had"] == 1


def test_android_packages_move_to_their_own_shelf(monkeypatch, tmp_path):
    """An .apk no catalog will ever claim sat in the incoming folder for weeks: it goes to
    the Android shelf and is read there. One that does not open (half-copied) stays."""
    from test_apk import _apk
    from romcom.db import connect
    games, inc = _env(monkeypatch, tmp_path)
    shelf = tmp_path / "Android Games"
    monkeypatch.setattr(incoming, "settings", lambda: {
        "download_dir": str(games), "android_games_dir": str(shelf)})
    _apk(inc / "Example.apk")
    (inc / "Half.apk").write_bytes(b"PK\x03\x04 not all here yet")
    r = incoming.run()
    assert r["android"]["moved"] == ["Example.apk"] and r["android"]["games"] == 1
    assert (shelf / "Example.apk").exists() and not (inc / "Example.apk").exists()
    assert "Half.apk" in r["android"]["kept"] and (inc / "Half.apk").exists()
    assert connect().execute("SELECT package FROM android_games").fetchone()[0] == "com.example.game"
    assert not connect().execute("SELECT 1 FROM files WHERE path LIKE '%Example.apk'").fetchone()
    assert not incoming.loose_changed()                    # and no idle second run


def test_an_apks_bundle_is_an_android_package(tmp_path):
    """base.apk and its splits in one zip, no manifest of the bundle's own."""
    from test_apk import _apk
    from romcom import apk
    _apk(tmp_path / "base.apk", package="com.rockstar.rdr", label="RDR")
    with zipfile.ZipFile(tmp_path / "RDR.apks", "w") as z:
        z.write(tmp_path / "base.apk", "base.apk")
        z.writestr("split_config.arm64_v8a.apk", b"x")
        z.writestr("split_data0.apk", b"x")
    info = apk.read(tmp_path / "RDR.apks")
    assert info["package"] == "com.rockstar.rdr" and info["kind"] == "apks"
    assert info["splits"] == 2 and "arm64-v8a" in info["abis"]


def test_what_stays_in_the_incoming_folder_says_why(monkeypatch, tmp_path):
    """A damaged archive, an Xbox game unpacked to its files, a file of no known kind:
    each is reported with its reason instead of sitting there unexplained."""
    games, inc = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(incoming, "extract_xiso", lambda: None)   # no repack tool here
    (inc / "Mystery.qqq").write_bytes(b"????")
    (inc / "Broken.zip").write_bytes(b"PK\x03\x04 cut short")
    with zipfile.ZipFile(inc / "Metal Slug 3.zip", "w") as z:
        z.writestr("Metal Slug 3/default.xbe", b"XBEH")
        z.writestr("Metal Slug 3/ms3data.afs", b"AFS")
    (inc / "xbox" / "images").mkdir(parents=True)
    (inc / "xbox" / "images" / "Halo-thumb.png").write_bytes(b"png")
    r = incoming.run()
    why = {x["name"]: x["why"] for x in r["left"]}
    assert why["Metal Slug 3.zip"] == incoming.XBOX_WHY
    assert "could not be opened" in why["Broken.zip"]
    assert ".qqq" in why["Mystery.qqq"]
    assert "artwork" in why["xbox/"]
    assert incoming.pending() == []          # the damaged one is not re-read every 5 minutes
    assert not incoming.loose_changed()


def test_an_xbox_disc_and_a_switch_package_join_the_library(monkeypatch, tmp_path):
    """Xbox discs sat in the incoming folder as 'no handheld runs it'; a .nup was a file
    of no known kind. Both are games: the disc by its own header, wherever it lies."""
    from romcom import scanner
    from romcom.db import connect
    games, inc = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(scanner, "_known_systems", lambda: {"xbox", "switch", "psp"})
    (inc / "Halo 2-014.iso").write_bytes(b"\0" * 0x10000 + b"MICROSOFT*XBOX*MEDIA" + b"\0" * 64)
    (inc / "Zelda [0100].nup").write_bytes(b"NUP\x01" + b"z" * 64)
    r = incoming.run()
    assert sorted(r["tidied"]["moved"]) == ["Halo 2-014.iso", "Zelda [0100].nup"]
    got = dict(connect().execute("SELECT i.system, f.path FROM files f JOIN items i "
                                 "ON i.id=f.matched_item_id").fetchall())
    assert got["xbox"].endswith("Halo 2-014.iso") and got["switch"].endswith(".nup")
    assert r["left"] == []


def test_an_archive_once_called_artwork_is_looked_into_again(monkeypatch, tmp_path):
    """State written before damaged archives and Xbox game folders were told apart."""
    games, inc = _env(monkeypatch, tmp_path)
    p = inc / "Dino Crisis 3.zip"
    p.write_bytes(b"PK\x03\x04 cut short")
    incoming._save(inc, {incoming._key(p): {"games": 0, "held": 0}})
    assert incoming.pending() == [p]
    incoming.unpack()
    assert incoming.pending() == []
    assert "could not be opened" in incoming.leftovers(inc)[0]["why"]


def test_a_game_dropped_into_a_folder_already_there_is_noticed(monkeypatch, tmp_path):
    games, inc = _env(monkeypatch, tmp_path)
    (inc / "snes").mkdir()
    st = incoming._load(inc); st["_loose"] = sorted(incoming._loose(inc)); incoming._save(inc, st)
    assert not incoming.loose_changed()
    (inc / "snes" / "Turtles in Time.sfc").write_bytes(b"s" * 64)
    assert incoming.loose_changed()


def test_a_file_taken_out_by_hand_no_longer_counts_as_a_game_in_hand(monkeypatch, tmp_path):
    from romcom.db import connect
    games, inc = _env(monkeypatch, tmp_path)
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system,status,catalog_source) "
                   "VALUES('nointro-n64-x','Mario Story (Japan)','n64','FOUND','nointro')")
        db.execute("INSERT INTO files(path,bytes,mtime,sha1,matched_item_id,match_method) "
                   "VALUES(?,?,?,?,?,?)", (str(inc / "N64" / "Mario Story (Japan).zip"), 9, 1.0,
                                          "ab" * 20, "nointro-n64-x", "filename-exact"))
    assert incoming.forget_vanished(inc) == 1
    assert connect().execute("SELECT status FROM items WHERE id='nointro-n64-x'").fetchone()[0] == "MISSING"
    assert not connect().execute("SELECT 1 FROM files WHERE matched_item_id='nointro-n64-x'").fetchone()


def test_xbox_and_switch_catalogs_are_recognised_and_a_switch_dump_matches_by_title_id(monkeypatch, tmp_path):
    import json
    from romcom import catalog, scanner
    from romcom.db import connect
    assert catalog.detect_system(["Microsoft - Xbox"]) == "xbox"
    assert catalog.detect_system(["Microsoft - Xbox 360"]) is None
    assert catalog.detect_system(["Nintendo - Nintendo Switch"]) == "switch"
    db_file = tmp_path / "US.en.json"
    db_file.write_text(json.dumps({
        "70010000000025": {"id": "0100000000010000", "name": "Super Mario Odyssey", "releaseDate": 20171027},
        "70050000000001": {"id": "0100000000010800", "name": "Super Mario Odyssey Update"},
        "70070000000002": {"id": None, "name": "Some Bundle"}}), encoding="utf-8")
    assert catalog.import_titledb(db_file)["items"] == 1
    monkeypatch.setattr(scanner, "_known_systems", lambda: {"switch"})
    lib = tmp_path / "lib"; lib.mkdir()
    (lib / "SMO [0100000000010000][v0].nsp").write_bytes(b"PFS0" + b"n" * 64)
    (lib / "smo update [0100000000010800][v65536].nsp").write_bytes(b"PFS0" + b"u" * 64)
    scanner.scan(lib)
    rows = connect().execute("SELECT i.title, f.match_method FROM files f JOIN items i "
                             "ON i.id=f.matched_item_id").fetchall()
    assert [tuple(r) for r in rows] == [("Super Mario Odyssey", "title-id")] * 2


def test_an_xbox_game_kept_as_loose_files_is_made_a_disc_image(monkeypatch, tmp_path):
    import pytest
    from romcom import esde, scanner
    from romcom.db import connect
    if not esde.seven_zip() or not incoming.extract_xiso():
        pytest.skip("7-Zip or extract-xiso not installed")
    games, inc = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(scanner, "_known_systems", lambda: {"xbox"})
    with zipfile.ZipFile(inc / "Metal Slug 3 [!].zip", "w") as z:
        z.writestr("Metal Slug 3/default.xbe", b"XBEH" + b"\0" * 4096)
        z.writestr("Metal Slug 3/media/stats.xpr", b"x" * 4096)
    r = incoming.run()
    iso = games / "incoming" / "Metal Slug 3 [!]" / "Metal Slug 3.iso"
    assert iso.exists() and incoming.is_xbox_disc(iso)
    assert not (inc / "Metal Slug 3 [!].zip").exists()        # imported: the archive goes
    assert connect().execute("SELECT i.system FROM files f JOIN items i ON i.id=f.matched_item_id "
                             "WHERE f.path=?", (str(iso),)).fetchone()[0] == "xbox"
    assert r["left"] == []
