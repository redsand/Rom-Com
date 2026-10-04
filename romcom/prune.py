"""Thin catalog systems that were imported from dump-collection DATs.

GoodGBA-style sets catalog every known dump of every game — every region, every
trainer release, every bad dump — plus tens of thousands of homebrew and
public-domain entries, so one real library of ~2,700 GBA games arrives as
30,499 items. The dashboard counts them, organize copies them to the card, and
the community sync asks RAWG to score roms RAWG has never heard of.

Two rules, mirroring dupes.py's discipline:

- Junk goes outright, even when it is the only copy: bad/overdumped dumps
  (`[b…]`, `[o…]`) and homebrew or public-domain titles (`(PD)`, PocketNES /
  Goomba / Pictureboy emulator rips). They were never games worth a card slot.
- Retail variants compete, and the best dump wins — `_rank` prefers an item
  that actually holds files (deleting a download to keep an empty catalog row
  destroys content), then an unmodified dump over a trained/hacked/fixed one,
  then the newest revision ((Rev 2) over (Rev 1), (v2.01) over (v1.00)), then
  English regions/languages, and for a Japan-only game the newest translation
  patch. Pre-releases ((Beta), (Proto), (Demo), (Sample), (Preview)) are
  unofficial — they lose to a retail sibling and only survive as a region's
  sole entry.

Two scopes: the default folds a whole group to one keeper per game;
`per_region` (the "official" mode for Redump-style systems) keeps one keeper
per REGION of a game — `(Europe)`, `(France)` and `(Japan)` each keep their
best dump, so every regional retail release survives.

Never touched: keep=1 items, EXCLUDED rows, devices, and arcade — MAME's
parent/clone variants are deliberate structure, not waste. `local` adoptions ARE
in scope: the fullset downloads adopted on scan are where most of the junk lives
(26k of gba's 30k are local-adopted GoodGBA rips), and their titles carry the
same objective dump codes. Deletion is opt-in (`--apply` with an explicit
`--system`), preceded by a manifest in backups/ carrying the full item row and
every file hash, and every removal is an event on the surviving keeper.
"""
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .db import connect

HOMEBREW_PREFIXES = ("pocketnes", "goomba", "pictureboy")

# Parenthetical tokens that decorate a dump rather than distinguish a game.
# Anything else — (Disc 1), (God Mode), (Collector's Edition) — is identity.
_REGIONS = {
    "U", "UE", "JU", "JUE", "USA", "US", "NA", "NORTH AMERICA", "WORLD",
    "E", "EU", "EUROPE", "J", "JA", "JAPAN",
    "F", "FR", "FRANCE", "FRENCH", "G", "DE", "D", "GERMANY", "GERMAN",
    "I", "IT", "ITALY", "ITALIAN", "S", "ES", "SPAIN", "SPANISH",
    "SW", "SV", "SWEDEN", "SWEDISH", "B", "BR", "BRAZIL", "C", "CANADA",
    "A", "AU", "AUSTRALIA", "ASIA", "K", "KO", "KOREA", "KOREAN", "ZH", "CHINA",
    "NL", "H", "HOLLAND", "NETHERLANDS", "DUTCH", "FI", "FINLAND", "NO", "NORWAY",
    "PT", "PORTUGAL", "PL", "POLAND", "RU", "RUSSIA",
    "EN", "ENGLISH",
    "M1", "M2", "M3", "M4", "M5", "M6", "M8", "M10", "M12", "M16",
    "BETA", "PROTO", "PROTOTYPE", "SAMPLE", "DEMO", "PREVIEW",
    "NTSC", "PAL",
}

# Pre-release markers: unofficial by definition — they lose to a retail sibling.
_PRE = {"PROTO", "PROTOTYPE", "SAMPLE", "DEMO", "PREVIEW"}

# Region words: the first paren group made entirely of these names the release's
# region in per-region mode — (Europe), (USA, Europe), (Japan, Korea).
_REGION_WORDS = {
    "U", "E", "J", "UE", "JU", "JUE", "EU", "USA", "US", "WORLD",
    "EUROPE", "JAPAN", "JA", "FRANCE", "F", "GERMANY", "G", "ITALY", "I",
    "SPAIN", "S", "SWEDEN", "SW", "NETHERLANDS", "NL", "HOLLAND", "CANADA",
    "C", "BRAZIL", "B", "AUSTRALIA", "A", "ASIA", "KOREA", "K", "KO",
    "CHINA", "HONG KONG", "TAIWAN", "RUSSIA", "R", "ARGENTINA", "MEXICO",
    "INDIA", "DENMARK", "N", "NORWAY", "FINLAND", "FIN", "POLAND", "PL",
    "PORTUGAL", "PT", "GREECE", "GR", "TURKEY", "UAE", "SOUTH AFRICA",
    "SINGAPORE", "MALAYSIA", "INDONESIA", "THAILAND", "ISRAEL",
    "NEW ZEALAND", "CHILE", "COLOMBIA", "PERU", "SAUDI ARABIA",
}

