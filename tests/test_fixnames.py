"""Giving a rom back its real extension, without risking the archive."""
from romcom import fixnames
from romcom.db import connect


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "f.db"))
    from romcom.config import invalidate
    invalidate()
    monkeypatch.chdir(tmp_path)
    return connect()


def _rom(db, tmp_path, name, system="snes", iid="i1"):
    p = tmp_path / name
    p.write_bytes(b"x" * 32)
    with db:
        db.execute("INSERT OR IGNORE INTO items(id,title,system,status) VALUES(?,?,?,'VERIFIED')",
                   (iid, iid, system))
        db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,32,?)", (str(p), iid))
    return p


def test_a_decoy_suffix_is_stripped(monkeypatch, tmp_path):
    """`Stargate.smc.wmf` is a real 2 MB cartridge wearing a font extension, and everything
    that reads a rom by its visible extension refuses it."""
    db = _db(monkeypatch, tmp_path)
    src = _rom(db, tmp_path, "Stargate.smc.wmf")
    r = fixnames.fix(apply=True, db=db)
    assert r["renamed"] == 1
    assert (tmp_path / "Stargate.smc").exists() and not src.exists()


def test_the_catalog_follows_the_file(monkeypatch, tmp_path):
    """A rename that leaves the scan table pointing at the old path turns a working game into
    a missing one."""
    db = _db(monkeypatch, tmp_path)
    _rom(db, tmp_path, "Stargate.smc.wmf")
    fixnames.fix(apply=True, db=db)
    path = db.execute("SELECT path FROM files").fetchone()["path"]
    assert path.endswith("Stargate.smc")


def test_an_existing_clean_name_is_never_overwritten(monkeypatch, tmp_path):
    """The owner already has the good copy, and the two may not be identical."""
    db = _db(monkeypatch, tmp_path)
    (tmp_path / "Stargate.smc").write_bytes(b"ORIGINAL")
    _rom(db, tmp_path, "Stargate.smc.wmf")
    r = fixnames.fix(apply=True, db=db)
    assert r["renamed"] == 0 and r["blocked_clean_name_exists"] == 1
    assert (tmp_path / "Stargate.smc").read_bytes() == b"ORIGINAL"


def test_a_hidden_extension_for_another_system_is_left_alone(monkeypatch, tmp_path):
    """`Zelda.nes.wmf` filed under snes is a categorisation problem, not a naming one, and
    renaming it would bury the evidence."""
    db = _db(monkeypatch, tmp_path)
    _rom(db, tmp_path, "Zelda.nes.wmf", system="snes")
    assert fixnames.fix(apply=True, db=db)["renamed"] == 0


def test_an_ordinary_name_is_not_touched(monkeypatch, tmp_path):
    db = _db(monkeypatch, tmp_path)
    _rom(db, tmp_path, "Chrono Trigger.smc")
    assert fixnames.fix(apply=True, db=db)["found"] == 0


def test_a_dry_run_renames_nothing_and_writes_no_manifest(monkeypatch, tmp_path):
    db = _db(monkeypatch, tmp_path)
    src = _rom(db, tmp_path, "Stargate.smc.wmf")
    r = fixnames.fix(apply=False, db=db)
    assert r["found"] == 1 and r["renamed"] == 0 and "manifest" not in r
    assert src.exists()


def test_applying_writes_a_reversible_manifest(monkeypatch, tmp_path):
    """Renaming someone's archive is not a thing to do without a way back."""
    import json
    db = _db(monkeypatch, tmp_path)
    _rom(db, tmp_path, "Stargate.smc.wmf")
    r = fixnames.fix(apply=True, db=db)
    data = json.loads((tmp_path / r["manifest"]).read_text())
    assert data["renames"][0]["from"].endswith(".wmf")
    assert data["renames"][0]["to"].endswith(".smc")
