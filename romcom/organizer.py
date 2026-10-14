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


def _set_scores(db, names):
    """Each arcade set's card score (`_SCORE`), for picking a family's survivor."""
    out, lst = {}, list(names)
    for i in range(0, len(lst), 900):           # SQLite caps variables per statement
        chunk = lst[i:i + 900]
        q = ",".join("?" * len(chunk))
        for r in db.execute(
                "SELECT substr(external_id, instr(external_id,'/')+1) s, " + _SCORE + " sc"
                " FROM items i WHERE i.system='arcade'"
                f" AND substr(external_id, instr(external_id,'/')+1) IN ({q})", chunk):
            out[r["s"]] = r["sc"] or 0
    return out


def _prune_clones(db, core, sets):
    """'One copy per game' goes by the dat's own clone book, not just the catalog's
    titles: two sets of one MAME rom family must not both reach the card, however much
    their titles differ (doa2 beside doa2m, bloodstm beside bloodstm104). Every requested
    set is chased up its cloneof chain to its family root; each family with several
    members keeps a proper release over a bootleg or hack (ms5plus took Metal Slug 5's
    slot on score alone), then the best-scored, ties preferring the parent, then the
    first name."""
    from . import mameset
    path = mameset.dat_path(core)
    if not path:
        return list(sets), {}           # the build has its own guard for a missing dat
    family = mameset.cloneof_refs(sets, path)
    if not family:
        return list(sets), {}
    root = {}
    for s in sets:
        r, seen = s, set()
        while r in family and r not in seen:
            seen.add(r)
            r = family[r]
        root[s] = r
    scores = _set_scores(db, sets)
    from . import labels
    rough = labels.excluded_sets(sets, ["bootleg", "hack"], db)
    fam = {}
    for s in sets:
        fam.setdefault(root[s], []).append(s)
    drop = {}
    for members in fam.values():
        if len(members) < 2:
            continue
        best = min(members, key=lambda m: (m in rough, -scores.get(m, 0),
                                           0 if m == root[m] else 1, m))
        for m in members:
            if m != best:
                drop[m] = best
    return [s for s in sets if s not in drop], drop


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


def _selection(wanted_only=False, keep_only=False, rating_min=None, exclude_labels=None,
               left_out=None):
    """Every file an export would consider, best game first (see `_SCORE`). One query
    backs the export and the platform picker, so the picker's counts are exactly what
    an export copies. Games carrying an `exclude_labels` tag (`labels.DEFAULT_EXCLUDE`
    when None: fruit machines, gambling, not-a-game) are left out; `left_out`, a dict,
    collects how many items each system lost that way."""
    from . import labels
    db = connect()
    args = _gate_args(wanted_only, keep_only, rating_min)
    rows = db.execute(
        "SELECT f.path, f.bytes, f.sha1, f.md5, f.match_method, i.id item_id, "
        "i.system, i.title, i.external_id, i.rating, i.community_score"
        + _GATED + _ORDER, args).fetchall()
    # An arcade set is built from the dat by hash, not copied from its matched files — and
    # a chip shared by many sets records one owner, so a set whose every chip is owned by
    # a sibling had no row here and never reached a card, complete or not: mspacman,
    # puckman, atetris, mslug5. Playable sets with no file of their own join as one
    # file-less row each, under the same gates, in the same score order.
    orphans = db.execute(
        "SELECT NULL path, 0 bytes, NULL sha1, NULL md5, 'set' match_method, i.id item_id, "
        "i.system, i.title, i.external_id, i.rating, i.community_score FROM items i"
        " WHERE i.system='arcade' AND i.playable=1 AND COALESCE(i.is_device,0)=0"
        " AND NOT EXISTS (SELECT 1 FROM files f WHERE f.matched_item_id=i.id)"
        " AND " + _GATED.split(" WHERE ", 1)[1], args).fetchall()
    if orphans:
        rows = sorted([*rows, *orphans], key=lambda r: (-_score(r), r["system"] or "",
                                                        r["title"] or "", r["item_id"],
                                                        r["path"] or ""))
    drop = labels.excluded_items(exclude_labels, db)
    if not drop:
        return rows
    kept, gone = [], {}
    for r in rows:
        if r["item_id"] in drop:
            gone.setdefault(r["system"] or "unknown", set()).add(r["item_id"])
        else:
            kept.append(r)
    if left_out is not None:
        left_out.update({s: len(v) for s, v in gone.items()})
    return kept


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


