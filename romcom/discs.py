"""Disc games kept once, as CHD.

A Dreamcast game arrived as a 900 MB .7z of a .gdi and its tracks; the card export
converted it to a CHD (esde.make_chd) and kept that in the CHD cache on F: — and the
.7z stayed on H: as well. 581 such archives were 264 GB of H: holding games already
converted. Here the CHD becomes the game's library file, named for the game
(`<chd library>/<system folder>/<Game (Disc 1)>.chd`, on the cache's drive), and the
archive it came from is deleted — after `chdman verify` has read the CHD back whole.

The library's records follow: the CHD gets a files row matched to the game (match
'converted'), the archive's rows go. An export then copies the .chd as it is (esde.plan
treats a .chd as already in its card form), so nothing is converted twice.

`compact(convert=True)` also converts discs no export has converted yet, at most `limit`
per run: the upkeep step works through a large library a batch a day.
"""
import shutil
import subprocess
from pathlib import Path

from .config import settings
from .db import connect


def library_dir():
    """Where a disc's CHD lives as its game file: setting chd_library_dir, else
    'Games CHD' beside the CHD cache (F:\\Games CHD when the cache is F:\\romcom-chd-cache)."""
    from . import esde
    d = (settings().get("chd_library_dir") or "").strip().strip('"')
    if d:
        return Path(d)
    cache = esde.chd_cache_dir()
    return cache.parent / "Games CHD" if cache else None


def verify(chd):
    """chdman reads the CHD back and checks its hashes; True when it is whole."""
    from . import esde
    exe = esde.chdman()
    if not exe:
        return False
    try:
        p = subprocess.run([exe, "verify", "-i", str(chd)], capture_output=True, text=True,
                           timeout=3600, stdin=subprocess.DEVNULL, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def _games(db, systems=None):
    """{item_id: (system, [(path, bytes)])} for disc games whose files are not CHDs yet."""
    from . import esde
    out = {}
    for r in db.execute("""SELECT f.path, f.bytes, i.id, lower(i.system) s FROM files f
                           JOIN items i ON i.id=f.matched_item_id
                           WHERE f.path NOT LIKE '%|%' AND COALESCE(f.content,1)=1"""):
        if esde.folder(r["s"]) not in esde.CHD_SYSTEMS or (systems and r["s"] not in systems):
            continue
        if not Path(r["path"]).exists():
            continue
        out.setdefault(r["id"], (r["s"], []))[1].append((r["path"], r["bytes"] or 0))
    return out


# What 7-Zip says when an archive's own checksums fail: the download is broken, and no
# second attempt at converting it will do better.
import re as _re
_CORRUPT = _re.compile(r"Data Error|CRC Failed|Unexpected end of archive|Headers Error", _re.I)


def _let_go_corrupt(db, iid, src, why):
    """A corrupt archive goes, and its game is fetched again (from another source: the
    acquirer skips an address it has already downloaded from)."""
    try:
        Path(src).unlink()
    except OSError:
        return
    with db:
        db.execute("DELETE FROM files WHERE path=? OR path LIKE ?", (src, f"{src}|%"))
        db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",
                   (iid, "corrupt-download", f"{why[-200:]}: {src}"[:400]))
        if not db.execute("SELECT 1 FROM files WHERE matched_item_id=?", (iid,)).fetchone():
            db.execute("UPDATE items SET status='MISSING', wanted=1, authorized=1, "
                       "updated_at=CURRENT_TIMESTAMP WHERE id=?", (iid,))


_UNSAFE = _re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _stem(title):
    """A game title as a Windows-safe file stem."""
    t = _UNSAFE.sub(lambda m: " -" if m.group() == ":" else "_", title or "").strip(" .")
    return t[:150].rstrip(" .") or "game"


def _owner(db, path):
    row = db.execute("SELECT matched_item_id FROM files WHERE path=?", (str(path),)).fetchone()
    return row[0] if row else None


def _target(db, folder_dir, iid, stem):
    """The library CHD for one disc of one game: `<stem>.chd`, never a file another game
    owns. The name comes from the catalog, not from inside the archive: archives often
    hold a bare `disc.gdi`, and naming CHDs by that once sent 25 Dreamcast games to one
    `disc.chd`, each overwriting the last. A name already on disk counts as this game's
    only when the library says so; anything else (another game's, or a file nothing
    claims) gets the item id beside the title instead."""
    first = folder_dir / f"{stem}.chd"
    if not first.exists() or _owner(db, first) == iid:
        return first
    tag = iid.rsplit("-", 1)[-1][:12]
    for n in range(100):
        cand = folder_dir / (f"{stem} [{tag}].chd" if n == 0 else f"{stem} [{tag}-{n}].chd")
        if not cand.exists() or _owner(db, cand) == iid:
            return cand
    raise RuntimeError(f"no free CHD name for {stem}")


