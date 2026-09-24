"""The community layer's matching and budget, pinned without any network: bulk-pull
then match locally is the whole design (never one API call per catalog row), so the
matcher and the budget accounting are the parts that can silently rot."""
from romcom.db import connect
from romcom import community


def setup(monkeypatch, tmp_path, budget="50"):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "com.db"))
    monkeypatch.setenv("ROMCOM_RAWG_BUDGET", budget)
    from romcom.config import invalidate
    invalidate()
    return connect()


def item(iid, title):
    return {"id": iid, "title": title}


def test_matching_ignores_region_and_revision_tags(monkeypatch, tmp_path):
    """clean_query strips (USA)/(Rev A) groups, so the catalog's release-speak matches a
    provider's plain titles — the same normalization the site searches use."""
    records = [{"title": "Chrono Trigger", "score": 92, "votes": 400}]
    m = community._match_items(
        [item("snes-ct", "Chrono Trigger (USA) [Rev A]")], records)
    assert m["snes-ct"]["title"] == "Chrono Trigger"


def test_a_subset_match_picks_the_smallest_record(monkeypatch, tmp_path):
    """Providers decorate titles ('Forgotten Worlds (World)'); our tokens being a subset
    of the record's matches. The smallest record wins, and the reverse never matches —
    'Super Mario' must not claim 'Super Mario Bros. 3'."""
    records = [{"title": "Forgotten Worlds (World)", "score": 70},
               {"title": "Forgotten Worlds (World) (Arcade)", "score": 70},
               {"title": "Super Mario Bros. 3", "score": 99}]
    m = community._match_items(
        [item("fw", "Forgotten Worlds"), item("sm", "Super Mario")], records)
    assert m["fw"]["title"] == "Forgotten Worlds (World)"
    assert "sm" not in m


def test_the_daily_budget_stops_the_sync_at_its_cap(monkeypatch, tmp_path):
    """RAWG's free tier is 20k requests a month; an unmetered per-item search over even a
    tenth of the library would burn it in one sync. The counter must actually stop work."""
    db = setup(monkeypatch, tmp_path, budget="3")
    assert community._budget_left(db, "rawg") == 3
    community._spend(db, "rawg", 3)
    assert community._budget_left(db, "rawg") == 0
    try:
        community._request("https://example.invalid/x", {}, "rawg", db)
        raised = False
    except RuntimeError as e:
        raised = "budget" in str(e)
    assert raised, "a request past the cap must raise, not quietly proceed"


def test_the_budget_resets_each_day(monkeypatch, tmp_path):
    db = setup(monkeypatch, tmp_path, budget="3")
    community._spend(db, "rawg", 3)
    assert community._budget_left(db, "rawg") == 0
    with db:
        db.execute("UPDATE community_state SET v='2000-01-01' WHERE source='rawg' AND k='date'")
    assert community._budget_left(db, "rawg") == 3


def test_scores_upsert_and_denormalize_the_best_one(monkeypatch, tmp_path):
    """Per-source rows are kept so swapping keys can be audited and no source silently
    overwrites another; items.community_score carries the best so 'top games' stays one
    cheap indexed ORDER BY."""
    db = setup(monkeypatch, tmp_path)
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES('a','Game A','snes')")
        db.execute("INSERT INTO items(id,title,system) VALUES('b','Game B','snes')")
    community._write_scores(db, [
        {"item_id": "a", "source": "rawg", "score": 60, "votes": 30, "matched_title": "Game A"},
        {"item_id": "a", "source": "retroachievements", "score": 90, "votes": 250, "matched_title": "Game A"},
    ])
    # A re-sync of the same source is an upsert, not a duplicate row.
    community._write_scores(db, [
        {"item_id": "a", "source": "rawg", "score": 62, "votes": 31, "matched_title": "Game A"},
    ])
    rows = db.execute("SELECT source,score FROM community_scores WHERE item_id='a' "
                      "ORDER BY source").fetchall()
    assert [(r["source"], r["score"]) for r in rows] == [("rawg", 62), ("retroachievements", 90)]
    row = db.execute("SELECT community_score, community_source FROM items WHERE id='a'").fetchone()
    assert row["community_score"] == 90 and row["community_source"] == "retroachievements"
    # Untouched items stay NULL: no source has an opinion yet.
    assert db.execute("SELECT community_score FROM items WHERE id='b'").fetchone()["community_score"] is None


def test_a_sync_without_providers_reports_rather_than_raises(monkeypatch, tmp_path):
    """Fresh install, no keys: the sync must say what is missing, not blow up."""
    db = setup(monkeypatch, tmp_path)
    report = community.sync(db=db)
    assert report["scored"] == 0
    assert any("no provider configured" in e for e in report["errors"])