def export_systems(wanted_only=False, keep_only=False, rating_min=None, distinct_games=False,
                   arcade_core=None, arcade_status=None, exclude_labels=None):
    """Per-system file count and size an export would copy under the current gates.

    Sizes come from the scan's recorded `files.bytes`, not a stat of every file, so this is
    fast enough to back a picker. Arcade's figure is the matched chip files; the built sets
    can differ a little (shared chips are written into every set that needs them). With
    `arcade_status`, sets the health gate would hold back for every one of the chosen
    cores are left out — from the last stored judgement, not a fresh one (the export
    judges afresh), so the picker stays quick."""
    rows = _selection(wanted_only, keep_only, rating_min, exclude_labels)
    if arcade_status and arcade_status != "all":
        from . import arcadehealth, mameset
        cores = (list(mameset.PROFILES) if arcade_core == "all"
                 else [arcade_core if arcade_core in mameset.PROFILES else "mame"])
        dead = arcadehealth.blocked(cores, arcade_status)
        if dead:
            rows = [r for r in rows if (r["system"] or "").lower() != "arcade"
                    or (r["external_id"] or "").split("/", 1)[-1] not in dead]
    rows, _ = _canonical(rows, distinct_games)
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
             distinct_games=False, wipe=False, arcade_core="mame", arcade_status=None,
             artwork=False, exclude_labels=None, layout="flat", chd=True):
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

    `arcade_status` is the health gate (`arcadehealth.GATES`): 'no_broken' (the default)
    keeps sets that will not start off the card — missing chips, CHD or bios, or a driver
    the core never finished — 'works_only' keeps only the good ones, 'all' builds
    everything. Each core is judged against its own dat before its sets are chosen, and
    the gate runs before 'one copy per game' picks a family's survivor, so a working clone
    takes the place of a broken parent instead of the family vanishing.

    `artwork` copies the cached box art and snaps (`artwork.fetch`) of the games on the
    card into ES-DE's media folders on it, last, after every game and bios — nothing is
    downloaded during an export.

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
    from . import labels
    exclude_labels = labels.normalize_exclude(exclude_labels)
    labels_left_out = {}
    rows = _selection(wanted_only, keep_only, rating_min, exclude_labels, labels_left_out)
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
    from . import arcadehealth
    arcade_status = arcade_status if arcade_status in arcadehealth.GATES         else arcadehealth.DEFAULT_GATE
    gating = bool(arcade_rows) and arcade_status != "all"
    known, arcade_held_back = {}, {}
    if gating:
        asked = list(dict.fromkeys(
            (r["external_id"] or "").split("/", 1)[-1] for r in arcade_rows))
        for core in arcade_cores:
            if progress: progress(0, 0, f"judging {core} arcade sets…")
            arcadehealth.evaluate(core, db)
            known[core] = arcadehealth.statuses(core, db)
            # Reported against everything the recipe asked for: the rows filter below
            # removes the sets no core runs before the per-core gate ever sees them.
            held = arcadehealth.gate(core, asked, arcade_status, db, known=known[core])[1]
            if held:
                arcade_held_back[core] = held
        # Rows no chosen core can run go before the title collapse below, or a broken
        # variant could win a game's one slot over a working one.
        dead = arcadehealth.blocked(arcade_cores, arcade_status, db)
        arcade_rows = [r for r in arcade_rows
                       if (r["external_id"] or "").split("/", 1)[-1] not in dead]
    if distinct_games and arcade_rows:
        # Same bytes only: arcade's one-copy-per-game is the dat's clone book
        # (`_prune_clones` below). By title, Sega's tetris and Atari's atetris were one
        # game and only one reached the card.
        arcade_rows, arcade_dupes_skipped = _canonical(arcade_rows, False)
    arcade_sets = list(dict.fromkeys(
        (r["external_id"] or "").split("/", 1)[-1] for r in arcade_rows))
    # Each core has its own romset book, so the clone pruning is per core: two sets of
    # one MAME family must not both reach a MAME card, but an FBNeo family may differ.
    # The mame romof parents below follow mame's own pruned list.
    core_sets = {core: arcade_sets for core in arcade_cores}
    if gating:
        for core in arcade_cores:
            core_sets[core] = arcadehealth.gate(core, arcade_sets, arcade_status,
                                                db, known=known[core])[0]
    clones_pruned = {}
    if distinct_games and arcade_sets:
        for core in arcade_cores:
            core_sets[core], drop = _prune_clones(db, core, core_sets[core])
            if drop:
                clones_pruned[core] = drop
                arcade_dupes_skipped += len(drop)
    arcade_sets = core_sets.get("mame", arcade_sets)
    # The romof parents a set cannot boot without (neogeo, pgm…) come along whether or not
    # the recipe ticked them: mslug on a card without neogeo.zip beside it refuses to boot.
    # Every core, not just mame — FBNeo's dat lists no bios chips in its games at all.
    # Parents first, so a build that runs out of room stops before the games needing them.
    # One the core's own dat cannot build may come from a fallback dat (the 0.78 bios MAME
    # 2003-Plus leans on); what nothing can build is reported, not guessed at.
    arcade_bios_unavailable, fallback_bios = {}, {}
    for core in arcade_cores:
        dat = mameset.dat_path(core)
        if not dat or not core_sets[core]:
            continue
        machines = arcadehealth.parse(dat)
        parents = {}
        for name in core_sets[core]:
            for p in arcadehealth.needed_parents(machines, name):
                parents[p] = parents.get(p, 0) + 1
        if not parents:
            continue
        ok = mameset.assemblable(db, dat)
        hold = sorted(p for p in parents if p in ok)
        pending = {p: n for p, n in parents.items() if p not in ok}
        plan = []
        for alt in (arcadehealth.alt_dats(core, dat) if pending else []):
            got = sorted(set(pending) & mameset.assemblable(db, alt))
            if got:
                plan.append((alt, got))
                for g in got:
                    pending.pop(g)
        fallback_bios[core] = plan
        arcade_bios_unavailable.update(pending)
        core_sets[core] = hold + [x for x in core_sets[core] if x not in set(hold)]
    arcade_sets = core_sets.get("mame", arcade_sets)
    # Something to build on some core — arcade_sets alone only speaks for mame.
    arcade_any = any(core_sets.values())
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
    # What reaches the card is what ES-DE can list and launch (esde.py): its folder name,
    # its file types, archives unpacked where the emulator cannot read them, one entry per
    # disc. `layout` 'ranked' keeps the old <system>/<score>/ folders; 'flat' (the default)
    # puts every game straight in the system folder — gamelist.xml carries the rating.
    from . import esde
    layout = layout if layout in ("flat", "ranked") else "flat"
    if progress and rows: progress(0, 0, "planning the card layout (reading archives)…")
    games = _plan_games(rows, layout, chd=chd)
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
        targets = set(wanted_systems) | {esde.folder(s) for s in wanted_systems}
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
        kw = dict(db=db, dry_run=dry_run, zipped=True,
                  fresh=fresh_wipe if dry_run else False,
                  room_for=None if dry_run else room_for,
                  stop=None if dry_run else stop,
                  progress=None if progress is None else
                  lambda i, total, name: progress(i, total, f"{label}: {name}"))
        out = None
        for alt, names in fallback_bios.get(core, []):      # bios from other dats first
            r = mameset.build(names, dest_dir, dat=alt, **kw)
            out = r if out is None else _merge_builds(out, r)
            if r.get("stopped"):
                return out
        r = mameset.build(core_sets[core], dest_dir, profile=core, dat=dat, **kw)
        return r if out is None else _merge_builds(out, r)

    if not dry_run:
        # After a wipe nothing at the targets survives to be reused, so the copy needs the
        # selection's full bytes; without one, present same-size files are skipped and the
        # need reflects that. Clearing the ticked platforms counts as room the copy may use.
        if progress: progress(0, 0, "measuring what's left to copy…")
        need = (sum(s[-1] or 0 for g in games for s in g["steps"]) if wipe
                else _bytes_to_copy(games, dest))
        if arcade_any:
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

    if arcade_any:
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
    not_playable, unpacked, converted, card_games = {}, 0, 0, {}
    for i, g in enumerate(games):
        if stopped or (stop is not None and stop.is_set()):
            stopped = True
            break
        r = g["row"]
        system = r["system"] or "unknown"
        if progress: progress(i, len(games), r["title"] or "")
        missing += g["missing"]
        if g["dropped"]:
            not_playable[g["folder"]] = not_playable.get(g["folder"], 0) + g["dropped"]
        if not g["steps"]:
            continue
        base = dest / g["folder"] / g["sub"] if g["sub"] else dest / g["folder"]
        done = True
        for step in g["steps"]:
            if dry_run:
                planned_bytes += step[-1] or 0
                continue
            try:
                if step[0] == "copy":
                    _, src, rel, size = step
                    target = base / rel
                    if target.exists() and target.stat().st_size == size:
                        skipped += 1
                        continue
                    if not room_for(size):
                        errors.append({"file": "", "error": f"{dest} reached its free-space "
                                                            f"reserve — export stopped before "
                                                            f"{Path(src).name}"})
                        stopped = True
                        done = False
                        break
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
                    copied += 1
                elif step[0] == "m3u":
                    _, rel, text, size = step
                    target = base / rel
                    if target.exists() and target.read_text(encoding="utf-8") == text:
                        skipped += 1
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8", newline="\n")
                    copied += 1
                elif step[0] == "chd":
                    _, src, names, main, rel, size = step
                    target = base / rel
                    if target.exists() and target.stat().st_size > 0:
                        skipped += 1
                        continue
                    if not room_for(size):
                        errors.append({"file": "", "error": f"{dest} reached its free-space "
                                                            f"reserve — export stopped before "
                                                            f"{main}"})
                        stopped = True
                        done = False
                        break
                    if progress: progress(i, len(games), f"converting {main} to CHD…")
                    esde.make_chd(src, names, main, target, system)
                    copied += 1
                    converted += 1
                else:
                    _, src, names, rel_dir, size = step
                    tdir = base / rel_dir if rel_dir else base
                    if _unpacked(tdir, src, names):
                        skipped += 1
                        continue
                    if not room_for(size):
                        errors.append({"file": "", "error": f"{dest} reached its free-space "
                                                            f"reserve — export stopped before "
                                                            f"{Path(src).name}"})
                        stopped = True
                        done = False
                        break
                    try:
                        esde.extract(src, names, tdir)
                    except OSError:
                        for n in names:
                            _discard_partial(tdir / Path(n).name)
                        raise
                    copied += 1
                    unpacked += 1
            except OSError as e:
                errors.append({"file": Path(step[1]).name, "error": str(e)})
                done = False
                if getattr(e, "errno", None) == errno.ENOSPC:
                    errors.append({"file": "", "error": f"{dest} is full — export stopped"})
                    stopped = True
                    break
        if stopped and not done:
            break
        if done or dry_run:
            by_system[system] = by_system.get(system, 0) + 1
            prefix = f"{g['sub']}/" if g["sub"] else ""
            card_games.setdefault(g["folder"], []).extend(
                (prefix + e, r) for e in g["entries"])
    if progress: progress(len(games), len(games), "stopped" if stopped else "done")

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
    # Arcade's entries: the zips the build completed, under the folder it wrote them in.
    for core, rep_ in arcade_reports.items():
        sub = "" if len(arcade_cores) == 1 else f"{core}/"
        names = [s for s, v in (rep_.get("sets") or {}).items() if v.get("complete")]
        rows_by_set = _arcade_rows(db, names)
        card_games.setdefault("arcade", []).extend(
            (f"{sub}{s}.zip", rows_by_set[s]) for s in names if s in rows_by_set)
    gamelists = None
    if not dry_run and card_games:
        try:
            gamelists = _write_gamelists(dest, card_games, db)
        except OSError as e:
            errors.append({"file": "gamelist.xml", "error": str(e)})
    artwork_report = None
    if artwork and not dry_run and not stopped:
        from . import artwork as art
        if progress: progress(0, 0, "artwork for ES-DE…")
        artwork_report = art.export_media(
            dest, room_for=room_for, progress=progress, db=db,
            entries={f: [(e, r["item_id"]) for e, r in lst] for f, lst in card_games.items()})
        if artwork_report.get("stopped"):
            errors.append({"file": "", "error": f"{dest} reached its free-space reserve "
                                                f"while copying artwork — stopped"})
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
           "clones_pruned": clones_pruned or None,
           "arcade_status": arcade_status,
           "exclude_labels": exclude_labels, "labels_left_out": labels_left_out or None,
           "arcade_held_back": {core: _held_summary(h) for core, h in arcade_held_back.items()}
                               or None,
           "console_bios": {"dir": str(dest / "system"), "systems": console_bios},
           "dupes_skipped": dupes_skipped, "wiped": wiped, "artwork": artwork_report,
           "layout": layout, "not_playable": not_playable or None, "unpacked": unpacked,
           "converted_chd": converted,
           "gamelists": gamelists,
           "wanted_only": bool(wanted_only), "keep_only": bool(keep_only),
           "sources": sorted(sources) if sources else None, "stopped": stopped}
    if dry_run:
        out |= {"dry_run": True, "would_copy": sum(by_system.values()),
                "would_copy_bytes": planned_bytes,
                "would_copy_gb": round(planned_bytes / (1024 ** 3), 2)}
    return out


