"""Copy matched ROM files into a clean per-system layout, e.g. for an SD card."""
from pathlib import Path
import shutil
from .db import connect

def organize(dest, systems=None, progress=None):
    """Copy every scanned file matched to a catalog item into <dest>/<system>/<filename>.

    Files are copied (never moved); an existing target of the same size is skipped,
    so re-running only tops up what's new.
    """
    db = connect()
    rows = db.execute("""SELECT f.path, i.system, i.title FROM files f
      JOIN items i ON i.id=f.matched_item_id
      ORDER BY i.system, i.title""").fetchall()
    if systems:
        wanted = {s.lower() for s in systems}
        rows = [r for r in rows if (r["system"] or "").lower() in wanted]
    dest = Path(dest)
    copied = skipped = missing = 0
    errors = []
    by_system = {}
    for i, r in enumerate(rows):
        src = Path(r["path"])
        if progress: progress(i, len(rows), src.name)
        if not src.exists():
            missing += 1; continue
        folder = dest / (r["system"] or "unknown")
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
    return {"matched_files": len(rows), "copied": copied, "skipped": skipped,
            "missing": missing, "by_system": by_system, "errors": errors}
