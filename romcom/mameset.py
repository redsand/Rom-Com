"""Rebuild MAME sets on disk from a flat rom dump.

A MAME set is a directory (or zip) of chip images with exact names. Two facts make the
ordinary export model wrong for arcade:

1. One physical file belongs to MANY sets. Shared proms and bios chips are used across
   dozens of machines, but `files.matched_item_id` holds a single owner — so every set
   except the lucky one exports incomplete, and MAME rejects it.
2. A flattened dump renames collisions. The same chip arrives as `prom-2.5c`, `prom-2.5c_1`,
   `prom-2.5c_7`, and MAME looks for the canonical name only.

So arcade sets are built from the dat instead: it lists every rom a machine needs, with a
crc and a sha1, and the scan table already knows which file on disk has which hash. Match by
hash, write under the name the dat gives.
"""
import re
from pathlib import Path

from .config import ROOT
from .db import connect


def dat_path():
    d = ROOT / "DAT" / "MAME"
    if not d.exists():
        return None
    return next((p for p in sorted(d.glob("*.dat")) if "arcade" in p.name.lower()), None)


def set_roms(setnames, path=None):
    """{setname: [{name, crc, sha1}]} for the machines asked for, plus their device sets.

    Device refs are followed one level, which is what MAME itself needs: galaga cannot boot
    without namco54's roms, and the dat is where that dependency is written down.
    """
    path = path or dat_path()
    if not path:
        return {}
    want, out, deps = set(setnames), {}, set()
    cur = None
    for line in path.open(encoding="utf-8", errors="replace"):
        m = re.search(r'<(?:game|machine)\s+name="([^"]+)"', line)
        if m:
            cur = m.group(1)
            continue
        if cur not in want:
            continue
        d = re.search(r'<device_ref\s+name="([^"]+)"', line)
        if d:
            deps.add(d.group(1))
            continue
        if '<rom ' not in line:
            continue
        # Attributes are read one at a time on purpose. A single pattern with optional
        # crc/sha1 groups matched the name and silently dropped both hashes, which made
        # every set look like it had zero roms.
        nm = re.search(r'\bname="([^"]+)"', line)
        if not nm:
            continue
        crc = re.search(r'\bcrc="([0-9a-fA-F]+)"', line)
        sha = re.search(r'\bsha1="([0-9a-fA-F]+)"', line)
        if not (crc or sha):
            continue        # a rom with no hash cannot be located on disk
        out.setdefault(cur, []).append(
            {'name': nm.group(1), 'crc': (crc.group(1).lower() if crc else ''),
             'sha1': (sha.group(1).lower() if sha else '')})
    if deps - want:
        out.update(set_roms(deps - want, path=path))
    return out


def _index(db, roms):
    """hash -> a path on disk, for every rom wanted. One query, not one per chip."""
    crcs = {r["crc"] for rs in roms.values() for r in rs if r["crc"]}
    shas = {r["sha1"] for rs in roms.values() for r in rs if r["sha1"]}
    by_crc, by_sha = {}, {}
    for col, wanted, into in (("crc32", crcs, by_crc), ("sha1", shas, by_sha)):
        vals = [v for v in wanted if v]
        for i in range(0, len(vals), 900):          # SQLite caps variables per statement
            chunk = vals[i:i + 900]
            q = ",".join("?" * len(chunk))
            for row in db.execute(
                    f"SELECT {col} h, path FROM files WHERE lower({col}) IN ({q})", chunk):
                into.setdefault((row["h"] or "").lower(), row["path"])
    return by_crc, by_sha


