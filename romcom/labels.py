"""What a game is — a genre, plus tags that say whether it is a game worth a card slot.

Arcade sets are described by catver.ini (Progetto-SNAPS, one category per MAME set:
'Shooter / Flying Vertical', 'Slot Machine / Reels', 'Computer / Business'). MAME's
catalog is every machine it emulates, so a library of "arcade" is a quarter fruit
machines and office computers; the tags are what let an export leave those out.

Every system also gets tags from the dump's own name: No-Intro and friends mark hacks,
translations, prototypes, demos, unlicensed and pirate releases in brackets.

    genre        shooter, fighter, sports…  (kind 'genre'; arcade only, for now)
    fruit-machine, gambling, medal, redemption, mahjong, quiz, tabletop, mature
    not-a-game   computers, BIOS/system boards, devices, utilities, electromechanical
    hack, translation, homebrew, prototype, demo, beta, unlicensed, pirate, bootleg

`refresh` rewrites `item_labels` whole from these sources with the owner's
`label_overrides` applied on top: the owner says 'this is not a fruit machine' once and
every later refresh keeps it."""
import re
from pathlib import Path

from .db import connect
from . import memtrim

ROOT = Path(__file__).resolve().parent.parent

# What an export leaves out unless told otherwise: not games, or games of chance.
DEFAULT_EXCLUDE = ("fruit-machine", "gambling", "not-a-game")

# Tags shown as export choices, in this order, with what each means.
TAGS = {
    "fruit-machine": "fruit / slot machines",
    "gambling": "casino, poker, medal and other games of chance",
    "not-a-game": "computers, BIOS boards, devices, utilities",
    "mahjong": "mahjong and hanafuda",
    "quiz": "quiz games",
    "mature": "adult content",
    "redemption": "ticket redemption machines",
    "hack": "ROM hacks",
    "translation": "fan translations",
    "trainer": "cheat / trainer builds",
    "homebrew": "homebrew and public domain",
    "prototype": "unreleased prototypes",
    "demo": "demos, samples and kiosk builds",
    "beta": "beta builds",
    "unlicensed": "unlicensed releases",
    "pirate": "pirate releases",
    "bootleg": "arcade bootlegs",
    "wrapped": "another system's game inside a GBA emulator (PocketNES, SNESAdvance, PCEAdvance…)",
}

# catver's top level -> tags. Anything absent is a game genre and gets no tag.
_CAT_TAGS = {
    "Slot Machine": ("fruit-machine", "gambling"),
    "Gambling": ("gambling",),
    "Medal Game": ("gambling",),
    "Redemption Game": ("redemption",),
    "Quiz": ("quiz",),
    "Computer": ("not-a-game",),
    "System": ("not-a-game",),
    "Utilities": ("not-a-game",),
    "Calculator": ("not-a-game",),
    "Telephone": ("not-a-game",),
    "Medical Equipment": ("not-a-game",),
    "Music Player": ("not-a-game",),
    "Musical Instrument": ("not-a-game",),
    "Musical Instrument Accessory": ("not-a-game",),
    "Electromechanical": ("not-a-game",),     # pinball/EM: MAME runs no playfield
    "Printer": ("not-a-game",),
    "Board Game": (),
}

# The dump's own name. Each pattern runs case-insensitively on the title.
_NAME_TAGS = (
    ("hack", re.compile(r"\((?:[^)]*[\s,])?hack\b|\[h\d*[a-z]*\]|\[h[ \]]")),
    ("homebrew", re.compile(r"\((?:homebrew|aftermarket|pd|public domain)\)")),
    ("prototype", re.compile(r"\(proto(?:type)?\b")),
    ("demo", re.compile(r"\((?:[^)]*\s)?(?:demo|sample|kiosk)\b")),
    ("beta", re.compile(r"\(beta\b")),
    ("unlicensed", re.compile(r"\(unl\)|\(unlicensed\)")),
    ("pirate", re.compile(r"\(pirate\)")),
    ("not-a-game", re.compile(r"\[bios\]|\((?:test )?program\)|\(enhancement chip\)")),
    # GBA's public-domain pile names itself outside brackets: e-books ([ebb]), picture
    # slideshows, coding libraries' samples ('HEL Library V2.0 ... by Peter Schraut'),
    # music players.
    ("not-a-game", re.compile(r"\[eb[ab]\]|\bslide ?show\b|\bhel library\b|\bmusic player\b"
                              r"|\bmp3 player\b|\bpicture viewer\b|\breadboy\b"
                              r"|\bgoodbook advance\b|\(chinese book\b")),
    # The NES, SNES, PC Engine, Master System and Spectrum game is in its own folder,
    # where it runs natively; the GBA build of it inside an emulator is a second copy.
    ("wrapped", re.compile(r"^(?:pocketnes|snesadvance|pceadvance|drsms|smsadvance"
                           r"|zx advance|fc2gba|gbagi|goomba|cologne)\b|\(mb2gba\)")),
    ("homebrew", re.compile(r"\[pd\]|\bv\d+(?:\.\d+)+\b[^()]*\bby [a-z]")),
    ("demo", re.compile(r"\b(?:tech |intro )?demo\b(?! ?land)(?!lition)")),
    ("beta", re.compile(r"\bpublic beta\b")),
)
# GoodTools codes are case-sensitive: [T+Eng] is a translation, [t1] a trainer, [p1] a
# pirate dump. These run on the title as written.
_CASED_TAGS = (
    ("translation", re.compile(r"\[T[+\-]|\([Tt]ranslat|\bT[+-]Eng\b")),
    ("trainer", re.compile(r"\[t\d*\]|\[t\d*[ +]")),
    ("pirate", re.compile(r"\[p\d*\]")),
)
_BOOTLEG = re.compile(r"\(\s*bootleg|\bbootleg\)|, bootleg")

