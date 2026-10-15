"""Downloads that can never apply to the handheld, found and removed.

68 GB of H: was files nothing in the library claimed: PC games fetched for a DS title
of the same name (Call of Duty Black Ops, 11 GB), Xbox 360 games-on-demand packages,
DVD images of PC and console games that the wrong-download check had already let go.
None can run on the Odin 2, and none is a game the library is waiting for.

What counts, judged by what is inside and never by the name alone:

- `pc`: an archive with an installer or Windows program (.exe, .dll, .msi, .cab) and
  no console disc or rom beside it — a Dreamcast .cdi carrying a boot tool is a game;
- `x360`: an Xbox 360 games-on-demand package (0000[47]000 folders, DataNNNN parts);
- `dvd`: a disc image no console stamp names (not PlayStation, PSP, Wii, GameCube,
  PS3, original Xbox) that RomCom itself had already rejected as a wrong download.

Only top-level files of the download folder that no game claims are considered, and an
original Xbox disc is never removed (the owner keeps them).
"""
import re
from pathlib import Path

from .config import settings
from .db import connect

_PROGRAMS = {".exe", ".dll", ".msi", ".cab", ".inf"}
_GAME_INSIDE = {".gdi", ".cdi", ".cue", ".chd", ".iso", ".bin", ".img", ".nds", ".gba",
                ".gb", ".gbc", ".nes", ".sfc", ".smc", ".n64", ".z64", ".v64", ".md",
                ".sms", ".gg", ".32x", ".pbp", ".cso", ".wbfs", ".rvz", ".gcm", ".3ds",
                ".cia", ".adf", ".d64", ".st", ".pce", ".a26", ".lnx", ".ngp", ".ws",
                ".wsc", ".zip", ".7z"}
_X360 = re.compile(r"(^|[\\/])0000[47]000([\\/]|$)|(^|[\\/])data\d{4}$", re.I)
_XBOX_MAGIC = b"MICROSOFT*XBOX*MEDIA"


def _xbox_disc(path):
    """An original Xbox disc image (XISO): kept, the owner wants them."""
    try:
        with open(path, "rb") as f:
            f.seek(0x10000)
            return f.read(len(_XBOX_MAGIC)) == _XBOX_MAGIC
    except OSError:
        return False


def classify(path, rejected=False):
    """'pc', 'x360', 'dvd' or None (not bloat, or can't tell)."""
    from . import sniff
    p = Path(path)
    if p.suffix.lower() == ".iso" and _xbox_disc(p):
        return None
    ev = sniff.evidence(p)
    if not ev:
        return None
    if ev[0] == "names":
        names = ev[1]
        if any(_X360.search(n) for n in names):
            return "x360"
        exts = {Path(n.lower()).suffix for n in names}
        if exts & _PROGRAMS and not exts & _GAME_INSIDE:
            return "pc"
        return None
    # A disc image: ev = ('iso', systems its stamp names).
    if ev[1]:
        return None
    return "dvd" if rejected else None


def sweep(apply=False, db=None, progress=None, stop=None):
    """Find (and with `apply`, delete) bloat among the download folder's unclaimed
    top-level files. Returns {"files": [(kind, path, bytes)], "bytes": n, "applied"}."""
    db = db or connect()
    base = (settings().get("download_dir") or "").strip().strip('"')
    out = {"files": [], "bytes": 0, "applied": bool(apply)}
    if not base or not Path(base).exists():
        return out
    root = Path(base)
    rejected = set()
    for (d,) in db.execute("SELECT detail FROM events WHERE event IN "
                           "('wrong-download','stub-disc')"):
        for m in re.finditer(r"([A-Za-z]:\\[^;]+?)(?=; [A-Za-z]:\\|$)", d or ""):
            rejected.add(m.group(1).strip())
    claimed = {r[0].split("|", 1)[0] for r in db.execute(
        "SELECT path FROM files WHERE matched_item_id IS NOT NULL AND path LIKE ?",
        (f"{root}%",))}
    todo = [p for p in root.iterdir() if p.is_file() and str(p) not in claimed]
    for i, p in enumerate(sorted(todo)):
        if stop is not None and stop.is_set():
            break
        if progress and i % 100 == 0:
            progress(i, len(todo), f"bloat: looking at {p.name}")
        kind = classify(p, rejected=str(p) in rejected)
        if not kind:
            continue
        size = p.stat().st_size
        if apply:
            try:
                p.unlink()
            except OSError:
                continue
            with db:
                db.execute("DELETE FROM files WHERE path=? OR path LIKE ?", (str(p), f"{p}|%"))
                db.execute("INSERT INTO events(item_id,event,detail) VALUES(NULL,'bloat-removed',?)",
                           (f"{kind}: {p}"[:400],))
        out["files"].append((kind, str(p), size))
        out["bytes"] += size
    return out
