"""Copy matched ROM files into a clean per-system layout, e.g. for an SD card."""
from pathlib import Path
import errno
import shutil
from .db import connect

import re
from .config import ROOT


def _device_deps(setnames):
    """Device sets the given arcade sets need, read from the MAME dat.

    A MAME game is not self-contained. galaga will not boot without namco54's roms, which
    live in their own set, and MAME reports it as plainly as it can:
    `54xx.bin - NOT FOUND (namco54)`. The dat records the dependency as <device_ref>, so an
    export that ignores it produces a card full of games that refuse to start.

    Returns an empty set when no dat is present rather than failing: a missing reference file
    should degrade the export, not block it.
    """
    dat = next((p for p in (ROOT / "DAT" / "MAME").glob("*.dat")
                if "arcade" in p.name.lower()), None) if (ROOT / "DAT" / "MAME").exists() else None
    if not dat:
        return set()
    want, deps, cur = set(setnames), set(), None
    try:
        for line in dat.open(encoding="utf-8", errors="replace"):
            m = re.search(r'<(?:game|machine)\s+name="([^"]+)"', line)
            if m:
                cur = m.group(1)
                continue
            if cur in want:
                d = re.search(r'<device_ref\s+name="([^"]+)"', line)
                if d:
                    deps.add(d.group(1))
    except OSError:
        return set()
    return deps - want


# The curation gate, shared by the export and the platform picker so the picker's counts are
# exactly what an export would copy. The rating threshold only widens the keep gate while it
# is on: the third `?` is the on/off switch, so 0 (off) cannot accidentally widen the gate to
# everything — COALESCE(rating,0) >= 0 would have passed every unrated item to the card.
_GATED = """ FROM files f JOIN items i ON i.id=f.matched_item_id
      WHERE (? = 0 OR i.wanted = 1)
        AND (? = 0 OR i.keep = 1 OR (? > 0 AND COALESCE(i.rating,0) >= ?))"""

# The card-fill order: a game ranks by the highest opinion anyone holds about it — the
# crowd's 0-100 score or the owner's 1-10 rating scaled to the same scale (recommend.py's
# rule, without its keep/played penalties: those demote games recommendations want you to
# *try*; on a card, "kept" must not demote). Unranked games score 0 and go last. Files of
# one item share a score and the i.id tie-break, so a game's files stay adjacent and space
# runs out between games, not through one.
_SCORE = "MAX(COALESCE(i.community_score,0), COALESCE(i.rating,0)*10)"
_ORDER = f" ORDER BY {_SCORE} DESC, i.system, i.title, i.id, f.path"


def _gate_args(wanted_only, keep_only, rating_min):
    rating_min = int(rating_min or 0)
    return (1 if wanted_only else 0, 1 if keep_only else 0, rating_min, rating_min)


def _selection(wanted_only=False, keep_only=False, rating_min=None):
    """Every file an export would consider, best game first (see `_SCORE`). One query
    backs the export and the platform picker, so the picker's counts are exactly what
    an export copies."""
    return connect().execute(
        "SELECT f.path, f.bytes, f.sha1, f.md5, f.match_method, i.id item_id, "
        "i.system, i.title, i.external_id, i.rating, i.community_score"
        + _GATED + _ORDER,
        _gate_args(wanted_only, keep_only, rating_min)).fetchall()


def _content_key(r):
    """A file's content identity, or None when unprovable — a file with no strong hash is
    never treated as a duplicate of anything."""
    return r["sha1"] or r["md5"] or None


def _copy_rank(r):
    """Which of two identical-content copies wins on the card: hash-verified first
    (proof, per scanner's match methods — a name match is a guess), then the larger
    file, then a stable order so the pick never depends on row order."""
    return (0 if str(r["match_method"] or "").startswith("hash") else 1,
            -(r["bytes"] or 0), r["path"])


def _game_key(title):
    """The game a title is a variant OF: parenthetical groups that are pure region or
    revision metadata drop (community's deco-strip rule), what remains is letters and
    digits — 'Cars (USA)' and 'Cars (Germany)' are both 'cars'."""
    from .community import _strip_deco_groups
    return re.sub(r"[^a-z0-9]+", " ", _strip_deco_groups(title or "").lower()).strip()


