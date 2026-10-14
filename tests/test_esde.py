"""A card ES-DE can just open: its folder names, its file types, one entry per disc."""
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

from romcom import esde
from romcom.db import connect
from romcom.organizer import organize


def test_folders_and_file_types_are_es_des():
    assert esde.folder("ps1") == "psx" and esde.folder("gamecube") == "gc"
    assert esde.folder("snes") == "snes" and esde.folder("3ds") == "n3ds"
    assert esde.playable("snes", "Game.sfc") and esde.playable("snes", "Game.zip")
    assert not esde.playable("snes", "Game.nfo")
    assert esde.playable("ps1", "Game.cue") and not esde.playable("ps1", "Game.zip")
    assert esde.playable("weird-system", "x.foo") is None


def test_a_loose_cue_and_its_tracks_become_one_entry(tmp_path):
    cue, b1, b2 = tmp_path / "G (USA).cue", tmp_path / "G (USA) (Track 1).bin", tmp_path / "G (USA) (Track 2).bin"
    for f in (cue, b1, b2):
        f.write_bytes(b"x")
    steps, entries, dropped = esde.plan("ps1", [(str(cue), 1), (str(b1), 1), (str(b2), 1)])
    assert entries == ["G (USA).cue"]
    assert {s[2] for s in steps} == {"G (USA).cue/G (USA).cue", "G (USA).cue/G (USA) (Track 1).bin",
                                     "G (USA).cue/G (USA) (Track 2).bin"}


def _game(db, iid, title, system, path, score=None):
    db.execute("INSERT INTO items(id,title,system,status,community_score) VALUES(?,?,?,'VERIFIED',?)",
               (iid, title, system, score))
    data = path.read_bytes()
    db.execute("INSERT INTO files(path,bytes,sha1,matched_item_id,match_method) VALUES(?,?,?,?,'hash')",
               (str(path), len(data), hashlib.sha1(data + path.name.encode()).hexdigest(), iid))


def test_an_export_ES_DE_can_open(tmp_path, monkeypatch):
    monkeypatch.setattr(esde, "chdman", lambda: None)     # no CHD: unpack as it is
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "e.db"))
    from romcom.config import invalidate
    invalidate()
    lib = tmp_path / "lib"
    lib.mkdir()
    sfc = lib / "Super Game (USA).sfc"
    sfc.write_bytes(b"snes-rom")
    nfo = lib / "Super Game (USA).nfo"
    nfo.write_bytes(b"release notes")
    dc = lib / "Disc Game (Japan).zip"                 # a disc game zipped: must be unpacked
    with zipfile.ZipFile(dc, "w") as z:
        z.writestr("Disc Game (Japan).gdi", "3 tracks")
        z.writestr("track01.bin", "t1")
        z.writestr("track02.raw", "t2")
        z.writestr("readme.txt", "hi")
    gba = lib / "Pocket (Europe).zip"                   # a cartridge zip: RetroArch reads it
    with zipfile.ZipFile(gba, "w") as z:
        z.writestr("Pocket (Europe).gba", "gba")
    psx = lib / "Old Disc (USA).chd"
    psx.write_bytes(b"chd")
    db = connect()
    with db:
        _game(db, "s", "Super Game (USA)", "snes", sfc, score=88)
        db.execute("INSERT INTO files(path,bytes,sha1,matched_item_id,match_method) "
                   "VALUES(?,?,?,'s','name')", (str(nfo), 13, "f" * 40))
        _game(db, "d", "Disc Game (Japan)", "dreamcast", dc)
        _game(db, "g", "Pocket (Europe)", "gba", gba)
        _game(db, "p", "Old Disc (USA)", "ps1", psx, score=71)
    card = tmp_path / "card"
    # ES-DE already knows the snes game is a favorite: the export must not lose that.
    (card / "ES-DE" / "gamelists" / "snes").mkdir(parents=True)
    (card / "ES-DE" / "gamelists" / "snes" / "gamelist.xml").write_text(
        '<?xml version="1.0"?><gameList><game><path>./Super Game (USA).sfc</path>'
        '<favorite>true</favorite></game></gameList>', encoding="utf-8")
    r = organize(card)
    assert not r["errors"], r["errors"]
    assert (card / "snes" / "Super Game (USA).sfc").exists()
    assert not (card / "snes" / "Super Game (USA).nfo").exists()          # no game
    assert r["not_playable"] == {"snes": 1}
    disc = card / "dreamcast" / "Disc Game (Japan).gdi"
    assert disc.is_dir() and (disc / "Disc Game (Japan).gdi").exists()    # directory as file
    assert (disc / "track01.bin").exists() and (disc / "track02.raw").exists()
    assert not (disc / "readme.txt").exists()
    assert (card / "gba" / "Pocket (Europe).zip").exists()               # left zipped
    assert (card / "psx" / "Old Disc (USA).chd").exists()                # ps1 -> psx
    assert r["unpacked"] == 1
    gl = ET.parse(card / "ES-DE" / "gamelists" / "snes" / "gamelist.xml").getroot()
    game = gl.find("game")
    assert game.findtext("name") == "Super Game"
    assert game.findtext("rating") == "0.90"
    assert game.findtext("favorite") == "true"                          # ES-DE's own kept
    dcl = ET.parse(card / "ES-DE" / "gamelists" / "dreamcast" / "gamelist.xml").getroot()
    assert dcl.find("game").findtext("path") == "./Disc Game (Japan).gdi"
    # a re-run unpacks nothing again
    assert organize(card)["unpacked"] == 0


