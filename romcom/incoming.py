"""The incoming folder: drop games in, RomCom takes them from there.

Downloads that arrive by hand — a Google Drive folder exported as numbered zips, a
`psp.rar` of 45 ISOs, a loose `.sfc` — land in one folder (`incoming_dir`, by default
`<download dir> Incoming`, e.g. `H:\\Games Incoming`). The scanner reads loose files and
zip members it can hash, but it does not open a .rar, and a 5 GB disc image inside a zip
is never unpacked for an emulator. So each archive here is looked into once:

- members that are games for some system (a rom extension, a disc image or its tracks, a
  nested rom archive) are unpacked into `<download dir>/incoming/<archive name>/`;
- cover art, videos, manuals and frontend metadata are left behind, and so are folders
  for consoles nothing here runs (Xbox 360);
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
# Consoles nothing here runs; their discs stay in the archive. (The original Xbox was on
# this list until xemu; its discs are games like any other now.)
_SKIP_FOLDERS = re.compile(r"(^|[\\/])(xbox ?360|x360)([\\/]|$)", re.I)
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


ANDROID = (".apk", ".xapk", ".apks")     # installed, not emulated: the Android shelf's
# An Xbox disc says so itself: the XDVDFS header, at the start of a bare game partition
# (.xiso) or behind the video partition of a full dump (XGD1, XGD2, XGD3).
_XBOX_MAGIC = b"MICROSOFT*XBOX*MEDIA"
_XBOX_AT = (0x10000, 0x18310000, 0xFDA0000, 0x2090000)
XBOX_WHY = ("an unpacked Xbox game (default.xbe), not a disc image — with extract-xiso "
            "beside the Xbox emulator RomCom makes it one; kept for you")
HELD_WHY = "only Xbox 360 games, which nothing here runs — kept for you"
# An archive with no games was once always called 'artwork'; a damaged 7z and an Xbox
# game folder were among them. Entries older than this are looked into once more.
_LOOKED = 3


def is_xbox_disc(p):
    """Whether a disc image is an original-Xbox disc, by its own header."""
    if Path(p).suffix.lower() != ".iso":
        return False
    try:
        with open(p, "rb") as f:
            for off in _XBOX_AT:
                f.seek(off)
                if f.read(len(_XBOX_MAGIC)) == _XBOX_MAGIC:
                    return True
    except OSError:
        pass
    return False


def _held_members(names):
    """Games an archive holds that are left in it: Xbox 360 discs, and an original-Xbox
    game unpacked to its files (default.xbe) rather than kept as a disc image."""
    return [n for n in names
            if (_SKIP_FOLDERS.search(n) and Path(n).suffix.lower() in _TRACKS)
            or Path(n).name.lower() == "default.xbe"]


def extract_xiso():
    """extract-xiso.exe: on PATH, or in `extract-xiso/` beside the Xbox emulator's folder."""
    import shutil
    c = shutil.which("extract-xiso")
    if c:
        return c
    try:
        from .player import emulator_options
        for _name, template in emulator_options("xbox"):
            for token in template.replace('"', " ").split():
                if token.lower().endswith("xemu.exe"):
                    emus = Path(token).parent.parent
                    for cand in (Path(token).with_name("extract-xiso.exe"),
                                 emus / "extract-xiso" / "extract-xiso.exe",
                                 emus / "extract-xiso" / "artifacts" / "extract-xiso.exe"):
                        if cand.exists():
                            return str(cand)
    except Exception:
        pass
    return None


