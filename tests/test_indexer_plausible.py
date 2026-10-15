"""A website result must be the same game: the names that fooled it, both ways."""
import pytest

from romcom.indexer import plausible


@pytest.mark.parametrize("title,cand,ok", [
    ("Taxi 2 - Le Jeu (France)", "TAXI.2.LE.JEU.FRENCH-SPHYNX.7z", True),
    ("Tomb Raider Chronicles - La Leggenda di Lara Croft (Italy)", "Tomb Raider Chronicles (I).zip", True),
    ("Castlevania - Order Of Ecclesia (USA)", "Castlevania - Symphony of the Night (USA).chd", False),
    ("Ace Attorney Investigations - Miles Edgeworth (Europe)",
     "Ace Attorney Investigations 2 [Translated].rar", False),
    ("Skies of Arcadia (USA) (Disc 1)", "Killer7 (USA) (Disc 1) (Rev 1).rvz", False),
])
def test_scene_names_and_dropped_subtitles(title, cand, ok):
    assert plausible(title, "dreamcast", cand) is ok
