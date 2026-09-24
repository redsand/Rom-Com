"""Recommendations, or: why the assistant burned 100 tool calls and still could not
name five games.

Nothing in the catalog said how good a game was, so "give me the top 5 I should play"
had no answer to compute — the chat brute-forced franchise names one query at a time
and never concluded. Now there are three signals to rank with: the owner's rating
(items.rating), the crowd's score (items.community_score), and keep. This module turns
them into one query.

The ranking rule is deliberately simple and explainable: a game ranks by the highest
opinion anyone holds about it (the crowd's score, or the owner's rating — either can
vouch), minus a penalty when the owner has already kept or played it, because "what
should I play next" is a question about the *unplayed* shelf.

Junk is excluded at query time: the in-hand pool contains .srm save files, text
walkthroughs, homebrew demos, bootlegs and photo booths adopted as games, and
recommending any of them is exactly the "playing random games" the owner complained
about. verify.classify is the same judge the downloader uses, so one definition of
junk governs what comes in and what gets suggested.
"""
from .db import connect
from .verify import classify

import re

# What one opinion is worth relative to another. The owner's own rating outranks a
# crowd score it ties with — they are the one playing it.
_RATING_BOOST = 10   # an owner 1-10 rating, scaled to the crowd's 0-100
_KEEP_PENALTY = 15   # already earmarked for the card: stop suggesting it
_PLAYED_PENALTY = 25  # already played: suggest it last, not first

# "What should I play" means retail games. (PD) homebrew, aftermarket roms, demos and
# prototypes are real catalog entries — the archivist rule keeps every one — and
# classify() cannot flag them because it judges filenames, while these titles arrive
# bare. Without this filter the rank-0 fallback sorted by title and put a (PD) GBA
# advertisement at #1 of "top 5 games".
_NOT_A_GAME = re.compile(r"\((?:PD|Homebrew|Aftermarket|Demo|Proto|Beta|Sample)\)", re.I)


def top(db=None, system=None, n=5, include_played=False):
    """The n best games to play next, as plain dicts. In-hand only — a recommendation
    you must download first is a shopping list, not an answer."""
    db = db or connect()
    sql = """SELECT id, title, system, year, rating, community_score, community_source,
                    keep, last_played, playable, status
             FROM items WHERE COALESCE(is_device,0)=0
             AND ((status IN ('FOUND','DOWNLOADED','VERIFIED','NORMALIZED','INSTALLED','TESTED')
                   AND system != 'arcade')
                  OR (system='arcade' AND playable=1))"""
    p = []
    if system:
        sql += " AND system=?"
        p.append(system)
    if not include_played:
        sql += " AND last_played IS NULL"
    rows = [dict(r) for r in db.execute(sql, p)]
    # Junk filter in Python: classify() reads the title, and the SQL pool is already
    # bounded to in-hand items (tens of thousands, not 250k).
    out = []
    for r in rows:
        verdict = classify(r["title"], r["system"])
        if verdict == "junk" or _NOT_A_GAME.search(r["title"] or ""):
            continue
        crowd = r["community_score"] or 0
        own = (r["rating"] or 0) * _RATING_BOOST
        rank_ = max(crowd, own)
        if r["keep"]:
            rank_ -= _KEEP_PENALTY
        r["rank"] = rank_
        if own > 0 and own >= crowd:
            r["how"] = "you rated it"
        elif crowd:
            r["how"] = f"{r['community_source'] or 'crowd'} ranks it {crowd}"
        else:
            r["how"] = "in your library"
        out.append(r)
    out.sort(key=lambda r: (-r["rank"], r["title"].lower()))
    return out[:n]


def summary(db=None, system=None, n=5, include_played=False):
    """top() shaped for the chat tool and the API: same dicts, fewer bytes on the wire."""
    rows = top(db=db, system=system, n=n, include_played=include_played)
    return {"recommendations": [
        {"id": r["id"], "title": r["title"], "system": r["system"], "year": r["year"],
         "rating": r["rating"], "community_score": r["community_score"],
         "community_source": r["community_source"], "why": r["how"], "rank": r["rank"]}
        for r in rows]}