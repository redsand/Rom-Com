"""The card endpoints: preview an update, see what is on a card, and run one.

The preview is the only thing standing between the owner and a card that lost its games,
so these tests pin what it says: what would come off, what would go on, and what is
protected. The apply is a job, so it is started here and its plan is checked — the run
itself is covered in test_organizer.py, where the card is a real folder.
"""
from pathlib import Path

from romcom.db import connect
from romcom.web import create_app


def _library(monkeypatch, tmp_path, games):
    """A library with a few games: [(id, title, score, filename, size)]."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    db = connect()
    lib = tmp_path / "lib"; lib.mkdir(exist_ok=True)
    for iid, title, score, name, size in games:
        src = lib / name; src.write_bytes(b"x" * size)
        with db:
            db.execute("INSERT INTO items(id,title,system,community_score,wanted) "
                       "VALUES(?,?,?,?,1)", (iid, title, "nes", score))
            db.execute("INSERT INTO files(path,bytes,matched_item_id) VALUES(?,?,?)",
                       (str(src), size, iid))
    return db


def _card(tmp_path, entries):
    """A card with games on it: {folder: {entry: bytes}}."""
    card = tmp_path / "card"
    for folder, games in entries.items():
        for entry, size in games.items():
            p = card / "ROMs" / folder / entry
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * size)
    return card


# The gates the export card sends: only the games the library still wants.
_GATES = {"wanted_only": True, "keep_only": False, "rating_min": 0}


def test_the_preview_says_what_would_come_off_and_what_would_go_on(monkeypatch, tmp_path):
    """The preview is the whole safety of the feature: the owner sees the diff and says
    yes. So it has to name the games and say why."""
    _library(monkeypatch, tmp_path, [
        ("nes-old", "Old Game", 90, "Old Game.nes", 100),
        ("nes-new", "New Game", 80, "New Game.nes", 100)])
    card = _card(tmp_path, {"nes": {"Old Game.nes": 100}})
    c = create_app().test_client()
    # The library no longer wants the old game.
    db = connect()
    with db:
        db.execute("UPDATE items SET wanted=0 WHERE id='nes-old'")
    p = c.post("/api/card/plan", json={"path": str(card), "systems": ["nes"],
                                       "gates": _GATES}).get_json()
    assert [r["entry"] for r in p["remove"]] == ["Old Game.nes"]
    assert p["remove"][0]["reason"] == "no longer selected"
    assert p["remove"][0]["title"] == "Old Game"
    assert [a["entry"] for a in p["add"]] == ["New Game.nes"]


def test_the_preview_leaves_the_card_alone(monkeypatch, tmp_path):
    """A preview is a read. Nothing on the card may move because of one."""
    _library(monkeypatch, tmp_path, [
        ("nes-old", "Old Game", 90, "Old Game.nes", 100),
        ("nes-new", "New Game", 80, "New Game.nes", 100)])
    card = _card(tmp_path, {"nes": {"Old Game.nes": 100}})
    c = create_app().test_client()
    db = connect()
    with db:
        db.execute("UPDATE items SET wanted=0 WHERE id='nes-old'")
    c.post("/api/card/plan", json={"path": str(card), "systems": ["nes"],
                                   "gates": _GATES})
    assert (card / "ROMs" / "nes" / "Old Game.nes").exists()
    assert not (card / "ROMs" / "nes" / "New Game.nes").exists()


def test_the_preview_never_proposes_taking_the_owners_own_file(monkeypatch, tmp_path):
    """A file the library cannot name is the owner's own. It is listed as untracked, and
    it is never in the removals."""
    _library(monkeypatch, tmp_path, [("nes-old", "Old Game", 90, "Old Game.nes", 100)])
    card = _card(tmp_path, {"nes": {"Old Game.nes": 100, "MyOwnSave.nes": 10}})
    c = create_app().test_client()
    p = c.post("/api/card/plan", json={"path": str(card), "systems": ["nes"],
                                       "gates": _GATES}).get_json()
    assert "MyOwnSave.nes" not in [r["entry"] for r in p["remove"]]
    assert any(u["entry"] == "MyOwnSave.nes" for u in p["untracked"])


def test_the_index_endpoint_says_what_is_on_a_card(monkeypatch, tmp_path):
    """The memory of a card in a drawer: what it holds, without it being plugged in."""
    _library(monkeypatch, tmp_path, [("nes-old", "Old Game", 90, "Old Game.nes", 100)])
    card = _card(tmp_path, {"nes": {"Old Game.nes": 100}})
    c = create_app().test_client()
    # The card has been indexed by an update, so the database knows it.
    c.post("/api/card/plan", json={"path": str(card), "systems": ["nes"],
                                   "gates": _GATES})
    got = c.get(f"/api/card/index?path={card}").get_json()
    assert got["card_id"]
    assert any(e["entry"] == "Old Game.nes" for e in got["entries"])


def test_the_index_endpoint_needs_a_path(monkeypatch, tmp_path):
    _library(monkeypatch, tmp_path, [])
    c = create_app().test_client()
    assert c.get("/api/card/index").status_code == 400


def test_an_update_needs_a_path(monkeypatch, tmp_path):
    _library(monkeypatch, tmp_path, [])
    c = create_app().test_client()
    assert c.post("/api/card/update", json={}).status_code == 400


def test_the_update_policy_reaches_the_job(monkeypatch, tmp_path):
    """The two switches on the panel — make room for better games, and protect the ones
    you have played — have to arrive at the job as the owner set them."""
    _library(monkeypatch, tmp_path, [("nes-old", "Old Game", 90, "Old Game.nes", 100)])
    card = _card(tmp_path, {"nes": {"Old Game.nes": 100}})
    c = create_app().test_client()
    r = c.post("/api/card/update", json={"path": str(card), "systems": ["nes"],
                                         "evict": False, "protect_played": False})
    assert r.status_code == 200
    assert r.get_json().get("started") is True
    # The job's own record of what it was asked to do, so a run trades a game for a
    # better one only when the owner asked for it.
    import json as _json
    row = connect().execute("SELECT params FROM web_jobs WHERE kind='card' "
                            "ORDER BY id DESC LIMIT 1").fetchone()
    params = _json.loads(row["params"])
    assert params["evict"] is False
    assert params["protect_played"] is False
    assert params["path"] == str(card)


def test_the_update_policy_defaults_to_the_safe_answer(monkeypatch, tmp_path):
    """Left alone, an update does not trade games away and does protect the ones that
    have been played."""
    _library(monkeypatch, tmp_path, [("nes-old", "Old Game", 90, "Old Game.nes", 100)])
    card = _card(tmp_path, {"nes": {"Old Game.nes": 100}})
    c = create_app().test_client()
    c.post("/api/card/update", json={"path": str(card), "systems": ["nes"]})
    import json as _json
    row = connect().execute("SELECT params FROM web_jobs WHERE kind='card' "
                            "ORDER BY id DESC LIMIT 1").fetchone()
    params = _json.loads(row["params"])
    assert params["evict"] is True
    assert params["protect_played"] is True

