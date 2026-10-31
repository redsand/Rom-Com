"""Index what is on an SD card, and work out what should come off it.

A card is the one part of a library that leaves the building. It is written here,
carried to a handheld, played on, and carried back — and nothing used to remember what
was on it, so an export could only ever add to a card or wipe a platform whole. There was
no way to say "that game fell out of the recipe, take it off", or "this new game is
better than that one, make room".

This module is the memory:

- `index_card` reads a card and records what is on it — in the database, and in a
  manifest the card carries with it;
- `plan_update` works out what would come off and what would go on, touching nothing;
- `apply_removals` does the removals, writing down what it removed first.

The rule that shapes all of it: an update only ever removes what RomCom knows it put
there, or what the recipe no longer selects, and it never removes anything the library
cannot name. A card is removable media, and the owner may well have put their own files
on it.
"""
from datetime import datetime
from pathlib import Path
import json
import os
import shutil
import uuid

from .db import connect

# The card's own record of itself, at the card's root. A dotfile: it sits beside ROMs/,
# ES-DE/ and system/, and none of those code paths walk the card root (esde's
# adopt_roms_layout moves only known ES-DE system folders), so nothing else ever sees it.
MANIFEST = ".romcom-card.json"
VERSION = 1

# Folders at the card's root that hold no games.
_NOT_GAMES = {"system", "es-de", "bios", "retroarch"}


# ------------------------------------------------------------------ the card's own identity
def card_root(card):
    """The card itself: `card`, or the card above a ROMs folder."""
    from . import esde
    return esde.card_root(card)


def manifest_path(card):
    return card_root(card) / MANIFEST


