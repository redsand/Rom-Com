"""Community scores from public pools, so "what should I play next" has an answer
that isn't a guess.

The owner rates what they play (items.rating) — this module is the other half: what
the crowd thinks. Scores land in community_scores (raw, per source, so a second source
can never silently overwrite a first) and denormalized into items.community_score as
the best available, which makes "top games" one indexed ORDER BY.

Two providers, chosen for being free, and honest about what each actually offers:

RAWG still has true community votes: a 0-5 `rating` with `ratings_count`. We bulk-pull
the top-rated games per platform once (a handful of calls per system) and match locally,
then spend the *daily* budget on per-item searches only for in-hand games the bulk pull
missed — the free tier is 20k requests a month and a naive per-item fetch would burn it
on a catalog of 250k rows in an afternoon.

RetroAchievements removed game *ratings* in a site redesign (API_GetGameRating.php now
returns 410 Gone; verified against the RAWeb source). What it still has is how many
people actually played each game (NumDistinctPlayers) — a popularity signal from
exactly the retro audience, fetched per matched in-hand game and converted to a
within-console percentile so a portable's player count can be compared to a console's.

Neither score ever touches status/verification: community opinion ranks games, it does
not prove them, and an item with no file stays exactly as unclaimed as it was.
"""
import json
import os
import random
import re
import threading
import time
from datetime import date

import requests

from .config import settings
from .db import connect
from .searchcache import cached
from .indexer import clean_query, _tokens

UA = "Rom-Com/1.0 (personal ROM library manager)"
RAWG_BASE = "https://api.rawg.io/api"
RA_BASE = "https://retroachievements.org/API"

# RAWG: platforms pulled per system, pages per platform, and the minimum votes below
# which a community rating is one person's opinion wearing a number.
RAWG_PAGES = 5
RAWG_PAGE_SIZE = 40
RAWG_MIN_VOTES = 3
# Per-item RAWG searches per sync — the safety valve on the monthly budget even when
# the daily budget is set high.
RAWG_SEARCH_MAX = 200
# RA: player counts fetched per sync. First sync starts cold; each later one tops up,
# so a library of thousands converges over a few runs instead of one multi-hour crawl.
RA_FETCH_CAP = 600
# An RA player count older than this is eligible for a refresh.
RA_STALE_DAYS = 30
# Popularity with fewer players than this is indistinguishable from noise.
RA_MIN_PLAYERS = 5

# Our system slugs -> the names these sites call the same hardware. Exact matches are
# preferred; substring is the fallback for names like "Sega Genesis/Mega Drive".
_ALIASES = {
    "nes": ["nes", "nintendo entertainment system", "famicom"],
    "snes": ["snes", "super nintendo entertainment system", "super nintendo", "super famicom"],
    "n64": ["nintendo 64"], "nds": ["nintendo ds"], "3ds": ["nintendo 3ds"],
    "gb": ["game boy"], "gbc": ["game boy color"], "gba": ["game boy advance"],
    "virtualboy": ["virtual boy"],
    "genesis": ["genesis", "mega drive", "sega mega drive", "sega genesis"],
    "mastersystem": ["sega master system", "master system"],
    "gamegear": ["sega game gear", "game gear"],
    "segacd": ["sega cd", "segacd", "mega-cd", "mega cd"],
    "32x": ["sega 32x", "32x"], "saturn": ["sega saturn", "saturn"],
    "dreamcast": ["sega dreamcast", "dreamcast"],
    "ps1": ["playstation", "playstation 1", "ps1"],
    "ps2": ["playstation 2", "ps2"], "ps3": ["playstation 3", "ps3"],
    "psp": ["playstation portable", "psp"], "vita": ["playstation vita", "ps vita", "vita"],
    "wii": ["nintendo wii", "wii"], "wiiu": ["wii u"],
    "gamecube": ["nintendo gamecube", "gamecube"],
    "arcade": ["arcade"],
    "atari2600": ["atari 2600", "2600"], "atari7800": ["atari 7800"],
    "lynx": ["atari lynx", "lynx"],
    "neogeopocket": ["neo geo pocket", "neo geo pocket color"],
    "pcengine": ["pc engine", "turbografx", "turbografx-16"],
    "pcenginecd": ["pc engine cd", "turbografx cd"],
    "msx": ["msx"], "msx2": ["msx"],
    "wonderswan": ["wonderswan"], "wonderswancolor": ["wonderswan color"],
    "x68000": ["x68000"], "amiga": ["amiga"], "c64": ["commodore 64"],
    "3do": ["3do"], "cdi": ["cd-i", "philips cd-i"], "dos": ["dos", "pc dos"],
}