def repack_xbe(archive, names, target):
    """An Xbox game kept as its files (default.xbe and the rest) is no disc, and the
    emulator boots discs: each game folder in the archive is made an XISO in `target`.
    Returns the images made; [] when extract-xiso or 7-Zip is not to be found."""
    import shutil
    import subprocess
    from . import esde
    tool, seven = extract_xiso(), esde.seven_zip()
    if not tool or not seven:
        return []
    target = Path(target)
    tmp = target.with_name(target.name + ".xbe-tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    made = []
    try:
        p = subprocess.run([seven, "x", "-y", f"-o{tmp}", str(archive)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=7200, errors="replace")
        if p.returncode != 0:
            raise OSError(f"7-Zip could not unpack {Path(archive).name}: "
                          f"{(p.stderr or p.stdout or '').strip()[-200:]}")
        target.mkdir(parents=True, exist_ok=True)
        for n in names:
            if Path(n).name.lower() != "default.xbe":
                continue
            root = (tmp / n).parent
            name = root.name if root != tmp else Path(archive).stem
            iso = target / (re.sub(r'[<>:"/\\|?*]', "_", name) + ".iso")
            q = subprocess.run([tool, "-c", str(root), str(iso)], cwd=str(target),
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=7200, errors="replace")
            if q.returncode != 0 or not is_xbox_disc(iso):
                iso.unlink(missing_ok=True)
                raise OSError(f"extract-xiso could not make {iso.name}: "
                              f"{(q.stderr or q.stdout or '').strip()[-200:]}")
            made.append(iso)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return made


def pending(folder=None):
    """Archives in the incoming folder not yet looked into."""
    folder = folder or incoming_dir()
    if not folder or not folder.exists():
        return []
    done = _load(folder)

    def looked(p):
        st = done.get(_key(p))
        return isinstance(st, dict) and (st.get("games") or st.get("to")
                                         or st.get("v", 1) >= _LOOKED)
    return [p for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() in ARCHIVES and not looked(p)]


def _loose(folder):
    """What lies in the incoming folder besides archives, as a set that changes when
    anything lands: a file by name and size, a folder by how many files it holds and
    their size — so a game dropped into a folder already there is noticed too. (A
    folder's own st_size says nothing about its contents, and on Windows it is not even
    the same number from one call to the next.)"""
    out = set()
    for p in folder.iterdir():
        if p.name == STATE or p.name.startswith("."):
            continue
        try:
            if p.is_dir():
                n = size = newest = 0
                for f in p.rglob("*"):
                    if f.is_file():
                        st = f.stat()
                        n += 1
                        size += st.st_size
                        newest = max(newest, int(st.st_mtime))
                out.add(f"{p.name}|{n}|{size}|{newest}")
            elif p.suffix.lower() not in ARCHIVES:
                # The time as well as the size: Explorer gives a file its full size the
                # moment a copy starts and its real time only when the copy ends, so a
                # file first seen half-copied is looked at again once it is whole.
                st = p.stat()
                out.add(f"{p.name}|{st.st_size}|{int(st.st_mtime)}")
        except OSError:
            continue
    return out


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
            bad = None if names else ("could not be opened: damaged, empty, or an "
                                      "incomplete download — replace it and it is tried again")
        except Exception as e:
            names, bad = [], f"unreadable: {e}"[:200]
        if bad:
            # Remembered under this size and time, so the folder is not re-read every five
            # minutes for ever; a file still being copied changes both and is tried again.
            out["skipped"][p.name] = bad
            state[_key(p)] = {"games": 0, "error": bad, "v": _LOOKED}
            _save(folder, state)
            continue
        games = game_members(names)
        # Games left in the archive on purpose (Xbox): the archive is kept for the owner
        # to decide about, never deleted with them inside.
        held = _held_members(names)
        xbe = any(Path(n).name.lower() == "default.xbe" for n in names)
        if xbe and not games:
            target = Path(base) / "incoming" / re.sub(r'[<>:"/\\|?*]', "_", p.stem)
            try:
                free = shutil.disk_usage(Path(base)).free
            except OSError:
                free = None
            need = 2 * sum(s for _, s in listing)      # the files, then the image of them
            if free is not None and free < need + ROOM_SPARE:
                out["skipped"][p.name] = (f"not enough room yet: needs {need / 1e9:.1f} GB, "
                                          f"{free / 1e9:.1f} GB free — tried again later")
                continue
            if progress:
                progress(i, len(todo), f"incoming: making a disc image of {p.name}")
            try:
                made = repack_xbe(p, names, target)
            except Exception as e:
                bad = f"could not be made a disc image: {e}"[:200]
                out["skipped"][p.name] = bad
                state[_key(p)] = {"games": 0, "error": bad, "v": _LOOKED}
                _save(folder, state)
                continue
            if made:
                state[_key(p)] = {"games": len(made), "to": str(target), "held": 0,
                                  "v": _LOOKED}
                _save(folder, state)
                out["unpacked"].append(str(target))
                out["files"] += len(made)
                continue
        if not games:
            out["skipped"][p.name] = (XBOX_WHY if xbe else HELD_WHY if held
                                      else "no games inside (artwork, videos or manuals) — kept")
            state[_key(p)] = {"games": 0, "held": len(held), "xbe": xbe, "v": _LOOKED}
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
        state[_key(p)] = {"games": len(games), "to": str(target), "held": len(held),
                          "v": _LOOKED}
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
    # A copy in the incoming folder itself goes when the same bytes are anywhere else
    # (the unpacked import counts: a hand-made 'nes/' beside nes.rar's import is the same
    # 828 games); an unpacked copy goes only when the library held it before the import.
    roots = [str(Path(base) / "incoming"), str(folder)]
    for r in db.execute(
            "SELECT path, sha1, bytes FROM files WHERE sha1 IS NOT NULL AND path NOT LIKE '%|%' "
            "AND (path LIKE ? OR path LIKE ?)", (roots[0] + "%", roots[1] + "%")).fetchall():
        if not any(r["path"].startswith(t) for t in scanned_ok):
            continue
        in_drop = r["path"].startswith(roots[1])
        twin = db.execute(
            "SELECT 1 FROM files WHERE sha1=? AND path<>? AND path NOT LIKE ? "
            + ("" if in_drop else "AND path NOT LIKE ? ") + "LIMIT 1",
            (r["sha1"], r["path"], roots[1] + "%") + (() if in_drop else (roots[0] + "%",))
        ).fetchone()
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
        def is_claimed(m):
            return any(c == m or c.startswith(m + "|") for c in claimed)
        if p.is_dir():
            # A folder of games and cover art: each claimed game moves on its own; the
            # art, and anything nothing claimed, stays for the owner.
            for f in sorted(x for x in p.rglob("*") if x.is_file()):
                if not is_claimed(str(f)):
                    continue
                target = loose / p.name / f.relative_to(p)
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(f), str(target))
                except OSError:
                    continue
                with db:
                    _repath(db, str(f), target)
                out["moved"].append(f"{p.name}/{f.relative_to(p).as_posix()}")
            continue
        mine = {str(p)}
        if not is_claimed(str(p)):
            continue                          # nothing claimed it: kept
        target = loose / p.name
        if target.exists():
            continue
        loose.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(p), str(target))
        except OSError:
            continue
        with db:
            _repath(db, str(p), target)
        out["moved"].append(p.name)
    return out