def build(setnames, dest, db=None, dry_run=False):
    """Write <dest>/<set>/<canonical rom name> for each set. Returns a per-set report."""
    db = db or connect()
    roms = set_roms(setnames)
    if not roms:
        return {"sets": {}, "error": "no MAME dat found under DAT/MAME", "copied": 0}
    by_crc, by_sha = _index(db, roms)
    dest = Path(dest)
    report, copied, total_bytes = {}, 0, 0
    import shutil
    for name, chips in sorted(roms.items()):
        found, missing = [], []
        for chip in chips:
            src = by_sha.get(chip["sha1"]) or by_crc.get(chip["crc"])
            if not src or not Path(src).exists():
                missing.append(chip["name"])
                continue
            found.append((src, chip["name"]))
        if not dry_run and found:
            folder = dest / name
            folder.mkdir(parents=True, exist_ok=True)
            for src, canonical in found:
                target = folder / canonical
                s = Path(src)
                if target.exists() and target.stat().st_size == s.stat().st_size:
                    continue
                shutil.copy2(s, target)
                copied += 1
        total_bytes += sum(Path(s).stat().st_size for s, _ in found if Path(s).exists())
        report[name] = {"roms": len(chips), "found": len(found), "missing": missing[:8],
                        "complete": not missing}
    return {"sets": report, "copied": copied, "bytes": total_bytes,
            "complete": sum(1 for v in report.values() if v["complete"]),
            "incomplete": sum(1 for v in report.values() if not v["complete"])}


def assemblable(db=None, path=None):
    """Every machine in the dat whose roms are all present on disk, by hash.

    This is the honest answer to "can I play this", and item status is not. A MAME set is
    assembled from chips scattered through a flat dump, so an item with no matched file of
    its own can still be complete — Ms. Pac-Man is CATALOGED with zero matched files and
    builds perfectly. Conversely a VERIFIED item can be missing a chip another set claimed.

    One pass over the dat and two hash sets: about a second for 13,743 machines.
    """
    from .db import connect
    db = db or connect()
    path = path or dat_path()
    if not path:
        return set()
    sets, cur = {}, None
    for line in path.open(encoding="utf-8", errors="replace"):
        m = re.search(r'<(?:game|machine)\s+name="([^"]+)"', line)
        if m:
            cur = m.group(1)
            continue
        if "<rom " not in line:
            continue
        sha = re.search(r'\bsha1="([0-9a-fA-F]+)"', line)
        crc = re.search(r'\bcrc="([0-9a-fA-F]+)"', line)
        if sha or crc:
            sets.setdefault(cur, []).append(
                ((sha.group(1) if sha else "").lower(), (crc.group(1) if crc else "").lower()))
    have_sha = {(r[0] or "").lower() for r in db.execute(
        "SELECT sha1 FROM files WHERE sha1 IS NOT NULL")}
    have_crc = {(r[0] or "").lower() for r in db.execute(
        "SELECT crc32 FROM files WHERE crc32 IS NOT NULL")}
    return {name for name, roms in sets.items()
            if roms and all((s in have_sha) or (c in have_crc) for s, c in roms)}


def refresh_playable(db=None):
    """Recompute items.playable for arcade. Returns how many are playable.

    Stored rather than computed per request: the UI asks this for every row on every page,
    and a second of dat parsing per page load is not a trade worth making.
    """
    from .db import connect
    db = db or connect()
    ok = assemblable(db)
    with db:
        db.execute("UPDATE items SET playable=0 WHERE system='arcade' AND playable<>0")
        if ok:
            q = ",".join("?" * len(ok))
            db.execute(f"""UPDATE items SET playable=1 WHERE system='arcade'
                           AND COALESCE(is_device,0)=0
                           AND substr(external_id, instr(external_id,'/')+1) IN ({q})""",
                       tuple(ok))
    return db.execute("SELECT COUNT(*) c FROM items WHERE playable=1").fetchone()["c"]