def compact(apply=False, convert=False, limit=None, systems=None, progress=None,
            stop=None, db=None):
    """Make each disc game's CHD its library file and delete the archive it came from.

    Only archives are let go (a .7z/.zip/.rar holding one disc): a loose .cue and its
    tracks are left as they are. A disc whose CHD is not in the cache is converted only
    with `convert`, at most `limit` conversions. Nothing is deleted unless the CHD
    verifies. Returns {"games", "chds", "converted", "freed", "skipped": {name: why},
    "planned_bytes"} — with apply=False, what it would do."""
    from . import esde
    db = db or connect()
    lib = library_dir()
    out = {"games": 0, "chds": 0, "converted": 0, "freed": 0, "planned_bytes": 0,
           "skipped": {}, "applied": bool(apply)}
    if lib is None or not esde.chdman():
        out["error"] = "no CHD cache folder or no chdman: nothing to compact"
        return out
    work = _games(db, systems)
    done = 0
    for n, (iid, (system, files)) in enumerate(sorted(work.items())):
        if stop is not None and stop.is_set():
            break
        steps, _, _ = esde.plan(system, files, chd=True)
        chd_steps = [st for st in steps if st[0] == "chd" and st[2] is not None]
        if not chd_steps:
            continue
        out["games"] += 1
        title = (db.execute("SELECT title FROM items WHERE id=?", (iid,)).fetchone() or [None])[0]
        base = _stem(title or Path(chd_steps[0][3]).stem)
        # One CHD per disc. A game with several (one archive per disc) names each by its
        # own disc file, or by its archive when those inner names are the same.
        stems = [base] if len(chd_steps) == 1 else [f"{base} - {_stem(Path(st[3]).stem)}" for st in chd_steps]
        if len(set(stems)) < len(stems):
            stems = [f"{base} - {_stem(Path(st[1]).stem)}" for st in chd_steps]
        for (_, src, names, main, rel, _est), stem in zip(chd_steps, stems):
            if stop is not None and stop.is_set():
                break
            target = _target(db, lib / esde.folder(system), iid, stem)
            rel = target.name
            cached = esde.chd_cached(src, main, system)
            if not target.exists() and not cached.exists():
                if not convert or (limit is not None and out["converted"] >= limit):
                    continue
                if not apply:
                    out["converted"] += 1
                    out["planned_bytes"] += Path(src).stat().st_size
                    continue
                if progress:
                    progress(n, len(work), f"converting {main} to CHD…")
                try:
                    esde._convert_chd(src, names, main, cached, system)
                except Exception as e:
                    if _CORRUPT.search(str(e)) and Path(src).suffix.lower() in esde.UNPACK:
                        _let_go_corrupt(db, iid, src, str(e))
                        out.setdefault("corrupt", []).append(Path(src).name)
                    else:
                        out["skipped"][Path(src).name] = f"could not convert: {e}"[:200]
                    continue
                out["converted"] += 1
            size = Path(src).stat().st_size
            if not apply:
                out["chds"] += 1
                out["planned_bytes"] += size
                continue
            if not target.exists():
                if progress:
                    progress(n, len(work), f"checking {rel}…")
                if not verify(cached):
                    out["skipped"][Path(src).name] = "the CHD did not verify: archive kept"
                    try:
                        cached.unlink()        # a bad CHD is converted afresh next time
                    except OSError:
                        pass
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(cached), str(target))
            elif not verify(target):
                out["skipped"][Path(src).name] = "the library CHD did not verify: archive kept"
                continue
            st = target.stat()
            with db:
                db.execute("""INSERT INTO files(path,bytes,mtime,matched_item_id,match_method,
                              content,scanned_at) VALUES(?,?,?,?,'converted',1,CURRENT_TIMESTAMP)
                              ON CONFLICT(path) DO UPDATE SET matched_item_id=excluded.matched_item_id,
                              match_method='converted'""", (str(target), st.st_size, st.st_mtime, iid))
            try:
                Path(src).unlink()
            except OSError as e:
                out["skipped"][Path(src).name] = f"could not delete: {e}"[:200]
                continue
            with db:
                db.execute("DELETE FROM files WHERE path=? OR path LIKE ?", (src, f"{src}|%"))
                db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",
                           (iid, "compacted", f"{src} -> {target}"[:400]))
            out["chds"] += 1
            out["freed"] += size
            done += 1
    return out
