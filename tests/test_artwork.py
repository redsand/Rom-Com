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
