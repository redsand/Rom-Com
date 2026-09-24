"""Which core each system needs, and keeping that answer in one place."""
from pathlib import Path

import pytest

from romcom import cores
from romcom.config import load_yaml
from romcom.db import connect


def test_emulators_yaml_names_the_core_the_registry_recommends():
    """The two must not drift, and drifting is silent until a game refuses to launch.

    emulators.yaml said `melonds_libretro.dll` while the installer fetched
    `melondsds_libretro.dll` — the buildbot's actual name — so every one of 462 DS games
    would have failed with a missing core that had in fact just been downloaded.
    """
    cfg = (load_yaml("emulators.yaml") or {}).get("emulators") or {}
    wrong = []
    for system, template in cfg.items():
        spec = cores.CORES.get(system)
        if not spec:
            continue
        named = [Path(t).name for t in str(template).replace('"', " ").split()
                 if t.lower().endswith(".dll")]
        if named and named[0] != f"{spec[0]}.dll":
            wrong.append((system, named[0], f"{spec[0]}.dll"))
    assert not wrong, f"emulators.yaml disagrees with cores.CORES: {wrong}"


def test_every_recommendation_explains_itself():
    """A bare core name is the thing that sends people hunting; the reason is the product."""
    for system, (core, name, why, confidence, _alt) in cores.CORES.items():
        assert core and core.endswith("_libretro"), system
        assert name and why, system
        assert confidence in ("certain", "strong", "depends"), (system, confidence)


def test_a_tradeoff_always_names_the_alternative():
    """`depends` means the default may be wrong for you — useless without the other option."""
    for system, (_c, _n, _w, confidence, alt) in cores.CORES.items():
        if confidence == "depends":
            assert alt, f"{system} is a tradeoff but names no alternative"


def test_status_sorts_by_what_it_unlocks(monkeypatch, tmp_path):
    """Download order should follow impact, not the alphabet."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "c.db"))
    from romcom.config import invalidate
    invalidate()
    monkeypatch.setattr(cores, "cores_dir", lambda: tmp_path / "cores")
    db = connect()
    with db:
        for i, (system, n) in enumerate([("nes", 3), ("gba", 50), ("snes", 10)]):
            for k in range(n):
                iid = f"{system}{k}"
                db.execute("INSERT INTO items(id,title,system,status) VALUES(?,?,?,'VERIFIED')",
                           (iid, iid, system))
                db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,1,?)",
                           (str(tmp_path / f"{iid}.rom"), iid))
    st = cores.status(db)
    assert [r["system"] for r in st["rows"]] == ["gba", "snes", "nes"]
    assert st["missing_count"] == 3 and st["games_blocked"] == 63


def test_an_installed_core_is_not_reported_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "c2.db"))
    from romcom.config import invalidate
    invalidate()
    cdir = tmp_path / "cores"; cdir.mkdir()
    (cdir / "mgba_libretro.dll").write_bytes(b"x")
    monkeypatch.setattr(cores, "cores_dir", lambda: cdir)
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('g','G','gba','VERIFIED')")
        db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,1,'g')",
                   (str(tmp_path / "g.gba"),))
    st = cores.status(db)
    assert st["missing_count"] == 0
    assert st["rows"][0]["installed"] is True


def test_arcade_is_not_a_libretro_core(monkeypatch, tmp_path):
    """MAME runs arcade directly; listing a core to download for it would be a wild goose."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "c3.db"))
    from romcom.config import invalidate
    invalidate()
    monkeypatch.setattr(cores, "cores_dir", lambda: tmp_path / "cores")
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('a','A','arcade','VERIFIED')")
        db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,1,'a')",
                   (str(tmp_path / "a.zip"),))
    row = cores.status(db)["rows"][0]
    assert row["system"] == "arcade" and row["core"] is None and row["installed"] is True


def test_one_core_serving_several_systems_downloads_once(monkeypatch, tmp_path):
    """Genesis Plus GX covers four systems; fetching it four times is just slower."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "c4.db"))
    from romcom.config import invalidate
    invalidate()
    cdir = tmp_path / "cores"
    monkeypatch.setattr(cores, "cores_dir", lambda: cdir)
    db = connect()
    with db:
        for system in ("genesis", "gamegear", "mastersystem", "segacd"):
            db.execute("INSERT INTO items(id,title,system,status) VALUES(?,?,?,'VERIFIED')",
                       (system, system, system))
            db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,1,?)",
                       (str(tmp_path / f"{system}.bin"), system))
    fetched = []
    def fake_open(url, timeout=0):
        fetched.append(url)
        raise RuntimeError("no network in tests")
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    r = cores.install(db=db, dest=cdir)
    assert len(fetched) == 1, fetched
    assert "genesis_plus_gx_libretro" in fetched[0]
    assert r["installed"] == 0 and len(r["failed"]) == 1
