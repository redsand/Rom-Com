"""The same dat imported twice under two source labels leaves one entry as two rows."""
from romcom.db import connect
from romcom.dedupe import groups, dedupe


def setup(monkeypatch, tmp_path, rows):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "d.db"))
    import romcom.dedupe as dd
    monkeypatch.setattr(dd, "load_yaml", lambda name: {
        "catalogs": {"redump": {"enabled": True, "systems": ["gamecube"]}}})
    db = connect()
    with db:
        for r in rows:
            db.execute("""INSERT INTO items(id,title,system,wanted,authorized,status,
                          catalog_source,external_id) VALUES(?,?,?,?,?,?,?,?)""",
                       (r["id"], r.get("title", "T"), r["system"], r.get("wanted", 0),
                        r.get("authorized", 0), r.get("status", "CATALOGED"),
                        r["src"], r["ext"]))
    return db


def test_the_config_declared_source_wins(monkeypatch, tmp_path):
    """catalogs.yaml says gamecube comes from redump, so the generic `dat` copy is the one
    that goes -- not whichever row the database happened to return first."""
    setup(monkeypatch, tmp_path, [
        {"id": "dat-gc-1", "system": "gamecube", "src": "dat", "ext": "gamecube/Game"},
        {"id": "redump-gc-1", "system": "gamecube", "src": "redump", "ext": "gamecube/Game"}])
    assert [g[0]["id"] for g in groups()] == ["redump-gc-1"]
    r = dedupe(dry_run=False)
    assert (r["groups"], r["removed"]) == (1, 1)
    assert [x["id"] for x in connect().execute("SELECT id FROM items")] == ["redump-gc-1"]


def test_a_named_source_beats_the_generic_dat_even_when_undeclared(monkeypatch, tmp_path):
    """amiga is in no catalogs: block, so the declared-source rule cannot decide it. A real
    publisher name still has to beat the fallback `dat` label."""
    setup(monkeypatch, tmp_path, [
        {"id": "dat-amiga-1", "system": "amiga", "src": "dat", "ext": "amiga/G"},
        {"id": "nointro-amiga-1", "system": "amiga", "src": "nointro", "ext": "amiga/G"}])
    assert groups()[0][0]["id"] == "nointro-amiga-1"


def test_merging_never_un_wants_an_item(monkeypatch, tmp_path):
    """The loser may be the row the owner marked wanted. Dropping it must not quietly
    withdraw that intent, or a merge silently shrinks the missing list."""
    setup(monkeypatch, tmp_path, [
        {"id": "redump-gc-2", "system": "gamecube", "src": "redump", "ext": "gamecube/G2",
         "wanted": 0, "authorized": 0},
        {"id": "dat-gc-2", "system": "gamecube", "src": "dat", "ext": "gamecube/G2",
         "wanted": 1, "authorized": 1}])
    dedupe(dry_run=False)
    row = connect().execute("SELECT id,wanted,authorized FROM items").fetchone()
    assert (row["id"], row["wanted"], row["authorized"]) == ("redump-gc-2", 1, 1)


def test_children_follow_the_survivor(monkeypatch, tmp_path):
    db = setup(monkeypatch, tmp_path, [
        {"id": "redump-gc-3", "system": "gamecube", "src": "redump", "ext": "gamecube/G3"},
        {"id": "dat-gc-3", "system": "gamecube", "src": "dat", "ext": "gamecube/G3"}])
    with db:
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('dat-gc-3','sha1','abc')")
        db.execute("INSERT INTO aliases(item_id,alias) VALUES('dat-gc-3','Only On The Loser')")
        db.execute("INSERT INTO events(item_id,event) VALUES('dat-gc-3','adopted')")
    dedupe(dry_run=False)
    db = connect()
    assert db.execute("SELECT item_id FROM file_hashes").fetchone()["item_id"] == "redump-gc-3"
    assert db.execute("SELECT item_id FROM aliases").fetchone()["item_id"] == "redump-gc-3"
    assert db.execute("SELECT item_id FROM events").fetchone()["item_id"] == "redump-gc-3"


def test_same_title_different_external_id_is_left_alone(monkeypatch, tmp_path):
    """Identity is the dat's own key, not the title. Two entries can share a title and still
    be different things, so title collisions must not be merged."""
    setup(monkeypatch, tmp_path, [
        {"id": "a", "system": "gamecube", "src": "redump", "ext": "gamecube/Disc 1", "title": "Game"},
        {"id": "b", "system": "gamecube", "src": "redump", "ext": "gamecube/Disc 2", "title": "Game"}])
    assert groups() == []
    assert dedupe(dry_run=False)["removed"] == 0


def test_dry_run_changes_nothing(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, [
        {"id": "redump-gc-4", "system": "gamecube", "src": "redump", "ext": "gamecube/G4"},
        {"id": "dat-gc-4", "system": "gamecube", "src": "dat", "ext": "gamecube/G4"}])
    assert dedupe(dry_run=True)["removed"] == 1
    assert connect().execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 2
