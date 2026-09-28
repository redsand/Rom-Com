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


def test_matching_unspaced_name_variants(monkeypatch, tmp_path):
    """The catalog's No-Intro spelling and RAWG's spelling differ on the spacebar:
    'Megaman Zero 3' vs 'Mega Man Zero 3' are the same game, but token overlap
    alone sees nothing in common. The compact form (letters and digits, order
    kept) bridges it — in the bulk matcher and in the budgeted search alike."""
    records = [{"title": "Mega Man Zero 3", "score": 92, "votes": 300}]
    m = community._match_items(
        [item("mz", "Megaman Zero 3 (Europe)"), item("mm", "Mega Man Zero 3")], records)
    assert m["mz"]["title"] == "Mega Man Zero 3"
    assert m["mm"]["title"] == "Mega Man Zero 3"
    # and the bridge doesn't overreach: 'Megaman' is not 'Mega Man X'
    bad = community._match_items([item("mx", "Megaman")],
        [{"title": "Mega Man X", "score": 90, "votes": 200}])
    assert bad == {}


def test_the_search_path_scores_a_compact_match_as_perfect(monkeypatch, tmp_path):
    db = setup(monkeypatch, tmp_path)
    results = [{"name": "Mega Man Battle Network", "rating": 4.3, "ratings_count": 80},
               {"name": "Totally Different Game", "rating": 4.9, "ratings_count": 900}]
    monkeypatch.setattr(community, "_request",
                        lambda *a, **k: {"results": results})
    monkeypatch.setattr(community, "_rawg_platform_id", lambda db_, slug: (24, "GBA"))
    rec = community._rawg_search(db, "gba", "Megaman Battle Network (USA)")
    assert rec["title"] == "Mega Man Battle Network"


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


def test_the_search_budget_goes_to_the_newest_owned_games_first(monkeypatch, tmp_path):
    """The owner reads his library newest to oldest, so a thin search budget must land
    there too — not on whatever row order SQLite happens to return."""
    db = setup(monkeypatch, tmp_path, budget="50")
    _fake_rawg(monkeypatch)
    with db:
        db.execute("INSERT INTO items(id,title,system,year,status) "
                   "VALUES('old','Old Game','snes',1989,'VERIFIED')")
        db.execute("INSERT INTO items(id,title,system,year,status) "
                   "VALUES('new','New Game','snes',2004,'VERIFIED')")
    calls = []
    monkeypatch.setattr(community, "_rawg_search",
                        lambda db_, slug, title: calls.append(title)
                        or {"title": title, "score": 80, "votes": 10})
    community.sync(db=db)
    assert calls == ["New Game", "Old Game"]


def test_bulk_matching_never_touches_the_search_budget(monkeypatch, tmp_path):
    """Day one's bug: the bulk pull charged the budget, so 400 requests of platform
    lists left nothing for the per-item searches the budget exists for. Bulk is
    cached and free; searches alone pay."""
    db = setup(monkeypatch, tmp_path, budget="0")   # nothing to search with
    monkeypatch.setenv("RAWG_API_KEY", "k")
    from romcom.config import invalidate
    invalidate()
    with connect() as d:
        d.execute("INSERT INTO app_settings(key,value) VALUES('rawg_enabled','true')")
    invalidate()
    with db:
        db.execute("INSERT INTO items(id,title,system,status) "
                   "VALUES('own','In Hand','snes','VERIFIED')")
    monkeypatch.setattr(community, "rawg_top_games",
                        lambda db_, slug: [{"title": "In Hand", "score": 85, "votes": 40}])
    report = community.sync(db=db)
    assert report["scored"] == 1
    assert db.execute("SELECT community_score FROM items WHERE id='own'"
                      ).fetchone()["community_score"] == 85


def _404(url):
    import requests as rq
    resp = rq.Response()
    resp.status_code = 404
    resp.url = url
    resp.request = rq.Request("GET", url).prepare()
    return rq.HTTPError("404 Client Error", response=resp)


