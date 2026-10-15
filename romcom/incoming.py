"""The incoming folder: drop games in, RomCom takes them from there.

Downloads that arrive by hand — a Google Drive folder exported as numbered zips, a
`psp.rar` of 45 ISOs, a loose `.sfc` — land in one folder (`incoming_dir`, by default
`<download dir> Incoming`, e.g. `H:\\Games Incoming`). The scanner reads loose files and
zip members it can hash, but it does not open a .rar, and a 5 GB disc image inside a zip
is never unpacked for an emulator. So each archive here is looked into once:

- members that are games for some system (a rom extension, a disc image or its tracks, a
  nested rom archive) are unpacked into `<download dir>/incoming/<archive name>/`;
- cover art, videos, manuals and frontend metadata are left behind, and so are folders
  for consoles the handheld cannot run (Xbox);
- the archive is remembered (name, size, time) in `.romcom-incoming.json` beside it, so
  it is never unpacked twice; nothing in the incoming folder is deleted.

Then the unpacked folders and the incoming folder itself are scanned, which matches what
the catalogs know and adopts the rest under their own names (a PSP disc by its PARAM.SFO).
"""
import json
import re
from pathlib import Path

from .config import settings

ARCHIVES = (".zip", ".7z", ".rar")
STATE = ".romcom-incoming.json"
# Frontend furniture that rides along with game folders.
_NOT_GAMES = re.compile(r"(^|[\\/])(images|videos|manuals|media|snaps?|box(?:art|3d)?|"
                        r"covers|marquees?|screenshots|fanart|wheel|titles)([\\/]|$)", re.I)
_JUNK_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".mp4", ".avi", ".mkv",
              ".pdf", ".txt", ".nfo", ".xml", ".ini", ".url", ".htm", ".html", ".db", ".dat",
              ".diz", ".srt", ".cfg"}
# Consoles no handheld emulator runs well; their discs stay in the archive.
_SKIP_FOLDERS = re.compile(r"(^|[\\/])(xbox|xbox ?360|x360)([\\/]|$)", re.I)
_TRACKS = {".bin", ".raw", ".img", ".sub", ".iso", ".cue", ".gdi", ".cdi", ".chd", ".cso",
           ".pbp", ".wbfs", ".rvz", ".wia", ".gcz", ".nkit", ".mdf", ".mds", ".ccd"}


def incoming_dir():
    """The incoming folder, or None when there is none."""
    s = settings()
    d = (s.get("incoming_dir") or "").strip().strip('"')
    if d:
        return Path(d)
    base = (s.get("download_dir") or "").strip().strip('"')
    if not base:
        return None
    p = Path(f"{base.rstrip(chr(92) + '/')} Incoming")
    return p if p.exists() else None


def _key(p):
    st = p.stat()
    return f"{p.name}|{st.st_size}|{int(st.st_mtime)}"