_PACE_LOCK = threading.Lock()
_LAST = {"rawg": 0.0, "ra": 0.0}


def _pace(source):
    """Space requests out; each source has its own politeness delay with jitter."""
    if source == "rawg":
        delay, jitter = settings()["rawg_delay"], settings()["rawg_jitter"]
    else:
        delay, jitter = settings()["ra_delay"], 0.0
    pause = max(0.0, float(delay)) + (random.uniform(0, jitter) if jitter else 0.0)
    with _PACE_LOCK:
        wait = _LAST[source] + pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST[source] = time.monotonic()


def _state(db, source, key):
    row = db.execute("SELECT v FROM community_state WHERE source=? AND k=?",
                     (source, key)).fetchone()
    return row["v"] if row else None


def _set_state(db, source, key, value):
    db.execute("INSERT INTO community_state(source,k,v) VALUES(?,?,?) "
               "ON CONFLICT(source,k) DO UPDATE SET v=excluded.v", (source, key, str(value)))


def _spend(db, source, n=1):
    """Count a request against the source's daily budget. Returns the new count."""
    today = date.today().isoformat()
    if _state(db, source, "date") != today:
        _set_state(db, source, "date", today)
        _set_state(db, source, "count", 0)
    count = int(_state(db, source, "count") or 0) + n
    _set_state(db, source, "count", count)
    return count


def _budget_left(db, source):
    today = date.today().isoformat()
    if _state(db, source, "date") != today:
        return settings()["rawg_budget"]
    cap = settings()["rawg_budget"]
    return max(0, cap - int(_state(db, source, "count") or 0))


# ---------------------------------------------------------------- provider plumbing

def rawg_key():
    """RAWG's API key. os.getenv, never settings(): the config.py secret convention —
    a key outside _env_settings() cannot be persisted, echoed, or edited from the UI."""
    return (os.getenv("RAWG_API_KEY") or "").strip()


def ra_credentials():
    """RetroAchievements username + web API key (same secret convention as RAWG's)."""
    return (os.getenv("RA_USERNAME") or "").strip(), (os.getenv("RA_API_KEY") or "").strip()


def providers_ready(db=None):
    """Which providers have both their switch and their credentials."""
    s = settings()
    out = {"rawg": bool(s["rawg_enabled"]) and bool(rawg_key()),
           "ra": bool(s["ra_enabled"]) and all(ra_credentials())}
    return out


def _request(url, params, source, db, budget=True):
    """Paced GET with retry on transient errors. Raises on exhaustion; the sync's
    per-system try/except turns one flaky platform into a report line, not an abort."""
    attempt = 0
    while True:
        if budget:
            if _budget_left(db, "rawg" if source == "rawg" else "ra") <= 0:
                raise RuntimeError(f"{source} daily request budget exhausted")
        _pace(source)
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA},
                             timeout=settings()["rawg_timeout" if source == "rawg" else "ra_timeout"])
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            r.raise_for_status()
            if budget:
                _spend(db, "rawg" if source == "rawg" else "ra")
            return r.json()
        except requests.RequestException:
            if attempt >= 3:
                raise
            attempt += 1
            time.sleep(2 * attempt)


def _match_platform(names, slug):
    """Which of a site's platform names is our slug, if any. Exact alias match wins;
    substring is the fallback for names like 'Sega Genesis/Mega Drive'."""
    norm = {n: re.sub(r"\s+", " ", n.lower()).strip() for n in names}
    wanted = _ALIASES.get(slug, [])
    for name, n in norm.items():
        if n in wanted:
            return name
    best = None
    for name, n in norm.items():
        for a in sorted(wanted, key=len, reverse=True):
            if a and a in n:
                best = best or name
    return best


def _norm_title(title):
    return clean_query((title or "").lower())


def _index(records):
    """Token-set -> record index, for exact-title matching without an API call."""
    out = {}
    for r in records:
        t = frozenset(_tokens(_norm_title(r["title"])))
        if t:
            out.setdefault(t, r)
    return out