def _merge_builds(a, b):
    """Two `mameset.build` reports as one — a core's fallback-dat bios build and its own."""
    out = dict(b)
    out["sets"] = {**(a.get("sets") or {}), **(b.get("sets") or {})}
    for k in ("copied", "bytes", "bytes_needed", "complete", "incomplete"):
        out[k] = (a.get(k) or 0) + (b.get(k) or 0)
    for k in ("stopped", "out_of_room"):
        out[k] = bool(a.get(k) or b.get(k))
    return out


def _held_summary(held):
    """What the health gate kept off the card for one core: how many, grouped by the fix
    each would need, with a few names — the full list would bury the export result."""
    from .arcadehealth import _reason_kind
    by = {}
    for why in held.values():
        status, _, reason = why.partition(": ")
        k = _reason_kind(reason) if reason else status
        by[k] = by.get(k, 0) + 1
    return {"count": len(held),
            "by_reason": dict(sorted(by.items(), key=lambda kv: -kv[1])),
            "sample": dict(sorted(held.items())[:20])}


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


def _plan_games(rows, layout, chd=False):
    """Group the selection's files by game (adjacent in `_ORDER`) and plan each one's way
    onto the card with `esde.plan`: folder, files to copy, archives to unpack."""
    from . import esde
    groups = []
    for r in rows:
        if groups and groups[-1][0]["item_id"] == r["item_id"]:
            groups[-1].append(r)
        else:
            groups.append([r])
    out = []
    for g in groups:
        r0, present, missing = g[0], [], 0
        for r in g:
            try:
                present.append((r["path"], Path(r["path"]).stat().st_size))
            except OSError:
                missing += 1
        steps, entries, dropped = (esde.plan(r0["system"], present, chd=chd) if present
                                   else ([], [], 0))
        out.append({"row": r0, "folder": esde.folder(r0["system"]),
                    "sub": _rank_folder(r0) if layout == "ranked" else "",
                    "steps": steps, "entries": entries, "dropped": dropped,
                    "missing": missing})
    _group_discs(out)
    return out


