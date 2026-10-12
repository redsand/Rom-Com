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
# exactly what an export would copy. Either knob filters on its own: keep_only holds back
# everything the keeper did not keep, rating_min holds back everything nobody rates that
# high. "Rated at least N" speaks the library's own scale (`_SCORE`: the crowd's 0-100 score
# or the owner's 1-10 rating scaled x10 — an owner who never rates anything still has the
# crowd's opinion to go on). Both knobs together pass kept OR that good. 0 must stay a
# pass-through both for the outer switch and for the threshold itself: a 0 threshold with
# no opinion anywhere would have locked out every unrated game instead.
_GATED = """ FROM files f JOIN items i ON i.id=f.matched_item_id
      WHERE (? = 0 OR i.wanted = 1)
        AND (CASE WHEN ? = 0 THEN 1
                  ELSE (? AND i.keep = 1)
                    OR (? > 0 AND MAX(COALESCE(i.community_score,0),
                                      COALESCE(i.rating,0)*10) >= ? * 10) END)"""

# The card-fill order: a game ranks by the highest opinion anyone holds about it — the
# crowd's 0-100 score or the owner's 1-10 rating scaled to the same scale (recommend.py's
# rule, without its keep/played penalties: those demote games recommendations want you to
# *try*; on a card, "kept" must not demote). Unranked games score 0 and go last. Files of
# one item share a score and the i.id tie-break, so a game's files stay adjacent and space
# runs out between games, not through one.
_SCORE = "MAX(COALESCE(i.community_score,0), COALESCE(i.rating,0)*10)"
_ORDER = f" ORDER BY {_SCORE} DESC, i.system, i.title, i.id, f.path"


def _fill_order(db, names):
    """Rank a romset fill best-game-first, on the same scale `_SCORE` ranks cards. Arcade
    sets sit in the items table as `<source>/<setname>` external ids. A fill stops at the
    free-space reserve, and an alphabetical fill stopped halfway leaves a card without
    every famous M-Z game on it; ranked, it is the obscure zero-score tail that drops."""
    scores = {r["s"]: r["sc"] for r in db.execute(
        "SELECT substr(external_id, instr(external_id,'/')+1) s, " + _SCORE + " sc"
        " FROM items i WHERE i.system='arcade'"
        " GROUP BY substr(external_id, instr(external_id,'/')+1)")}
    return sorted(names, key=lambda n: (-scores.get(n, 0), n))