def _match_items(items, records):
    """Match catalog items to a provider's game records. Exact normalized-token match
    first; then a tolerant pass where our tokens are a subset of the record's, so
    'Forgotten Worlds' finds 'Forgotten Worlds (World)' too. The smallest such record
    wins (closest match). The record may exceed the item by at most one token — a
    decoration clean_query leaves behind ('(Arcade)', '(Demo)'), never a different game:
    without that guard 'Super Mario' claimed 'Super Mario Bros. 3' and inherited its
    score. The reverse direction (record ⊂ item) never matches at all."""
    index = _index(records)
    matched = {}
    for item in items:
        toks = frozenset(_tokens(_norm_title(item["title"])))
        if not toks:
            continue
        if toks in index:
            matched[item["id"]] = index[toks]
            continue
        best, best_len = None, None
        for rt, r in index.items():
            if toks <= rt and len(rt) - len(toks) <= 1:
                if best is None or len(rt) < best_len:
                    best, best_len = r, len(rt)
        if best:
            matched[item["id"]] = best
    return matched


# ---------------------------------------------------------------- RAWG

def rawg_platforms(db):
    def fetch():
        out, url = [], f"{RAWG_BASE}/platforms"
        params = {"key": rawg_key(), "page_size": 40}
        for _ in range(5):  # ~70 platforms exist; 5 pages is the ceiling
            data = _request(url, params, "rawg", db)
            out += [p["name"] for p in data.get("results", [])]
            if not data.get("next"):
                break
            params["page"] = len(out) // 40 + 1
        return out
    return cached("rawg", "platforms", fetch)


def _rawg_platform_id(db, slug):
    names = rawg_platforms(db)
    # The /platforms listing gives names only; /games needs the numeric id, which arrives
    # via the games endpoint's platform objects. Fetch the id from one probe query.
    name = _match_platform(names, slug)
    if not name:
        return None, None
    def fetch():
        data = _request(f"{RAWG_BASE}/platforms", {"key": rawg_key(), "page_size": 40}, "rawg", db)
        for page in [data] + [_request(f"{RAWG_BASE}/platforms", {"key": rawg_key(), "page_size": 40, "page": p}, "rawg", db) for p in range(2, 6)]:
            for p in page.get("results", []):
                if p["name"].lower().strip() == name.lower().strip():
                    return {"id": p["id"], "name": p["name"]}
        return {}
    rec = cached("rawg", f"platform-id:{name}", fetch)
    return (rec or {}).get("id"), name


def rawg_top_games(db, slug):
    """A platform's top-rated games — the bulk half of RAWG. ~5 calls per system,
    so the whole library's popular games cost dozens of requests, not thousands."""
    pid, name = _rawg_platform_id(db, slug)
    if not pid:
        return None
    def fetch():
        out, params = [], {"key": rawg_key(), "platforms": pid, "ordering": "-rating",
                           "page_size": RAWG_PAGE_SIZE}
        for page in range(1, RAWG_PAGES + 1):
            params["page"] = page
            data = _request(f"{RAWG_BASE}/games", params, "rawg", db)
            for g in data.get("results", []):
                if g.get("rating") and (g.get("ratings_count") or 0) >= RAWG_MIN_VOTES:
                    out.append({"title": g["name"], "score": round(g["rating"] * 20),
                                "votes": g.get("ratings_count"),
                                "year": (g.get("released") or "")[:4] or None})
            if not data.get("next"):
                break
        return out
    return cached("rawg", f"top:{slug}", fetch, ttl_minutes=60 * 24 * 14)


def _rawg_search(db, slug, title):
    """One item's RAWG lookup — the expensive half, which is why it is budget-gated
    and only spent on in-hand games the bulk pull missed."""
    pid, _ = _rawg_platform_id(db, slug)
    params = {"key": rawg_key(), "search": clean_query(title), "page_size": 3}
    if pid:
        params["platforms"] = pid
    data = _request(f"{RAWG_BASE}/games", params, "rawg", db)
    best = None
    qt = _tokens(_norm_title(title))
    for g in data.get("results", []):
        if not g.get("rating") or (g.get("ratings_count") or 0) < RAWG_MIN_VOTES:
            continue
        rt = _tokens(_norm_title(g["name"]))
        if not qt:
            continue
        overlap = len(qt & rt) / max(1, len(qt | rt))
        if overlap >= 0.6 and (best is None or overlap > best[0]):
            best = (overlap, g)
    if not best:
        return None
    g = best[1]
    return {"title": g["name"], "score": round(g["rating"] * 20),
            "votes": g.get("ratings_count"), "year": (g.get("released") or "")[:4] or None}


# ---------------------------------------------------------------- RetroAchievements