_DISC = re.compile(r"\s*\((?:disc|disk|cd)\s*(\d+)(?:\s*of\s*\d+)?\)", re.I)


def _group_discs(games):
    """A multi-disc game is one catalog item per disc, so ES-DE listed 'Disc 1' and
    'Disc 2' as two games and the emulator could not swap between them. Discs of one
    title go into one `<Game>.m3u/` folder with the playlist ES-DE launches (a directory
    interpreted as a file): one entry, and the core swaps discs from the .m3u."""
    from . import esde
    disc_systems = esde.CHD_SYSTEMS | {"gc", "wii"}
    # One item holding every disc (Killer7's two .rvz under one catalog entry).
    for g in games:
        if g["folder"] not in disc_systems or len(g["entries"]) < 2:
            continue
        found = [(_DISC.search(Path(e).stem), e) for e in g["entries"]]
        if not all(m for m, _ in found) or len({int(m.group(1)) for m, _ in found}) < 2:
            continue
        bases = {_DISC.sub("", Path(e).stem).strip() for _, e in found}
        if len(bases) != 1:
            continue
        d = f"{_safe(bases.pop())}.m3u"
        found.sort(key=lambda x: int(x[0].group(1)))
        lines = [_disc_line(g, e) for _, e in found]
        g["steps"] = [_under(s, d) for s in g["steps"]]
        text = "\n".join(lines) + "\n"
        g["steps"].append(("m3u", f"{d}/{d}", text, len(text.encode())))
        g["entries"] = [d]
    groups = {}
    for g in games:
        if g["folder"] not in disc_systems or len(g["entries"]) != 1:
            continue
        m = _DISC.search(g["row"]["title"] or "")
        if not m:
            continue
        base = _DISC.sub("", g["row"]["title"]).strip()
        groups.setdefault((g["folder"], g["sub"], base.lower()), []).append(
            (int(m.group(1)), g, base))
    for key, members in groups.items():
        if len({n for n, _, _ in members}) < 2:
            continue
        members.sort(key=lambda x: x[0])
        base = _safe(members[0][2])
        d = f"{base}.m3u"
        lines = []
        for _, g, _ in members:
            lines.append(_disc_line(g, g["entries"][0]))
            g["steps"] = [_under(s, d) for s in g["steps"]]
            g["entries"] = []
        last = members[-1][1]
        text = "\n".join(lines) + "\n"
        last["steps"].append(("m3u", f"{d}/{d}", text, len(text.encode())))
        members[0][1]["entries"] = [d]


