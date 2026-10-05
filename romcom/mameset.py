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

Two shapes, two consumers: a directory per set is a PC MAME rompath (what `play` stages
into, because MAME assembles it itself), but no emulator on an Android handheld scans
loose chip folders — every one of them (MAME4droid, FBNeo, RetroArch's MAME/FBNeo cores,
the frontends) reads one zip per game, so the card export builds `dest/<set>.zip`.
"""
import os
import re
import zipfile
from pathlib import Path

from .config import ROOT
from .db import connect


# Each emulator core speaks its own romset version: current MAME, FBNeo, MAME 2003-Plus.
# A zip that satisfies one core can fail on another — chip versions and set names drift —
# so each profile has its own dat folder under DAT/. `mame` filters the folder for the
# file whose name says "arcade" (the full MAME dat ships beside it); the others take the
# first dat in their folder, because each project publishes exactly one.
PROFILES = {"mame": ("MAME", "arcade"),
            "fbneo": ("FBNeo", None),
            "mame2003": ("MAME2003-Plus", None)}


def dat_path(profile="mame"):
    """The romset dat for an emulator core profile, or None when it is not installed.
    `mame` is the current-MAME arcade dat (the file whose name says 'arcade'); the others
    take the first dat in their own folder, because each project ships exactly one."""
    spec = PROFILES.get(profile)
    if not spec:
        return None
    d = ROOT / "DAT" / spec[0]
    if not d.exists():
        return None
    if spec[1]:
        return next((p for p in sorted(d.glob("*.dat")) if spec[1] in p.name.lower()), None)
    return next(iter(sorted(list(d.glob("*.dat")) + list(d.glob("*.xml")))), None)


def set_roms(setnames, path=None):
    """{setname: [{name, crc, sha1}]} for the machines asked for, plus their device sets.

    Device refs are followed one level, which is what MAME itself needs: galaga cannot boot
    without namco54's roms, and the dat is where that dependency is written down. The
    FBNeo and MAME 2003-Plus dats carry everything inline instead — every set is
    self-contained, bios chips included — so there is nothing to follow there.
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


def romof_gaps(path):
    """Bios/device sets the dat's games depend on via romof= but never define — the
    cores look for them as their own zips (neogeo.zip, pgm.zip), so a romset build must
    supply them or the games referencing them will not boot. {setname: games needing it}."""
    names, refs = set(), {}
    for line in Path(path).open(encoding="utf-8", errors="replace"):
        m = re.search(r'<(?:game|machine)\s+name="([^"]+)"', line)
        if m:
            names.add(m.group(1))
            r = re.search(r'\bromof="([^"]+)"', line)
            if r:
                refs[r.group(1)] = refs.get(r.group(1), 0) + 1
    return {t: n for t, n in refs.items() if t not in names}


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


def build(setnames, dest, db=None, dry_run=False, stop=None, room_for=None, progress=None,
          fresh=False, zipped=False, profile="mame", dat=None):
    """Write <dest>/<set>/<canonical rom name> for each set — or <dest>/<set>.zip when
    `zipped`, the shape every handheld emulator and frontend scans. Returns a per-set report.

    The romset is `profile`'s (mame / fbneo / mame2003 — each core its own dat), or an
    explicit `dat` path. `stop` (a threading.Event) is checked between sets; the report
    then says `stopped`. `room_for(nbytes)` is asked before each set is written; when it
    says no, the build stops before writing any of that set and the report says
    `out_of_room`. `bytes_needed` is what a real run would write (targets missing or a
    different size) — what a free-space check wants; `bytes` is the size of everything
    found, whether already present or not. `fresh=True` counts every target as missing:
    the sizing pass for a wipe run, whose folder is about to be cleared, must measure
    the full rebuild — not the delta over the old sets it is about to delete. A zip is
    rewritten whole or not at all (temp file + replace), so a card never holds a
    half-written set. `progress(i, total, setname)` fires per set, so the export's job
    status says where the (long, otherwise silent) build is instead of sitting on
    "starting…"."""
    db = db or connect()
    roms = set_roms(setnames, dat or dat_path(profile))
    if not roms:
        where = f"DAT/{PROFILES.get(profile, (profile,))[0]}/" if profile in PROFILES else str(dat)
        return {"sets": {}, "error": f"no romset dat found for {profile!r} — put its dat "
                                     f"under {where}", "copied": 0,
                "bytes_needed": 0}
    by_crc, by_sha = _index(db, roms)
    dest = Path(dest)
    report, copied, total_bytes, needed_bytes = {}, 0, 0, 0
    stopped = out_of_room = False
    import shutil
    for i, (name, chips) in enumerate(sorted(roms.items())):
        if progress: progress(i, len(roms), name)
        if stop is not None and stop.is_set():
            stopped = True
            break
        # A machine can list one chip name several times — an alternate or bad dump with a
        # different hash but the same filename. The zip must hold one entry per name or the
        # core may load the wrong chip, and the first alternate the dump actually has wins.
        by_name = {}
        for chip in chips:
            by_name.setdefault(chip["name"], []).append(chip)
        found, missing = [], []
        for chip_name, entries in by_name.items():
            src = next((s for s in (by_sha.get(e["sha1"]) or by_crc.get(e["crc"]) for e in entries)
                       if s and Path(s).exists()), None)
            if src:
                found.append((src, chip_name))
            else:
                missing.append(chip_name)
        folder = dest / name
        sized = [(src, canonical, Path(src).stat().st_size) for src, canonical in found]
        for _, _, size in sized:
            total_bytes += size
        if zipped:
            # A zip is all-or-nothing: it is complete or it is rebuilt whole, so a card
            # can never hold a half-written set. Entries are STORED — rom chips are
            # high-entropy, deflate would only burn CPU on the card.
            target = dest / f"{name}.zip"
            have = {}
            if not fresh and target.exists():
                try:
                    with zipfile.ZipFile(target) as z:
                        have = {i.filename: i.file_size for i in z.infolist()}
                except (OSError, zipfile.BadZipFile):
                    have = {}            # unreadable: rebuild rather than trust it
            todo = [(src, canonical, size) for src, canonical, size in sized
                    if have.get(canonical) != size]
            set_need = sum(size for _, _, size in sized) if todo else 0
        else:
            todo = []
            for src, canonical, size in sized:
                t = folder / canonical
                if fresh or not (t.exists() and t.stat().st_size == size):
                    todo.append((src, t, size))
            set_need = sum(size for _, _, size in todo)
        needed_bytes += set_need
        if not dry_run and todo:
            if room_for is not None and not room_for(set_need):
                stopped = out_of_room = True
                break
            if zipped:
                dest.mkdir(parents=True, exist_ok=True)
                partial = target.with_suffix(".zip.partial")
                with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED) as z:
                    for src, canonical, _ in sized:
                        z.write(src, canonical)
                os.replace(partial, target)
                copied += len(sized)
            else:
                folder.mkdir(parents=True, exist_ok=True)
                for src, target, _ in todo:
                    shutil.copy2(src, target)
                    copied += 1
        report[name] = {"roms": len(chips), "found": len(found), "missing": missing[:8],
                        "complete": not missing}
    return {"sets": report, "copied": copied, "bytes": total_bytes,
            "bytes_needed": needed_bytes,
            "complete": sum(1 for v in report.values() if v["complete"]),
            "incomplete": sum(1 for v in report.values() if not v["complete"]),
            "stopped": stopped, "out_of_room": out_of_room}


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