_SET = "substr(external_id, instr(external_id,'/')+1)"


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def catver_path():
    hits = sorted((ROOT / "DAT").rglob("catver.ini"))
    return hits[0] if hits else None


_catver = {}


def catver(path=None):
    """{setname: category} from catver.ini's [Category] section, cached per mtime."""
    path = path or catver_path()
    if not path:
        return {}
    memtrim.use("labels.catver", _catver)
    key = (str(path), Path(path).stat().st_mtime)
    if key not in _catver:
        out, on = {}, False
        for line in Path(path).open(encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("["):
                on = line == "[Category]"
            elif on and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        _catver.clear()
        _catver[key] = out
    return _catver[key]


def from_category(category):
    """'Shooter / Flying Vertical * Mature *' -> ({labels: kind}). The top level is the
    genre; a top level in `_CAT_TAGS` adds what kind of machine it is; Mahjong anywhere
    in the path is mahjong, whichever top level it files under."""
    if not category:
        return {}
    mature = "* Mature *" in category
    parts = [p.strip() for p in category.replace("* Mature *", "").split("/")]
    top = parts[0]
    out = {"genre:" + slug(top): "genre"} if top else {}
    for t in _CAT_TAGS.get(top, ()):
        out[t] = "tag"
    if any(p in ("Mahjong", "Hanafuda", "Home Mahjong") for p in parts):
        out["mahjong"] = "tag"
    if mature:
        out["mature"] = "tag"
    return out


def from_name(title):
    t = (title or "").lower()
    out = {tag: "tag" for tag, pat in _NAME_TAGS if pat.search(t)}
    out.update({tag: "tag" for tag, pat in _CASED_TAGS if pat.search(title or "")})
    return out


def _derive_row(r, cats):
    got = {k: (v, "name") for k, v in from_name(r["title"]).items()}
    if (r["system"] or "").lower() == "arcade":
        got.update({k: (v, "catver") for k, v in from_category(cats.get(r["s"])).items()})
        if r["dev"]:
            got["not-a-game"] = ("tag", "device")
        if _BOOTLEG.search((r["title"] or "").lower()):
            got["bootleg"] = ("tag", "name")
    return got


_ROW = f"SELECT id, title, system, COALESCE(is_device,0) dev, {_SET} s FROM items"


def derive(db):
    """{item_id: {label: (kind, source)}} for every catalog item, before overrides."""
    cats = catver()
    out = {}
    for r in db.execute(_ROW):
        got = _derive_row(r, cats)
        if got:
            out[r["id"]] = got
    return out


def refresh(db=None):
    """Rewrite `item_labels` from catver and the dump names, owner overrides applied.
    Returns {label: items} for the labels it wrote."""
    db = db or connect()
    labels = derive(db)
    for r in db.execute("SELECT item_id, label, action FROM label_overrides"):
        cur = labels.setdefault(r["item_id"], {})
        if r["action"] == "remove":
            cur.pop(r["label"], None)
        else:
            cur[r["label"]] = ("genre" if r["label"].startswith("genre:") else "tag", "mine")
    rows = [(iid, lab, kind, src) for iid, got in labels.items()
            for lab, (kind, src) in got.items()]
    with db:
        db.execute("DELETE FROM item_labels")
        db.executemany("INSERT INTO item_labels(item_id,label,kind,source) VALUES(?,?,?,?)", rows)
    counts = {}
    for _, lab, _, _ in rows:
        counts[lab] = counts.get(lab, 0) + 1
    return counts


def set_override(item_id, label, action, db=None):
    """The owner adds or removes a label on one item ('add' / 'remove'), or with
    action None drops their word and lets the derived label stand. Applied at once."""
    db = db or connect()
    label = (label or "").strip().lower()
    if not label:
        raise ValueError("label required")
    with db:
        if action is None:
            db.execute("DELETE FROM label_overrides WHERE item_id=? AND label=?",
                       (item_id, label))
        elif action in ("add", "remove"):
            db.execute("""INSERT INTO label_overrides(item_id,label,action) VALUES(?,?,?)
                          ON CONFLICT(item_id,label) DO UPDATE SET action=excluded.action,
                          updated_at=CURRENT_TIMESTAMP""", (item_id, label, action))
        else:
            raise ValueError(f"action must be add, remove or None, not {action!r}")
        db.execute("DELETE FROM item_labels WHERE item_id=? AND label=?", (item_id, label))
        if action is None:
            derived = derive_one(db, item_id).get(label)
            if derived:
                db.execute("INSERT INTO item_labels(item_id,label,kind,source) VALUES(?,?,?,?)",
                           (item_id, label, *derived))
        elif action == "add":
            db.execute("INSERT INTO item_labels(item_id,label,kind,source) VALUES(?,?,?,'mine')",
                       (item_id, label, "tag"))


def derive_one(db, item_id):
    r = db.execute(_ROW + " WHERE id=?", (item_id,)).fetchone()
    return _derive_row(r, catver()) if r else {}


def normalize_exclude(exclude):
    """None means the default; a list (even empty) is the caller's choice. Unknown tags
    are dropped — a browser is not trusted with the spelling."""
    if exclude is None:
        return list(DEFAULT_EXCLUDE)
    if isinstance(exclude, str):
        exclude = [x for x in exclude.split(",") if x]
    return [x for x in exclude if x in TAGS]


def excluded_items(exclude, db=None):
    """Item ids carrying any of `exclude`."""
    exclude = normalize_exclude(exclude)
    if not exclude:
        return set()
    db = db or connect()
    return {r[0] for r in db.execute(
        f"SELECT DISTINCT item_id FROM item_labels WHERE label IN ({','.join('?' * len(exclude))})",
        exclude)}


def excluded_sets(sets, exclude, db=None):
    """Arcade setnames among `sets` carrying any of `exclude` — from the stored labels
    where the catalog has the set, from catver directly where it does not (a romset dat
    can define sets the catalog never listed)."""
    exclude = set(normalize_exclude(exclude))
    if not exclude:
        return set()
    db = db or connect()
    stored, known = {}, set()
    for r in db.execute(f"""SELECT {_SET} s, l.label FROM items i
                            LEFT JOIN item_labels l ON l.item_id=i.id
                            WHERE i.system='arcade'"""):
        known.add(r["s"])
        if r["label"]:
            stored.setdefault(r["s"], set()).add(r["label"])
    cats = catver()
    out = set()
    for s in sets:
        labs = stored.get(s, set()) if s in known else set(from_category(cats.get(s)))
        if labs & exclude:
            out.add(s)
    return out


def counts(db=None, held_only=True):
    """{label: items} — over the games in hand by default, which is what an owner choosing
    what to leave off a card is counting."""
    db = db or connect()
    held = ("AND i.status IN ('FOUND','DOWNLOADED','VERIFIED','NORMALIZED','INSTALLED','TESTED')"
            if held_only else "")
    return {r["label"]: r["n"] for r in db.execute(
        f"""SELECT l.label, l.kind, COUNT(*) n FROM item_labels l JOIN items i ON i.id=l.item_id
            WHERE 1=1 {held} GROUP BY l.label, l.kind ORDER BY n DESC""")}


def for_items(ids, db=None):
    """{item_id: [labels]} for a page of library rows, genre first."""
    db = db or connect()
    ids = list(ids)
    out = {}
    for i in range(0, len(ids), 800):
        chunk = ids[i:i + 800]
        for r in db.execute(
                f"""SELECT item_id, label, kind FROM item_labels
                    WHERE item_id IN ({','.join('?' * len(chunk))})
                    ORDER BY kind='genre' DESC, label""", chunk):
            out.setdefault(r["item_id"], []).append(r["label"])
    return out
