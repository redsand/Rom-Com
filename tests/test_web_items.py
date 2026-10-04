"""The library's sort and views. The owner's complaint: "i cant sort by descending... it
shows me the unranked games" — the sort existed but had one direction, and with no crowd
data yet the desc sort was invisible next to a wall of NULLs. These pin that directions
flip on request, that unranked rows sort to the bottom rather than the top, and that the
rated/crowd views hide the wall entirely."""
from romcom.db import connect
from romcom.web import create_app


def make_client(monkeypatch, tmp_path, rows):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "items.db"))
    db = connect()
    with db:
        for r in rows:
            db.execute("INSERT INTO items(id,title,system,year,rating,community_score,status)"
                       " VALUES(?,?,?,?,?,?,?)",
                       (r["id"], r.get("title", r["id"]), r.get("system", "snes"),
                        r.get("year"), r.get("rating"), r.get("community_score"),
                        r.get("status", "CATALOGED")))
    return create_app().test_client()


def ids(c, query):
    return [r["id"] for r in c.get("/api/items?limit=50&" + query).get_json()["items"]]


def test_rating_sort_puts_the_verdicts_first_and_nulls_last(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path, [
        {"id": "none", "title": "Never played"},
        {"id": "low", "title": "Clunker", "rating": 2},
        {"id": "high", "title": "Favourite", "rating": 9},
    ])
    assert ids(c, "sort=rating") == ["high", "low", "none"]        # natural desc
    assert ids(c, "sort=rating&dir=asc") == ["low", "high", "none"]


def test_crowd_sort_does_not_sink_unscored_to_the_top(monkeypatch, tmp_path):
    """COALESCE(community_score,-1): no source has spoken, so the row sorts below
    everything the crowd actually ranked — it must not leapfrog them."""
    c = make_client(monkeypatch, tmp_path, [
        {"id": "none", "title": "Unscored"},
        {"id": "mid", "title": "Decent", "community_score": 40},
        {"id": "top", "title": "Legend", "community_score": 98},
    ])
    assert ids(c, "sort=community") == ["top", "mid", "none"]
    assert ids(c, "sort=community&dir=asc") == ["mid", "top", "none"]


def test_title_sort_flips_and_year_defaults_to_newest_first(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path, [
        {"id": "b", "title": "B", "year": 1990},
        {"id": "a", "title": "A", "year": 2005},
    ])
    assert ids(c, "sort=title") == ["a", "b"]
    assert ids(c, "sort=title&dir=desc") == ["b", "a"]
    assert ids(c, "sort=year")[0] == "a"            # 2005 before 1990
    assert ids(c, "sort=year&dir=asc")[0] == "b"


def test_the_rated_and_crowd_views_hide_the_unranked_wall(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path, [
        {"id": "rated", "title": "Played and loved", "rating": 8, "status": "VERIFIED"},
        {"id": "crowded", "title": "Crowd knows it", "community_score": 71},
        {"id": "void", "title": "Nothing known", "status": "VERIFIED"},
    ])
    assert ids(c, "view=rated") == ["rated"]
    assert ids(c, "view=crowd") == ["crowded"]
    # and an unknown view still falls through to everything, not nothing
    assert set(ids(c, "view=all")) == {"rated", "crowded", "void"}


def test_an_unknown_sort_value_falls_back_to_the_system_order(monkeypatch, tmp_path):
    """The ORDER BY whitelist is server-side for a reason: the request can only pick a
    named fragment, never inject SQL."""
    c = make_client(monkeypatch, tmp_path, [
        {"id": "y", "title": "A game", "system": "snes"},
        {"id": "x", "title": "Another", "system": "arcade"},
    ])
    assert ids(c, "sort=title;DROP TABLE items") == ["x", "y"]   # snes sorts after arcade


def test_rank_sort_uses_the_higher_of_your_verdict_and_the_crowd(monkeypatch, tmp_path):
    """The card export's score as a browse order: rating 9 -> 90 beats a crowd 40, and a
    game nobody has an opinion about stays at the bottom whichever way it's flipped."""
    c = make_client(monkeypatch, tmp_path, [
        {"id": "mine", "title": "My pick", "rating": 9},
        {"id": "theirs", "title": "Crowd pick", "community_score": 40},
        {"id": "mystery", "title": "No opinion"},
    ])
    assert ids(c, "sort=rank") == ["mine", "theirs", "mystery"]
    assert ids(c, "sort=rank&dir=asc") == ["theirs", "mine", "mystery"]


def test_summary_counts_held_games_with_an_opinion(monkeypatch, tmp_path):
    """The dashboard's Ranked numbers answer "are all games rated yet?": of the games
    actually in hand, how many the crowd or the owner has said anything about."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "items.db"))
    db = connect()
    with db:
        for iid, score in (("scored", 80), ("quiet", None)):
            db.execute("INSERT INTO items(id,title,system,community_score) VALUES(?,?,?,?)",
                       (iid, iid, "nes", score))
        for iid in ("scored", "quiet"):
            db.execute("INSERT INTO files(path,bytes,matched_item_id,content) VALUES(?,?,?,1)",
                       (f"x:/{iid}", 10, iid))
    s = create_app().test_client().get("/api/summary").get_json()
    [row] = s["by_system"]
    assert row["have"] == 2 and row["ranked"] == 1
    assert s["on_disk"] == 2 and s["ranked"] == 1