def read_manifest(card):
    """The card's own record of itself, or None when it has none.

    A card written by hand, or by a RomCom older than this, has no manifest: it is an
    untracked card until it is indexed.
    """
    try:
        data = json.loads(manifest_path(card).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_manifest(card, data):
    """Write the card's manifest, atomically.

    A half-written manifest would lose the card's identity, and a card that cannot be
    recognised is a card that cannot be told apart from the owner's own files. The
    temporary file plus replace is the same rule esde.write_gamelists follows.
    """
    path = manifest_path(card)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path


def identify(card, db=None, create=True):
    """The card's id in the database, minting one the first time it is seen.

    A card's identity is a uuid written onto the card in its manifest, so the same card
    in a different reader — a different drive letter, even a different machine — is still
    the same card. The path is a convenience only: a card that has been moved is
    recognised by its manifest, and a card whose manifest is gone is a new card.

    A card that states an identity the database has never seen is a NEW card, even if
    something else used to sit in that drive letter. The card's own word beats the
    reader's memory of the slot: otherwise a card whose database was rebuilt would
    inherit the old card's index, and the two would be told apart by nothing at all.

    A cloned card carries the original's uuid, so it is taken for the same card. That is
    harmless: an update works from what is physically on the mounted card, and two copies
    of one card hold the same games.

    A card with no manifest is matched by drive letter only to a card that has never
    carried an id of its own (`cards.uuid` is NULL). A card that has carried one would
    be wearing it now; a card in its slot without it is a different card, and taking it
    for the old one would overwrite the old card's index and stamp its id on a stranger.
    """
    db = db or connect()
    card = card_root(card)
    path = str(card)
    manifest = read_manifest(card)
    cid = (manifest or {}).get("card_id")
    row = None
    if cid:
        row = db.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
        if row is None:
            if not create:
                return None
            with db:
                db.execute("INSERT OR IGNORE INTO cards(id,path,last_seen) "
                           "VALUES(?,?,CURRENT_TIMESTAMP)", (cid, path))
            row = db.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    else:
        # A card the app knows only by where it was last plugged in.
        row = db.execute("SELECT * FROM cards WHERE path=? AND id IS NOT NULL "
                         "AND uuid IS NULL ORDER BY updated_at DESC LIMIT 1",
                         (path,)).fetchone()
        if row is None:
            if not create:
                return None
            cid = uuid.uuid4().hex
            with db:
                db.execute("INSERT OR IGNORE INTO cards(id,path,last_seen) "
                           "VALUES(?,?,CURRENT_TIMESTAMP)", (cid, path))
            row = db.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    with db:
        db.execute("UPDATE cards SET path=?, last_seen=CURRENT_TIMESTAMP, "
                   "updated_at=CURRENT_TIMESTAMP WHERE id=?", (path, row["id"]))
    if cid:
        _carried(db, row["id"])
    return row["id"]


def _carried(db, cid):
    """Record that the card carries its id on the card, so the drive letter alone never
    names it again."""
    with db:
        db.execute("UPDATE cards SET uuid=id WHERE id=? AND uuid IS NULL", (cid,))


def stamp_identity(card, cid, manifest=None):
    """Give the card its own id on the card, so the id travels with the card.

    The database remembers a card by the drive letter it was last plugged into, which is
    a fact about the reader and not about the card. The uuid on the card is the fact
    about the card: the same card in a different reader is still the same card, and its
    index comes with it instead of starting again as a set of guesses.

    Best effort — a card that cannot be written to is still perfectly indexable, it just
    cannot carry its own name.
    """
    if manifest and manifest.get("card_id"):
        return False
    root = card_root(card)
    if not root.is_dir():
        return False
    try:
        write_manifest(card, {
            "card_id": cid, "written_at": datetime.now().isoformat(timespec="seconds"),
            "romcom_version": VERSION, "profile": (manifest or {}).get("profile") or {},
            "entries": (manifest or {}).get("entries") or []})
    except OSError:
        return False
    return True


# ------------------------------------------------------------------------ reading the card
def _tree_bytes(path):
    """The bytes a unit on the card takes up: the file, or everything under the folder.

    A game is not always a file. A disc dumped as cue+bin becomes a folder
    (`Game (USA).cue/`) that ES-DE lists as one game, and an unpacked archive becomes a
    folder of its members; the card's budget is spent on the whole tree.
    """
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _system_is_ranked(folder):
    """Whether a system folder is in the 'ranked' layout (<system>/<score>/ games).

    A score folder is named after the score, so a number. Anything else is a game the
    owner put there, and is left alone.
    """
    try:
        for c in folder.iterdir():
            if c.is_dir() and c.name.isdigit():
                return True
    except OSError:
        pass
    return False


# What ES-DE itself puts in every system folder it creates. Not a game: counted as one,
# every empty folder on the card read as a console with games, and the BIOS check asked
# an arcade-only card for twelve consoles' BIOS.
_ESDE_FILES = {"systeminfo.txt"}


def walk_card(card):
    """What is physically on the card: one unit per game ES-DE would list.

    Returns a list of dicts: folder (the ES-DE system folder), entry (the path ES-DE
    lists, relative to <card>/ROMs/<folder>), path (the unit to delete), bytes (its
    footprint), kind ("file" or "directory").

    The unit is the thing ES-DE shows and the thing that is deleted: a file, or a folder
    (a disc dumped as cue+bin, which ES-DE reads as one game). A game in a score folder
    ('ranked' layout) is listed with that folder in its path, so the entry still names
    exactly one game.
    """
    from . import esde
    card = card_root(card)
    roms = esde.roms_dir(card)
    out = []
    if not roms.is_dir():
        return out
    known = esde.system_folders()
    for top in sorted(p for p in roms.iterdir() if p.is_dir()):
        sys_ = top.name.lower()
        if sys_ in _NOT_GAMES or sys_.startswith(".") or sys_ not in known:
            continue
        ranked = _system_is_ranked(top)
        for child in sorted(top.iterdir()):
            if child.name.startswith(".") or child.name.lower() in _ESDE_FILES:
                continue
            if child.is_file():
                out.append({"folder": top.name, "entry": child.name, "path": child,
                            "bytes": _tree_bytes(child), "kind": "file"})
            elif child.is_dir() and not ranked:
                out.append({"folder": top.name, "entry": child.name, "path": child,
                            "bytes": _tree_bytes(child), "kind": "directory"})
            elif child.is_dir():
                # The 'ranked' layout: <system>/<score>/<game>.
                for g in sorted(child.iterdir()):
                    if g.name.startswith("."):
                        continue
                    out.append({"folder": top.name, "entry": f"{child.name}/{g.name}",
                                "path": g, "bytes": _tree_bytes(g),
                                "kind": "file" if g.is_file() else "directory"})
    return out


def _name_maps(db):
    """{filename in lower case: item_id} for the library's files, and the same for arcade
    set names.

    A card file is known by its name: an arcade zip by its set name, everything else by
    the name the export copied it under (the source file's own). The same two maps
    artwork.export_media builds, from the same tables.
    """
    by_file = {}
    for r in db.execute("SELECT path, matched_item_id FROM files "
                        "WHERE matched_item_id IS NOT NULL AND COALESCE(content,1)=1"):
        by_file.setdefault(Path(r["path"]).name.lower(), r["matched_item_id"])
    by_set = {}
    for r in db.execute("SELECT id, substr(external_id, instr(external_id,'/')+1) s FROM items "
                        "WHERE lower(system)='arcade'"):
        by_set.setdefault(r["s"], r["id"])
    return by_file, by_set


def _item_rows(db, ids):
    """{item_id: {title, score, keep}} for the items named, in one query."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        q = ",".join("?" * len(chunk))
        for r in db.execute(
                "SELECT id, title, keep, rating, community_score FROM items "
                f"WHERE id IN ({q})", chunk):
            out[r["id"]] = {"title": r["title"] or "",
                            "score": max(r["community_score"] or 0, (r["rating"] or 0) * 10),
                            "keep": bool(r["keep"])}
    return out


def _play_counts(card):
    """{entry path: {"playcount", "lastplayed", "favorite", "completed"}} from the card's
    own gamelists.

    ES-DE on the handheld keeps its own bookkeeping on the card: how many times a game
    was played, when, and whether it is a favourite. The library knows nothing of this
    (its own play data only moves when a game is launched from the PC), so the card is
    the only place the owner's play counts exist.
    """
    from . import esde
    out = {}
    root = card_root(card) / "ES-DE" / "gamelists"
    if not root.is_dir():
        return out
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        for entry, info in esde.read_gamelist(card_root(card), folder.name).items():
            out[(folder.name, entry)] = info
    return out


def index_card(card, db=None, progress=None):
    """Read a card and record what is on it.

    The index is a description of the card, not a log of intentions: the card is walked
    and what is there is what is recorded, so a card that was edited by hand, or a run
    that was interrupted, is described truthfully the next time it is read.

    An entry comes to be known two ways. One the export wrote is known exactly — the
    manifest records it, and the export knows what it put there. An entry found by
    walking a card that has no manifest is matched by NAME, which is a guess: the
    export's own `_copy_rank` calls a name match second-best to a hash-verified one. A
    guessed entry is recorded as such and is not evicted on rank until it has been seen
    on a card once.

    Returns a report: the card's id, how much was found, and what changed.
    """
    from . import esde
    db = db or connect()
    card = card_root(card)
    cid = identify(card, db)
    manifest = read_manifest(card) or {}
    # The card carries its own id from the first time it is seen, so the next time it is
    # plugged into a different reader it is recognised as the same card rather than
    # starting over as a set of guesses.
    if stamp_identity(card, cid, manifest):
        manifest = read_manifest(card) or {}
        _carried(db, cid)
    known = {(e.get("folder"), e.get("entry")): e
             for e in (manifest.get("entries") or []) if e.get("folder") and e.get("entry")}

    by_file, by_set = _name_maps(db)
    arcade_folders = set(esde.ARCADE_FOLDERS.values())
    found = walk_card(card)
    plays = _play_counts(card)
    # What the index already knew. "Tracked" is a fact about the past — RomCom put this
    # there — so a row that is already tracked stays tracked even when the manifest does
    # not mention it. Without this, every export's manifest would demote the games it did
    # not write this time to mere name-matched guesses, and they could never be evicted.
    prior = {(r["folder"], r["entry"]): dict(r)
             for r in db.execute("SELECT * FROM card_entries WHERE card_id=?", (cid,))}

    entries, seen = [], set()
    for u in found:
        key = (u["folder"], u["entry"])
        seen.add(key)
        m = known.get(key)
        if m is not None:
            # The manifest knows this one: RomCom wrote it.
            item_id, tracked = m.get("item_id"), 1
        elif key in prior and prior[key].get("item_id"):
            item_id, tracked = prior[key]["item_id"], prior[key]["tracked"]
        else:
            if u["folder"].lower() in arcade_folders:
                item_id = by_set.get(Path(u["entry"]).stem)
            else:
                item_id = by_file.get(Path(u["entry"]).name.lower())
            tracked = 0
        info = plays.get(key) or {}
        entries.append({"folder": u["folder"], "entry": u["entry"], "item_id": item_id,
                        "bytes": u["bytes"], "tracked": tracked,
                        "playcount": int(info.get("playcount") or 0),
                        "last_played": info.get("lastplayed"),
                        "favorite": bool(info.get("favorite"))})

    items = _item_rows(db, [e["item_id"] for e in entries if e["item_id"]])
    for e in entries:
        info = items.get(e["item_id"]) or {}
        e["title"] = info.get("title") or ""
        e["score"] = info.get("score") or 0
        e["keep"] = info.get("keep") or False

    # The index must describe the card, not a log of intentions, so a row for something
    # that is no longer on the card goes, and a row for something new arrives.
    with db:
        for e in entries:
            db.execute(
                "INSERT INTO card_entries(card_id,folder,entry,item_id,bytes,score,"
                "playcount,last_played,favorite,tracked) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(card_id,folder,entry) DO UPDATE SET item_id=excluded.item_id,"
                "bytes=excluded.bytes,score=excluded.score,playcount=excluded.playcount,"
                "last_played=excluded.last_played,favorite=excluded.favorite,"
                "tracked=excluded.tracked,"
                "updated_at=CURRENT_TIMESTAMP",
                (cid, e["folder"], e["entry"], e["item_id"], e["bytes"], e["score"],
                 e["playcount"], e["last_played"], 1 if e["favorite"] else 0, e["tracked"]))
        keep = {e["folder"] + "\x00" + e["entry"] for e in entries}
        for row in db.execute("SELECT folder,entry FROM card_entries WHERE card_id=?", (cid,)).fetchall():
            if row["folder"] + "\x00" + row["entry"] not in keep:
                db.execute("DELETE FROM card_entries WHERE card_id=? AND folder=? AND entry=?",
                           (cid, row["folder"], row["entry"]))

    return {"card_id": cid, "card": str(card), "entries": len(entries),
            "tracked": sum(1 for e in entries if e["tracked"]),
            "untracked": sum(1 for e in entries if not e["item_id"]),
            "bytes": sum(e["bytes"] for e in entries),
            "manifest": bool(manifest),
            "by_system": _by_system(entries)}


def _by_system(entries):
    out = {}
    for e in entries:
        a = out.setdefault(e["folder"], {"entries": 0, "bytes": 0})
        a["entries"] += 1
        a["bytes"] += e["bytes"]
    return dict(sorted(out.items()))


def stored(card, db=None):
    """What the app believes is on a card, from the index alone — no walk, no writes.

    This is the card as the app last saw it: a card in a drawer still has an entry here,
    which is the whole reason the index is in the database and not only on the card.
    """
    db = db or connect()
    card = card_root(card)
    cid = identify(card, db, create=False)
    if cid is None:
        return {"card_id": None, "card": str(card), "known": False, "entries": [],
                "by_system": {}, "bytes": 0, "last_seen": None, "label": None}
    row = db.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    entries = []
    for r in db.execute("SELECT * FROM card_entries WHERE card_id=? ORDER BY folder, entry",
                        (cid,)):
        entries.append({"folder": r["folder"], "entry": r["entry"], "item_id": r["item_id"],
                        "bytes": r["bytes"] or 0, "score": r["score"],
                        "playcount": r["playcount"] or 0, "last_played": r["last_played"],
                        "tracked": bool(r["tracked"])})
    return {"card_id": cid, "card": str(card), "known": True,
            "label": (row["label"] if row else None),
            "last_seen": (row["last_seen"] if row else None),
            "entries": entries, "by_system": _by_system(entries),
            "bytes": sum(e["bytes"] for e in entries)}


# ---------------------------------------------------------------------- the desired state
def desired_entries(games, arcade=None, db=None):
    """The entries the export would put on the card, from its own plan.

    The export's `games` list is the single source of truth for what belongs on a card:
    it carries the folder, the sub-folder, the paths ES-DE lists, and the item each game
    belongs to. The reconcile reads the same plan, so the two can never disagree about
    what a card should hold — which is the only way to be sure an update never removes a
    game the export was about to write.

    `arcade` is the arcade half of that plan: {core: [set names]}. Arcade is built
    rather than copied, so it never appears in `games` — but the sets are on the card all
    the same, as `<set>.zip` in the core's folder. Leaving them out of the desired set
    would make an update read every arcade set as "no longer selected" and take the whole
    arcade library off the card.

    Returns (entries, wanted items). The two are not the same: a game whose library file
    has gone missing is still wanted, it just cannot be written right now, and it must
    not be read as "no longer selected" and taken off the card.
    """
    out = {}
    for g in games:
        folder = g["folder"]
        prefix = f"{g['sub']}/" if g.get("sub") else ""
        for e in g.get("entries") or []:
            out[(folder, prefix + e)] = {
                "item_id": g["row"]["item_id"], "title": g["row"]["title"] or "",
                "score": _score(g["row"]), "bytes": _game_bytes(g),
            }
    wanted = {g["row"]["item_id"] for g in games}
    if arcade:
        out.update(_arcade_entries(arcade, db=db))
    return out, wanted


def _arcade_entries(arcade, db=None):
    """The arcade sets a card should hold, as entries.

    Each set goes on the card as `<set>.zip` in the folder of the core that builds it
    (`esde.ARCADE_FOLDERS`: mame→arcade, fbneo→fbneo, mame2003→mame — three cores cannot
    share a folder, since the same set name is three different zips). A set's score is
    its catalog score, so an arcade set is ranked against the other games the same way.
    """
    from . import esde
    db = db or connect()
    core_sets = arcade.get("core_sets") or {}
    names = []
    for core, sets in core_sets.items():
        folder = esde.ARCADE_FOLDERS.get(core, "arcade")
        for s in sets or []:
            names.append((folder, s, core))
        # The build writes more than the sets it is given: the bios it takes from a
        # fallback dat, and every device the sets reference (galaga and bosco cannot boot
        # without namco51.zip beside them). Those zips are the sets' own dependencies; left
        # out of the desired state they read as "no longer selected" and came off.
        for _alt, extra in (arcade.get("fallback_bios") or {}).get(core) or []:
            for s in extra:
                names.append((folder, s, core))
        for s in _device_sets(core, sets or []):
            names.append((folder, s, core))
    if not names:
        return {}
    ids = {s for _, s, _ in names}
    rows = {}
    for r in db.execute("SELECT id, title, rating, community_score FROM items "
                        "WHERE lower(system)='arcade' AND "
                        "substr(external_id, instr(external_id,'/')+1) IN "
                        "(" + ",".join("?" * len(ids)) + ")", tuple(ids)):
        rows.setdefault(r["id"], r)
    by_set = {}
    for r in db.execute("SELECT id, substr(external_id, instr(external_id,'/')+1) s "
                        "FROM items WHERE lower(system)='arcade'"):
        by_set.setdefault(r["s"], r["id"])
    out = {}
    for folder, s, _core in names:
        item_id = by_set.get(s)
        title, score = "", 0
        if item_id:
            r = rows.get(item_id)
            if r:
                title = r["title"] or ""
                score = max(r["community_score"] or 0, (r["rating"] or 0) * 10)
        out[(folder, f"{s}.zip")] = {"item_id": item_id, "title": title,
                                     "score": score, "bytes": 0}
    return out


def _device_sets(core, sets):
    """The device sets `sets` pull in through device_ref, all the way down, that have
    chips of their own — the zips `mameset.build` writes beside the games."""
    from . import arcadehealth, mameset
    dat = mameset.dat_path(core)
    if not dat or not sets:
        return []
    machines = arcadehealth.parse(dat)
    seen, todo, out = set(sets), list(sets), []
    while todo:
        m = machines.get(todo.pop())
        for d in (m or {}).get("devices") or []:
            if d in seen:
                continue
            seen.add(d)
            todo.append(d)
            if (machines.get(d) or {}).get("roms"):
                out.append(d)
    return sorted(out)


def _score(row):
    """A game's export score — the same expression the export ranks by."""
    return max(row["community_score"] or 0, (row["rating"] or 0) * 10)


def _game_bytes(g):
    """A game's whole footprint: every step's size. A disc is a cue and its tracks, an
    unpacked archive is its members, a CHD is the converted file — the card's budget is
    spent on all of it."""
    return sum(step[-1] or 0 for step in g.get("steps") or [])


def plan_update(card, games, db=None, systems=None, evict=False, protect_played=True,
                free_bytes=None, reserve_bytes=None, budget_bytes=None, arcade=None):
    """What an update of this card would do, without touching anything.

    The diff has two halves. What the recipe no longer selects comes off — that is a
    decision the library has already made, and a game that fell out of the gates is
    simply not wanted any more. And what is on the card is ranked against what wants to
    go on, so a better game can take a slot from a worse one.

    The second half is the delicate one, so it is fenced in:

    - only a game that is ON the card can be evicted, and only to make room for a game
      that is NOT on it (an update never shrinks a card for its own sake);
    - only when the newcomer genuinely ranks higher;
    - never a game the owner marked keep, never one the handheld has played (unless the
      policy says that protection is off), never one RomCom did not write (a name-matched
      guess is not proof), and never a game's BIOS or arcade parent — evicting a parent
      breaks every game that needs it.

    `budget_bytes` caps how much of the card games may take; by default the card's free
    space and the export's reserve decide it.
    """
    db = db or connect()
    card = card_root(card)
    cid = identify(card, db)
    want, wanted_items = desired_entries(games, arcade=arcade, db=db)

    actual = {}
    for r in db.execute("SELECT * FROM card_entries WHERE card_id=?", (cid,)):
        actual[(r["folder"], r["entry"])] = dict(r)

    from . import esde
    # A scope is given in the library's own system names ("ps1"), but a card's folders
    # are ES-DE's ("psx"), so the scope is translated before it is compared.
    wanted_systems = ({s.lower() for s in systems} if systems else None)
    if wanted_systems is not None:
        wanted_systems |= {esde.folder(s) for s in wanted_systems}
        # Arcade is one system in the library but a folder per core on the card: the
        # fbneo sets live in fbneo/, not arcade/. Each core the update builds brings its
        # folder into scope, or that core's whole folder was never looked at.
        # And only those: "arcade" is also the mame core's folder name, so an fbneo-only
        # update had arcade/ in scope, read every mame set as "moved" to fbneo/ and took
        # the whole mame folder off the card (2026-10-30, 1,848 sets, ~40 GB).
        if "arcade" in wanted_systems:
            wanted_systems -= {f.lower() for f in esde.ARCADE_FOLDERS.values()}
            wanted_systems |= {esde.ARCADE_FOLDERS.get(c, "arcade").lower()
                               for c in ((arcade or {}).get("core_sets") or {})}
    arcade_folders = {f.lower() for f in esde.ARCADE_FOLDERS.values()}

    def in_scope(folder):
        if wanted_systems is None:
            return True
        return folder.lower() in wanted_systems

    broken = _let_go(db, {r["item_id"] for r in actual.values() if r["item_id"]}
                     - {w["item_id"] for w in want.values()})
    remove, keep, untracked, protected = [], [], [], []
    for key, row in actual.items():
        folder, entry = key
        d = want.get(key)
        if d is None and folder.lower() in arcade_folders and "." not in entry:
            # A MAME disc game is `<set>.zip` and a `<set>/` folder of CHDs beside it. The
            # folder is part of the set: read as a stray copy of it, it came off as
            # "moved" and took the game's disc with it (14 sets, 17.9 GB on the arcade
            # card). It goes with its zip — kept while the zip is wanted.
            d = want.get((folder, entry + ".zip"))
        if d is None:
            if not row["item_id"]:
                untracked.append(row)          # the library cannot name it: never touched
                continue
            if not in_scope(folder):
                keep.append({**row, "reason": "out of scope"})
                continue
            # The same game under another name, or another name for the same place. Not
            # across arcade folders: the same set in another core's folder is a different
            # zip for a different emulator, never a move of this one.
            same = next((w for k, w in want.items() if w["item_id"] == row["item_id"]
                         and not (folder.lower() in arcade_folders
                                  and k[0].lower() != folder.lower())), None)
            if row["item_id"] in broken:
                # The library threw its own copy away as the wrong game, a stub or a
                # corrupt download: the card's copy came from the same file and does
                # not work either. Keeping it as "still wanted" left a broken game on
                # the card with nothing to replace it.
                remove.append({**row, "reason": "broken — the library let its file go",
                               "title": _title(db, row)})
                continue
            if row["item_id"] in wanted_items and same is None:
                # Still wanted, but its library file is gone so it cannot be written
                # where it belongs. It stays: the export will put it back when the
                # file is back, and until then the card keeps its copy.
                keep.append({**row, "reason": "unwritable", "title": _title(db, row)})
                continue
            if same is not None:
                # Wanted, but somewhere else now: the score folder moved, or the
                # library's copy of it was renamed. The old copy comes off and the
                # export writes the new one, so the card does not hold the game twice.
                remove.append({**row, "reason": "moved", "title": _title(db, row)})
                continue
            remove.append({**row, "reason": "no longer selected", "title": _title(db, row)})
        else:
            row = dict(row)
            if not row.get("tracked") and row.get("item_id") and d.get("item_id") == row["item_id"]:
                # The card holds exactly the entry the recipe writes for this game, under
                # the name it writes: that is RomCom's own output, from an export made
                # before cards were tracked. Treating it as a stranger's file protected
                # every game on such a card from ever being traded for a better one.
                row["tracked"] = 1
            keep.append({**row, "reason": "kept"})

    # What wants to go on that is not on the card.
    on_card = {k for k in actual}
    incoming = [{"folder": k[0], "entry": k[1], **v}
                for k, v in want.items() if k not in on_card]
    # Best first, as the export ranks. The recipe's own order is per system and by name,
    # and walking it that way handed the card's room to whatever sorted first: an
    # unrated newcomer that may not trade for anything took the space, and a rated game
    # later in the alphabet never got to displace an unrated one already on the card.
    incoming.sort(key=lambda a: -(a.get("score") or 0))

    # The room there is.
    from .organizer import _free_bytes, _reserve_bytes as _reserve
    free = free_bytes if free_bytes is not None else _free_bytes(card)
    reserve = reserve_bytes if reserve_bytes is not None else _reserve(card)
    usable = budget_bytes if budget_bytes is not None else (free - reserve if free is not None else None)

    # The space a game that is coming off frees for the ones that are going on. The plan
    # has to know what will fit BEFORE anything is removed, so the space the removals
    # will free is counted here.
    freed = sum(r.get("bytes") or 0 for r in remove
                if r.get("reason") != "outranked")
    room = (free or 0) + freed - reserve

    # The newcomers, best first. The export copies in rank order and stops when the card
    # is full, and the plan works the same way: the best newcomer gets the room, and one
    # that does not fit is left off rather than forcing a trade — a card full of better
    # games is not made better by a worse one.
    left = max(0, room)
    fits, no_room, evictions = [], [], []
    if free is None:
        fits = list(incoming)           # the card's size is unknown: assume it all fits
    else:
        cands = _candidates(keep, protect_played, db, protected)
        for a in incoming:
            if a["bytes"] <= left:
                fits.append(a)
                left -= a["bytes"]
                continue
            if not evict:
                no_room.append(a)
                continue
            # Make room for it by giving up games that rank worse than it does.
            made = _make_room(cands, a, left)
            if not made:
                no_room.append(a)
                continue
            for e in made:
                keep = [k for k in keep
                        if (k["folder"], k["entry"]) != (e["folder"], e["entry"])]
                remove.append(e)
                evictions.append(e)
            left += sum(e.get("bytes") or 0 for e in made)
            cands = [c for c in cands
                     if (c["row"]["folder"], c["row"]["entry"])
                     not in {(e["folder"], e["entry"]) for e in made}]
            if a["bytes"] <= left:
                fits.append(a)
                left -= a["bytes"]
            else:
                no_room.append(a)

    return {"card_id": cid, "card": str(card),
            "remove": remove, "add": fits, "keep": keep, "untracked": untracked,
            "protected": protected, "no_room": no_room,
            "evicted": evictions,
            "bytes_remove": sum(r.get("bytes") or 0 for r in remove),
            "bytes_add": sum(a["bytes"] for a in fits),
            "free_bytes": free, "reserve_bytes": reserve, "usable_bytes": usable}


# What the library writes down when it lets a game's file go because the file was bad:
# another game or platform (reconcile.wrong_downloads), a 'disc' too small to hold the
# disc (reconcile.stub_discs), an archive whose checksums fail (discs._let_go_corrupt).
BROKEN_EVENTS = ("wrong-download", "stub-disc", "corrupt-download")


def _let_go(db, item_ids):
    """The items among `item_ids` whose library file was let go as broken and that have
    no file in the library now. Only games the recipe cannot write are asked about: a
    game the library holds a good copy of again is simply written over."""
    out, ids = set(), [i for i in item_ids if i]
    for n in range(0, len(ids), 900):
        chunk = ids[n:n + 900]
        qs = ",".join("?" * len(chunk))
        out |= {r[0] for r in db.execute(
            f"SELECT DISTINCT e.item_id FROM events e WHERE e.item_id IN ({qs}) "
            f"AND e.event IN ({','.join('?' * len(BROKEN_EVENTS))}) "
            "AND NOT EXISTS(SELECT 1 FROM files f WHERE f.matched_item_id=e.item_id)",
            chunk + list(BROKEN_EVENTS))}
    return out


def _title(db, row):
    if not row.get("item_id"):
        return ""
    r = db.execute("SELECT title FROM items WHERE id=?", (row["item_id"],)).fetchone()
    return (r["title"] if r else "") or ""


def _candidates(keep, protect_played, db, protected):
    """The games on the card that could be given up, worst first.

    The order of sacrifice is the export's own fill order, reversed: a game nobody
    rates, that the owner has never played on the handheld, and that the owner never
    marked keep, goes before one that is rated and played. The ties are broken so the
    answer never depends on the order the rows came back in.

    A game that may not be given up is not a candidate at all — it is reported as
    protected, with the reason, so the owner can see what was held back and why.
    """
    cands = []
    for row in keep:
        item_id = row.get("item_id")
        if not item_id:
            continue
        item = db.execute("SELECT title, keep, rating, community_score FROM items WHERE id=?",
                          (item_id,)).fetchone()
        if item is None:
            continue
        # A game outside the update's scope is not the update's business. The recipe
        # named the systems it may touch, and a game on another system is not one of
        # them however badly it ranks.
        if row.get("reason") == "out of scope":
            protected.append({**row, "title": item["title"], "why": "outside this update's scope"})
            continue
        # A game whose library file has gone missing is still wanted — the export will
        # put it back when the file is back. Trading it away for a newcomer would take
        # a game the owner still wants, and the file may well be back tomorrow.
        if row.get("reason") == "unwritable":
            protected.append({**row, "title": item["title"],
                              "why": "still wanted — its library file is missing"})
            continue
        # The score the index recorded is the one to compare: it is the score the game
        # was PLACED by, which is what a newcomer is measured against.
        score = row.get("score")
        if score is None:
            score = max(item["community_score"] or 0, (item["rating"] or 0) * 10)
        if item["keep"]:
            protected.append({**row, "title": item["title"], "why": "kept by you"})
            continue
        if row.get("favorite"):
            protected.append({**row, "title": item["title"], "why": "a favourite in ES-DE"})
            continue
        if protect_played and (row.get("playcount") or 0) > 0:
            protected.append({**row, "title": item["title"],
                              "why": f"played {row['playcount']}× on the handheld"})
            continue
        if not row.get("tracked"):
            protected.append({**row, "title": item["title"],
                              "why": "not written by RomCom — matched by name only"})
            continue
        cands.append({"row": row, "score": score, "title": item["title"] or "",
                      "playcount": row.get("playcount") or 0,
                      "last_played": row.get("last_played") or ""})
    cands.sort(key=lambda c: (0 if c["playcount"] == 0 else 1, c["score"],
                              c["last_played"], c["title"]))
    return cands


def _make_room(cands, newcomer, room):
    """The games to give up so `newcomer` fits in `room`.

    Only games that rank strictly worse than the newcomer are given up: an update that
    trades a good game for a worse one is not an update, it is churn. As many as the
    shortfall needs are given up, and no more.
    """
    need = newcomer["bytes"] - room
    if need <= 0:
        return []
    out, freed = [], 0
    for c in cands:
        if freed >= need:
            break
        if c["score"] >= newcomer["score"]:
            continue                    # ranked as well as the newcomer: not for the trade
        row = c["row"]
        out.append({**row, "reason": "outranked", "title": c["title"],
                    "by": newcomer["score"]})
        freed += row.get("bytes") or 0
    return out if freed >= need else []


# --------------------------------------------------------------------------- doing it
def write_removal_manifest(plan, card=None, db=None):
    """Write down what an update is about to remove, BEFORE it removes it.

    The house rule (prune.py, dupes.py, fixnames.py): the record precedes the deletion,
    so a removal is reversible by record. This one is about a card rather than a catalog
    row, so it says where each game came from in the library as well — the export only
    ever copies, so the library still has the file, and putting a game back is a copy
    away.
    """
    db = db or connect()
    card = Path(card or plan.get("card") or "")
    removed = []
    for r in plan.get("remove") or []:
        src = ""
        if r.get("item_id"):
            row = db.execute("SELECT path FROM files WHERE matched_item_id=? "
                             "ORDER BY bytes DESC LIMIT 1", (r["item_id"],)).fetchone()
            src = row["path"] if row else ""
        removed.append({"folder": r.get("folder"), "entry": r.get("entry"),
                        "item_id": r.get("item_id"), "title": r.get("title") or "",
                        "bytes": r.get("bytes") or 0, "score": r.get("score"),
                        "playcount": r.get("playcount") or 0,
                        "last_played": r.get("last_played"),
                        "reason": r.get("reason"), "source_path": src,
                        "path": str(card / "ROMs" / (r.get("folder") or "")
                                    / (r.get("entry") or ""))})
    Path("backups").mkdir(exist_ok=True)
    manifest = Path("backups") / f"card-removed-{datetime.now():%Y%m%d-%H%M%S}.json"
    manifest.write_text(json.dumps({
        "at": datetime.now().isoformat(timespec="seconds"),
        "note": "games removed from an SD card to make room for better-ranked ones; "
                "'entry' is the path ES-DE listed and 'source_path' the library file it "
                "was copied from, so a game can be put back on the card",
        "card": {"id": plan.get("card_id"), "path": str(card)},
        "removed": removed}, indent=1, default=str), encoding="utf-8")
    return str(manifest)


def apply_removals(card, plan, db=None, stop=None, progress=None):
    """Take the planned games off the card.

    Each removal is the whole unit the index recorded: a file, or a folder (a disc dumped
    as cue+bin is one game in a folder, and half a disc is not a game). The manifest is
    written first, so what went is on record before anything is unlinked.

    Returns a report: what was removed, what failed, and the bytes freed.
    """
    db = db or connect()
    card = card_root(card)
    from . import esde
    roms = esde.roms_dir(card)
    manifest = write_removal_manifest(plan, card=card, db=db)
    removed, errors, freed = [], [], 0
    for i, r in enumerate(plan.get("remove") or []):
        if stop is not None and stop.is_set():
            break
        if progress:
            progress(i, len(plan["remove"]), r.get("entry") or "")
        target = roms / (r.get("folder") or "") / (r.get("entry") or "")
        try:
            # What is freed is measured, not taken from the index: the index says what
            # the game took when the card was read, and the card is the truth about what
            # is on it now.
            if target.is_dir():
                freed += _tree_bytes(target)
                shutil.rmtree(target)
            elif target.exists():
                freed += target.stat().st_size
                target.unlink()
            else:
                errors.append({"entry": r.get("entry"), "error": "not on the card"})
                continue
        except OSError as e:
            errors.append({"entry": r.get("entry"), "error": str(e)})
            continue
        removed.append(r)
        with db:
            db.execute("DELETE FROM card_entries WHERE card_id=? AND folder=? AND entry=?",
                       (plan.get("card_id"), r.get("folder"), r.get("entry")))
            if r.get("item_id"):
                db.execute("INSERT INTO events(item_id,event,detail) "
                           "VALUES(?,'card-removed',?)",
                           (r["item_id"],
                            f"{r.get('title') or r.get('entry')} removed from the card "
                            f"({r.get('reason')})"))
    # ES-DE's side of the card follows: a removed game's gamelist entry and its images
    # would otherwise stay behind for good (the images are a few MB a game, and stale
    # entries are what ES-DE's "orphaned data" cleanup is for).
    try:
        forget_in_esde(card, removed)
    except OSError as e:
        errors.append({"entry": "", "error": f"could not tidy ES-DE's files: {e}"})
    # The card's own manifest has to say what is on it NOW, not what was on it before
    # the removals: a manifest still listing a game that has just been deleted would
    # make the next update think the game is there.
    try:
        _write_index_manifest(card, plan.get("card_id"), db)
    except OSError as e:
        errors.append({"entry": "", "error": f"could not write the card's manifest: {e}"})
    return {"removed": len(removed), "bytes_freed": freed, "errors": errors,
            "manifest": manifest,
            "entries": [{"folder": r.get("folder"), "entry": r.get("entry"),
                         "item_id": r.get("item_id"), "title": r.get("title") or "",
                         "reason": r.get("reason"), "bytes": r.get("bytes") or 0}
                        for r in removed]}


def forget_in_esde(card, removed):
    """Drop removed games from ES-DE's gamelists and delete their images.

    ES-DE names a game's media after the entry it lists without its extension, in a
    folder per kind (covers, screenshots, ...) that mirrors the system folder, and its
    gamelist keys a game by "./<entry>". Returns {"entries": n, "images": n}."""
    import xml.etree.ElementTree as ET
    card = card_root(card)
    es = card / "ES-DE"
    by_folder = {}
    for r in removed:
        if r.get("folder") and r.get("entry"):
            by_folder.setdefault(r["folder"], set()).add(r["entry"].replace("\\", "/"))
    dropped = images = 0
    for folder, entries in by_folder.items():
        gl = es / "gamelists" / folder / "gamelist.xml"
        try:
            tree = ET.parse(gl)
        except (OSError, ET.ParseError):
            tree = None
        if tree is not None:
            root = tree.getroot()
            gone = {"./" + e for e in entries}
            for g in list(root.findall("game")):
                if (g.findtext("path") or "").strip() in gone:
                    root.remove(g)
                    dropped += 1
            tmp = gl.with_suffix(".xml.tmp")
            tree.write(tmp, encoding="utf-8", xml_declaration=True)
            os.replace(tmp, gl)
        media = es / "downloaded_media" / folder
        if not media.is_dir():
            continue
        for kind in (k for k in media.iterdir() if k.is_dir()):
            for e in entries:
                rel = Path(e)
                for img in (kind / rel.parent).glob(f"{_glob_escape(rel.stem)}.*"):
                    if img.is_file() and img.stem == rel.stem:
                        img.unlink()
                        images += 1
    return {"entries": dropped, "images": images}


def _glob_escape(name):
    """A file name as a literal glob pattern: [ ] * ? are common in ROM names."""
    return "".join(f"[{c}]" if c in "[]*?" else c for c in name)


def record_export(card, card_games, db=None, profile=None):
    """Write down what an export put on a card — the manifest, and the index.

    The export already knows exactly what it wrote: `card_games` is {folder: [(entry
    path, item row)]}, the record the gamelists and the artwork are written from. That
    is the index, so nothing has to be guessed or re-walked: an entry the export wrote
    is known exactly, and is marked as such.

    The manifest goes onto the card as well as into the database, so the card carries
    its own identity and its own contents wherever it is plugged in.

    What the export did NOT write stays in the index. An export tops a card up; it does
    not empty it, so the games already there that this run skipped are still there, and
    an index that forgot them would make the next update think they had gone.
    """
    db = db or connect()
    card = card_root(card)
    cid = identify(card, db)
    ids = {r["item_id"] for lst in card_games.values() for _, r in lst if r["item_id"]}
    scores = _item_rows(db, ids)
    entries = []
    for folder, lst in sorted(card_games.items()):
        for entry, row in lst:
            info = scores.get(row["item_id"]) or {}
            entries.append({"folder": folder, "entry": entry, "item_id": row["item_id"],
                            "title": row["title"] or "", "score": info.get("score") or 0})
    # The byte count is measured, not guessed: the export may have skipped a file that
    # was already there, and the index's job is to say what the card holds.
    from . import esde
    roms = esde.roms_dir(card)
    with db:
        for e in entries:
            nbytes = _tree_bytes(roms / (e["folder"] or "") / (e["entry"] or ""))
            db.execute(
                "INSERT INTO card_entries(card_id,folder,entry,item_id,bytes,score,"
                "playcount,last_played,tracked) VALUES(?,?,?,?,?,?,0,NULL,1) "
                "ON CONFLICT(card_id,folder,entry) DO UPDATE SET item_id=excluded.item_id,"
                "bytes=excluded.bytes,score=excluded.score,tracked=1,"
                "updated_at=CURRENT_TIMESTAMP",
                (cid, e["folder"], e["entry"], e["item_id"], nbytes, e["score"]))
    # The manifest is the card's own record of ITSELF, so it holds the whole card: what
    # this run wrote and what was already there. A manifest holding only this run's
    # games would make the next update think everything else had been removed.
    _write_index_manifest(card, cid, db, profile=profile)
    return cid


def _write_index_manifest(card, cid, db, profile=None):
    """Write the card's manifest from the index — the whole card, not just this run.

    The profile is the recipe the card was written under. It is kept from the manifest
    already on the card when a rewrite does not supply one, so a removal does not lose
    the card's record of how it was built.
    """
    known = {(r["folder"], r["entry"]): dict(r)
             for r in db.execute("SELECT * FROM card_entries WHERE card_id=?", (cid,))}
    manifest_entries = []
    for (folder, entry), row in sorted(known.items()):
        manifest_entries.append({"folder": folder, "entry": entry, "item_id": row["item_id"],
                                 "bytes": row["bytes"] or 0, "score": row["score"]})
    if profile is None:
        profile = (read_manifest(card) or {}).get("profile") or {}
    _carried(db, cid)
    return write_manifest(card, {
        "card_id": cid, "written_at": datetime.now().isoformat(timespec="seconds"),
        "romcom_version": VERSION, "profile": profile,
        "entries": manifest_entries})




# ------------------------------------------------------------------------ the card's health
_CHD_MAGIC = b"MComprHD"


def _entry_problems(path):
    """What is wrong with one game on the card, as a list of short sentences (empty when
    nothing is). Reads headers and descriptors only — a 256 GB card is checked in
    seconds, not by re-reading every byte."""
    import zipfile
    from . import esde
    p = Path(path)
    out = []
    if p.is_dir():
        files = [f for f in p.rglob("*") if f.is_file()]
        if not files:
            return ["an empty folder"]
        for f in files:
            if f.stat().st_size == 0:
                out.append(f"{f.name} is empty")
        for d in (f for f in files if f.suffix.lower() in (".cue", ".gdi")):
            if not esde._cue_tracks_present(d):
                out.append(f"{d.name} names a track that is not in the folder")
        return out
    try:
        size = p.stat().st_size
    except OSError as e:
        return [f"cannot be read: {e}"]
    if size == 0:
        return ["the file is empty"]
    ext = p.suffix.lower()
    try:
        if ext == ".chd":
            with open(p, "rb") as f:
                if f.read(8) != _CHD_MAGIC:
                    out.append("not a CHD (bad header)")
        elif ext == ".zip":
            with zipfile.ZipFile(p) as z:
                if not z.namelist():
                    out.append("an empty zip")
        elif ext in (".cue", ".gdi"):
            if not esde._cue_tracks_present(p):
                out.append("names a track that is not beside it")
        elif ext == ".m3u":
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not (p.parent / line).exists():
                    out.append(f"lists {line}, which is not on the card")
    except zipfile.BadZipFile:
        out.append("a damaged zip")
    except OSError as e:
        out.append(f"cannot be read: {e}")
    return out


def check_card(card, db=None):
    """Is every game on the card whole, and does every console on it have its BIOS?

    Returns {"games": n checked, "problems": [{folder, entry, title, problems}],
    "bios": {system: {"dir", "missing": [slot alternatives]}}}. Read-only."""
    from . import cores, esde
    db = db or connect()
    card = card_root(card)
    found = walk_card(card)
    by_file, by_set = _name_maps(db)
    problems = []
    for u in found:
        why = _entry_problems(u["path"])
        if why:
            iid = by_file.get(Path(u["entry"]).name.lower())
            title = ""
            if iid:
                r = db.execute("SELECT title FROM items WHERE id=?", (iid,)).fetchone()
                title = r["title"] if r else ""
            problems.append({"folder": u["folder"], "entry": u["entry"], "title": title,
                             "item_id": iid, "problems": why})
    folders = {u["folder"].lower() for u in found}
    bios = {}
    for system, spec in cores.BIOS.items():
        if esde.folder(system) not in folders or not spec.get("slots"):
            continue
        base = card / "system" / spec["dir"] if spec.get("dir") else card / "system"
        missing = [alts for alts in spec["slots"]
                   if not any((base / n).is_file() and (base / n).stat().st_size > 0
                              for n in alts)]
        bios[system] = {"dir": str(base), "missing": [" or ".join(a) for a in missing]}
    return {"card": str(card), "games": len(found), "problems": problems, "bios": bios}