# Titles adopted from filenames keep the rom's extension; fold that away too.
_EXTENSION = re.compile(r"\.(z64|v64|n64|gba|gbc|gb|nes|fds|sfc|smc|nds|3ds|iso|cue|bin|"
                        r"wbfs|gcm|nsp|xci|vhd|dsk|adf|col|rom|zip|7z)$")

# Region-rank vocabulary: which paren tokens say "English-market dump" vs
# "European dump" vs "Japanese dump". Language codes (En, De, Es…) count as
# their language, so (World) (En) beats (World) (De).
_ENGLISH = {"U", "UE", "JU", "JUE", "USA", "US", "NA", "NORTH AMERICA", "WORLD",
            "EN", "ENGLISH"}
_EUROPE = {"E", "EU", "EUROPE", "M1", "M2", "M3", "M4", "M5", "M6", "M8", "M10",
           "M12", "M16", "F", "FR", "FRANCE", "FRENCH", "G", "DE", "GERMANY",
           "GERMAN", "I", "IT", "ITALY", "ITALIAN", "S", "ES", "SPAIN",
           "SPANISH", "SW", "SV", "SWEDEN", "SWEDISH", "H", "NL", "HOLLAND",
           "NETHERLANDS", "DUTCH", "DA", "DANISH", "FI", "FINNISH", "NO",
           "NORWEGIAN", "PT", "PORTUGUESE", "PL", "POLISH", "RU", "RUSSIAN"}


def _junk_reason(title):
    """Why this title is removed even as the only copy, or None."""
    t = title or ""
    low = t.lower()
    if "[b" in t or "[o" in t:
        return "bad dump"
    if "(pd)" in low or low.startswith(HOMEBREW_PREFIXES):
        return "homebrew"
    return None


def _decoration(token):
    """Is this parenthetical token region/language/revision/trainer decoration?"""
    t = token.strip()
    if t.upper() in _REGIONS:
        return True
    if re.fullmatch(r"[A-Z]{1,3}\d*", t):            # UE, JUE, M5, F…
        return True
    if re.fullmatch(r"[Tt][+-]\d+", t):              # t+1 trainer
        return True
    if re.fullmatch(r"[Vv]\d+(\.\d+)*", t):          # v1.0, v1.1 revision
        return True
    if re.fullmatch(r"[Rr]ev\.? ?[A-Z0-9]?", t):     # Rev A
        return True
    return False