def test_platform_paging_stops_at_the_end_of_the_list(monkeypatch, tmp_path):
    """RAWG has ~51 platforms: two pages at page_size 40. The id probe used to request
    pages 2-5 unconditionally, and the page-3 404 aborted the probe — so no system
    ever got a platform id and the first live sync scored nothing at all."""
    db = setup(monkeypatch, tmp_path)
    pages = {1: {"results": [{"id": 1, "name": "PC"}], "next": "?page=2"},
             2: {"results": [{"id": 119, "name": "SEGA 32X"}], "next": None}}
    seen = []
    def fake_request(url, params, source, db_, budget=True):
        p = params.get("page", 1)
        seen.append(p)
        if p in pages:
            return pages[p]
        raise _404(url)
    monkeypatch.setattr(community, "_request", fake_request)
    monkeypatch.setattr(community, "rawg_platforms", lambda db_: ["SEGA 32X"])
    monkeypatch.setattr(community, "rawg_key", lambda: "k")
    pid, name = community._rawg_platform_id(db, "32x")
    assert (pid, name) == (119, "SEGA 32X")
    assert seen == [1, 2]          # honored next=null: never asked for a page 3


def test_a_lying_next_link_404s_the_probe_to_a_clean_stop(monkeypatch, tmp_path):
    """If the API's next link points somewhere that 404s, the probe must conclude
    'not found' rather than raise — one platform's bad page is not a failed sync."""
    db = setup(monkeypatch, tmp_path)
    def fake_request(url, params, source, db_, budget=True):
        if params.get("page", 1) == 1:
            return {"results": [{"id": 1, "name": "PC"}], "next": "?page=2"}
        raise _404(url)
    monkeypatch.setattr(community, "_request", fake_request)
    monkeypatch.setattr(community, "rawg_platforms", lambda db_: ["SEGA 32X"])
    monkeypatch.setattr(community, "rawg_key", lambda: "k")
    pid, name = community._rawg_platform_id(db, "32x")
    assert pid is None and name == "SEGA 32X"


def test_a_short_platforms_game_list_ends_cleanly_too(monkeypatch, tmp_path):
    """A platform with fewer pages of games than the loop might ask for: the top-games
    pull must keep what it found instead of losing the whole system's bulk match to
    a 404."""
    db = setup(monkeypatch, tmp_path)
    pages = {1: {"results": [{"name": "Kolibri", "rating": 3.9, "ratings_count": 12}],
                 "next": "?page=2"}}
    def fake_request(url, params, source, db_, budget=True):
        if params.get("page", 1) in pages:
            return pages[params["page"]]
        raise _404(url)
    monkeypatch.setattr(community, "_request", fake_request)
    monkeypatch.setattr(community, "_rawg_platform_id", lambda db_, slug: (119, "SEGA 32X"))
    monkeypatch.setattr(community, "rawg_key", lambda: "k")
    recs = community.rawg_top_games(db, "32x")
    assert recs == [{"title": "Kolibri", "score": 78, "votes": 12, "year": None}]


def test_the_bulk_pull_pages_through_the_whole_platform(monkeypatch, tmp_path):
    """The owner asked for every owned item to be covered, not just each platform's
    famous 200. The pull follows next to the end of the list; the page cap is a
    safety ceiling, not the working limit."""
    db = setup(monkeypatch, tmp_path)
    pages = {1: {"results": [{"name": "Page One", "rating": 4.0, "ratings_count": 30}],
                 "next": "?page=2"},
             2: {"results": [{"name": "Page Two", "rating": 4.5, "ratings_count": 40}],
                 "next": "?page=3"},
             3: {"results": [{"name": "Page Three", "rating": 3.5, "ratings_count": 6}],
                 "next": None}}
    seen = []
    def fake_request(url, params, source, db_, budget=True):
        seen.append(params.get("page", 1))
        return pages[params["page"]]
    monkeypatch.setattr(community, "_request", fake_request)
    monkeypatch.setattr(community, "_rawg_platform_id", lambda db_, slug: (24, "Game Boy Advance"))
    monkeypatch.setattr(community, "rawg_key", lambda: "k")
    recs = community.rawg_top_games(db, "gba")
    assert [r["title"] for r in recs] == ["Page One", "Page Two", "Page Three"]
    assert seen == [1, 2, 3]      # followed next to its end, then stopped


