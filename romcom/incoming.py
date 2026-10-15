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


def unpack(folder=None, progress=None, stop=None):
    """Unpack the games from every new archive in the incoming folder. Returns
    {"archives": n, "unpacked": [folders], "files": n, "skipped": {archive: why}}."""
    from . import esde
    folder = folder or incoming_dir()
    out = {"archives": 0, "unpacked": [], "files": 0, "skipped": {}, "dir": str(folder or "")}
    if not folder or not folder.exists():
        return out
    base = (settings().get("download_dir") or "").strip().strip('"')
    if not base:
        return out
    state = _load(folder)
    todo = pending(folder)
    for i, p in enumerate(todo):
        if stop is not None and stop.is_set():
            break
        if progress:
            progress(i, len(todo), f"incoming: looking into {p.name}")
        out["archives"] += 1
        try:
            names = [n for n, _ in esde.members(p)]
        except Exception as e:
            out["skipped"][p.name] = f"unreadable: {e}"[:200]
            continue                      # not remembered: tried again next time
        games = game_members(names)
        if not games:
            out["skipped"][p.name] = "no games inside (artwork, videos or manuals)"
            state[_key(p)] = {"games": 0}
            _save(folder, state)
            continue
        target = Path(base) / "incoming" / re.sub(r'[<>:"/\\|?*]', "_", p.stem)
        try:
            if progress:
                progress(i, len(todo), f"incoming: unpacking {len(games)} game file(s) "
                                       f"from {p.name}")
            esde.extract(p, games, target)
        except Exception as e:
            out["skipped"][p.name] = f"could not unpack: {e}"[:200]
            continue
        state[_key(p)] = {"games": len(games), "to": str(target)}
        _save(folder, state)
        out["unpacked"].append(str(target))
        out["files"] += len(games)
    return out


def run(progress=None, stop=None):
    """Unpack what is new, then scan the unpacked folders and the incoming folder itself
    (loose files dropped there). Returns the unpack report plus the scans' counts."""
    from .scanner import scan
    folder = incoming_dir()
    r = unpack(folder, progress=progress, stop=stop)
    if not folder:
        return r | {"note": "no incoming folder (set incoming_dir, or create "
                            "'<download dir> Incoming')"}
    scans = {"matched": 0, "adopted": 0, "files": 0}
    try:
        state = _load(folder)
        state["_loose"] = sorted(_loose(folder))
        _save(folder, state)
    except OSError:
        pass
    for path in [*r["unpacked"], str(folder)]:
        if stop is not None and stop.is_set():
            break
        try:
            s = scan(path, name_match=True, adopt=True,
                     progress=None if progress is None else
                     (lambda i, t, n, st=None: progress(i, t, f"incoming: scanning {n}")))
        except Exception as e:
            r["skipped"][path] = f"scan failed: {e}"[:200]
            continue
        for k in scans:
            scans[k] += s.get(k) or 0
    return r | {"scan": scans}