def prove(setnames=None, db=None, rompath=None, batch=200, progress=None):
    """Ask MAME itself whether sets are complete, and record the verdict.

    `assemblable()` is our own arithmetic over the dat; this is the emulator that will
    actually run the game, checking every rom's size and hash with `-verifyroms`. For an
    archive that is the difference between believing a set is good and knowing it. MAME
    accepts many set names per invocation, so this is batched rather than one process per
    game.

    Sets are staged into the rompath first, because MAME can only verify what it can see.
    Returns {"good": [...], "bad": {set: reason}, "checked": n}.
    """
    import re
    import subprocess
    from .db import connect
    from .player import emulator_options, rompath as configured_rompath

    db = db or connect()
    root = rompath or configured_rompath()
    if not root:
        return {"error": "no rompath configured in emulators.yaml", "checked": 0,
                "good": [], "bad": {}}
    exe = None
    for _name, template in emulator_options("arcade"):
        for token in template.replace('"', " ").split():
            if token.lower().endswith("mame.exe") and Path(token).exists():
                exe = token
                break
        if exe:
            break
    if not exe:
        return {"error": "mame.exe not found in the arcade emulator command", "checked": 0,
                "good": [], "bad": {}}

    if setnames is None:
        setnames = sorted(r["external_id"].split("/", 1)[-1] for r in db.execute(
            "SELECT external_id FROM items WHERE system='arcade' AND playable=1"
            " AND COALESCE(is_device,0)=0"))
    setnames = list(setnames)
    good, best, bad = [], [], {}
    for i in range(0, len(setnames), batch):
        chunk = setnames[i:i + batch]
        build(chunk, root, db=db)          # MAME can only verify what it can see
        if progress:
            progress(i + len(chunk), len(setnames))
        try:
            p = subprocess.run([exe, "-rompath", str(root), "-verifyroms", *chunk],
                               capture_output=True, text=True, timeout=1800,
                               cwd=str(Path(exe).parent))
        except Exception as e:
            for s in chunk:
                bad[s] = f"{type(e).__name__}: {e}"
            continue
        for line in (p.stdout or "").splitlines():
            m = re.match(r"romset (\S+)(?: \[\S+\])? is good", line)
            if m:
                good.append(m.group(1))
                continue
            # "best available" is not a failure. It is MAME saying the set is as complete as
            # anyone's copy can be -- the only roms absent were never dumped -- and the game
            # runs. Filing those with the genuinely broken sets understated this library by
            # 787 playable games.
            m = re.match(r"romset (\S+)(?: \[\S+\])? is best available", line)
            if m:
                best.append(m.group(1))
                continue
            m = re.match(r"romset (\S+)(?: \[\S+\])? is bad", line)
            if m:
                bad.setdefault(m.group(1), line.strip())
    good, best = sorted(set(good)), sorted(set(best))
    return {"checked": len(setnames), "good": good, "best_available": best, "bad": bad,
            "good_count": len(good), "best_count": len(best), "bad_count": len(bad),
            "runnable": sorted(set(good) | set(best))}


def record_proof(result, db=None):
    """Write MAME's verdict into the catalog, replacing our own arithmetic.

    `assemblable()` is a calculation over the dat; this is the emulator's answer. They
    disagreed on 230 of 6,041 sets — sets whose roms all appeared present by hash but which
    MAME will not run — and where they disagree the emulator is right, because it is the
    thing that has to load the game.
    """
    db = db or connect()
    runnable = set(result.get("runnable") or [])
    bad = set((result.get("bad") or {}).keys())
    if not runnable and not bad:
        return {"marked_playable": 0, "marked_unplayable": 0}
    # The acquire watcher writes continuously, and busy_timeout alone does not always
    # outlast a long sweep of its own. Retry rather than lose a verdict that took 13 minutes
    # of MAME to establish.
    import sqlite3
    import time as _time
    for attempt in range(6):
        try:
            _apply(db, runnable, bad)
            break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == 5:
                raise
            _time.sleep(3 * (attempt + 1))
    return {"marked_playable": len(runnable), "marked_unplayable": len(bad)}


def _apply(db, runnable, bad):
    with db:
        for names, value in ((runnable, 1), (bad, 0)):
            names = sorted(names)
            for i in range(0, len(names), 800):
                chunk = names[i:i + 800]
                q = ",".join("?" * len(chunk))
                db.execute(
                    f"""UPDATE items SET playable=?, updated_at=CURRENT_TIMESTAMP
                        WHERE system='arcade' AND COALESCE(is_device,0)=0
                          AND substr(external_id, instr(external_id,'/')+1) IN ({q})""",
                    (value, *chunk))
