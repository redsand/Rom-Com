from pathlib import Path

import pytest

from romcom import prune
from romcom.db import connect


def seed(monkeypatch, tmp_path, items, files=()):
    """items: (id, title, system, kwargs); files: (path-under-tmp, id, bytes)."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.chdir(tmp_path)
    db = connect()
    with db:
        for iid, title, system, kw in items:
            db.execute(
                "INSERT INTO items(id,title,system,authorized,wanted,status,keep,catalog_source,is_device)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (iid, title, system, kw.get("authorized", 0), kw.get("wanted", 1),
                 kw.get("status", "VERIFIED"), kw.get("keep", 0),
                 kw.get("catalog_source", "dat"), kw.get("is_device", 0)))
        for rel, iid, size in files:
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * size)
            db.execute("INSERT INTO files(path,bytes,sha1,matched_item_id,match_method,content)"
                       " VALUES(?,?,?,?,?,1)",
                       (str(p), size, f"sha-{iid}", iid, "hash"))
    return db


def test_junk_is_removed_even_as_the_only_copy(monkeypatch, tmp_path):
    """Bad dumps and homebrew are not variants of anything — they go even when
    they are the sole entry, because a card slot for them was never the point."""
    db = seed(monkeypatch, tmp_path, [
        ("j1", "Super Metroid (U)", "snes", {}),
        ("j2", "Super Metroid (U) [b1]", "snes", {}),
        ("j3", "PocketNES v9.98 - Super Mario Bros. (PD)", "gba", {}),
        ("j4", "Bananas (PD)", "gba", {}),
        ("j5", "Goomba v2.2 - Zelda (PD)", "gba", {}),
    ])
    r = prune.audit(db, ["snes", "gba"])
    assert r["junk"] == 4 and r["variants"] == 0
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 5   # audit only
    out = prune.cleanup(apply=True, systems=["snes", "gba"], db=db)
    assert out["deleted"] == 4
    left = {t[0] for t in db.execute("SELECT title FROM items").fetchall()}
    assert left == {"Super Metroid (U)"}


def test_variants_collapse_to_the_best_dump(monkeypatch, tmp_path):
    """Regions, revisions and trainers of one game compete; the English
    unmodified dump wins and the rest are folded into it."""
    db = seed(monkeypatch, tmp_path, [
        ("v1", "Pokemon - Ruby (U)", "gba", {}),
        ("v2", "Pokemon - Ruby (E)", "gba", {}),
        ("v3", "Pokemon - Ruby (J)", "gba", {}),
        ("v4", "Pokemon - Ruby (U)(t+1)(Inferi)", "gba", {}),
        ("v5", "Pokemon - Ruby (U) [hI]", "gba", {}),
    ], files=[("r/v1.gba", "v1", 10), ("r/v2.gba", "v2", 10), ("r/v4.gba", "v4", 10)])
    out = prune.cleanup(apply=True, systems=["gba"], db=db)
    assert out["deleted"] == 4
    left = {t[0] for t in db.execute("SELECT title FROM items").fetchall()}
    assert left == {"Pokemon - Ruby (U)"}
    ev = db.execute("SELECT event, detail FROM events").fetchall()
    assert all(e[0] == "prune-removed" for e in ev)
    assert any("variant" in e[1] and "Pokemon - Ruby (U)" in e[1] for e in ev)
    assert not any(Path(f).exists() for f in [tmp_path / "r/v2.gba", tmp_path / "r/v4.gba"])
    assert (tmp_path / "r/v1.gba").exists()          # the keeper's file survives


def test_a_download_beats_a_better_empty_row(monkeypatch, tmp_path):
    """Never delete a held file to keep an empty catalog row: the file-holding
    (E) variant survives, the cleaner catalog-only (U) row is the one folded."""
    db = seed(monkeypatch, tmp_path, [
        ("c1", "Megaman Zero (U)", "gba", {}),        # better region, no file
        ("c2", "Megaman Zero (E)", "gba", {}),
    ], files=[("m/c2.gba", "c2", 10)])
    out = prune.cleanup(apply=True, systems=["gba"], db=db)
    assert out["deleted"] == 1
    left = {t[0]: t[0] for t in db.execute("SELECT id, title FROM items").fetchall()}
    assert list(left) == ["c2"]
    assert (tmp_path / "m/c2.gba").exists()


def test_a_sole_variant_is_its_own_keeper(monkeypatch, tmp_path):
    """A Japan-only game with a fan translation has no English rival — the
    translated dump is the best there is, and it stays."""
    db = seed(monkeypatch, tmp_path, [
        ("s1", "Densetsu no Stafy (Japan) [T-En by Pablitox v0.1]", "gba", {}),
        ("s2", "Bokura no Taiyou (J)", "gba", {}),
    ])
    assert prune.audit(db, ["gba"])["items"] == 0


def test_translation_beats_the_raw_japanese_dump(monkeypatch, tmp_path):
    db = seed(monkeypatch, tmp_path, [
        ("t1", "F-Zero - Climax (Japan) [T-En by Normmatt v1.0]", "gba", {}),
        ("t2", "F-Zero - Climax (J)", "gba", {}),
    ], files=[("f/t1.gba", "t1", 10), ("f/t2.gba", "t2", 10)])
    out = prune.cleanup(apply=True, systems=["gba"], db=db)
    assert out["deleted"] == 1
    assert db.execute("SELECT id FROM items").fetchone()[0] == "t1"


def test_never_touched(monkeypatch, tmp_path):
    """Keep-marked and excluded rows, devices, and arcade's deliberate
    parent/clone structure are out of bounds."""
    db = seed(monkeypatch, tmp_path, [
        ("n2", "Super Metroid (U) [b1]", "snes", {"keep": 1}),
        ("n3", "Super Metroid (U) [b1]", "snes", {"status": "EXCLUDED"}),
        ("n4", "Thing (J)", "snes", {"is_device": 1}),
        ("a1", "Galaga (bootleg set 1)", "arcade", {}),
        ("a2", "Galaga", "arcade", {}),
    ])
    assert prune.audit(db)["items"] == 0


def test_local_adoptions_are_in_scope(monkeypatch, tmp_path):
    """The fullset rips the scanner adopted as `local` items are where most of
    the junk lives — the dump codes in their names are the evidence."""
    db = seed(monkeypatch, tmp_path, [
        ("l1", "PocketNES v9.98 - Super Mario Bros. (PD)", "gba",
         {"catalog_source": "local"}),
    ], files=[("l/l1.gba", "l1", 10)])
    out = prune.cleanup(apply=True, systems=["gba"], db=db)
    assert out["deleted"] == 1 and not (tmp_path / "l/l1.gba").exists()


def test_discs_are_not_variants(monkeypatch, tmp_path):
    """(Disc 1) and (Disc 2) are identity, not decoration — a fold here would
    delete the rest of a multi-disc game."""
    db = seed(monkeypatch, tmp_path, [
        ("d1", "Chrono Cross (USA) (Disc 1)", "ps1", {}),
        ("d2", "Chrono Cross (USA) (Disc 2)", "ps1", {}),
    ])
    assert prune.audit(db)["items"] == 0


def test_hack_names_are_not_decoration(monkeypatch, tmp_path):
    """Romhack titles carry their identity in parens and brackets — (God Mode)
    and [2020-09-17] distinguish different fan games of the same base."""
    db = seed(monkeypatch, tmp_path, [
        ("h1", "3 Emeralds, The (God Mode) by Mawwo7 (MaiK) [2020-09-17] (SMW Hack)",
         "romhacks", {}),
        ("h2", "3 Emeralds, The by Mawwo7 (MaiK) [2009-04-15] (SMW Hack)",
         "romhacks", {}),
    ])
    assert prune.audit(db)["items"] == 0


def test_the_newest_translation_of_a_japan_only_game_wins(monkeypatch, tmp_path):
    """GoodN64's Final Fantasy 2 group: every dump is a mapper hack of the
    Japanese rom, so the keeper is decided by patch — the raw hack must not
    win, and a newer patch beats an older one."""
    db = seed(monkeypatch, tmp_path, [
        ("k1", "Final Fantasy 2 (J) [hM02]", "nes", {}),
        ("k2", "Final Fantasy 2 (J) [hM02][T-Eng0.99]", "nes", {}),
        ("k3", "Final Fantasy 2 (J) [hM02][T-Eng1.02]", "nes", {}),
        ("k4", "Final Fantasy 3 (J) [T-Eng0.46]", "nes", {}),
        ("k5", "Final Fantasy 3 (J) [T-Eng1.1]", "nes", {}),
    ])
    out = prune.cleanup(apply=True, systems=["nes"], db=db)
    assert out["deleted"] == 3
    left = {t[0] for t in db.execute("SELECT title FROM items").fetchall()}
    assert left == {"Final Fantasy 2 (J) [hM02][T-Eng1.02]",
                    "Final Fantasy 3 (J) [T-Eng1.1]"}


def test_a_filenames_extension_folds_away(monkeypatch, tmp_path):
    """A local adoption titled by its filename (1080 Snowboarding.z64) is the
    same game as the catalog's (USA) row — the extension must not keep them
    in separate groups."""
    db = seed(monkeypatch, tmp_path, [
        ("e1", "1080 Snowboarding.z64", "n64", {"catalog_source": "local"}),
        ("e2", "1080 Snowboarding (USA)", "n64", {}),
    ], files=[("e/e1.z64", "e1", 10)])
    out = prune.cleanup(apply=True, systems=["n64"], db=db)
    assert out["deleted"] == 1
    assert db.execute("SELECT title FROM items").fetchone()[0] == "1080 Snowboarding.z64"
    assert (tmp_path / "e/e1.z64").exists()      # the download survives


def test_per_region_keeps_one_per_region(monkeypatch, tmp_path):
    """Official mode: every regional retail release survives — one keeper per
    region, not one per game."""
    db = seed(monkeypatch, tmp_path, [
        ("r1", "FIFA 15 (Europe)", "vita", {}),
        ("r2", "FIFA 15 (France)", "vita", {}),
        ("r3", "FIFA 15 (Germany)", "vita", {}),
        ("r4", "FIFA 15 (Japan)", "vita", {}),
        ("r5", "FIFA 15 (USA)", "vita", {}),
    ])
    out = prune.cleanup(apply=True, systems=["vita"], per_region=True, db=db)
    assert out["deleted"] == 0 and db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 5


def test_per_region_keeps_the_latest_revision(monkeypatch, tmp_path):
    """Within a region, the newest revision wins and the old ones fold away."""
    db = seed(monkeypatch, tmp_path, [
        ("v1", "Spider-Man (USA) (v1.00)", "ps2", {}),
        ("v2", "Spider-Man (USA) (v2.01)", "ps2", {}),
        ("v3", "Spider-Man (USA)", "ps2", {}),
        ("v4", "Spider-Man (Europe) (Rev 1)", "ps2", {}),
        ("v5", "Spider-Man (Europe)", "ps2", {}),
    ])
    out = prune.cleanup(apply=True, systems=["ps2"], per_region=True, db=db)
    assert out["deleted"] == 3
    left = {t[0] for t in db.execute("SELECT title FROM items").fetchall()}
    assert left == {"Spider-Man (USA) (v2.01)", "Spider-Man (Europe) (Rev 1)"}


def test_per_region_drops_betas_when_retail_exists(monkeypatch, tmp_path):
    """A region keeps its retail release; the beta of the same region goes.
    A region whose only entry is a beta keeps it — there is nothing official
    to prefer."""
    db = seed(monkeypatch, tmp_path, [
        ("b1", "Eternal Champions (Europe)", "genesis", {}),
        ("b2", "Eternal Champions (Europe) (Beta)", "genesis", {}),
        ("b3", "Secret of Evermore (USA) (Beta)", "snes", {}),
    ])
    out = prune.cleanup(apply=True, systems=["genesis", "snes"],
                        per_region=True, db=db)
    assert out["deleted"] == 1
    left = {t[0] for t in db.execute("SELECT title FROM items").fetchall()}
    assert "Eternal Champions (Europe) (Beta)" not in left
    assert "Secret of Evermore (USA) (Beta)" in left


def test_per_region_prefers_the_english_language(monkeypatch, tmp_path):
    """Same region, same revision, different languages: the English dump wins."""
    db = seed(monkeypatch, tmp_path, [
        ("l1", "Cave Story - Doukutsu Monogatari (World) (De) (v0.8.8)", "genesis", {}),
        ("l2", "Cave Story - Doukutsu Monogatari (World) (En) (v0.8.8)", "genesis", {}),
        ("l3", "Cave Story - Doukutsu Monogatari (World) (Es) (v0.8.8)", "genesis", {}),
    ])
    out = prune.cleanup(apply=True, systems=["genesis"], per_region=True, db=db)
    assert out["deleted"] == 2
    assert db.execute("SELECT title FROM items").fetchone()[0] == \
        "Cave Story - Doukutsu Monogatari (World) (En) (v0.8.8)"


def test_per_region_keeps_the_latest_hack_version(monkeypatch, tmp_path):
    """Romhack release chains are versions of one hack: keep the newest, and
    the version numbers fold into the same group."""
    db = seed(monkeypatch, tmp_path, [
        ("h1", "Mix 5 - Test (V8.1) by VIP (SMW Hack)", "romhacks", {}),
        ("h2", "Mix 5 - Test (V9.2) by VIP (SMW Hack)", "romhacks", {}),
        ("h3", "Mix 5 - Test (V9.3) by VIP (SMW Hack)", "romhacks", {}),
    ])
    out = prune.cleanup(apply=True, systems=["romhacks"], per_region=True, db=db)
    assert out["deleted"] == 2
    assert db.execute("SELECT title FROM items").fetchone()[0] == \
        "Mix 5 - Test (V9.3) by VIP (SMW Hack)"


def test_per_region_folds_a_demo_into_its_retail(monkeypatch, tmp_path):
    """DOS catalog titles carry '(demo)' as part of the name — it is
    pre-release, not identity, so the demo folds away when the retail game
    is cataloged."""
    db = seed(monkeypatch, tmp_path, [
        ("m1", "Driller (demo) (1988)(Incentive Software) [Action]", "dos", {}),
        ("m2", "Driller (1988)(Incentive Software) [Action]", "dos", {}),
    ])
    out = prune.cleanup(apply=True, systems=["dos"], per_region=True, db=db)
    assert out["deleted"] == 1
    assert db.execute("SELECT title FROM items").fetchone()[0] == \
        "Driller (1988)(Incentive Software) [Action]"


def test_apply_requires_an_explicit_scope(monkeypatch, tmp_path):
    db = seed(monkeypatch, tmp_path, [("x", "Thing (PD)", "gba", {})])
    with pytest.raises(ValueError):
        prune.cleanup(apply=True, db=db)
    assert prune.cleanup(apply=True, systems=["gba"], db=db)["deleted"] == 1


def test_apply_writes_a_reversible_manifest(monkeypatch, tmp_path):
    db = seed(monkeypatch, tmp_path, [
        ("m1", "Kirby (U)", "gba", {}),
        ("m2", "Kirby (J)", "gba", {}),
        ("m3", "Kirby (U) [b]", "gba", {}),
    ], files=[("k/m1.gba", "m1", 16), ("k/m3.gba", "m3", 16)])
    out = prune.cleanup(apply=True, systems=["gba"], db=db)
    m = tmp_path / out["manifest"]
    assert m.exists()
    import json
    data = json.loads(m.read_text(encoding="utf-8"))
    by_id = {x["id"]: x for x in data["removed"]}
    assert set(by_id) == {"m2", "m3"}
    assert by_id["m3"]["reason"] == "bad dump"
    assert by_id["m2"]["reason"] == "variant" and by_id["m2"]["kept"]["id"] == "m1"
    assert by_id["m3"]["item"]["system"] == "gba"            # the full catalog row
    assert by_id["m3"]["files"][0]["sha1"] == "sha-m3"


def test_gba_numbering_and_groups_fold_away(monkeypatch, tmp_path):
    """GoodGBA's release numbers, trainer groups and ripper credits are all
    decoration on the same game — the base key must see through them."""
    db = seed(monkeypatch, tmp_path, [
        ("g1", "0039 - Army Men Advance (UE)(M5)(t+1)(Inferi)", "gba", {}),
        ("g2", "0039 - Army Men Advance (UE)(M5)(t+1)(+Source)(Rocco)", "gba", {}),
        ("g3", "Army Men Advance (U)", "gba", {}),
    ])
    out = prune.cleanup(apply=True, systems=["gba"], db=db)
    assert out["deleted"] == 2
    assert db.execute("SELECT title FROM items").fetchone()[0] == "Army Men Advance (U)"