def test_disc_games_become_one_chd_each(tmp_path, monkeypatch):
    """A zipped .gdi and a loose .cue + .bin each become one .chd ES-DE lists; a re-run
    converts nothing again. chdman itself is stood in for: the test is the plumbing."""
    made = []

    def fake_make_chd(src, names, main, target, system):
        made.append((Path(src).name, main))
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"MComprHD")

    monkeypatch.setattr(esde, "chdman", lambda: "chdman.exe")
    monkeypatch.setattr(esde, "make_chd", fake_make_chd)
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "c.db"))
    from romcom.config import invalidate
    invalidate()
    lib = tmp_path / "lib"
    lib.mkdir()
    dc = lib / "Disc Game (Japan).zip"
    with zipfile.ZipFile(dc, "w") as z:
        z.writestr("Disc Game (Japan).gdi", "3")
        z.writestr("track01.bin", "t1")
    cue, binf = lib / "Loose (USA).cue", lib / "Loose (USA).bin"
    cue.write_bytes(b"FILE"); binf.write_bytes(b"data")
    db = connect()
    with db:
        _game(db, "d", "Disc Game (Japan)", "dreamcast", dc)
        _game(db, "l", "Loose (USA)", "ps1", cue)
        db.execute("INSERT INTO files(path,bytes,sha1,matched_item_id,match_method) "
                   "VALUES(?,4,?,'l','hash')", (str(binf), "b" * 40))
    card = tmp_path / "card"
    r = organize(card)
    assert not r["errors"], r["errors"]
    assert (card / "dreamcast" / "Disc Game (Japan).chd").exists()
    assert (card / "psx" / "Loose (USA).chd").exists()
    assert not (card / "psx" / "Loose (USA).bin").exists()
    assert sorted(made) == [("Disc Game (Japan).zip", "Disc Game (Japan).gdi"),
                            ("Loose (USA).cue", "Loose (USA).cue")]
    gl = ET.parse(card / "ES-DE" / "gamelists" / "psx" / "gamelist.xml").getroot()
    assert gl.find("game").findtext("path") == "./Loose (USA).chd"
    assert organize(card)["converted_chd"] == 0


def test_a_multi_disc_game_is_one_entry_with_an_m3u(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "m.db"))
    from romcom.config import invalidate
    invalidate()
    lib = tmp_path / "lib"
    lib.mkdir()
    db = connect()
    with db:
        for n in (1, 2):
            f = lib / f"Big RPG (USA) (Disc {n}).chd"
            f.write_bytes(f"disc{n}".encode())
            _game(db, f"d{n}", f"Big RPG (USA) (Disc {n})", "ps1", f, score=90)
    card = tmp_path / "card"
    r = organize(card)
    assert not r["errors"], r["errors"]
    d = card / "psx" / "Big RPG (USA).m3u"
    assert (d / "Big RPG (USA).m3u").read_text() == \
        "Big RPG (USA) (Disc 1).chd\nBig RPG (USA) (Disc 2).chd\n"
    assert (d / "Big RPG (USA) (Disc 2).chd").exists()
    games = ET.parse(card / "ES-DE" / "gamelists" / "psx" / "gamelist.xml").getroot().findall("game")
    assert [g.findtext("path") for g in games] == ["./Big RPG (USA).m3u"]
    assert games[0].findtext("name") == "Big RPG"


