"""Give a rom back its real extension.

Some of this library arrived with a decoy suffix: `Stargate.smc.wmf`, `Aaahh!!! Real
Monsters.smc.wmf` — exactly 2,097,664 bytes, a genuine 2 MB SNES cartridge wearing a font or
image extension, presumably to slip past a filter somewhere. Everything that reads a rom by
its visible extension refuses them: RetroArch's playlist scanner, its core loader, and the
Play button by extension.

Renaming is the only real fix, and renaming someone's archive is not a thing to do casually.
So: a dry run by default, a manifest written before anything moves, and never an overwrite —
if the clean name is already taken, the decoy is left alone, because the owner already has
the good copy and the two may not be identical.
"""
import json
from datetime import datetime
from pathlib import Path

from .db import connect
from .scanner import EXT_SYSTEM


def candidates(db=None):
    """Files whose visible extension hides a real rom extension underneath."""
    db = db or connect()
    out = []
    for r in db.execute("""SELECT f.path, f.matched_item_id, i.system FROM files f
                           JOIN items i ON i.id = f.matched_item_id"""):
        p = Path(r["path"])
        ext = p.suffix.lower()
        inner = Path(p.stem).suffix.lower()
        if not inner or EXT_SYSTEM.get(ext):
            continue                       # already a sensible name
        if EXT_SYSTEM.get(inner) != (r["system"] or "").lower():
            continue                       # the hidden extension is not this system's
        target = p.with_name(p.stem)       # drop the decoy suffix
        out.append({"from": str(p), "to": str(target), "system": r["system"],
                    "item": r["matched_item_id"],
                    "blocked": target.exists(), "missing": not p.exists()})
    return out


def fix(apply=False, db=None):
    """Rename them. Reports what it would do unless `apply`."""
    db = db or connect()
    rows = candidates(db)
    doable = [r for r in rows if not r["blocked"] and not r["missing"]]
    report = {"found": len(rows),
              "blocked_clean_name_exists": sum(1 for r in rows if r["blocked"]),
              "missing_on_disk": sum(1 for r in rows if r["missing"]),
              "renamed": 0, "errors": [], "applied": bool(apply),
              "examples": [{k: r[k] for k in ("from", "to")} for r in doable[:6]]}
    if not apply or not doable:
        return report

    Path("backups").mkdir(exist_ok=True)
    manifest = Path("backups") / f"renames-{datetime.now():%Y%m%d-%H%M%S}.json"
    manifest.write_text(json.dumps(
        {"at": datetime.now().isoformat(timespec="seconds"),
         "note": "reverse by renaming 'to' back to 'from'",
         "renames": [{k: r[k] for k in ("from", "to")} for r in doable]},
        indent=1), encoding="utf-8")
    report["manifest"] = str(manifest)

    for r in doable:
        src, dst = Path(r["from"]), Path(r["to"])
        try:
            if dst.exists():
                continue                   # raced with something else; never overwrite
            src.rename(dst)
            with db:
                # The scan table must follow, or the catalog points at a path that is gone.
                db.execute("UPDATE files SET path=? WHERE path=?", (str(dst), str(src)))
            report["renamed"] += 1
        except OSError as e:
            report["errors"].append({"path": str(src), "error": str(e)})
    return report