def _canonical(rows, distinct_games=False):
    """One copy per game. Same-content siblings under one item always collapse — they
    are the same bytes wearing different names, because every download source names files
    differently and one game fetched twice is two files. With `distinct_games`,
    region/variant entries of one title collapse too: the best variant goes on the card
    (hash-verified file, then highest score). Multi-file games keep every distinct file —
    a .cue's .bin is content of its own. Returns (rows, dupes_skipped)."""
    out, dupes = [], 0
    i, n = 0, len(rows)
    while i < n:  # files of one item are adjacent in _ORDER
        j = i
        while j < n and rows[j]["item_id"] == rows[i]["item_id"]:
            j += 1
        best = {}  # content key -> the copy that goes on the card
        for r in rows[i:j]:
            key = _content_key(r)
            if key is None:  # unprovable identity is never a duplicate
                out.append(r)
            elif key in best:
                dupes += 1
                if _copy_rank(r) < _copy_rank(best[key]):
                    best[key] = r
            else:
                best[key] = r
        out.extend(best.values())
        i = j
    if distinct_games:
        metas = {}
        for r in out:
            m = metas.setdefault(r["item_id"], {"item_id": r["item_id"], "verified": False,
                                                "score": _score(r)})
            m["verified"] = m["verified"] or str(r["match_method"] or "").startswith("hash")
        winner = {}
        for r in out:
            gk = ((r["system"] or "").lower(), _game_key(r["title"]))
            cur = winner.get(gk)
            if cur is None or _metas_rank(metas[r["item_id"]], metas[cur]):
                winner[gk] = r["item_id"]
        before, out = len(out), [r for r in out
                                 if winner.get(((r["system"] or "").lower(),
                                                 _game_key(r["title"]))) == r["item_id"]]
        dupes += before - len(out)
    return out, dupes


def _metas_rank(a, b):
    """True when item a is the better variant of a game: the hash-verified dump, then the
    better-ranked game (the same score _ORDER sorts by), then a stable id order."""
    ka = (0 if a["verified"] else 1, -a["score"])
    kb = (0 if b["verified"] else 1, -b["score"])
    return ka < kb or (ka == kb and a["item_id"] < b["item_id"])


def _score(r):
    """A game's export score — the same expression _ORDER ranks by."""
    return max(r["community_score"] or 0, (r["rating"] or 0) * 10)


