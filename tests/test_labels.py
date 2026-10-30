"""Labels: what a game is, from catver.ini and the dump's name — and what an export leaves out."""
from romcom import labels, mameset
from romcom.db import connect

CATVER = """[FOLDER_SETTINGS]
RootFolderIcon mame

[Category]
mslug=Shooter / Run and Gun
neogeo=System / BIOS
m1hotpot=Slot Machine / Reels
pc9801=Computer / Japanese
mjgirl=Tabletop / Mahjong * Mature *

[VerAdded]
mslug=.037b11
"""

DAT = """<?xml version="1.0"?><datafile>
<machine name="neogeo" isbios="yes">
<rom name="bios.bin" size="16" crc="aaaa0001"/>
</machine>
<machine name="mslug" romof="neogeo">
<rom name="m.bin" size="16" crc="aaaa0002"/>
<driver status="good"/>
</machine>
<machine name="m1hotpot">
<rom name="h.bin" size="16" crc="aaaa0003"/>
<driver status="good"/>
</machine>
</datafile>"""


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "l.db"))
    from romcom.config import invalidate
    invalidate()
    cat = tmp_path / "catver.ini"
    cat.write_text(CATVER, encoding="utf-8")
    monkeypatch.setattr(labels, "catver_path", lambda: cat)
    dat = tmp_path / "mame.dat"
    dat.write_text(DAT, encoding="utf-8")
    monkeypatch.setattr(mameset, "dat_path", lambda profile="mame": dat)
    monkeypatch.setattr(mameset, "dat_paths", lambda profile="mame": [dat])
    flat = tmp_path / "flat"
    flat.mkdir()
    db = connect()
    with db:
        for iid, s, crc in (("i-neogeo", "neogeo", "aaaa0001"), ("i-mslug", "mslug", "aaaa0002"),
                            ("i-hot", "m1hotpot", "aaaa0003")):
            (flat / crc).write_bytes(b"x" * 16)
            db.execute("INSERT INTO items(id,title,system,external_id,status) "
                       "VALUES(?,?,'arcade',?,'VERIFIED')", (iid, s, f"arcade/{s}"))
            db.execute("INSERT INTO files(path,bytes,crc32,sha1,matched_item_id) VALUES(?,?,?,?,?)",
                       (str(flat / crc), 16, crc, crc * 5, iid))
        db.execute("INSERT INTO items(id,title,system,status) VALUES"
                   "('h','Final Fantasy Amazon (Hack)','nes','VERIFIED'),"
                   "('t','Off Road Challenge (U) [t1]','n64','VERIFIED'),"
                   "('tr','Mother 3 (J) [T+Eng]','gba','VERIFIED'),"
                   "('ok','Super Mario World (USA)','snes','VERIFIED')")
    return db


def test_catver_says_what_an_arcade_set_is():
    assert labels.from_category("Slot Machine / Reels") == {
        "genre:slot-machine": "genre", "fruit-machine": "tag", "gambling": "tag"}
    assert labels.from_category("Tabletop / Mahjong * Mature *") == {
        "genre:tabletop": "genre", "mahjong": "tag", "mature": "tag"}
    assert labels.from_category("Computer / Japanese")["not-a-game"] == "tag"
    assert labels.from_category("Shooter / Run and Gun") == {"genre:shooter": "genre"}


def test_the_dump_name_carries_its_own_tags():
    """GoodTools codes are case-sensitive: [T+Eng] is a translation, [t1] a trainer."""
    assert set(labels.from_name("Mother 3 (J) [T+Eng]")) == {"translation"}
    assert set(labels.from_name("Off Road Challenge (U) [t1]")) == {"trainer"}
    assert set(labels.from_name("Chiyo (J) [hFFE][p1]")) == {"hack", "pirate"}
    assert set(labels.from_name("Sword Master (USA) (Sample)")) == {"demo"}
    assert set(labels.from_name("Hero Quest (U) (Prototype) [!]")) == {"prototype"}
    assert labels.from_name("Demolition Man (USA)") == {}
    assert labels.from_name("Tetris (World) (Rev 1)") == {}