def ra_consoles(db):
    def fetch():
        user, key = ra_credentials()
        return _request(f"{RA_BASE}/API_GetConsoleIDs.php",
                        {"y": key, "z": user}, "ra", db, budget=False)
    return cached("ra", "consoles", fetch, ttl_minutes=60 * 24 * 14)


def ra_games(db, console_id):
    def fetch():
        user, key = ra_credentials()
        data = _request(f"{RA_BASE}/API_GetGameList.php",
                        {"y": key, "z": user, "c": console_id}, "ra", db, budget=False)
        return [{"title": g["Title"], "gid": g["ID"]} for g in data]
    return cached("ra", f"games:{console_id}", fetch, ttl_minutes=60 * 24 * 14)


def _ra_console_id(db, slug):
    name = _match_platform([c["Name"] for c in ra_consoles(db)], slug)
    if not name:
        return None, None
    for c in ra_consoles(db):
        if c["Name"] == name:
            return c["ID"], c["Name"]
    return None, None


def ra_players(db, gid):
    """How many distinct people have played this game — RA's popularity signal since
    ratings were removed from the site."""
    user, key = ra_credentials()
    data = _request(f"{RA_BASE}/API_GetGameExtended.php",
                    {"y": key, "z": user, "i": gid}, "ra", db)
    players = (data.get("NumDistinctPlayers") or 0)
    return int(players)


# ---------------------------------------------------------------- the sync

def _targets(db, systems=None):
    """What's worth scoring: in hand (or arcade-playable) plus anything wanted.
    Devices are never candidates; hardware has no reputation."""
    q = f"""SELECT id, title, system, year FROM items
        WHERE COALESCE(is_device,0)=0
        AND ((status IN ('FOUND','DOWNLOADED','VERIFIED','NORMALIZED','INSTALLED','TESTED'))
             OR (system='arcade' AND playable=1) OR wanted=1)"""
    p = []
    if systems:
        q += f" AND system IN ({','.join('?' * len(systems))})"
        p = list(systems)
    return db.execute(q, p).fetchall()


def _write_scores(db, rows):
    """rows: {item_id, source, score, votes, matched_title}. Raw records first, then
    the denormalized best-of, in one transaction so the watcher never sees half a sync."""
    if not rows:
        return 0
    with db:
        for r in rows:
            db.execute("""INSERT INTO community_scores(item_id,source,score,votes,matched_title,fetched_at)
                VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(item_id,source) DO UPDATE SET
                score=excluded.score, votes=excluded.votes,
                matched_title=excluded.matched_title, fetched_at=CURRENT_TIMESTAMP""",
                (r["item_id"], r["source"], r["score"], r["votes"], r["matched_title"]))
        # Best available = highest score; ties go to the more-voted (more trusted) source.
        db.execute("""UPDATE items SET
            community_score=(SELECT MAX(score) FROM community_scores cs WHERE cs.item_id=items.id),
            community_source=(SELECT source FROM community_scores cs WHERE cs.item_id=items.id
                ORDER BY score DESC, votes DESC LIMIT 1)
            WHERE id IN (SELECT DISTINCT item_id FROM community_scores)""")
    return len(rows)