def _base_key(title):
    """The game underneath the dump codes. Bracket groups fold away only when
    they are a translation tag ([T-En by …]) or a short dump code ([b1], [hI],
    [f_4]) — anything long enough to be a name ([Redesign], [2020-09-17]) is
    identity. Paren groups fold away when every token is region/language/
    revision/trainer decoration — and once a trainer group appears, the ripper
    credits behind it ((UE)(t+1)(Anthrox)) are decoration too. (Disc 1) and
    (God Mode) survive as identity. GoodGBA's leading release number folds,
    and so does a filename's rom extension (local adoptions keep `.z64`)."""
    t = title or ""

    def bracket(m):
        inner = m.group(1)
        if re.match(r"^[Tt][+-]", inner):
            return ""
        if len(inner) <= 5 and " " not in inner:
            return ""
        return m.group(0)

    t = re.sub(r"\[([^\]]*)\]", bracket, t)

    trained = False

    def paren(m):
        nonlocal trained
        toks = m.group(1).split(",")
        if any(re.fullmatch(r"[Tt][+-]\d+", x.strip()) for x in toks):
            trained = True
        if trained or all(_decoration(x) for x in toks):
            return ""
        return m.group(0)

    t = re.sub(r"\(([^)]*)\)", paren, t)
    t = re.sub(r"^\s*\d+\s*-\s*", "", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return _EXTENSION.sub("", t)


def _translation(title):
    """(has a translation patch, its version) — for a Japan-only game the
    translated dump is the one worth keeping, and newer patches beat older."""
    m = re.search(r"\[[Tt][+-][^\]]*?(\d+(?:\.\d+)?)", title or "")
    return (m is not None, float(m.group(1)) if m else 0.0)


def _region(title):
    """The release's region, from the first paren group made entirely of region
    words — (Europe), (USA, Europe), (Japan, Korea). None when no group names one."""
    for m in re.finditer(r"\(([^)]*)\)", title or ""):
        toks = [x.strip().upper() for x in m.group(1).split(",")]
        if toks and all(x in _REGION_WORDS for x in toks):
            return ",".join(toks)
    return None


def _prerelease(title):
    """(Beta), (Beta 1), (Proto), (Demo), (Sample), (Preview) — unofficial."""
    toks = {x.strip().upper() for m in re.finditer(r"\(([^)]*)\)", title or "")
            for x in m.group(1).split(",")}
    return any(x in _PRE or x.startswith("BETA") for x in toks)


def _revision(title):
    """The dump's revision, negated so lower sorts first (the newest wins):
    (Rev 2) over (Rev 1), (Rev A) = 1, (v2.01) over (v1.00), and version
    chains like (V9.3) over (V8.1) compare part by part."""
    best = ()
    for m in re.finditer(r"\(([^)]*)\)", title or ""):
        g = m.group(1).strip()
        mm = re.fullmatch(r"[Rr]ev\.? ?([A-Za-z]|\d+)", g)
        if mm:
            n = mm.group(1)
            parts = (ord(n.upper()) - 64,) if n.isalpha() else tuple(int(x) for x in n)
            best = max(best, parts)
        mm = re.fullmatch(r"[Vv](\d+(?:\.\d+)*)", g)
        if mm:
            best = max(best, tuple(int(x) for x in mm.group(1).split(".")))
    # Negated and flagged so the newest revision sorts first; no revision at all
    # sorts last — the latest dump is the one worth keeping.
    return (0,) + tuple(-x for x in best) if best else (1,)


def _rank(it):
    """Lower sorts first, so the keeper is g[0]. Official beats pre-release;
    a held file beats an empty row (never delete a download to keep one); an
    unmodified dump beats a trained/hacked/fixed one; then the newest revision,
    then English regions/languages, then a translation (a Japan-only game's
    playable dump), then size, then name for determinism."""
    translated, version = _translation(it["title"])
    return (1 if _prerelease(it["title"]) else 0,
            0 if it["files"] else 1,
            1 if _modified(it["title"]) else 0,
            _revision(it["title"]),
            _region_rank(it["title"]),
            0 if translated else 1,
            -version,
            -sum(f["bytes"] or 0 for f in it["files"]),
            it["title"], it["id"])


def _region_rank(title):
    """(market rank, language rank), lower sorts first: English-market dumps
    (U/USA/World), then European; within a market, the English-language dump
    beats the German/French/Spanish one."""
    t = title or ""
    toks = {x.strip().upper() for x in re.findall(r"\(([^)]*)\)", t)}
    if toks & _ENGLISH:
        market = 0
    elif toks & _EUROPE:
        market = 1
    elif toks & {"J", "JAPAN", "JA"}:
        market = 3
    else:
        market = 4
    lang = 1 if (toks & _EUROPE and "EN" not in toks
                 and not toks & {"U", "UE", "JU", "JUE", "USA", "US"}) else 0
    return (market, lang)


def _modified(title):
    """Trained, hacked or fixed dumps are real content of the wrong kind."""
    t = title or ""
    return "(t+" in t or "[h" in t or "[f" in t


def _items(db, systems=None):
    """Eligible items with their files: everything except keep-marked and
    excluded rows, devices, and arcade (whose parent/clone structure is
    deliberate). `local` adoptions are in scope — the fullset rips adopted on
    scan are where most of the junk lives."""
    q = """SELECT i.id, i.title, i.system, i.status, i.keep, i.catalog_source,
                  f.path, f.bytes, f.sha1, f.md5, f.crc32
           FROM items i LEFT JOIN files f ON f.matched_item_id=i.id
           WHERE COALESCE(i.is_device,0)=0 AND i.status<>'EXCLUDED'
             AND COALESCE(i.keep,0)=0
             AND lower(COALESCE(i.system,''))<>'arcade'"""
    args = []
    if systems:
        q += " AND lower(COALESCE(i.system,'')) IN (%s)" % ",".join("?" * len(systems))
        args = [s.lower() for s in systems]
    out = {}
    for r in db.execute(q, args).fetchall():
        it = out.setdefault(r["id"], {"id": r["id"], "title": r["title"],
                                       "system": r["system"], "files": []})
        if r["path"]:
            it["files"].append({"path": r["path"], "bytes": r["bytes"],
                                "sha1": r["sha1"], "md5": r["md5"], "crc32": r["crc32"]})
    return out


def _plan(db, systems=None, per_region=False):
    """removals: item_id -> the item with a reason and, for variants, its keeper.
    One bucket per game by default; one per REGION in the official mode, so
    every regional retail release survives."""
    removals = {}
    groups = defaultdict(list)
    for it in _items(db, systems).values():
        reason = _junk_reason(it["title"])
        if reason:
            removals[it["id"]] = {**it, "reason": reason}
        else:
            groups[(it["system"] or "unknown", _base_key(it["title"]))].append(it)
    for g in groups.values():
        buckets = defaultdict(list)
        for x in g:
            buckets[(_region(x["title"]) or "?") if per_region else None].append(x)
        for bucket in buckets.values():
            if len(bucket) < 2:
                continue
            # Pre-releases are unofficial: they only compete when the region has
            # no retail release at all.
            retail = [x for x in bucket if not _prerelease(x["title"])]
            pool = retail or bucket
            pool.sort(key=_rank)
            keeper = next((x for x in pool if x["files"]), pool[0])
            for x in bucket:
                if x is not keeper:
                    removals[x["id"]] = {**x,
                                         "reason": "pre-release" if _prerelease(x["title"]) else "variant",
                                         "kept": {"id": keeper["id"], "title": keeper["title"]}}
    return removals


def audit(db=None, systems=None, per_region=False):
    """What a prune would remove, per system, without touching anything."""
    removals = _plan(db or connect(), systems, per_region)
    by_system = {}
    for r in removals.values():
        s = by_system.setdefault(r["system"] or "unknown",
                                 {"items": 0, "junk": 0, "pre_releases": 0,
                                  "variants": 0, "bytes": 0})
        s["items"] += 1
        key = {"bad dump": "junk", "homebrew": "junk",
               "pre-release": "pre_releases", "variant": "variants"}[r["reason"]]
        s[key] += 1
        s["bytes"] += sum(f["bytes"] or 0 for f in r["files"])
    sample = sorted(removals.values(),
                    key=lambda r: -(sum(f["bytes"] or 0 for f in r["files"])))[:6]
    return {"items": len(removals),
            "junk": sum(1 for r in removals.values() if r["reason"] == "bad dump"
                        or r["reason"] == "homebrew"),
            "pre_releases": sum(1 for r in removals.values() if r["reason"] == "pre-release"),
            "variants": sum(1 for r in removals.values() if r["reason"] == "variant"),
            "bytes": sum(sum(f["bytes"] or 0 for f in r["files"]) for r in removals.values()),
            "by_system": by_system,
            "sample": [{"title": r["title"], "system": r["system"], "reason": r["reason"],
                        "files": len(r["files"])} for r in sample]}


def cleanup(apply=False, systems=None, per_region=False, db=None):
    """Remove the junk and the losing variants. Reports what it would do unless
    `apply`; applying requires an explicit `systems` scope — this deletes catalog
    rows, so 'everywhere' is never the default."""
    db = db or connect()
    if apply and not systems:
        raise ValueError("prune --apply needs --system: it deletes catalog rows, "
                         "so scope it (e.g. --system gba)")
    removals = _plan(db, systems, per_region)
    report = {"applied": bool(apply), **{k: v for k, v in audit(db, systems, per_region).items()
                                         if k != "sample"},
              "deleted": 0, "files_deleted": 0, "bytes_freed": 0,
              "errors": [], "manifest": None,
              "sample": audit(db, systems, per_region)["sample"]}
    if not apply or not removals:
        return report

    # The manifest precedes the deletions (fixnames' rule): the full item row and
    # every file hash, so a removal is reversible by record.
    rows = {}
    for r in removals.values():
        rows[r["id"]] = dict(db.execute("SELECT * FROM items WHERE id=?", (r["id"],)).fetchone())
    Path("backups").mkdir(exist_ok=True)
    manifest = Path("backups") / f"prune-removed-{datetime.now():%Y%m%d-%H%M%S}.json"
    manifest.write_text(json.dumps({
        "at": datetime.now().isoformat(timespec="seconds"),
        "note": "junk and losing dump-variant items removed; 'kept' is the surviving "
                "entry the content was folded into, 'item' the full catalog row",
        "removed": [{**{k: r[k] for k in ("id", "system", "title", "reason")},
                     "kept": r.get("kept"), "item": rows[r["id"]], "files": r["files"]}
                    for r in sorted(removals.values(), key=lambda r: r["id"])]},
        indent=1), encoding="utf-8")
    report["manifest"] = str(manifest)

    for r in sorted(removals.values(), key=lambda r: r["id"]):
        # A locked file (an export may be reading it) keeps the whole item in place.
        try:
            for f in r["files"]:
                Path(f["path"]).unlink(missing_ok=True)
        except OSError as e:
            report["errors"].append({"id": r["id"], "error": str(e)})
            continue
        with db:
            db.execute("DELETE FROM files WHERE matched_item_id=?", (r["id"],))
            db.execute("DELETE FROM items WHERE id=?", (r["id"],))
            if r.get("kept"):
                db.execute("INSERT INTO events(item_id,event,detail) VALUES(?, 'prune-removed', ?)",
                           (r["kept"]["id"], f"{r['title']} ({r['reason']})"))
        report["deleted"] += 1
        report["files_deleted"] += len(r["files"])
        report["bytes_freed"] += sum(f["bytes"] or 0 for f in r["files"])
    return report