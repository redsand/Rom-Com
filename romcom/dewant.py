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

The queue also repeats itself. A Redump catalog lists every region's pressing of a
game, every demo and magazine disc; a No-Intro one lists the Virtual Console, Evercade
and Limited Run re-releases of a cartridge the library has held for years. Three more
categories take those out:

  demo     demos, betas, prototypes, kiosk, bonus and magazine discs
  have-it  another edition of a game the library already holds, in a form as readable
  variant  several editions of one game nobody holds yet: the best stays in the queue

Each item is taken out once. It leaves a 'dewant' event behind, and an item that has
one is never taken again: marking it wanted afterwards is the owner's answer.
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
    r"|^\s*\[Wi-?Fi\]"
    r"|^(?:BOOT2|MIOS|BC|IOS\d+|RVL)-",        # a Wii's own system software
    re.I | re.X)

# Not the game: a demo, a pre-release build, the disc a magazine or a shop gave away.
DEMO = re.compile(
    r"\((?:[^)]*\s)?(?:demo|beta|proto(?:type)?|sample|kiosk|preview|trial|taikenban"
    r"|bonus dis[ck]|promo)\b[^)]*\)"
    r"|official xbox magazine|\bdemo dis[ck]\b|\bdemo$", re.I)

# Systems whose catalog does not name editions the way a DAT does (a title-id list, a
# compatibility table): two titles that fold alike there are two games.
_NO_EDITIONS = {"switch", "scummvm", "windows", "dos"}

_ENGLISH_REGIONS = {"USA", "WORLD", "EUROPE", "AUSTRALIA", "UK", "CANADA", "U", "E", "UE",
                    "JU", "JUE", "W"}
_LANGS = {"EN", "JA", "FR", "DE", "ES", "IT", "NL", "PT", "SV", "NO", "DA", "FI", "RU",
          "KO", "ZH", "PL", "CS", "EL", "TR", "HU", "AR", "HE", "CA"}
_CHANGED = re.compile(r"\[T[+-]|\bhack\b|\[h|\[f|\(t\+", re.I)


def _tokens(title):
    return {x.strip().upper() for g in re.findall(r"\(([^)]*)\)", title or "")
            for x in g.split(",")}


def _english(title):
    """Can an English-language player use this edition? A language list decides;
    without one the region does; a title with neither is given the benefit."""
    toks = _tokens(title)
    if re.search(r"\[T[+-]En", title or "", re.I):
        return True
    if toks & _LANGS:
        return "EN" in toks
    if toks & _ENGLISH_REGIONS:
        return True
    return not re.search(r"\((?:[A-Z][a-z]+(?:, )?)+\)", title or "")


def _game(title):
    """The game under an edition's tags: every (…) and […] folds away except the disc
    number, so (USA), (Europe) (En,Fr,De), (Virtual Console) and (Limited Run Games)
    are one game and (Disc 2) is not (Disc 1)."""
    t = title or ""
    disc = re.search(r"\((dis[ck] \d+)\)", t, re.I)
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", t)
    t = re.sub(r"[^a-z0-9+]+", " ", t.lower()).strip()
    return f"{t} {disc.group(1).lower()}" if disc else t


def _edition_rank(title):
    """Lower is the edition worth queueing: readable, the original release rather than a
    patched dump or a re-release, an English market first."""
    toks = _tokens(title)
    extra = sum(1 for x in toks if x not in _LANGS and x not in _ENGLISH_REGIONS
                and not re.fullmatch(r"REV ?\w+|V[\d.]+|DIS[CK] \d+", x))
    return (0 if _english(title) else 1, 1 if _CHANGED.search(title or "") else 0,
            0 if toks & {"USA", "WORLD", "U", "UE"} else 1, extra, title or "")

CATEGORIES = {
    "tutor": lambda t: bool(TUTOR.search(t)),
    "print": lambda t: bool(PRINT.search(t)),
    "japan-only": lambda t: bool(JAPAN_ONLY.search(t)) and not ENGLISH.search(t),
    "junk": lambda t: bool(JUNK_ID.search(t)),
    "demo": lambda t: bool(DEMO.search(t)),
}

_EXCLUDED_SYSTEMS = {"arcade"}


def candidates(systems=None, db=None):
    """[(item_id, system, title, category)] the backlog holds in the unwanted
    categories — read-only, the report a dry run prints."""
    db = db or connect()
    out = []
    q = """SELECT id, system, title FROM items
           WHERE wanted=1 AND status IN ('CATALOGED','MISSING')
             AND lower(system) NOT IN ('arcade','romhacks') AND COALESCE(keep,0)=0
             AND NOT EXISTS (SELECT 1 FROM files WHERE matched_item_id=items.id)
             AND NOT EXISTS (SELECT 1 FROM events e WHERE e.item_id=items.id
                             AND e.event='dewant')"""
    args = []
    if systems:
        q += f" AND system IN ({','.join('?' * len(systems))})"
        args = list(systems)
    rest = {}
    for r in db.execute(q, args):
        for cat, is_it in CATEGORIES.items():
            if is_it(r["title"] or ""):
                out.append((r["id"], r["system"], r["title"], cat))
                break
        else:
            if (r["system"] or "").lower() not in _NO_EDITIONS:
                rest.setdefault(((r["system"] or "").lower(), _game(r["title"])), []).append(r)
    if not rest:
        return out
    # The editions of those games the library holds: a real copy (not a demo, not a
    # patched dump) answers for every edition no more readable than it is.
    held = {}
    for r in db.execute("""SELECT DISTINCT i.system, i.title FROM items i
                           JOIN files f ON f.matched_item_id=i.id
                           WHERE lower(i.system) NOT IN ('arcade','romhacks')"""):
        t = r["title"] or ""
        key = ((r["system"] or "").lower(), _game(t))
        if key in rest and not DEMO.search(t) and not _CHANGED.search(t):
            held[key] = held.get(key, False) or _english(t)
    for key, group in rest.items():
        if key in held:
            for r in group:
                if held[key] or not _english(r["title"]):
                    out.append((r["id"], r["system"], r["title"], "have-it"))
        elif len(group) > 1:
            group.sort(key=lambda r: _edition_rank(r["title"]))
            for r in group[1:]:
                out.append((r["id"], r["system"], r["title"], "variant"))
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
            db.executemany("INSERT INTO events(item_id,event,detail) VALUES(?,'dewant',?)",
                           [(r[0], r[3]) for r in rows])
    return out


def undo(category=None, db=None):
    """Put back what `dewant` took out (one category, or all of it): wanted and
    authorized again. The 'dewant' events stay, so the next run leaves them alone."""
    db = db or connect()
    q = ("SELECT DISTINCT e.item_id FROM events e JOIN items i ON i.id=e.item_id "
         "WHERE e.event='dewant' AND i.wanted=0")
    args = []
    if category:
        q += " AND e.detail=?"
        args.append(category)
    ids = [r[0] for r in db.execute(q, args)]
    with db:
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            db.execute(f"""UPDATE items SET wanted=1, authorized=1, updated_at=CURRENT_TIMESTAMP
                           WHERE id IN ({','.join('?' * len(chunk))})""", chunk)
    return {"restored": len(ids), "category": category}