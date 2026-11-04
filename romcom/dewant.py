"""Take categories nobody wants out of the acquisition backlog.

The catalog is a DAT's whole world — and a No-Intro set is half language tutors,
regional crosswords and Japanese-only software no English-language player can use.
Armed by the thousand, they are the acquirer's whole queue: it burns its sweeps on
"no usable result" for a German crossword, while the games worth having wait behind
it, and the owner watches a feed that never downloads anything.

`dewant` names the categories in the owner's own words — tutors, print, Japan-only —
and takes only the BACKLOG: a fileless CATALOGED/MISSING item. A game the library
holds is never touched (its `wanted` is a fact about the owner, not the queue), and
arcade is never touched (its Japan-only games are half the point of the set).
Nothing is deleted; `wanted` and `authorized` go to 0, and flipping an item back to
wanted in the library UI undoes it.
"""
import re

from .db import connect

# Language courses, tutors, and the educational shelf.
TUTOR = re.compile(
    r"\b(learn\w*|lernhilfen?|lernen|vokabel\w*|vocab\w*|kanji|katakana|hiragana"
    r"|eigo|kotoba|zatsuyou|kanken|eiken|toeic|toefl|hōyaku)\b"
    r"|\b(english|englisch|french|fran\w+|franz\w+|spanish|spanisch|italian"
    r"|italienisch|german|deutsch|japanese)\b[\w ]{0,14}"
    r"\b(coach|buddy|trainer|training|course|class|lessons?|words?|vocabulary|lab)\b"
    r"|\b(coach|buddy)\b[\w ]{0,14}\b(english|french|german|italian|spanish|japanese)\b"
    r"|\b(teacher|tutor|nachhilfe|schulfuxx|schul-?|preschool|kindergarten"
    r"|educational|driving test theory)\b",
    re.I)

# Print: newspapers, crosswords, dictionaries, magazines — things to read, not play.
PRINT = re.compile(
    r"kreuzwort|zeitung|magazine|newspaper|newspaper|wortschatz|sprach\w+"
    r"|dictionar\w+|thesaurus|crossword|word ?search|sudoku trainer"
    r"|sueddeutsche|bild-?zeitung|asahi|mainichi|yomiuri|nikkei|pocket magazine"
    r"|\bPONS\b|langenscheidt|duden|kompas",
    re.I)

# A game only Japan ever got, in a language the owner cannot read: the region marker
# with no English-speaking region or (En) language tag beside it. "(Japan, USA) (En)"
# is not this — an English release exists and the game is wanted.
JAPAN_ONLY = re.compile(r"\((?:Japan|J)\)")
ENGLISH = re.compile(r"\(En|\(USA|\(U\)|\(Europe|\(E\)|\(World|\(Australia|\(Canada")

# Catalog noise: a DAT's bookkeeping rows wearing a game's clothes — "4471_New-9FA55393…",
# "5768 - 19FF3134 TO B47571DC", "[Wi-Fi] …". A numbered game title ("1942", "007: …")
# is a game; the payload after the id has to be hex for the row to be bookkeeping.
JUNK_ID = re.compile(
    r"^\s*(?:z\d{3,4}|\d{3,5}|no\s?\d{2,5}|disc\s?\d+)\s*[-_:~]"
    r"\s*(?:new|old)?-?[0-9A-Fa-f]{4,}"
    r"|\b(?:new|old)-?[0-9A-Fa-f]{8}\b"
    r"|[0-9A-Fa-f]{8}\s*(?:to|TO)\s*[0-9A-Fa-f]{4,}"
    r"^\s*\[Wi-?Fi\]", re.I | re.X)

CATEGORIES = {
    "tutor": lambda t: bool(TUTOR.search(t)),
    "print": lambda t: bool(PRINT.search(t)),
    "japan-only": lambda t: bool(JAPAN_ONLY.search(t)) and not ENGLISH.search(t),
    "junk": lambda t: bool(JUNK_ID.search(t)),
}

_EXCLUDED_SYSTEMS = {"arcade"}


def candidates(systems=None, db=None):
    """[(item_id, system, title, category)] the backlog holds in the unwanted
    categories — read-only, the report a dry run prints."""
    db = db or connect()
    out = []
    q = """SELECT id, system, title FROM items
           WHERE wanted=1 AND status IN ('CATALOGED','MISSING')
             AND lower(system) NOT IN ('arcade','romhacks')
             AND NOT EXISTS (SELECT 1 FROM files WHERE matched_item_id=items.id)"""
    args = []
    if systems:
        q += f" AND system IN ({','.join('?' * len(systems))})"
        args = list(systems)
    for r in db.execute(q, args):
        for cat, is_it in CATEGORIES.items():
            if is_it(r["title"] or ""):
                out.append((r["id"], r["system"], r["title"], cat))
                break
    return out


def dewant(apply=False, systems=None, db=None):
    """Take the unwanted categories out of the backlog. Returns
    {"count", "by_category", "by_system", "sample", "applied"}; nothing is written
    without `apply`."""
    db = db or connect()
    rows = candidates(systems, db)
    by_cat, by_sys = {}, {}
    for _, sysname, _, cat in rows:
        by_cat[cat] = by_cat.get(cat, 0) + 1
        by_sys[sysname] = by_sys.get(sysname, 0) + 1
    sample = {}
    for _, _, title, cat in rows:
        sample.setdefault(cat, []).append(title)
    out = {"count": len(rows), "by_category": by_cat, "by_system": by_sys,
           "sample": {c: t[:8] for c, t in sample.items()}, "applied": bool(apply)}
    if apply and rows:
        with db:
            for i in range(0, len(rows), 900):
                chunk = [r[0] for r in rows[i:i + 900]]
                db.execute(f"""UPDATE items SET wanted=0, authorized=0,
                               updated_at=CURRENT_TIMESTAMP
                               WHERE id IN ({','.join('?' * len(chunk))})""", chunk)
    return out