def android(folder, db=None):
    """Android packages dropped in the incoming folder are installed, not emulated: no
    catalog will ever claim them, so each readable one moves to the Android shelf
    (android_games_dir) and the shelf is read. A package that does not open (still being
    copied, or damaged) stays. Returns {"moved": [names], "kept": {name: why}, "games": n}."""
    import shutil
    from . import apk
    from .db import connect
    out = {"moved": [], "kept": {}, "games": None}
    found = [p for p in sorted(folder.rglob("*"))
             if p.is_file() and p.suffix.lower() in ANDROID]
    if not found:
        return out
    shelf = (settings().get("android_games_dir") or "").strip().strip('"')
    why = (None if shelf else "no Android games folder is set (android_games_dir)")
    if not why and not remove_enabled():
        why = "incoming_remove is off, so it is not moved to the Android shelf"
    if not why:
        try:
            Path(shelf).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            why = f"the Android shelf {shelf} cannot be made: {e}"[:200]
    if why:
        out["kept"] = {p.name: why for p in found}
        return out
    db = db or connect()
    for p in found:
        try:
            info = apk.read(p)
        except Exception as e:
            out["kept"][p.name] = f"not a readable Android package: {e}"[:200]
            continue
        target = Path(shelf) / p.name
        try:
            if target.exists():
                try:
                    there = apk.read(target)
                except Exception:
                    there = {}
                if target.stat().st_size != p.stat().st_size or any(
                        there.get(k) != info.get(k) for k in ("package", "version_code")):
                    out["kept"][p.name] = ("another file of this name is already on the "
                                           "Android shelf")
                    continue
                p.unlink()                    # the shelf holds this very package already
            else:
                shutil.move(str(p), str(target))
        except OSError as e:
            out["kept"][p.name] = f"could not be moved: {e}"[:200]
            continue
        with db:
            _forget(db, p)
        out["moved"].append(p.name)
    if out["moved"]:
        try:
            s = apk.scan(shelf, db=db)
            out["games"] = s["games"]
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"[:200]
    return out


def forget_vanished(folder, db=None):
    """Files taken out of the incoming folder by hand leave their rows behind, and a row
    is what says a game is in hand: 15 games were FOUND or VERIFIED on the strength of
    files deleted weeks before. Rows whose file is gone are dropped, and a catalog game
    left with no file at all goes back to MISSING (an adopted one is swept by the scan).
    Returns how many rows went."""
    import os
    from .db import connect
    db = db or connect()
    rows = db.execute("SELECT path, matched_item_id FROM files WHERE path LIKE ?",
                      (str(folder) + os.sep + "%",)).fetchall()
    gone = [r for r in rows if not Path(r["path"].split("|")[0]).exists()]
    if not gone:
        return 0
    with db:
        for r in gone:
            db.execute("DELETE FROM files WHERE path=?", (r["path"],))
        for item in {r["matched_item_id"] for r in gone if r["matched_item_id"]}:
            db.execute(
                "UPDATE items SET status='MISSING', updated_at=CURRENT_TIMESTAMP WHERE id=? "
                "AND status IN ('FOUND','VERIFIED') AND catalog_source<>'local' "
                "AND NOT EXISTS (SELECT 1 FROM files f WHERE f.matched_item_id=items.id)",
                (item,))
    return len(gone)