class _FlakyDb:
    """A connection proxy that raises 'database is locked' on the first N score
    writes, then lets the rest through — what the live watcher's write stretches
    look like to the sync."""
    def __init__(self, real, fails):
        self._real, self._fails, self._n = real, fails, 0
    def execute(self, sql, *p):
        if "INSERT INTO community_scores" in sql and self._n < self._fails:
            self._n += 1
            import sqlite3
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *p)
    def __enter__(self):
        self._real.__enter__()
        return self
    def __exit__(self, *a):
        return self._real.__exit__(*a)


def test_a_locked_write_is_retried_not_discarded(monkeypatch, tmp_path):
    """One busy-timeout threw away 400 budgeted search results — a whole day's
    searches. The write is an idempotent upsert, so it retries through the lock."""
    db = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(community.time, "sleep", lambda s: None)
    with db:
        db.execute("INSERT INTO items(id,title,system) VALUES('a','Game A','snes')")
    rows = [{"item_id": "a", "source": "rawg", "score": 85, "votes": 30, "matched_title": "Game A"}]
    assert community._write_scores(_FlakyDb(db, fails=2), rows) == 1
    assert db.execute("SELECT community_score FROM items WHERE id='a'"
                      ).fetchone()["community_score"] == 85


def test_a_persistently_locked_write_gives_up_loudly(monkeypatch, tmp_path):
    """Retries are bounded: a lock that never lifts must surface as an error, not
    spin forever."""
    import sqlite3 as _sq
    db = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(community.time, "sleep", lambda s: None)
    rows = [{"item_id": "a", "source": "rawg", "score": 85, "votes": 30, "matched_title": "Game A"}]
    try:
        community._write_scores(_FlakyDb(db, fails=99), rows)
        raised = False
    except _sq.OperationalError:
        raised = True
    assert raised


def test_spending_does_not_hold_the_write_lock(monkeypatch, tmp_path):
    """A running sync pinned SQLite's single write lock: _set_state left its implicit
    transaction open, so every web write — login, and the last_seen stamp every
    authenticated API call makes — timed out and the whole UI froze for as long as
    the sync ran. Spending must commit immediately."""
    db = setup(monkeypatch, tmp_path, budget="50")
    community._spend(db, "rawg", 1)
    db2 = connect()
    db2.execute("PRAGMA busy_timeout=250")   # fail fast, not after the 15s default
    try:
        with db2:   # raises "database is locked" if db still holds the write lock
            db2.execute("INSERT INTO community_state(source,k,v) VALUES('t','k','v')")
    finally:
        db2.close()


def test_a_sync_without_providers_reports_rather_than_raises(monkeypatch, tmp_path):
    """Fresh install, no keys: the sync must say what is missing, not blow up."""
    db = setup(monkeypatch, tmp_path)
    report = community.sync(db=db)
    assert report["scored"] == 0
    assert any("no provider configured" in e for e in report["errors"])


def _fake_rawg(monkeypatch):
    """A RAWG provider that is 'on' with no bulk pull, so the per-item search loop is
    the only thing that can produce a score."""
    monkeypatch.setenv("RAWG_API_KEY", "k")
    with connect() as d:
        d.execute("INSERT INTO app_settings(key,value) VALUES('rawg_enabled','true')")
    # settings() memoizes app_settings for 3s; opening the db above primed that cache
    # empty, so the write must invalidate it the way the settings POST does.
    from romcom.config import invalidate
    invalidate()
    monkeypatch.setattr(community, "rawg_top_games", lambda db_, slug: [])


def test_owned_games_are_scored_before_the_wishlist(monkeypatch, tmp_path):
    """The owner's order: what he can play tonight is scored first, so a thin daily
    budget lands on owned games before it can be spent on games he doesn't have."""
    db = setup(monkeypatch, tmp_path, budget="50")
    _fake_rawg(monkeypatch)
    with db:
        db.execute("INSERT INTO items(id,title,system,status,wanted) "
                   "VALUES('own','In Hand','snes','VERIFIED',0)")
        db.execute("INSERT INTO items(id,title,system,status,wanted) "
                   "VALUES('wish','On The List','snes','CATALOGED',1)")
    calls = []
    monkeypatch.setattr(community, "_rawg_search",
                       lambda db_, slug, title: calls.append(title) or {"title": title, "score": 80, "votes": 10})
    report = community.sync(db=db)
    assert calls == ["In Hand", "On The List"]
    assert report["scored"] == 2