def test_refresh_keeps_the_owners_word(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    labels.refresh(db)
    got = labels.for_items(["i-hot", "i-mslug", "h", "ok"], db)
    assert set(got["i-hot"]) == {"genre:slot-machine", "fruit-machine", "gambling"}
    assert got["i-mslug"] == ["genre:shooter"]
    assert got["h"] == ["hack"] and "ok" not in got
    labels.set_override("i-hot", "fruit-machine", "remove", db)
    labels.set_override("ok", "classic", "add", db)
    labels.refresh(db)                                         # the word survives
    got = labels.for_items(["i-hot", "ok"], db)
    assert "fruit-machine" not in got["i-hot"] and got["ok"] == ["classic"]
    labels.set_override("i-hot", "fruit-machine", None, db)    # cleared: derived again
    assert "fruit-machine" in labels.for_items(["i-hot"], db)["i-hot"]


def test_an_export_leaves_out_fruit_machines_but_not_the_bios_a_game_needs(monkeypatch, tmp_path):
    from romcom.organizer import organize
    db = _env(monkeypatch, tmp_path)
    labels.refresh(db)
    r = organize(tmp_path / "sd", systems=["arcade"], arcade_status="all")
    card = tmp_path / "sd" / "ROMs" / "arcade"
    assert (card / "mslug.zip").exists()
    assert (card / "neogeo.zip").exists()                # not-a-game, but mslug needs it
    assert not (card / "m1hotpot.zip").exists()
    assert r["labels_left_out"] == {"arcade": 2}         # m1hotpot and the neogeo item
    r = organize(tmp_path / "sd2", systems=["arcade"], arcade_status="all", exclude_labels=[])
    assert (tmp_path / "sd2" / "ROMs" / "arcade" / "m1hotpot.zip").exists()


def test_a_romset_leaves_out_fruit_machines_and_keeps_needed_bios(monkeypatch, tmp_path):
    from romcom.organizer import romset
    db = _env(monkeypatch, tmp_path)
    labels.refresh(db)
    dest = tmp_path / "card"
    romset("mame", dest, arcade_status="all")
    assert sorted(p.stem for p in dest.glob("*.zip")) == ["mslug", "neogeo"]
    romset("mame", tmp_path / "card2", arcade_status="all", exclude_labels=["mahjong"])
    assert (tmp_path / "card2" / "m1hotpot.zip").exists()


def test_the_library_filters_by_label(monkeypatch, tmp_path):
    from romcom.web import create_app
    db = _env(monkeypatch, tmp_path)
    labels.refresh(db)
    c = create_app().test_client()
    ids = lambda q: sorted(r["id"] for r in c.get("/api/items?limit=50&" + q).get_json()["items"])
    assert ids("label=hack") == ["h"]
    assert ids("system=arcade&nolabel=fruit-machine,gambling,not-a-game") == ["i-mslug"]
    row = next(r for r in c.get("/api/items?limit=50&system=arcade").get_json()["items"]
               if r["id"] == "i-hot")
    assert "fruit-machine" in row["labels"]
    info = c.get("/api/labels").get_json()
    assert info["counts"]["fruit-machine"] == 1 and "fruit-machine" in info["default_exclude"]
    assert c.post("/api/labels/override", json={"item_id": "i-hot", "label": "fruit-machine",
                                                "action": "remove"}).status_code == 200
    assert ids("label=fruit-machine") == []


def test_gba_public_domain_pile_is_labelled_and_real_games_are_not():
    """GBA's PD pile names itself outside brackets: e-books, slideshows, library samples,
    demos. None of them is a game for a card; Kirby's Dream Land and Demon's Crest are."""
    from romcom.labels import from_name
    assert "not-a-game" in from_name("Lord of the Rings, The by J.R.R Tolkien [ebb]")
    assert "not-a-game" in from_name("3x3 Eyes JPEG Slideshow 31")
    assert "homebrew" in from_name("Battle Picross V1.0 by Sean Reid (PD)")
    assert "beta" in from_name("HEL Library V2.0 Public Beta 18 - Map by Peter Schraut")
    assert "demo" in from_name("GBAGI V1.0 - King's Quest IV Demo")
    for real in ("Kirby's Dream Land (USA)", "Demolition Racer (USA)", "Demon's Crest (USA)",
                 "Final Fantasy V Advance (USA)", "Doom II (USA)"):
        assert from_name(real) == {}, real


def test_emulator_wrapped_gba_builds_and_book_readers():
    from romcom.labels import from_name
    for t in ("PCEAdvance V5.9 - Metal Stoker (J)", "SNESAdvance - Stealth (J)",
              "FC2GBA - 26 Games", "WarioWare Inc. - Selected 4 (J) (MB2GBA)"):
        assert "wrapped" in from_name(t), t
    for t in ("Belinda by Anne Rice [eba]", "ReadBoy V1.0 - SDYX",
              "GoodBook Advance II 1.0 - (Chinese Book 02)"):
        assert "not-a-game" in from_name(t), t
    assert from_name("Goombas Revenge (USA)") == {}


def test_japanese_trial_discs_are_demos():
    from romcom.labels import from_name
    assert "demo" in from_name("Aero Dancing F (Japan) (Taikenban)")
    assert "demo" in from_name("Frame Gride Test Operation Disc")


def test_cheat_cartridges_and_accessories_are_not_games():
    """A card update put 'Action Replay', 'CodeBreaker' and 'GBA AV Adapter' on a card
    beside the games: they are dumps of a device."""
    from romcom.labels import from_name
    for t in ("Action Replay (G) [a1]", "Action Replay GBX (Europe)", "CodeBreaker (USA)",
              "GameShark Pro (USA)", "Game Genie (USA) (Unl)", "GBA AV Adapter (China)",
              "Pro Action Replay (Europe)"):
        assert "not-a-game" in from_name(t), t
    for real in ("Codename - Kids Next Door (USA)", "Game Boy Wars (Japan)",
                 "Replay Racer (USA)"):
        assert "not-a-game" not in from_name(real), real