def _gate_args(wanted_only, keep_only, rating_min):
    rating_min = int(rating_min or 0)
    curating = 1 if (keep_only or rating_min) else 0
    return (1 if wanted_only else 0, curating, 1 if keep_only else 0, rating_min, rating_min)


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
             distinct_games=False, wipe=False, arcade_core="mame"):
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
    "keep OR at least N on the library's own scale" (the crowd's 0-100 score or the owner's
    1-10 rating scaled x10 — same rule `_SCORE` ranks the fill order by), because keep alone
    is all-or-nothing: an owner who rates a game 8 should not have to also remember a second
    checkbox. 0/None disables it.

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

    `arcade_core` is which core's romset the card gets: 'mame' (current MAME, the default),
    'fbneo', 'mame2003' — or 'all', which builds each core into its own folder under
    <dest>/arcade/. The same catalog feeds each core's own dat: the cores speak different
    romset versions, so a zip rebuilt for one can fail on another.

    `wipe` clears the card first: every <dest>/<system> folder for a ticked platform (and
    arcade, when ticked) is deleted before anything is copied — a clean card instead of a
    top-up. Nothing outside those folders is ever touched, and without an explicit
    platform list it refuses rather than clear an unfiltered destination. The clear is
    planned before it happens: an export that cannot fit even on a cleared card refuses
    with the card untouched, so a too-big selection can never empty the card and then
    fail. Re-running was the old "clean": skip-if-present silently kept games that had
    fallen out of the recipe, and a layout change left the old copy behind beside the new
    one.
    """
    db = connect()
    if progress: progress(0, 0, "selecting items…")
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
    from . import mameset
    # Which core's romset the card gets. A browser is not trusted with the spelling:
    # anything unexpected means the default.
    if arcade_core == "all":
        arcade_cores = ["mame", "fbneo", "mame2003"]   # one folder per core, under arcade/
    elif arcade_core in mameset.PROFILES:
        arcade_cores = [arcade_core]
    else:
        arcade_cores = ["mame"]
    # The selection's order is the fill order (best game first), so an export that runs
    # out of room misses the obscure tail, not the famous middle. 'One copy per game'
    # reaches arcade too: the sets come from the same deduped rows the plan's arcade
    # figure counts, or every clone variant of one game would build although the plan
    # promised only one.
    arcade_dupes_skipped = 0
    arcade_rows = ([r for r in rows if (r["system"] or "").lower() == "arcade"]
                   if arcade_wanted else [])
    if distinct_games and arcade_rows:
        arcade_rows, arcade_dupes_skipped = _canonical(arcade_rows, True)
    arcade_sets = list(dict.fromkeys(
        (r["external_id"] or "").split("/", 1)[-1] for r in arcade_rows))
    arcade_bios_unavailable = {}
    if arcade_sets and "mame" in arcade_cores:
        # The romof parents (neogeo, pgm…) come along whether or not the recipe ticked
        # them: mslug on a card without neogeo.zip beside it refuses to boot. Parents
        # first, so a build that runs out of room stops before the games needing them.
        mame_dat = mameset.dat_path("mame")
        if mame_dat:
            parents = mameset.romof_refs(arcade_sets, mame_dat)
            hold = sorted(set(parents) & mameset.assemblable(db, mame_dat))
            arcade_bios_unavailable = {k: len(v) for k, v in parents.items() if k not in set(hold)}
            arcade_sets = hold + [s for s in arcade_sets if s not in set(hold)]
    arcade_reports = {}
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
    rows, nonarcade_dupes = _canonical(rows, distinct_games)
    dupes_skipped = nonarcade_dupes + arcade_dupes_skipped
    dest = Path(dest)
    # Console BIOS rides along: a card whose Dreamcast folder is full of games plays
    # nothing without dc_boot.bin in RetroArch's System Directory, and no item-driven
    # query can ever fetch it — the dump's BIOS files sit in `files` unmatched, so this
    # looks them up by exact filename and fills only the slots the export covers.
    from . import cores
    if wanted_systems is not None:
        bios_systems = wanted_systems & set(cores.BIOS)
    else:
        bios_systems = ({(r["system"] or "").lower() for r in rows if r["system"]}
                        & set(cores.BIOS))
    bios_plan = cores.bios_report(db, systems=bios_systems)
    stopped = False
    errors = []

    # `wipe` is planned before it is performed: the ticked platforms' folders are walked
    # and counted, but nothing is deleted until the export has been shown to fit. A card
    # that cannot hold the selection even cleared must refuse with its contents intact —
    # the first version deleted first and refused second, which emptied the card and then
    # said no. Only a ticked platform's own folder is ever deleted — never the card root,
    # never a folder the export is not about to write, never a file sitting loose on the
    # card. Without an explicit platform list it refuses: "clear everything" is not a thing
    # to guess at.
    wipe_plan = []
    if wipe:
        if wanted_systems is None:
            return {"error": "wipe needs an explicit platform list — refusing to clear an "
                             "unfiltered destination",
                    "copied": 0, "matched_files": len(rows), "by_system": {}, "errors": [],
                    "dupes_skipped": dupes_skipped, "stopped": False}
        targets = set(wanted_systems)
        if arcade_wanted:
            targets.add("arcade")
        if dest.exists():
            for child in dest.iterdir():
                if not child.is_dir() or child.name.lower() not in targets:
                    continue
                if progress: progress(0, 0, f"measuring {child.name} (to clear it)…")
                files = nbytes = 0
                for p in child.rglob("*"):
                    try:
                        if p.is_file():
                            files += 1
                            nbytes += p.stat().st_size
                    except OSError:
                        pass
                wipe_plan.append((child, files, nbytes))
    wiped = None
    if wipe:
        wiped = {"folders": [c.name for c, _, _ in wipe_plan],
                 "files": sum(f for _, f, _ in wipe_plan),
                 "bytes": sum(b for _, _, b in wipe_plan)}

    reserve = _reserve_bytes(dest)

    def room_for(nbytes):
        free = _free_bytes(dest)
        return free is not None and free - nbytes >= reserve

    # A wipe rebuilds the arcade folder whole, so its sizing pass measures a full build.
    fresh_wipe = bool(wipe and arcade_wanted)

    def build_arcade(core, dry_run):
        """One core's zip build. A single-core card writes the zips straight into
        <dest>/arcade — the layout a frontend scans — but three cores cannot share it
        (the same set name would be three different zips), so each gets its own folder.
        The measuring pass wants the full rebuild under a wipe; the real build does not,
        because the wipe (if any) has already cleared the folder by then."""
        dest_dir = dest / "arcade" if len(arcade_cores) == 1 else dest / "arcade" / core
        base = "arcade" if len(arcade_cores) == 1 else f"arcade/{core}"
        label = f"measuring {base}" if dry_run else base
        dat = mameset.dat_path(core)
        if not dat:
            # build would fall back to the default dat and quietly write another core's
            # romset under this one's name — refuse instead.
            return {"sets": {}, "error": f"no romset dat for {core!r} — put its dat under "
                                         f"DAT/{mameset.PROFILES.get(core, (core,))[0]}/",
                    "copied": 0, "bytes": 0, "bytes_needed": 0, "complete": 0,
                    "incomplete": 0, "stopped": False, "out_of_room": False}
        return mameset.build(arcade_sets, dest_dir, db=db, dry_run=dry_run, zipped=True,
                             profile=core, dat=dat,
                             fresh=fresh_wipe if dry_run else False,
                             room_for=None if dry_run else room_for,
                             stop=None if dry_run else stop,
                             progress=None if progress is None else
                             lambda i, total, name: progress(i, total, f"{label}: {name}"))

    if not dry_run:
        # After a wipe nothing at the targets survives to be reused, so the copy needs the
        # selection's full bytes; without one, present same-size files are skipped and the
        # need reflects that. Clearing the ticked platforms counts as room the copy may use.
        if progress: progress(0, 0, "measuring what's left to copy…")
        need = (sum(r["bytes"] or 0 for r in rows) if wipe else _bytes_to_copy(rows, dest))
        if arcade_sets:
            # A wipe clears the arcade folder before rebuilding it, so the sizing pass
            # must measure the FULL build — fresh=True — not the delta over the old
            # sets it is about to delete. The delta once let a whole 70 GB rebuild
            # past the fit check as a few GB of missing chips, and the run then hit
            # the free-space reserve and stopped mid-copy. Zipped because the card is
            # for a handheld: no emulator there scans loose chip folders — they all
            # read one zip per game.
            for core in arcade_cores:
                need += build_arcade(core, dry_run=True).get("bytes_needed", 0)
        for fills in bios_plan["picked"].values():
            need += _bios_need(dest, fills)
        free = _free_bytes(dest)
        if free is None:
            return {"error": f"cannot read free space on {dest} — is the card mounted?",
                    "copied": 0, "matched_files": len(rows), "by_system": {}, "errors": [],
                    "dupes_skipped": dupes_skipped, "stopped": False}
        room = free + (wiped["bytes"] if wipe else 0)
        if need > room - reserve and not fill:
            gb = 1024 ** 3
            clearing = (f" ({(wiped['bytes'] / gb):.1f} GB more once the ticked platforms "
                        f"are cleared — still not enough)") if wipe else ""
            return {"error": f"export needs {need / gb:.1f} GB but {dest} has "
                             f"{free / gb:.1f} GB free{clearing} "
                             f"({reserve / gb:.1f} GB is kept in reserve) — pick fewer "
                             f"platforms, tighten the curation gates, or tick 'fill the "
                             f"card' to copy until it is nearly full",
                    "needed_bytes": need, "free_bytes": free, "reserve_bytes": reserve,
                    "copied": 0, "matched_files": len(rows), "by_system": {}, "errors": [],
                    "dupes_skipped": dupes_skipped, "stopped": False}
        # The refusal is past: the plan's deletions can go ahead and make the room real.
        for child, _, _ in wipe_plan:
            if progress: progress(0, 0, f"clearing {child.name}…")
            shutil.rmtree(child, ignore_errors=True)

    def room_for(nbytes):
        free = _free_bytes(dest)
        return free is not None and free - nbytes >= reserve

    if arcade_sets:
        for core in arcade_cores:
            r = build_arcade(core, dry_run=dry_run)
            arcade_reports[core] = r
            if r.get("error"):
                errors.append({"file": "", "error": f"arcade ({core}): {r['error']}"})
            if r.get("out_of_room"):
                errors.append({"file": "", "error": f"{dest} reached its free-space reserve "
                                                    f"during the arcade build — export stopped"})
            if r.get("stopped"):
                stopped = True
                break

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

    # The BIOS files go in after the games — nobody browses for them, and a run that
    # stops early has already said so. Copying is the same skip-if-present/room rule the
    # games use; what makes the report earn its place is `missing`: the slots nothing in
    # the library fills, which are the ones that leave a whole console unplayable.
    console_bios = {}
    for system in sorted(bios_plan["picked"] | bios_plan["missing"].keys()):
        fills = bios_plan["picked"].get(system, {})
        rep = {"copied": [], "skipped": [],
               "missing": [" or ".join(s) for s in bios_plan["missing"].get(system, [])]}
        console_bios[system] = rep
        for name in sorted(fills):
            srcb, size = fills[name]
            if stopped or (stop is not None and stop.is_set()):
                stopped = True
                break
            target = dest / "system" / name
            if dry_run:
                planned_bytes += size
                rep["copied"].append(name)
                continue
            try:
                if target.exists() and target.stat().st_size == size:
                    rep["skipped"].append(name)
                elif not room_for(size):
                    errors.append({"file": "", "error": f"{dest} reached its free-space "
                                                        f"reserve — stopped before {name}"})
                    stopped = True
                    break
                else:
                    (dest / "system").mkdir(parents=True, exist_ok=True)
                    shutil.copy2(Path(srcb), target)
                    rep["copied"].append(name)
            except OSError as e:
                errors.append({"file": name, "error": str(e)})
                if e.errno == errno.ENOSPC:
                    _discard_partial(target)
                    errors.append({"file": "", "error": f"{dest} is full — export stopped"})
                    stopped = True
                    break
        if stopped:
            break
    if arcade_reports:
        by_system["arcade"] = sum(r["complete"] + r["incomplete"]
                                  for r in arcade_reports.values())
        copied += sum(r["copied"] for r in arcade_reports.values())
        if dry_run:
            planned_bytes += sum(r.get("bytes_needed", 0) for r in arcade_reports.values())
    out = {"matched_files": len(rows), "copied": copied, "skipped": skipped,
           "missing": missing, "by_system": by_system, "errors": errors,
           "arcade": (next(iter(arcade_reports.values())) if len(arcade_reports) == 1
                      else arcade_reports),
           "arcade_bios_unavailable": arcade_bios_unavailable,
           "console_bios": {"dir": str(dest / "system"), "systems": console_bios},
           "dupes_skipped": dupes_skipped, "wiped": wiped,
           "wanted_only": bool(wanted_only), "keep_only": bool(keep_only),
           "sources": sorted(sources) if sources else None, "stopped": stopped}
    if dry_run:
        out |= {"dry_run": True, "would_copy": sum(by_system.values()),
                "would_copy_bytes": planned_bytes,
                "would_copy_gb": round(planned_bytes / (1024 ** 3), 2)}
    return out


def _bios_need(dest, fills):
    """Bytes a real run would write for one system's BIOS files (same skip-if-present
    rule as the games)."""
    need = 0
    for name, (_, size) in fills.items():
        try:
            if (dest / "system" / name).stat().st_size == size:
                continue
        except OSError:
            pass
        need += size
    return need


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


def romset(profile, dest, leave_bytes=0, progress=None, db=None, stop=None):
    """Build one emulator core's romset onto a folder: every set its dat can assemble from
    the dump, as one zip per set, plus the bios parents (neogeo.zip, pgm.zip…) its games
    reference but its dat never defines — the cores look for those as their own zips, and
    without them a card full of games still refuses to boot.

    A zip that satisfies one core can fail on another: MAME current, FBNeo and
    MAME 2003-Plus each speak their own romset version, so each gets its own dat
    (DAT/MAME, DAT/FBNeo, DAT/MAME2003-Plus) and its own folder on the card —
    `romcom romset fbneo J:\arcade-fbneo`. `leave_bytes` is room to hold back for what
    comes after, so several cores can share one card by running biggest-first with a
    floor for the rest. The build never refuses — filling the folder is the point — and
    stops cleanly at the free-space reserve; complete zips are skipped, so a re-run is a
    top-up. Sets fill best-game-first (`_fill_order`), so a card that runs out of room
    misses the obscure tail, not the famous middle. Bios parents are built before the
    games that need them, first from the current-MAME dat and then from any extra dat
    in the profile's own folder — a core's games may need bios sets of another romset
    era (MAME 2003-Plus's cvs, taitofx1…) that neither dat of its own defines. What
    still cannot be built is reported, not guessed at."""
    from . import mameset
    db = db or connect()
    dest = Path(dest)
    empty = {"copied": 0, "bytes": 0, "bytes_needed": 0, "complete": 0, "incomplete": 0,
             "stopped": False, "out_of_room": False, "sets": {}}
    dat = mameset.dat_path(profile)
    if not dat:
        where = f"DAT/{mameset.PROFILES[profile][0]}" if profile in mameset.PROFILES else "DAT/"
        return {"error": f"no romset dat for {profile!r} — put its dat under {where}", **empty}
    if progress:
        progress(0, 0, f"{profile}: finding what the dump can assemble…")
    sets = _fill_order(db, mameset.assemblable(db, dat))
    if not sets:
        return {"error": f"{profile}: nothing assemblable from what is on disk", **empty}

    def room_for(nbytes):
        free = _free_bytes(dest)
        return free is not None and free - nbytes >= _reserve_bytes(dest) + leave_bytes

    # Bios parents first: they are small, they are shared by dozens of games, and a card
    # built out of room wants them on it before the games that need them.
    bios_built, bios_unavailable = [], {}
    gaps = mameset.romof_gaps(dat)
    if gaps:
        mame_dat = mameset.dat_path("mame")
        alts = [p for p in [mame_dat, *mameset.dat_paths(profile)[1:]] if p and p != dat]
        pending = dict(gaps)
        for alt in alts:
            if not pending:
                break
            hold = sorted(set(pending) & mameset.assemblable(db, alt))
            if not hold:
                continue
            if progress:
                progress(0, 0, f"{profile}: {len(hold)} bios sets from {alt.name} "
                               f"({', '.join(hold[:5])}…)")
            r = mameset.build(hold, dest, db=db, zipped=True, dat=alt,
                              room_for=room_for, stop=stop,
                              progress=None if progress is None else
                              lambda i, total, name: progress(i, total, f"bios: {name}"))
            bios_built += [s for s, v in (r.get("sets") or {}).items() if v.get("complete")]
            if r.get("out_of_room"):
                return {"profile": profile, "dat": dat.name, "assemblable": len(sets),
                        "bios_built": bios_built, "bios_unavailable": {},
                        "error": f"{dest} reached its free-space reserve during the bios "
                                 f"build — nothing else written", **{k: v for k, v in r.items()
                                                                   if k != "sets"}}
            for s in hold:
                pending.pop(s, None)
        bios_unavailable = pending
    if progress:
        progress(0, len(sets), f"{profile}: building {len(sets)} sets…")
    r = mameset.build(sets, dest, db=db, zipped=True, dat=dat, room_for=room_for,
                      stop=stop,
                      progress=None if progress is None else
                      lambda i, total, name: progress(i, total, name))
    return {"profile": profile, "dat": dat.name, "assemblable": len(sets),
            "bios_built": bios_built, "bios_unavailable": bios_unavailable,
            **{k: v for k, v in r.items() if k != "sets"},
            "stopped": bool(r.get("stopped") or r.get("out_of_room"))}