def _disc_line(g, entry):
    """A disc's line in the .m3u: the entry itself, or the main file inside it when the
    entry is a directory interpreted as a file ("Disc 1.cue/Disc 1.cue")."""
    nested = any(s[0] == "copy" and s[2].startswith(entry + "/") or
                 s[0] == "extract" and s[3] == entry for s in g["steps"])
    return f"{entry}/{entry}" if nested else entry


def _under(step, d):
    """The same step, written inside folder `d` of the system folder."""
    if step[0] == "copy":
        return (step[0], step[1], f"{d}/{step[2]}", step[3])
    if step[0] == "extract":
        return (step[0], step[1], step[2], f"{d}/{step[3]}" if step[3] else d, step[4])
    if step[0] == "chd":
        return step[:4] + (f"{d}/{step[4]}", step[5])
    return step


def _unpacked(tdir, archive, names):
    """Is an archive's unpack already on the card, every file at its full size?"""
    from . import esde
    sizes = dict(esde.members(archive))
    for n in names:
        try:
            if (tdir / Path(n).name).stat().st_size != sizes.get(n):
                return False
        except OSError:
            return False
    return True


def _bytes_to_copy(games, dest):
    """Bytes a real run would write for the planned games: what is not already on the card
    at its full size (the same rule the copy loop uses to skip)."""
    need = 0
    for g in games:
        base = dest / g["folder"] / g["sub"] if g["sub"] else dest / g["folder"]
        for step in g["steps"]:
            if step[0] == "copy":
                try:
                    if (base / step[2]).stat().st_size == step[3]:
                        continue
                except OSError:
                    pass
            elif step[0] == "chd":
                if (base / step[4]).exists():
                    continue
            elif step[0] == "m3u":
                continue
            elif _unpacked(base / step[3] if step[3] else base, step[1], step[2]):
                continue
            need += step[-1] or 0
    return need


