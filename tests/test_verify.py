"""Is what arrived actually a game for the system we asked about?"""
import zipfile

import pytest

from romcom import verify


def test_a_music_album_is_rejected(tmp_path):
    """The failure this exists for. A Nintendo DS title search returned an album; the item
    went DOWNLOADED, the tracks were adopted as games, and the first anyone knew was melonDS
    answering "ROM isn't valid, did you select the right file?"."""
    d = tmp_path / "Wolf - Edge Of The World"
    d.mkdir()
    for n in ("01 Medicine Man.mp3", "album.sfv", "listing.m3u", "cover.jpg"):
        (d / n).write_bytes(b"x")
    ok, why = verify.check_download(d, "nds")
    assert ok is False
    assert "nds" in why and ".mp3" in why


def test_a_real_rom_passes(tmp_path):
    rom = tmp_path / "Mario Kart DS (USA).nds"
    rom.write_bytes(b"x" * 64)
    assert verify.check_download(rom, "nds") == (True, None)


def test_an_archive_is_judged_by_its_contents_not_its_extension(tmp_path):
    """A .zip says nothing about what is inside, and inside is the whole question."""
    good = tmp_path / "game.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("Mario Kart DS (USA).nds", "x")
    assert verify.check_download(good, "nds")[0] is True

    bad = tmp_path / "album.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("01 - track.mp3", "x")
        z.writestr("folder.jpg", "x")
    ok, why = verify.check_download(bad, "nds")
    assert ok is False and ".mp3" in why


def test_a_rom_wearing_a_decoy_extension_still_passes(tmp_path):
    """`Stargate.smc.ttf` is a real 2 MB cartridge with a font suffix. Judging on the outer
    extension alone would reject genuine downloads."""
    rom = tmp_path / "Stargate.smc.ttf"
    rom.write_bytes(b"x" * 64)
    assert verify.check_download(rom, "snes")[0] is True


def test_a_rom_for_a_different_system_is_rejected(tmp_path):
    """Accepting this is how nes files ended up filed under arcade."""
    rom = tmp_path / "Donkey Kong (JU).nes"
    rom.write_bytes(b"x" * 64)
    assert verify.check_download(rom, "arcade")[0] is False


def test_one_real_rom_rescues_a_mixed_bundle(tmp_path):
    """Sources routinely ship a readme and cover art beside the game; rejecting the lot
    because a .txt is present would throw away working downloads."""
    d = tmp_path / "bundle"
    d.mkdir()
    (d / "readme.txt").write_bytes(b"x")
    (d / "cover.jpg").write_bytes(b"x")
    (d / "Chrono Trigger.smc").write_bytes(b"x")
    assert verify.check_download(d, "snes")[0] is True


def test_an_empty_directory_is_not_a_download(tmp_path):
    d = tmp_path / "nothing"
    d.mkdir()
    assert verify.check_download(d, "snes") == (False, "downloaded directory is empty")


@pytest.mark.parametrize("title,skipped", [
    ("Wolf - Edge Of The World (2011) FLAC", True),
    ("Some Album [320kbps] Discography", True),
    ("Movie.2011.1080p.BluRay.x264", True),
    ("Mario Kart DS (USA)", False),
    ("Street Fighter II - The World Warrior", False),
])
def test_obvious_non_games_are_skipped_before_downloading(title, skipped):
    """Conservative on purpose: a false positive here means a game silently never downloads,
    which is worse than the occasional wasted fetch."""
    assert bool(verify.result_smells_wrong({"title": title})) is skipped


def test_the_acquirer_skips_a_smelly_result():
    from romcom.acquirer import _pick
    results = [{"url": "http://x/album", "title": "Wolf - Edge Of The World FLAC", "score": 99},
               {"url": "http://x/game", "title": "Mario Kart DS (USA)", "score": 50}]
    assert _pick(results, min_score=1)["url"] == "http://x/game"


def test_unrecognised_extensions_are_not_evidence_against_a_download(tmp_path):
    """Arcade sets are chip images — .ic27, .u5, no extension at all. Treating unknown as
    wrong would refuse most of what this library actually holds."""
    d = tmp_path / "galaga"
    d.mkdir()
    for n in ("gg1_1b.3p", "prom-2.5c", "54xx.bin"):
        (d / n).write_bytes(b"x")
    assert verify.check_download(d, "arcade")[0] is True


def test_art_and_readmes_alone_are_still_rejected(tmp_path):
    """The album case arrived as mp3 plus cover art; art on its own is not a game either."""
    d = tmp_path / "scans"
    d.mkdir()
    for n in ("cover.jpg", "back.png", "readme.txt"):
        (d / n).write_bytes(b"x")
    ok, why = verify.check_download(d, "snes")
    assert ok is False and "snes" in why