def test_a_thin_budget_is_spent_on_owned_games_first(monkeypatch, tmp_path):
    """Budget of one search: the owned game wins it, the wishlist waits for tomorrow."""
    db = setup(monkeypatch, tmp_path, budget="1")
    _fake_rawg(monkeypatch)
    with db:
        db.execute("INSERT INTO items(id,title,system,status,wanted) "
                   "VALUES('own','In Hand','snes','VERIFIED',0)")
        db.execute("INSERT INTO items(id,title,system,status,wanted) "
                   "VALUES('wish','On The List','snes','CATALOGED',1)")
    calls = []
    def fake_search(db_, slug, title):
        community._spend(db_, "rawg", 1)  # the real search pays a request per call
        calls.append(title)
        return {"title": title, "score": 80, "votes": 10}
    monkeypatch.setattr(community, "_rawg_search", fake_search)
    community.sync(db=db)
    assert calls == ["In Hand"]


def test_the_ra_game_list_asks_for_the_console_by_its_id(monkeypatch, tmp_path):
    """RA's game-list endpoint takes the console in `i` — `c` is the COUNT parameter.
    The first live RA sync sent c=<console id> and got {"success": false} back — a dict,
    which the list code iterated as strings ('string indices must be integers'), killing
    every system's RA pass on day one while the sync still exited 0."""
    db = setup(monkeypatch, tmp_path)
    seen = {}
    def fake_request(url, params, source, db_, budget=True):
        seen.update(params)
        return [{"Title": "Game Boy Game", "ID": 5}]
    monkeypatch.setattr(community, "_request", fake_request)
    monkeypatch.setattr(community, "ra_credentials", lambda: ("u", "k"))
    games = community.ra_games(db, 4)
    assert games == [{"title": "Game Boy Game", "gid": 5}]
    assert seen.get("i") == 4 and "c" not in seen


def _fake_ra(monkeypatch):
    """An RA provider that is 'on', with the console lookup and game list stubbed so the
    per-game players fetch is the only thing that can run."""
    monkeypatch.setenv("RA_USERNAME", "u")
    monkeypatch.setenv("RA_API_KEY", "k")
    with connect() as d:
        d.execute("INSERT INTO app_settings(key,value) VALUES('ra_enabled','true')")
    from romcom.config import invalidate
    invalidate()
    monkeypatch.setattr(community, "_ra_console_id", lambda db_, slug: (4, "Game Boy"))
    monkeypatch.setattr(community, "ra_games",
                        lambda db_, cid: [{"title": "In Hand", "gid": 9}])


def test_percentiles_rank_against_the_whole_console_not_the_batch(monkeypatch, tmp_path):
    """RA_FETCH_CAP means a big console's first sweep fetches only a slice (600 of
    arcade's 2,331). Percentiling that slice alone crowned the batch's most popular
    game as the console's very best — a score the 30-day staleness window then froze.
    The percentile must rank against every player count known for the console,
    including what earlier sweeps already stored, so it converges as batches land."""
    db = setup(monkeypatch, tmp_path)
    _fake_ra(monkeypatch)
    monkeypatch.setattr(community, "ra_games",
                        lambda db_, cid: [{"title": "Second Game", "gid": 10}])
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('top','In Hand','gb','VERIFIED')")
        db.execute("INSERT INTO items(id,title,system,status) VALUES('mid','Second Game','gb','VERIFIED')")
        # an earlier sweep already recorded a far more popular game on this console
        db.execute("INSERT INTO community_scores(item_id,source,score,votes,matched_title,fetched_at) "
                   "VALUES('top','retroachievements',90,1000,'In Hand',CURRENT_TIMESTAMP)")
    monkeypatch.setattr(community, "ra_players", lambda db_, gid: 500)
    community.sync(db=db)
    row = db.execute("SELECT score FROM community_scores "
                     "WHERE item_id='mid' AND source='retroachievements'").fetchone()
    # 500 players against a known distribution of [500, 1000]: half the console,
    # not the top of a one-game batch (which the old slice-only math scored 100).
    assert row["score"] == 50


