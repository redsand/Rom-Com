"""Session 17 asked "give me the top 5 I should play" and got 100 tool calls and zero
answers, because nothing in the catalog said how good a game was. recommend.top is the
one query that answers it; these tests pin the contract the chat now leans on:
in-hand only, junk excluded, and an order that can be explained per row."""
from romcom.db import connect
from romcom import recommend


def setup(monkeypatch, tmp_path, rows):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "rec.db"))
    from romcom.config import invalidate
    invalidate()
    db = connect()
    with db:
        for r in rows:
            db.execute(
                "INSERT INTO items(id,title,system,status,community_score,community_source,"
                "rating,keep,last_played,playable) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["title"], r.get("system", "snes"), r.get("status", "VERIFIED"),
                 r.get("community_score"), r.get("community_source"), r.get("rating"),
                 r.get("keep", 0), r.get("last_played"), r.get("playable", 0)))
    return db


def test_the_crowd_ranks_the_shelf(monkeypatch, tmp_path):
    db = setup(monkeypatch, tmp_path, [
        {"id": "a", "title": "Also-ran", "community_score": 20, "community_source": "rawg"},
        {"id": "b", "title": "Beloved Classic", "community_score": 95, "community_source": "rawg"},
        {"id": "c", "title": "Middling", "community_score": 60, "community_source": "rawg"},
    ])
    rows = recommend.top(db=db)
    assert [r["id"] for r in rows] == ["b", "c", "a"]
    assert rows[0]["how"] == "rawg ranks it 95"


def test_the_owners_rating_can_outrank_the_crowd(monkeypatch, tmp_path):
    """max(crowd, own*10): a 10 from the owner counts like a 100 from the crowd, and
    wins a tie because the explanation says so too."""
    db = setup(monkeypatch, tmp_path, [
        {"id": "a", "title": "Crowd pleaser", "community_score": 80},
        {"id": "b", "title": "Personal favourite", "rating": 9},
    ])
    rows = recommend.top(db=db)
    assert rows[0]["id"] == "b" and rows[0]["how"] == "you rated it"


def test_only_games_in_hand_are_recommended(monkeypatch, tmp_path):
    """A recommendation you must download first is a shopping list, not an answer."""
    db = setup(monkeypatch, tmp_path, [
        {"id": "a", "title": "In hand", "status": "VERIFIED", "community_score": 90},
        {"id": "b", "title": "Merely cataloged", "status": "CATALOGED", "community_score": 99},
    ])
    rows = recommend.top(db=db)
    assert [r["id"] for r in rows] == ["a"]


def test_arcade_needs_to_be_assemblable_not_merely_cataloged(monkeypatch, tmp_path):
    """Arcade status cannot answer playability — a set is built from hashes scattered
    across a flat dump — so the playable flag is the gate, exactly as elsewhere."""
    db = setup(monkeypatch, tmp_path, [
        {"id": "a", "title": "Galaga", "system": "arcade", "status": "FOUND",
         "playable": 1, "community_score": 88},
        {"id": "b", "title": "Missing roms", "system": "arcade", "status": "FOUND",
         "playable": 0, "community_score": 99},
    ])
    rows = recommend.top(db=db)
    assert [r["id"] for r in rows] == ["a"]


def test_junk_never_gets_a_recommendation(monkeypatch, tmp_path):
    """The in-hand pool contains adopted walkthroughs and cross-system chip matches;
    suggesting them is exactly the "playing random games" the owner complained about.
    classify() is the same judge the downloader uses, so one definition of junk governs
    both ends."""
    db = setup(monkeypatch, tmp_path, [
        {"id": "a", "title": "Chrono Trigger (USA)", "community_score": 92},
        {"id": "w", "title": "Chrono Trigger Walkthrough.pdf", "community_score": 99},
        {"id": "x", "title": "Sonic the Hedgehog.nes", "community_score": 99},
    ])
    rows = recommend.top(db=db)
    assert [r["id"] for r in rows] == ["a"]


def test_homebrew_and_demos_stay_in_the_library_but_out_of_the_top_five(monkeypatch, tmp_path):
    """With no crowd scores yet every rank is 0 and the tiebreak is alphabetical, which
    put a (PD) GBA advertisement at #1. classify() judges filenames and cannot see these
    — a bare title has no extension — so the parenthetical marker does it here."""
    db = setup(monkeypatch, tmp_path, [
        {"id": "a", "title": "Golden Eye (PD) Advertisement"},
        {"id": "d", "title": "Star Fox 2 (Beta)"},
        {"id": "h", "title": "Some Homebrew (Aftermarket)"},
        {"id": "g", "title": "Actual Game"},
    ])
    rows = recommend.top(db=db)
    assert [r["id"] for r in rows] == ["g"]


def test_already_played_games_wait_their_turn(monkeypatch, tmp_path):
    db = setup(monkeypatch, tmp_path, [
        {"id": "a", "title": "Fresh", "community_score": 60},
        {"id": "b", "title": "Seen it", "community_score": 95, "last_played": "2026-01-01 10:00"},
    ])
    assert [r["id"] for r in recommend.top(db=db)] == ["a"]
    assert [r["id"] for r in recommend.top(db=db, include_played=True)] == ["b", "a"]


def test_a_kept_game_steps_aside_for_new_suggestions(monkeypatch, tmp_path):
    """keep means it is already earmarked for the card; "what should I play" is a
    question about the unplayed shelf, so kept games sink, not surface."""
    db = setup(monkeypatch, tmp_path, [
        {"id": "a", "title": "Kept", "community_score": 80, "keep": 1},
        {"id": "b", "title": "Unkept", "community_score": 70},
    ])
    rows = recommend.top(db=db)
    assert rows[0]["id"] == "b"


def test_the_system_filter_and_n_are_honoured(monkeypatch, tmp_path):
    db = setup(monkeypatch, tmp_path, [
        {"id": "a", "title": "Snes game", "system": "snes", "community_score": 50},
        {"id": "b", "title": "Genesis game", "system": "genesis", "community_score": 99},
    ])
    assert [r["id"] for r in recommend.top(db=db, system="genesis")] == ["b"]
    assert len(recommend.top(db=db, n=1)) == 1


def test_summary_shapes_rows_for_the_chat_tool(monkeypatch, tmp_path):
    db = setup(monkeypatch, tmp_path, [
        {"id": "a", "title": "Ok game", "community_score": 77, "community_source": "rawg"}])
    out = recommend.summary(db=db)
    assert out["recommendations"][0] == {
        "id": "a", "title": "Ok game", "system": "snes", "year": None,
        "rating": None, "community_score": 77, "community_source": "rawg",
        "why": "rawg ranks it 77", "rank": 77}