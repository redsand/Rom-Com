"""Copy matched ROM files into a clean per-system layout, e.g. for an SD card."""
from pathlib import Path
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


def _safe(name):
    """A directory name MAME and Windows will both accept."""
    out = "".join(c for c in str(name) if c not in '<>:"/\\|?*').strip().rstrip(".")
    return out[:120] or "unknown"


def organize(dest, systems=None, progress=None, wanted_only=False, sources=None,
             dry_run=False, keep_only=False):
    """Copy files matched to a catalog item into <dest>/<system>/<filename>.

    Files are copied (never moved); an existing target of the same size is skipped,
    so re-running only tops up what's new.

    The filters are what make this a curation tool rather than a bulk copier:

    `wanted_only` restricts the export to items marked wanted, which is the whole point of
    curating a library and then deploying a subset of it. Off by default so existing callers
    keep their behaviour.

    `sources` restricts by `catalog_source`. Arcade is the case that needs it: a flat MAME
    dump leaves hundreds of loose chip files adopted as `local` items with names like
    `115b101`, and copying those to a card is pure noise next to the catalogued sets.

    `dry_run` reports exactly what would be copied, and how much space it needs, without
    touching the destination — worth doing before writing to a card.
    """
    db = connect()
    rows = db.execute("""SELECT f.path, i.system, i.title, i.external_id FROM files f
      JOIN items i ON i.id=f.matched_item_id
      WHERE (? = 0 OR i.wanted = 1) AND (? = 0 OR i.keep = 1)
      ORDER BY i.system, i.title""", (1 if wanted_only else 0, 1 if keep_only else 0)).fetchall()
    # Arcade is built, not copied. One physical file belongs to many sets, and
    # `files.matched_item_id` records a single owner, so copying matched files leaves every
    # set but one incomplete -- galaga came out missing prom-2.5c that way. A flattened dump
    # also renames collisions (`prom-2.5c_7`), and MAME only looks for the canonical name.
    # mameset.build resolves each set's roms from the dat by hash and writes them under the
    # names MAME expects, devices included.
    arcade_sets = sorted({(r["external_id"] or "").split("/", 1)[-1]
                          for r in rows if (r["system"] or "").lower() == "arcade"})
    arcade_report = None
    if arcade_sets:
        from . import mameset
        arcade_report = mameset.build(arcade_sets, Path(dest) / "arcade", db=db, dry_run=dry_run)
        rows = [r for r in rows if (r["system"] or "").lower() != "arcade"]

    if sources:
        keep = {s.lower() for s in sources}
        ids = {r["path"] for r in db.execute(
            """SELECT f.path FROM files f JOIN items i ON i.id=f.matched_item_id
               WHERE lower(COALESCE(i.catalog_source,'')) IN (%s)"""
            % ",".join("?" * len(keep)), tuple(keep))}
        rows = [r for r in rows if r["path"] in ids]
    if systems:
        wanted = {s.lower() for s in systems}
        rows = [r for r in rows if (r["system"] or "").lower() in wanted]
    dest = Path(dest)
    copied = skipped = missing = 0
    planned_bytes = 0
    errors = []
    by_system = {}
    for i, r in enumerate(rows):
        src = Path(r["path"])
        if progress: progress(i, len(rows), src.name)
        if not src.exists():
            missing += 1; continue
        if dry_run:
            planned_bytes += src.stat().st_size
            by_system[r["system"] or "unknown"] = by_system.get(r["system"] or "unknown", 0) + 1
            continue
        folder = dest / (r["system"] or "unknown")
        # Arcade is not one-file-per-game. A MAME set is many chip images that only mean
        # anything together, and a flat copy is doubly broken: 253,351 arcade files share
        # only 126,980 distinct basenames, so half of them would silently overwrite the
        # other half, and MAME could not read the result either. A directory named for the
        # set, holding its chips, is a layout MAME loads directly.
        if (r["system"] or "").lower() == "arcade":
            setname = (r["external_id"] or "").split("/", 1)[-1] or r["title"]
            folder = folder / _safe(setname)
        target = folder / src.name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size == src.stat().st_size:
                skipped += 1
            else:
                shutil.copy2(src, target)
                copied += 1
            by_system[r["system"] or "unknown"] = by_system.get(r["system"] or "unknown", 0) + 1
        except OSError as e:
            errors.append({"file": src.name, "error": str(e)})
    if progress: progress(len(rows), len(rows), "done")
    if arcade_report:
        by_system["arcade"] = arcade_report["complete"] + arcade_report["incomplete"]
        copied += arcade_report["copied"]
    out = {"matched_files": len(rows), "copied": copied, "skipped": skipped,
           "missing": missing, "by_system": by_system, "errors": errors,
           "arcade": arcade_report,
           "wanted_only": bool(wanted_only), "keep_only": bool(keep_only),
           "sources": sorted(sources) if sources else None}
    if dry_run:
        out |= {"dry_run": True, "would_copy": sum(by_system.values()),
                "would_copy_bytes": planned_bytes,
                "would_copy_gb": round(planned_bytes / (1024 ** 3), 2)}
    return out