def _load(folder):
    try:
        return json.loads((folder / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(folder, state):
    tmp = folder / (STATE + ".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(folder / STATE)


def game_members(names):
    """The members of an archive worth unpacking: games, not the frontend around them."""
    from .scanner import EXT_SYSTEM, ARCADE_SET_EXTS
    out = []
    for n in names:
        if _NOT_GAMES.search(n) or _SKIP_FOLDERS.search(n):
            continue
        ext = Path(n).suffix.lower()
        if ext in _JUNK_EXTS:
            continue
        if ext in _TRACKS or ext in EXT_SYSTEM or ext in ARCHIVES or ext in ARCADE_SET_EXTS:
            out.append(n)
    return out


def pending(folder=None):
    """Archives in the incoming folder not yet looked into."""
    folder = folder or incoming_dir()
    if not folder or not folder.exists():
        return []
    done = _load(folder)
    return [p for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() in ARCHIVES and _key(p) not in done]


def _loose(folder):
    return {f"{p.name}|{p.stat().st_size}" for p in folder.iterdir()
            if p.name != STATE and not p.name.startswith(".")
            and (p.is_dir() or p.suffix.lower() not in ARCHIVES)}


def loose_changed(folder=None):
    """Whether files or folders other than archives have landed since the last run."""
    folder = folder or incoming_dir()
    if not folder or not folder.exists():
        return False
    return _loose(folder) != set(_load(folder).get("_loose") or [])


ROOM_SPARE = 2 * 1024 ** 3      # left free on the drive after an archive is unpacked


def unpack(folder=None, progress=None, stop=None, only=None):
    """Unpack the games from every new archive in the incoming folder (or just `only`).
    An archive whose games would not fit with ROOM_SPARE left over is not started: it is
    reported and tried again once space is back, rather than filling the drive halfway
    through. Returns {"archives": n, "unpacked": [folders], "files": n,
    "skipped": {archive: why}}."""
    import shutil
    from . import esde
    folder = folder or incoming_dir()
    out = {"archives": 0, "unpacked": [], "files": 0, "skipped": {}, "dir": str(folder or "")}
    if not folder or not folder.exists():
        return out
    base = (settings().get("download_dir") or "").strip().strip('"')
    if not base:
        return out
    state = _load(folder)
    todo = only if only is not None else pending(folder)
    for i, p in enumerate(todo):
        if stop is not None and stop.is_set():
            break
        if progress:
            progress(i, len(todo), f"incoming: looking into {p.name}")
        out["archives"] += 1
        try:
            listing = esde.members(p)
            names = [n for n, _ in listing]
        except Exception as e:
            out["skipped"][p.name] = f"unreadable: {e}"[:200]
            continue                      # not remembered: tried again next time
        games = game_members(names)
        # Games left in the archive on purpose (an Xbox folder): the archive is kept for
        # the owner to decide about, never deleted with them inside.
        held = [n for n in names if _SKIP_FOLDERS.search(n)
                and Path(n).suffix.lower() in _TRACKS]
        if not games:
            out["skipped"][p.name] = ("only games RomCom leaves out (Xbox) — kept" if held
                                      else "no games inside (artwork, videos or manuals) — kept")
            state[_key(p)] = {"games": 0, "held": len(held)}
            _save(folder, state)
            continue
        target = Path(base) / "incoming" / re.sub(r'[<>:"/\\|?*]', "_", p.stem)
        sizes = dict(listing)
        need = sum(sizes.get(n, 0) for n in games)
        try:
            free = shutil.disk_usage(Path(base)).free
        except OSError:
            free = None
        if free is not None and free < need + ROOM_SPARE:
            out["skipped"][p.name] = (f"not enough room yet: needs {need / 1e9:.1f} GB, "
                                      f"{free / 1e9:.1f} GB free — tried again later")
            continue                      # not remembered: tried again next time
        try:
            if progress:
                progress(i, len(todo), f"incoming: unpacking {len(games)} game file(s) "
                                       f"from {p.name}")
            esde.extract(p, games, target)
        except Exception as e:
            out["skipped"][p.name] = f"could not unpack: {e}"[:200]
            continue
        state[_key(p)] = {"games": len(games), "to": str(target), "held": len(held)}
        _save(folder, state)
        out["unpacked"].append(str(target))
        out["files"] += len(games)
    return out


def remove_enabled():
    return str(settings().get("incoming_remove", "true")).strip().lower() not in (
        "0", "false", "no", "off")


def _forget(db, path):
    """Drop the files rows of a path gone from disk (the file and its archive members)."""
    db.execute("DELETE FROM files WHERE path=? OR path LIKE ?", (str(path), f"{path}|%"))


def _repath(db, old, new):
    """A moved file keeps its rows, matches and hashes: only the path changes."""
    db.execute("UPDATE files SET path=? WHERE path=?", (str(new), str(old)))
    db.execute("UPDATE files SET path=? || substr(path, ?) WHERE path LIKE ?",
               (str(new), len(str(old)) + 1, f"{old}|%"))


def tidy(folder, base, scanned_ok, db=None):
    """With incoming_remove on (the default): no game kept twice. An archive whose games
    were unpacked and scanned is deleted; a loose file the scan matched or adopted moves
    into <download dir>/incoming/loose/ (a rename on the same drive). Kept for the owner:
    archives holding games RomCom leaves out (Xbox) or nothing importable, and loose files
    nothing claimed. A game the library already held — the same bytes somewhere outside
    the incoming folders — is not kept a second time: the unpacked or loose copy is
    deleted at once instead of waiting for the daily duplicate sweep.
    Returns {"deleted": [...], "moved": [...], "already_had": n, "bytes": n}."""
    import shutil
    from .db import connect
    out = {"deleted": [], "moved": [], "already_had": 0, "bytes": 0}
    if not remove_enabled() or not folder or not folder.exists():
        return out
    db = db or connect()
    state = _load(folder)
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() not in ARCHIVES:
            continue
        st = state.get(_key(p)) or {}
        if not st.get("to") or st.get("held") or st["to"] not in scanned_ok:
            continue
        size = p.stat().st_size
        try:
            p.unlink()
        except OSError:
            continue
        with db:
            _forget(db, p)
        out["deleted"].append(p.name)
        out["bytes"] += size
    # Copies of games already in the library, from this import's unpacked folders and the
    # incoming folder itself: same sha1 as a file outside both. Deleted, rows forgotten.
    roots = [str(Path(base) / "incoming"), str(folder)]
    for r in db.execute(
            "SELECT path, sha1, bytes FROM files WHERE sha1 IS NOT NULL AND path NOT LIKE '%|%' "
            "AND (path LIKE ? OR path LIKE ?)", (roots[0] + "%", roots[1] + "%")).fetchall():
        if not any(r["path"].startswith(t) for t in scanned_ok):
            continue
        twin = db.execute(
            "SELECT 1 FROM files WHERE sha1=? AND path<>? AND path NOT LIKE ? AND path NOT LIKE ? "
            "LIMIT 1", (r["sha1"], r["path"], roots[0] + "%", roots[1] + "%")).fetchone()
        if not twin:
            continue
        try:
            Path(r["path"]).unlink()
        except OSError:
            continue
        with db:
            _forget(db, r["path"])
        out["already_had"] += 1
        out["bytes"] += r["bytes"] or 0
    loose = Path(base) / "incoming" / "loose"
    if str(folder) not in scanned_ok:
        return out
    claimed = {r[0] for r in db.execute(
        "SELECT path FROM files WHERE path LIKE ? AND matched_item_id IS NOT NULL",
        (f"{folder}%",))}
    for p in sorted(folder.iterdir()):
        if p.name == STATE or p.name.startswith(".") or (
                p.is_file() and p.suffix.lower() in ARCHIVES):
            continue
        mine = ({str(p)} if p.is_file() else
                {str(f) for f in p.rglob("*") if f.is_file()})
        if not mine or not all(any(c == m or c.startswith(m + "|") for c in claimed)
                               for m in mine):
            continue                          # something in it nothing claimed: kept
        target = loose / p.name
        if target.exists():
            continue
        loose.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(p), str(target))
        except OSError:
            continue
        with db:
            for m in mine:
                _repath(db, m, target / Path(m).relative_to(p) if p.is_dir() else target)
        out["moved"].append(p.name)
    return out


def run(progress=None, stop=None):
    """Import the incoming folder one archive at a time, smallest first: unpack it, scan
    what came out, and (incoming_remove) delete the archive and any copy of a game the
    library already held before starting the next — so a drive with little room left
    still gets through a large pile, the space each archive frees paying for the next.
    Then the loose files: scanned, and moved into the library when claimed (`tidy`).
    Returns the unpack report plus the scans' counts and what was tidied."""
    from .scanner import scan
    folder = incoming_dir()
    r = {"archives": 0, "unpacked": [], "files": 0, "skipped": {}, "dir": str(folder or "")}
    if not folder or not folder.exists():
        return r | {"note": "no incoming folder (set incoming_dir, or create "
                            "'<download dir> Incoming')"}
    base = (settings().get("download_dir") or "").strip().strip('"')
    scans = {"matched": 0, "adopted": 0, "files": 0}
    tidied = {"deleted": [], "moved": [], "already_had": 0, "bytes": 0}
    state = _load(folder)
    # Folders unpacked by an earlier run were scanned then: their archives may go too.
    scanned_ok = {v["to"] for v in state.values()
                  if isinstance(v, dict) and v.get("to") and Path(v["to"]).exists()}

    def _scan(path):
        try:
            s = scan(path, name_match=True, adopt=True,
                     progress=None if progress is None else
                     (lambda i, t, n, st=None: progress(i, t, f"incoming: scanning {n}")))
        except Exception as e:
            r["skipped"][path] = f"scan failed: {e}"[:200]
            return False
        for k in scans:
            scans[k] += s.get(k) or 0
        return True

    def _tidy():
        if not base or (stop is not None and stop.is_set()):
            return
        t = tidy(folder, base, scanned_ok)
        tidied["deleted"] += t["deleted"]
        tidied["moved"] += t["moved"]
        tidied["already_had"] += t["already_had"]
        tidied["bytes"] += t["bytes"]

    _tidy()                                   # what earlier runs imported, first
    todo = sorted(pending(folder), key=lambda p: p.stat().st_size)
    for i, p in enumerate(todo):
        if stop is not None and stop.is_set():
            break
        if progress:
            progress(i, len(todo), f"incoming: {p.name} ({i + 1} of {len(todo)})")
        one = unpack(folder, progress=None, stop=stop, only=[p])
        r["archives"] += one["archives"]
        r["files"] += one["files"]
        r["skipped"].update(one["skipped"])
        for target in one["unpacked"]:
            r["unpacked"].append(target)
            if _scan(target):
                scanned_ok.add(target)
        _tidy()
    try:
        state = _load(folder)
        state["_loose"] = sorted(_loose(folder))
        _save(folder, state)
    except OSError:
        pass
    if not (stop is not None and stop.is_set()) and _scan(str(folder)):
        scanned_ok.add(str(folder))
        _tidy()
    return r | {"scan": scans, "tidied": tidied}