def test_a_cue_without_its_tracks_is_no_game_and_a_lone_track_gets_a_chd(tmp_path, monkeypatch):
    monkeypatch.setattr(esde, "chdman", lambda: "chdman.exe")
    cue = tmp_path / "Gone (USA).cue"
    cue.write_text('FILE "Gone (USA) (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n')
    steps, entries, dropped = esde.plan("ps1", [(str(cue), 60)], chd=True)
    assert (steps, entries, dropped) == ([], [], 1)
    lone = tmp_path / "Lone (Japan).bin"
    lone.write_bytes(b"\0" * 2352 * 4)
    steps, entries, _ = esde.plan("ps1", [(str(lone), 2352 * 4)], chd=True)
    assert entries == ["Lone (Japan).chd"] and steps[0][0] == "chd"
    odd = tmp_path / "Odd.bin"                       # not whole sectors: not a disc track
    odd.write_bytes(b"\0" * 1000)
    assert esde.plan("ps1", [(str(odd), 1000)], chd=True)[1] == ["Odd.bin"]


def test_gamecube_discs_share_one_m3u_too(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "g.db"))
    from romcom.config import invalidate
    invalidate()
    lib = tmp_path / "lib"
    lib.mkdir()
    db = connect()
    with db:
        for n in (1, 2):
            f = lib / f"Killer7 (USA) (Disc {n}).rvz"
            f.write_bytes(f"d{n}".encode())
            _game(db, f"k{n}", f"Killer7 (USA) (Disc {n})", "gamecube", f)
    organize(tmp_path / "card")
    d = tmp_path / "card" / "gc" / "Killer7 (USA).m3u"
    assert (d / "Killer7 (USA).m3u").read_text().splitlines() == [
        "Killer7 (USA) (Disc 1).rvz", "Killer7 (USA) (Disc 2).rvz"]


def test_one_item_holding_every_disc_gets_an_m3u(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "k.db"))
    from romcom.config import invalidate
    invalidate()
    lib = tmp_path / "lib"
    lib.mkdir()
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('k','Killer7 (USA)','gamecube','VERIFIED')")
        for n in (1, 2):
            f = lib / f"Killer7 (USA) (Disc {n}) (Rev 1).rvz"
            f.write_bytes(f"d{n}".encode())
            db.execute("INSERT INTO files(path,bytes,sha1,matched_item_id,match_method) "
                       "VALUES(?,2,?,'k','hash')", (str(f), str(n) * 40))
    organize(tmp_path / "card")
    d = tmp_path / "card" / "gc" / "Killer7 (USA) (Rev 1).m3u"
    assert (d / "Killer7 (USA) (Rev 1).m3u").read_text().splitlines() == [
        "Killer7 (USA) (Disc 1) (Rev 1).rvz", "Killer7 (USA) (Disc 2) (Rev 1).rvz"]


def test_one_copy_per_game_puts_one_dump_of_a_cartridge_game_on_the_card(tmp_path, monkeypatch):
    """The verified dump, not its fan translations, trainers or bad dumps beside it."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "o.db"))
    from romcom.config import invalidate
    invalidate()
    lib = tmp_path / "lib"
    lib.mkdir()
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('m','Metroid Fusion (USA)','gba','VERIFIED')")
        for name, method in (("Metroid Fusion (U) [T+Bra0.70].gba", "filename-exact"),
                             ("Metroid Fusion (U) [b1].gba", "filename-exact"),
                             ("Metroid Fusion (U) [t1].gba", "filename-exact"),
                             ("Metroid Fusion (U) [!].gba", "hash")):
            f = lib / name
            f.write_bytes(name.encode())
            db.execute("INSERT INTO files(path,bytes,sha1,matched_item_id,match_method) "
                       "VALUES(?,?,?,'m',?)", (str(f), len(name), hashlib.sha1(name.encode()).hexdigest(), method))
    card = tmp_path / "card"
    r = organize(card, distinct_games=True)
    assert sorted(p.name for p in (card / "gba").iterdir()) == ["Metroid Fusion (U) [!].gba"]
    assert r["dupes_skipped"] == 3
    organize(tmp_path / "card2")                       # off: every dump, as before
    assert len(list((tmp_path / "card2" / "gba").iterdir())) == 4
