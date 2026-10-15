"""Artwork: libretro thumbnails matched by name, cached, and laid out for ES-DE."""
import urllib.parse

from romcom import artwork
from romcom.db import connect

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


class FakeResponse:
    def __init__(self, status, text="", content=b""):
        self.status_code, self.text, self.content = status, text, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeServer:
    """thumbnails.libretro.com: a directory index per folder, and the images in it."""

    def __init__(self, tree):
        self.tree, self.gets = tree, []

    def get(self, url, timeout=None):
        self.gets.append(url)
        path = urllib.parse.unquote(url[len(artwork.BASE) + 1:])
        db_name, kind, name = path.split("/", 2)
        names = self.tree.get((db_name, kind))
        if names is None:
            return FakeResponse(404)
        if not name:
            links = "".join(f'<a href="{urllib.parse.quote(n)}.png">{n}.png</a>' for n in names)
            return FakeResponse(200, text=f'<a href="?C=N;O=D">Name</a><a href="/x/">Parent</a>{links}')
        return FakeResponse(200, content=PNG) if name[:-4] in names else FakeResponse(404)


def _env(monkeypatch, tmp_path, tree):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "a.db"))
    from romcom.config import invalidate
    invalidate()
    monkeypatch.setattr(artwork, "CACHE", tmp_path / "cache")
    server = FakeServer(tree)
    monkeypatch.setattr(artwork, "_session", lambda: server)
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system,status,external_id) VALUES"
                   "('smw','Super Mario World','snes','VERIFIED','snes/smw'),"
                   "('ct','Chrono Trigger','snes','VERIFIED','snes/ct'),"
                   "('ms','Metal Slug - Super Vehicle-001','arcade','VERIFIED','arcade/mslug'),"
                   "('sf2','Street Fighter II: The World Warrior (World 910522)','arcade',"
                   "'VERIFIED','arcade/sf2'),"
                   "('gone','Nothing Anywhere','snes','VERIFIED','snes/gone'),"
                   "('wish','Not In Hand','snes','CATALOGED','snes/wish')")
        for iid, path in (("smw", "D:/roms/Super Mario World (USA).sfc"),
                          ("ct", "D:/roms/Chrono Trigger (USA) (Rev 1).sfc"),
                          ("gone", "D:/roms/Nothing Anywhere (USA).sfc")):
            db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,1,?,1)",
                       (path, iid))
    return db, server


SNES = "Nintendo - Super Nintendo Entertainment System"
TREE = {
    (SNES, "Named_Boxarts"): ["Super Mario World (USA)", "Super Mario World (Japan)",
                              "Chrono Trigger (Japan)", "Chrono Trigger (USA)"],
    (SNES, "Named_Snaps"): ["Super Mario World (USA)"],
    ("MAME", "Named_Boxarts"): [],
    ("MAME", "Named_Snaps"): ["Metal Slug - Super Vehicle-001",
                              "Street Fighter II_ The World Warrior (World 910522)"],
}


def test_names_match_exactly_then_without_region_tags():
    ix = artwork.Index(["Chrono Trigger (Japan)", "Chrono Trigger (USA)",
                        "Street Fighter II_ The World Warrior (World 910522)"])
    assert ix.match("Chrono Trigger (USA) (Rev 1)") == "Chrono Trigger (USA)"   # region kept
    assert ix.match("Chrono Trigger (Japan) (Rev A)") == "Chrono Trigger (Japan)"
    assert ix.match("Street Fighter II: The World Warrior (World 910522)") \
        == "Street Fighter II_ The World Warrior (World 910522)"               # libretro's _
    assert ix.match("Secret of Mana (USA)") is None
    assert artwork.Index(["Ninja, The (USA, Europe)"]).match("The Ninja (Euro, USA)")         == "Ninja, The (USA, Europe)"                                          # article moved


def test_fetch_caches_what_matches_and_records_the_misses(monkeypatch, tmp_path):
    db, server = _env(monkeypatch, tmp_path, TREE)
    r = artwork.fetch(db=db, workers=2)
    have = {(x["item_id"], x["kind"]): x["path"] for x in db.execute("SELECT * FROM artwork")}
    assert have[("smw", "boxart")].endswith("Super Mario World (USA).png")
    assert have[("ct", "boxart")].endswith("Chrono Trigger (USA).png")
    assert have[("ms", "snap")] and have[("sf2", "snap")]
    assert have[("gone", "boxart")] is None                     # a checked miss
    assert ("wish", "boxart") not in have                       # not in hand: never asked
    snes = next(s for s in r["systems"] if s["system"] == "snes")
    assert (snes["games"], snes["boxart"], snes["snap"]) == (3, 2, 1)
    # A second run asks the server nothing: hits are cached, misses wait a week.
    server.gets.clear()
    artwork.fetch(db=db, workers=2)
    assert server.gets == []