def _rank_folder(r):
    """The score folder a game files under on the card: 90, 80, ... 0. RetroArch has no
    way to show a rating, but it can show a directory — so the ranking the export sorted
    by is also where the game lands, and browsing the card shows which tier is which."""
    return str(_score(r) // 10 * 10)


def export_systems(wanted_only=False, keep_only=False, rating_min=None, distinct_games=False):
    """Per-system file count and size an export would copy under the current gates.

    Sizes come from the scan's recorded `files.bytes`, not a stat of every file, so this is
    fast enough to back a picker. Arcade's figure is the matched chip files; the built sets
    can differ a little (shared chips are written into every set that needs them)."""
    rows, _ = _canonical(_selection(wanted_only, keep_only, rating_min), distinct_games)
    agg = {}
    for r in rows:
        a = agg.setdefault(r["system"] or "unknown",
                           {"system": r["system"] or "unknown", "files": 0, "bytes": 0})
        a["files"] += 1
        a["bytes"] += r["bytes"] or 0
    return sorted(agg.values(), key=lambda a: a["system"])


def _safe(name):
    """A directory name MAME and Windows will both accept."""
    out = "".join(c for c in str(name) if c not in '<>:"/\\|?*').strip().rstrip(".")
    return out[:120] or "unknown"


def organize(dest, systems=None, progress=None, wanted_only=False, sources=None,
             dry_run=False, keep_only=False, rating_min=None, stop=None, fill=False,
             distinct_games=False, wipe=False):
    """Copy files matched to a catalog item into <dest>/<system>/<score>/<filename> — the
    score folder (90, 80, ... 0) is the ranking the copy order sorts by, made visible to
    RetroArch, which cannot show a rating.

    Files are copied (never moved); an existing target of the same size is skipped,
    so re-running only tops up what's new.

    The filters are what make this a curation tool rather than a bulk copier:

    `wanted_only` restricts the export to items marked wanted, which is the whole point of
    curating a library and then deploying a subset of it. Off by default so existing callers
    keep their behaviour.

    `keep_only` restricts the export to items marked keep. `rating_min` widens that gate to
    "keep OR rated at least N", because keep alone is all-or-nothing: an owner who rates a
    game 8 should not have to also remember a second checkbox. 0/None disables it.

    `sources` restricts by `catalog_source`. Arcade is the case that needs it: a flat MAME
    dump leaves hundreds of loose chip files adopted as `local` items with names like
    `115b101`, and copying those to a card is pure noise next to the catalogued sets.

    `dry_run` reports exactly what would be copied, and how much space it needs, without
    touching the destination — worth doing before writing to a card.

    A real run never fills the card. It refuses up front when the copy (arcade sets
    included) will not fit while leaving a reserve free, and it re-checks before every file
    and every arcade set, stopping with the reserve intact if space runs out anyway (another
    writer, filesystem overhead, a low estimate). A card filled to zero is the worst outcome
    available: the export is incomplete and the device can misbehave on a full filesystem.

    `fill` skips the up-front refusal and copies until the reserve is reached instead — for
    a card deliberately given to one platform that is larger than it (arcade, usually). The
    reserve still holds; only the all-or-nothing refusal is waived. Games copy best-rated
    first (see `_SCORE`), so when space runs out it is the low end of the library that is
    left off the card.

    `stop` is a threading.Event checked between files; setting it ends the run with
    `stopped: True`. A file mid-copy is finished, not truncated.

    `distinct_games` puts one copy of each game on the card even when the catalog holds
    region/variant entries of it ('Cars (USA)', 'Cars (Germany)'): the best variant is
    picked and the rest are not copied. Same-content copies under one entry are never
    copied regardless — they are the same file wearing different names. Both layers are
    counted by `dupes_skipped` in the result, and `export_systems` applies the same rules,
    so the picker's numbers stay honest.

    `wipe` clears the card first: every <dest>/<system> folder for a ticked platform (and
    arcade, when ticked) is deleted before anything is copied — a clean card instead of a
    top-up. Nothing outside those folders is ever touched, and without an explicit
    platform list it refuses rather than clear an unfiltered destination. Re-running was
    the old "clean": skip-if-present silently kept games that had fallen out of the
    recipe, and a layout change left the old copy behind beside the new one.
    """
    db = connect()
    rows = _selection(wanted_only, keep_only, rating_min)
    wanted_systems = {s.lower() for s in systems} if systems else None
    # Arcade is built, not copied. One physical file belongs to many sets, and
    # `files.matched_item_id` records a single owner, so copying matched files leaves every
    # set but one incomplete -- galaga came out missing prom-2.5c that way. A flattened dump
    # also renames collisions (`prom-2.5c_7`), and MAME only looks for the canonical name.
    # mameset.build resolves each set's roms from the dat by hash and writes them under the
    # names MAME expects, devices included.
    # A systems filter that leaves arcade out must leave arcade out: the set build used to
    # run regardless, so "export snes" also wrote the whole arcade library.
    arcade_wanted = wanted_systems is None or "arcade" in wanted_systems
    arcade_sets = sorted({(r["external_id"] or "").split("/", 1)[-1]
                          for r in rows if (r["system"] or "").lower() == "arcade"}
                         if arcade_wanted else ())
    arcade_report = None
    rows = [r for r in rows if (r["system"] or "").lower() != "arcade"]
    if sources:
        keep = {s.lower() for s in sources}
        ids = {r["path"] for r in db.execute(
            """SELECT f.path FROM files f JOIN items i ON i.id=f.matched_item_id
               WHERE lower(COALESCE(i.catalog_source,'')) IN (%s)"""
            % ",".join("?" * len(keep)), tuple(keep))}
        rows = [r for r in rows if r["path"] in ids]
    if wanted_systems is not None:
        rows = [r for r in rows if (r["system"] or "").lower() in wanted_systems]
    rows, dupes_skipped = _canonical(rows, distinct_games)
    dest = Path(dest)
    stopped = False
    errors = []

    # `wipe` runs before the space preflight, because deleting the old copy is what makes
    # room for the new one. Only a ticked platform's own folder is ever deleted — never the
    # card root, never a folder the export is not about to write, never a file sitting
    # loose on the card. Without an explicit platform list it refuses: "clear everything"
    # is not a thing to guess at.
    wiped = None
    if wipe:
        if wanted_systems is None:
            return {"error": "wipe needs an explicit platform list — refusing to clear an "
                             "unfiltered destination",
                    "copied": 0, "matched_files": len(rows), "by_system": {}, "errors": [],
                    "dupes_skipped": dupes_skipped, "stopped": False}
        targets = set(wanted_systems)
        if arcade_wanted:
            targets.add("arcade")
        wiped = {"folders": [], "files": 0, "bytes": 0}
        if dest.exists():
            for child in dest.iterdir():
                if not child.is_dir() or child.name.lower() not in targets:
                    continue
                for p in child.rglob("*"):
                    try:
                        if p.is_file():
                            wiped["files"] += 1
                            wiped["bytes"] += p.stat().st_size
                    except OSError:
                        pass
                if not dry_run:
                    shutil.rmtree(child, ignore_errors=True)
                wiped["folders"].append(child.name)

    reserve = _reserve_bytes(dest)
    if not dry_run:
        need = _bytes_to_copy(rows, dest)
        if arcade_sets:
            from . import mameset
            need += mameset.build(arcade_sets, dest / "arcade", db=db,
                                  dry_run=True).get("bytes_needed", 0)
        free = _free_bytes(dest)
        if free is None:
            return {"error": f"cannot read free space on {dest} — is the card mounted?",
                    "copied": 0, "matched_files": len(rows), "by_system": {}, "errors": [],
                    "dupes_skipped": dupes_skipped, "stopped": False}
        if need > free - reserve and not fill:
            gb = 1024 ** 3
            return {"error": f"export needs {need / gb:.1f} GB but {dest} has "
                             f"{free / gb:.1f} GB free ({reserve / gb:.1f} GB is kept in "
                             f"reserve) — pick fewer platforms, tighten the curation "
                             f"gates, or tick 'fill the card' to copy until it is nearly full",
                    "needed_bytes": need, "free_bytes": free, "reserve_bytes": reserve,
                    "copied": 0, "matched_files": len(rows), "by_system": {}, "errors": [],
                    "dupes_skipped": dupes_skipped, "stopped": False}

    def room_for(nbytes):
        free = _free_bytes(dest)
        return free is not None and free - nbytes >= reserve

    if arcade_sets:
        from . import mameset
        arcade_report = mameset.build(arcade_sets, dest / "arcade", db=db, dry_run=dry_run,
                                      stop=stop, room_for=None if dry_run else room_for)
        stopped = bool(arcade_report.get("stopped"))
        if arcade_report.get("out_of_room"):
            errors.append({"file": "", "error": f"{dest} reached its free-space reserve "
                                                f"during the arcade build — export stopped"})

    copied = skipped = missing = 0
    planned_bytes = 0
    by_system = {}
    for i, r in enumerate(rows):
        if stopped or (stop is not None and stop.is_set()):
            stopped = True
            break
        src = Path(r["path"])
        if progress: progress(i, len(rows), src.name)
        if not src.exists():
            missing += 1; continue
        if dry_run:
            planned_bytes += src.stat().st_size
            by_system[r["system"] or "unknown"] = by_system.get(r["system"] or "unknown", 0) + 1
            continue
        folder = dest / (r["system"] or "unknown") / _rank_folder(r)
        target = folder / src.name
        try:
            size = src.stat().st_size
            if target.exists() and target.stat().st_size == size:
                skipped += 1
            elif not room_for(size):
                errors.append({"file": "", "error": f"{dest} reached its free-space reserve "
                                                    f"— export stopped before {src.name}"})
                stopped = True
                break
            else:
                folder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                copied += 1
            by_system[r["system"] or "unknown"] = by_system.get(r["system"] or "unknown", 0) + 1
        except OSError as e:
            errors.append({"file": src.name, "error": str(e)})
            if e.errno == errno.ENOSPC:
                _discard_partial(target)
                errors.append({"file": "", "error": f"{dest} is full — export stopped"})
                stopped = True
                break
    if progress: progress(len(rows), len(rows), "stopped" if stopped else "done")
    if arcade_report:
        by_system["arcade"] = arcade_report["complete"] + arcade_report["incomplete"]
        copied += arcade_report["copied"]
        if dry_run:
            planned_bytes += arcade_report.get("bytes_needed", 0)
    out = {"matched_files": len(rows), "copied": copied, "skipped": skipped,
           "missing": missing, "by_system": by_system, "errors": errors,
           "arcade": arcade_report, "dupes_skipped": dupes_skipped, "wiped": wiped,
           "wanted_only": bool(wanted_only), "keep_only": bool(keep_only),
           "sources": sorted(sources) if sources else None, "stopped": stopped}
    if dry_run:
        out |= {"dry_run": True, "would_copy": sum(by_system.values()),
                "would_copy_bytes": planned_bytes,
                "would_copy_gb": round(planned_bytes / (1024 ** 3), 2)}
    return out


def _bytes_to_copy(rows, dest):
    """Bytes a real run would write for non-arcade rows: present sources whose target is
    missing or a different size (the same rule the copy loop uses to skip)."""
    need = 0
    for r in rows:
        src = Path(r["path"])
        try:
            size = src.stat().st_size
        except OSError:
            continue
        target = dest / (r["system"] or "unknown") / _rank_folder(r) / src.name
        try:
            if target.stat().st_size == size:
                continue
        except OSError:
            pass
        need += size
    return need


def _volume(dest):
    """The nearest existing ancestor of dest — dest itself may not exist yet."""
    p = Path(dest)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _free_bytes(dest):
    """Free space on dest's volume, or None when it cannot be determined."""
    try:
        return shutil.disk_usage(_volume(dest)).free
    except OSError:
        return None


def _reserve_bytes(dest):
    """Space an export always leaves free: 1 GiB or 1% of the volume, whichever is larger."""
    try:
        total = shutil.disk_usage(_volume(dest)).total
    except OSError:
        total = 0
    return max(1024 ** 3, total // 100)


def _discard_partial(target):
    """A copy that died on ENOSPC leaves a truncated file; remove it so the card isn't left
    holding a ROM that looks present and won't load."""
    try:
        target.unlink()
    except OSError:
        pass