def sync(systems=None, db=None, progress=None):
    """Pull community scores for everything worth having an opinion about. Returns a
    report; every provider failure is a line in it, not an aborted sync."""
    db = db or connect()
    ready = providers_ready(db)
    report = {"providers": ready, "systems": {}, "scored": 0, "errors": []}
    if not any(ready.values()):
        report["errors"].append(
            "no provider configured — set RAWG_API_KEY / RA_USERNAME+RA_API_KEY in .env "
            "and enable rawg_enabled / ra_enabled in Settings")
        return report
    targets = _targets(db, systems)
    by_system = {}
    for t in targets:
        by_system.setdefault(t["system"], []).append(dict(t))

    all_rows = []
    for slug, items in sorted(by_system.items()):
        if progress:
            progress(len(report["systems"]), len(by_system), slug)
        per = {"matched": 0, "rawg": 0, "ra": 0}
        # ---- RAWG: bulk top games, then budgeted per-item searches for the misses.
        if ready["rawg"]:
            try:
                item_ids = {x["id"] for x in items}
                top = rawg_top_games(db, slug)
                if top:
                    bulk = _match_items(items, top)
                    for iid, rec in bulk.items():
                        all_rows.append({"item_id": iid, "source": "rawg",
                                         "score": rec["score"], "votes": rec["votes"],
                                         "matched_title": rec["title"]})
                in_hand = [i for i in items if i["id"] not in
                           {r["item_id"] for r in all_rows if r["source"] == "rawg"}]
                searched = 0
                for i in in_hand:
                    if searched >= RAWG_SEARCH_MAX or _budget_left(db, "rawg") <= 0:
                        break
                    try:
                        rec = _rawg_search(db, slug, i["title"])
                    except Exception:
                        break  # budget exhausted or transient failure — stop cleanly
                    searched += 1
                    if rec:
                        all_rows.append({"item_id": i["id"], "source": "rawg",
                                         "score": rec["score"], "votes": rec["votes"],
                                         "matched_title": rec["title"]})
                per["rawg"] = sum(1 for r in all_rows
                                  if r["source"] == "rawg" and r["item_id"] in item_ids)
            except Exception as ex:
                report["errors"].append(f"rawg/{slug}: {ex}")
        # ---- RA: game list per console, then player counts for matched in-hand games.
        if ready["ra"]:
            try:
                cid, cname = _ra_console_id(db, slug)
                if cid:
                    games = ra_games(db, cid)
                    hits = _match_items(items, games)
                    per["matched"] += len(hits)
                    fresh_cut = f"-{RA_STALE_DAYS} days"
                    done = {r["item_id"] for r in db.execute(
                        "SELECT item_id FROM community_scores WHERE source='ra' "
                        "AND fetched_at > datetime('now', ?)", (fresh_cut,))}
                    todo = [(iid, g) for iid, g in hits.items() if iid not in done]
                    fetched = {}
                    for iid, g in todo[:RA_FETCH_CAP]:
                        try:
                            players = ra_players(db, g["gid"])
                        except Exception:
                            break  # budget/burst — keep what we have
                        if players >= RA_MIN_PLAYERS:
                            fetched[iid] = {"players": players, "title": g["title"]}
                    # Percentile within the console: the point is rank, and a portable's
                    # player counts live on a different scale than a console's.
                    ordered = sorted(fetched.items(), key=lambda kv: kv[1]["players"])
                    for pos, (iid, rec) in enumerate(ordered, 1):
                        all_rows.append({"item_id": iid, "source": "retroachievements",
                                         "score": round(100.0 * pos / max(1, len(ordered))),
                                         "votes": rec["players"],
                                         "matched_title": rec["title"]})
                    per["ra"] = len(fetched)
            except Exception as ex:
                report["errors"].append(f"ra/{slug}: {ex}")
        report["systems"][slug] = per

    report["scored"] = _write_scores(db, all_rows)
    report["scored_rows"] = len(all_rows)
    return report


def status(db=None):
    """What the community layer knows, for the CLI and Settings tab."""
    db = db or connect()
    ready = providers_ready()
    rows = {r["source"]: dict(r) for r in db.execute(
        "SELECT source, COUNT(*) n, AVG(score) avg, MAX(fetched_at) last"
        " FROM community_scores GROUP BY source")}
    covered = db.execute("SELECT COUNT(*) c FROM items WHERE community_score IS NOT NULL"
                         " AND COALESCE(is_device,0)=0").fetchone()["c"]
    return {"providers": ready, "covered_items": covered, "by_source": rows,
            "rawg_budget_left": _budget_left(db, "rawg"),
            "rawg_budget": settings()["rawg_budget"]}


def test():
    """Connectivity probes for the Settings tab."""
    db = connect()
    out = {}
    if settings()["rawg_enabled"]:
        if not rawg_key():
            out["rawg"] = {"ok": False, "detail": "RAWG_API_KEY not set in .env"}
        else:
            try:
                _request(f"{RAWG_BASE}/platforms", {"key": rawg_key(), "page_size": 1},
                         "rawg", db, budget=False)
                out["rawg"] = {"ok": True, "detail": ""}
            except Exception as ex:
                out["rawg"] = {"ok": False, "detail": str(ex)[:200]}
    if settings()["ra_enabled"]:
        if not all(ra_credentials()):
            out["retroachievements"] = {"ok": False, "detail": "RA_USERNAME/RA_API_KEY not set in .env"}
        else:
            try:
                data = ra_consoles(db)
                out["retroachievements"] = {"ok": True, "detail": f"{len(data)} consoles"}
            except Exception as ex:
                out["retroachievements"] = {"ok": False, "detail": str(ex)[:200]}
    return out