def _arcade_rows(db, names):
    """{setname: item row} for built sets, for their gamelist entries."""
    out, lst = {}, list(names)
    for i in range(0, len(lst), 900):
        chunk = lst[i:i + 900]
        for r in db.execute(
                "SELECT id item_id, title, system, rating, community_score, "
                "substr(external_id, instr(external_id,'/')+1) s FROM items "
                f"WHERE system='arcade' AND substr(external_id, instr(external_id,'/')+1) "
                f"IN ({','.join('?' * len(chunk))})", chunk):
            out.setdefault(r["s"], r)
    return out


def _write_gamelists(dest, card_games, db):
    """Every game the export put on the card, as ES-DE gamelist entries: a clean name, the
    rating the export ranked by, and the arcade genre."""
    from . import esde, labels
    ids = {r["item_id"] for lst in card_games.values() for _, r in lst}
    tags = labels.for_items(ids, db)
    games = {}
    for fold, lst in card_games.items():
        for entry, r in lst:
            genre = next((l[6:].replace("-", " ").title() for l in tags.get(r["item_id"], [])
                          if l.startswith("genre:")), None)
            name = r["title"] if fold == "arcade" else esde.display_name(r["title"])
            games.setdefault(fold, []).append({"path": entry, "name": name, "genre": genre,
                                               "rating": esde.rating(_score(r))})
    return esde.write_gamelists(dest, games)


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