def test_export_lays_media_out_the_way_es_de_reads_it(monkeypatch, tmp_path):
    db, _ = _env(monkeypatch, tmp_path, TREE)
    artwork.fetch(db=db, workers=2)
    card = tmp_path / "card"
    (card / "snes" / "90").mkdir(parents=True)
    (card / "snes" / "90" / "Super Mario World (USA).sfc").write_bytes(b"rom")
    (card / "arcade" / "mslug").mkdir(parents=True)
    (card / "arcade" / "mslug.zip").write_bytes(b"zip")
    (card / "arcade" / "mslug" / "x.chd").write_bytes(b"chd")       # not a game
    (card / "system").mkdir()
    (card / "system" / "bios.bin").write_bytes(b"b")
    r = artwork.export_media(card, db=db)
    media = card / "ES-DE" / "downloaded_media"
    assert (media / "snes" / "covers" / "90" / "Super Mario World (USA).png").read_bytes() == PNG
    assert (media / "snes" / "screenshots" / "90" / "Super Mario World (USA).png").exists()
    assert (media / "arcade" / "screenshots" / "mslug.png").exists()
    assert not (media / "arcade" / "covers").exists()          # MAME had no box art for it
    assert (r["games"], r["written"], r["without_art"]) == (2, 3, 0)
    assert artwork.export_media(card, db=db)["skipped"] == 3     # a re-run tops up only


JPG = b"\xff\xd8\xff" + b"j" * 32


class FakeADB:
    """ArcadeDB's image links: each answers with an image."""

    def __init__(self):
        self.gets = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        return FakeResponse(200, content=JPG if "type=flyer" in url else PNG)


def test_arcade_sets_get_art_from_arcadedb_by_set_name(monkeypatch, tmp_path):
    """libretro names arcade art after a MAME description — sf2 below has no box art
    there. ArcadeDB answers by the set name itself: the flyer, the snap and the marquee
    every set it lists, and a card gets each under its own extension."""
    from romcom import arcadedb
    db, _ = _env(monkeypatch, tmp_path, TREE)
    monkeypatch.setattr(arcadedb, "CACHE", tmp_path / "adb")
    adb = FakeADB()
    monkeypatch.setattr(arcadedb, "_session", lambda: adb)
    monkeypatch.setattr(arcadedb, "_shrink_flyer", lambda data: (data, ".jpg"))
    links = {f"url_image_{t}": f"https://adb/?mame=sf2&type={t}&resize=0"
             for t in ("flyer", "ingame", "marquee")}
    monkeypatch.setattr(arcadedb, "lookup",
                        lambda sets, session=None, stop=None:
                        {s: (links if s == "sf2" else {}) for s in sets})
    artwork.fetch(db=db, workers=2)
    have = {(x["item_id"], x["kind"]): x for x in db.execute("SELECT * FROM artwork")}
    assert have[("sf2", "boxart")]["path"].endswith("sf2.jpg")
    assert have[("sf2", "marquee")]["source"] == arcadedb.SOURCE
    assert have[("ms", "snap")]["source"] == "MAME"          # ArcadeDB had nothing: libretro
    assert have[("ms", "marquee")]["path"] is None
    card = tmp_path / "card"
    (card / "arcade").mkdir(parents=True)
    (card / "arcade" / "sf2.zip").write_bytes(b"zip")
    artwork.export_media(card, db=db)
    media = card / "ES-DE" / "downloaded_media" / "arcade"
    assert (media / "covers" / "sf2.jpg").read_bytes() == JPG
    assert (media / "marquees" / "sf2.png").exists()
    # A libretro miss from before ArcadeDB was asked is asked again, once.
    adb.gets.clear()
    artwork.fetch(db=db, workers=2)
    assert adb.gets == []


def test_arcadedb_facts_become_gamelist_fields():
    from romcom import arcadedb
    f = arcadedb.gamelist_fields({
        "manufacturer": "Konami", "year": "1996", "players": 1,
        "history": "Arcade Video game published 30 years ago:\r\n\r\nGTI Club is a racer."
                   "\r\n\r\n- TECHNICAL -\r\n\r\nCPU: PowerPC"})
    assert f == {"desc": "GTI Club is a racer.", "developer": "Konami",
                 "publisher": "Konami", "releasedate": "19960101T000000", "players": "1"}
    assert arcadedb.gamelist_fields({}) == {}


def test_a_set_without_a_flyer_gets_its_title_screen_as_cover(monkeypatch, tmp_path):
    from romcom import arcadedb
    monkeypatch.setattr(arcadedb, "CACHE", tmp_path / "adb")
    adb = FakeADB()
    monkeypatch.setattr(arcadedb, "_shrink_flyer", lambda data: (data, ".png"))
    p = arcadedb.image("x", "boxart", adb, {"url_image_title": "https://adb/?mame=x&type=title"})
    assert p and p.name == "x.png" and adb.gets == ["https://adb/?mame=x&type=title"]
    assert arcadedb.image("y", "boxart", adb, {}) is None and len(adb.gets) == 1
