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


def _gate_args(wanted_only, keep_only, rating_min):
    rating_min = int(rating_min or 0)
    return (1 if wanted_only else 0, 1 if keep_only else 0, rating_min, rating_min)


def export_systems(wanted_only=False, keep_only=False, rating_min=None):
    """Per-system file count and size an export would copy under the current gates.

    Sizes come from the scan's recorded `files.bytes`, not a stat of every file, so this is
    fast enough to back a picker. Arcade's figure is the matched chip files; the built sets
    can differ a little (shared chips are written into every set that needs them)."""
    db = connect()
    return [dict(r) for r in db.execute(
        "SELECT COALESCE(i.system,'unknown') system, COUNT(*) files, "
        "COALESCE(SUM(f.bytes),0) bytes" + _GATED + " GROUP BY 1 ORDER BY 1",
        _gate_args(wanted_only, keep_only, rating_min))]


def _safe(name):
    """A directory name MAME and Windows will both accept."""
    out = "".join(c for c in str(name) if c not in '<>:"/\\|?*').strip().rstrip(".")
    return out[:120] or "unknown"


def organize(dest, systems=None, progress=None, wanted_only=False, sources=None,
             dry_run=False, keep_only=False, rating_min=None, stop=None, fill=False):
    """Copy files matched to a catalog item into <dest>/<system>/<filename>.

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
    reserve still holds; only the all-or-nothing refusal is waived.

    `stop` is a threading.Event checked between files; setting it ends the run with
    `stopped: True`. A file mid-copy is finished, not truncated.
    """
    db = connect()
    rows = db.execute("SELECT f.path, i.system, i.title, i.external_id" + _GATED
                      + " ORDER BY i.system, i.title",
                      _gate_args(wanted_only, keep_only, rating_min)).fetchall()
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
    dest = Path(dest)
    stopped = False
    errors = []

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
                    "stopped": False}
        if need > free - reserve and not fill:
            gb = 1024 ** 3
            return {"error": f"export needs {need / gb:.1f} GB but {dest} has "
                             f"{free / gb:.1f} GB free ({reserve / gb:.1f} GB is kept in "
                             f"reserve) — pick fewer platforms, tighten the curation "
                             f"gates, or tick 'fill the card' to copy until it is nearly full",
                    "needed_bytes": need, "free_bytes": free, "reserve_bytes": reserve,
                    "copied": 0, "matched_files": len(rows), "by_system": {}, "errors": [],
                    "stopped": False}

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
        folder = dest / (r["system"] or "unknown")
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
           "arcade": arcade_report,
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
        target = dest / (r["system"] or "unknown") / src.name
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
