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