def romset(profile, dest, leave_bytes=0, progress=None, db=None, stop=None,
           arcade_status=None, exclude_labels=None):
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
    # What a set IS comes before whether it runs: a perfect fruit machine is still not a
    # game for the card (`labels`; bios parents come back below whatever their label).
    from . import labels
    exclude_labels = labels.normalize_exclude(exclude_labels)
    off = labels.excluded_sets(sets, exclude_labels, db)
    if off:
        from . import arcadehealth
        machines = arcadehealth.parse(dat)
        needed = {p for s in sets if s not in off
                  for p in arcadehealth.needed_parents(machines, s)}
        off -= needed          # neogeo is 'System / BIOS' to catver, and mslug needs it
    sets = [s for s in sets if s not in off]
    if not sets:
        return {"error": f"{profile}: every assemblable set carries a label left out "
                         f"({', '.join(exclude_labels)})", **empty}
    # Every chip present is not the same as runs: the health gate (see `organize`) also
    # holds back sets missing a CHD or bios parent, and drivers the core never finished.
    from . import arcadehealth
    arcade_status = arcade_status if arcade_status in arcadehealth.GATES         else arcadehealth.DEFAULT_GATE
    held = {}
    if arcade_status != "all":
        if progress:
            progress(0, 0, f"{profile}: judging which sets will run…")
        arcadehealth.evaluate(profile, db)
        sets, held = arcadehealth.gate(profile, sets, arcade_status, db)
        if not sets:
            return {"error": f"{profile}: every assemblable set is held back by the "
                             f"'{arcade_status}' health gate", **empty,
                    "held_back": _held_summary(held)}

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
    return {"profile": profile, "dat": dat.name, "assemblable": len(sets) + len(held),
            "bios_built": bios_built, "bios_unavailable": bios_unavailable,
            "arcade_status": arcade_status,
            "held_back": _held_summary(held) if held else None,
            **{k: v for k, v in r.items() if k != "sets"},
            "stopped": bool(r.get("stopped") or r.get("out_of_room"))}