def leftovers(folder=None, db=None):
    """What is still in the incoming folder, and why each thing is: the answer to 'I
    dropped it in and nothing happened'. [{"name", "bytes", "why"}], largest first."""
    from .db import connect
    from .scanner import EXT_SYSTEM
    folder = folder or incoming_dir()
    if not folder or not folder.exists():
        return []
    state = _load(folder)
    db = db or connect()
    claimed = {r[0].split("|")[0] for r in db.execute(
        "SELECT path FROM files WHERE path LIKE ? AND matched_item_id IS NOT NULL",
        (f"{folder}%",))}
    remove = remove_enabled()

    def archive(p):
        st = state.get(_key(p))
        if not isinstance(st, dict) or not (st.get("games") or st.get("to")
                                            or st.get("v", 1) >= _LOOKED):
            return "waiting to be imported"
        if st.get("error"):
            return st["error"]
        if st.get("to"):
            return ("imported — kept because it also holds games RomCom leaves out" if st.get("held")
                    else "imported — leaves on the next run" if remove
                    else "imported — kept because incoming_remove is off")
        return (XBOX_WHY if st.get("xbe") else HELD_WHY if st.get("held")
                else "no games inside (artwork, videos or manuals)")

    def loose(p):
        ext = p.suffix.lower()
        if ext in ANDROID:
            return "Android package — moves to the Android shelf on the next run"
        if is_xbox_disc(p):
            return "Xbox disc — joins the library on the next run"
        if str(p) in claimed:
            return ("matched to a game — moves into the library on the next run" if remove
                    else "matched to a game — kept here because incoming_remove is off")
        if ext in _JUNK_EXTS or _NOT_GAMES.search(str(p.relative_to(folder))):
            return "artwork or frontend metadata, not a game"
        if ext in EXT_SYSTEM or ext in _TRACKS:
            return "no catalog knows this file and its name matches no game"
        if ext in ARCHIVES:
            return ("an archive inside a folder is not unpacked — move it to the top of "
                    "the incoming folder")
        return f"not a kind of file RomCom knows ({ext or 'no extension'})"

    out = []
    for p in folder.iterdir():
        if p.name == STATE or p.name.startswith("."):
            continue
        try:
            if p.is_file():
                why = archive(p) if p.suffix.lower() in ARCHIVES else loose(p)
                out.append({"name": p.name, "bytes": p.stat().st_size, "why": why})
                continue
            whys, size = {}, 0
            for f in p.rglob("*"):
                if f.is_file():
                    size += f.stat().st_size
                    w = loose(f)
                    whys[w] = whys.get(w, 0) + 1
            out.append({"name": p.name + "/", "bytes": size,
                        "why": "; ".join(f"{n} file(s): {w}" for w, n in
                                         sorted(whys.items(), key=lambda x: -x[1]))
                               or "empty folder"})
        except OSError:
            continue
    return sorted(out, key=lambda x: -x["bytes"])


def run(progress=None, stop=None):
    """Import the incoming folder one archive at a time, smallest first: unpack it, scan
    what came out, and (incoming_remove) delete the archive and any copy of a game the
    library already held before starting the next — so a drive with little room left
    still gets through a large pile, the space each archive frees paying for the next.
    Then the loose files: Android packages move to their own shelf (`android`), the rest
    are scanned and moved into the library when claimed (`tidy`).
    Returns the unpack report plus the scans' counts, what was tidied, and what is still
    in the folder and why (`left`)."""
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
    stopped = stop is not None and stop.is_set()
    droid = None
    if not stopped:
        for what, step in (("android", lambda: android(folder)),
                           ("vanished", lambda: forget_vanished(folder))):
            try:
                got = step()
                if what == "android":
                    droid = got
            except Exception as e:
                r["skipped"][what] = f"{type(e).__name__}: {e}"[:200]
    before = set()
    try:
        before = _loose(folder)
        state = _load(folder)
        state["_loose"] = sorted(before)
        _save(folder, state)
    except OSError:
        pass
    if not stopped and _scan(str(folder)):
        scanned_ok.add(str(folder))
        _tidy()
        # A claimed game has just moved out, and the next look would take its absence for
        # news and run an idle second import. What was here before the scan and still is
        # is what was seen; anything that landed during the scan is not, and still counts.
        try:
            state = _load(folder)
            state["_loose"] = sorted(before & _loose(folder))
            _save(folder, state)
        except OSError:
            pass
    try:
        left = leftovers(folder)
    except Exception:
        left = []
    return r | {"scan": scans, "tidied": tidied, "android": droid, "left": left}