def test_ra_fetches_do_not_share_rawgs_daily_budget(monkeypatch, tmp_path):
    """RA's player fetches were counted against RAWG's 400/day — a number sized for
    RAWG's 20k-a-MONTH free tier — so one interrupted sync could spend the shared
    cap and starve the next run's RA fetches before a single player count arrived.
    RA has no monthly quota; its cap is its own knob."""
    db = setup(monkeypatch, tmp_path, budget="0")   # today's RAWG budget: already gone
    _fake_ra(monkeypatch)
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('own','In Hand','gb','VERIFIED')")
    monkeypatch.setattr(community, "ra_players", lambda db_, gid: 40)
    report = community.sync(db=db)
    assert report["systems"]["gb"]["ra"] == 1    # the RA fetch ran anyway


def test_a_write_failure_costs_one_system_not_the_sync(monkeypatch, tmp_path):
    """_write_scores already rides out locked writes, but anything else it raises must
    become a line in the report instead of unwinding the whole sync — the next
    system's player fetches spend a slice of the day's budget and must not be thrown
    away with the broken system."""
    db = setup(monkeypatch, tmp_path)
    _fake_ra(monkeypatch)
    monkeypatch.setattr(community, "ra_players", lambda db_, gid: 40)
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('g1','In Hand','gb','VERIFIED')")
    real = community._write_scores
    calls = []
    def flaky(db_, rows):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk exploded")
        return real(db_, rows)
    monkeypatch.setattr(community, "_write_scores", flaky)
    report = community.sync(db=db)          # must not raise
    assert any(e.startswith("write/") for e in report["errors"])
    assert report["systems"]["gb"]["ra"] == 1   # the system was still fetched and counted


def test_fresh_player_counts_are_not_refetched(monkeypatch, tmp_path):
    """Scores are stored under source='retroachievements'; the staleness cut that
    skips re-fetching used to read source='ra' — a spelling that never matched, so
    every sync re-paid the per-game players budget for scores it already had."""
    db = setup(monkeypatch, tmp_path)
    _fake_ra(monkeypatch)
    with db:
        db.execute("INSERT INTO items(id,title,system,status) VALUES('own','In Hand','gb','VERIFIED')")
        db.execute("INSERT INTO community_scores(item_id,source,score,votes,matched_title,fetched_at) "
                   "VALUES('own','retroachievements',50,40,'In Hand',CURRENT_TIMESTAMP)")
    calls = []
    monkeypatch.setattr(community, "ra_players", lambda db_, gid: calls.append(gid) or 40)
    report = community.sync(db=db)
    assert calls == []                    # the score is fresh: no re-fetch, no re-spend
    assert report["scored"] == 0
    with db:   # past RA_STALE_DAYS the players are fetched again
        db.execute("UPDATE community_scores SET fetched_at=datetime('now','-40 days')")
    report = community.sync(db=db)
    assert calls == [9]
    assert report["systems"]["gb"]["ra"] == 1
    row = db.execute("SELECT score,votes FROM community_scores "
                     "WHERE item_id='own' AND source='retroachievements'").fetchone()
    assert row["votes"] == 40 and row["score"] == 100   # solo game = top percentile


def test_an_interrupted_second_pass_still_leaves_the_first_written(monkeypatch, tmp_path):
    """Owned results persist even if the wishlist pass dies: the pass boundary is a
    commit point, so a crash mid-wishlist never costs the owned games their scores."""
    db = setup(monkeypatch, tmp_path, budget="50")
    _fake_rawg(monkeypatch)
    with db:
        db.execute("INSERT INTO items(id,title,system,status,wanted) "
                   "VALUES('own','In Hand','snes','VERIFIED',0)")
        db.execute("INSERT INTO items(id,title,system,status,wanted) "
                   "VALUES('wish','On The List','snes','CATALOGED',1)")

    def boom(db_, slug, title):
        raise RuntimeError("wishlist pass exploded")
    monkeypatch.setattr(community, "_rawg_search",
                       lambda db_, slug, title: (boom(db_, slug, title)
                                                 if title == "On The List"
                                                 else {"title": title, "score": 80, "votes": 10}))
    community.sync(db=db)
    row = db.execute("SELECT community_score FROM items WHERE id='own'").fetchone()
    assert row["community_score"] == 80