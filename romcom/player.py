"""Launch a game in an emulator so it can be auditioned, and record the verdict.

Why launching is a CLI operation and not a web button
-----------------------------------------------------
The web app runs as the `RomCom` Windows service under LocalSystem, in session 0. Session 0
is isolated from the desktop: a process started there can open a window, but nobody will
ever see it. So the server cannot start an emulator for you, and pretending otherwise would
produce an invisible process holding a ROM open.

The split that follows from that: `command_for()` resolves what should run and is safe to
call from anywhere including the service, while `launch()` actually spawns and is meant for
`romcom play`, run by you, in your own session. The web UI uses the first to show you the
exact command; marking keep/skip works from either, because that is only a database write.
"""
import os
import re
import shlex
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .config import load_yaml
from .db import connect

PLAY_STATUSES = ("UNPLAYED", "PLAYED", "KEEP", "SKIP")


def emulators():
    """system -> command template, from emulators.yaml. `{rom}` and `{set}` are substituted."""
    return (load_yaml("emulators.yaml") or {}).get("emulators") or {}


def rompath():
    """Where built MAME sets live, from emulators.yaml. None if not configured."""
    v = (load_yaml("emulators.yaml") or {}).get("rompath")
    return str(v).strip() or None if v else None


def ensure_arcade_set(setname, db=None):
    """Make sure a MAME set exists in the rompath, building it from the flat dump if not.

    Without this, Play only worked for sets someone had already exported by hand, and the
    failure was silent in the worst way: MAME printed `NOT FOUND (tried in progolf)` into a
    console nobody sees, exited 0, and the UI reported success. Clicking a game should just
    play it, so the set is built on demand — the roms are already on disk and the dat says
    which ones, so there is nothing to ask the owner about.
    """
    root = rompath()
    if not root:
        return None
    target = Path(root) / setname
    if target.is_dir() and any(target.iterdir()):
        return {"set": setname, "built": False, "path": str(target)}
    from . import mameset
    r = mameset.build([setname], root, db=db)
    info = (r.get("sets") or {}).get(setname) or {}
    if not info.get("complete", False):
        missing = ", ".join(info.get("missing") or []) or r.get("error") or "unknown"
        raise LookupError(
            f"{setname!r} cannot be assembled from what is on disk — missing: {missing}")
    return {"set": setname, "built": True, "path": str(target), "report": r}


def _item(db, ident):
    """An item by id, or by an unambiguous title match — typing a full id is miserable."""
    row = db.execute("SELECT * FROM items WHERE id=?", (ident,)).fetchone()
    if row:
        return row
    rows = db.execute("SELECT * FROM items WHERE title LIKE ? ORDER BY title LIMIT 6",
                      (f"%{ident}%",)).fetchall()
    if not rows:
        raise LookupError(f"no item matches {ident!r}")
    if len(rows) > 1:
        names = "\n  ".join(f"{r['id']}  {r['title']}" for r in rows)
        raise LookupError(f"{ident!r} matches several items — be more specific:\n  {names}")
    return rows[0]


def rom_for(db, item):
    """The biggest content file matched to the item.

    Biggest, not first: a set can carry a manual or a cue alongside the ROM, and the payload
    is reliably the largest of them. Returns None when nothing is on disk.
    """
    rows = db.execute(
        "SELECT path, bytes FROM files WHERE matched_item_id=? AND COALESCE(content,1)=1"
        " ORDER BY COALESCE(bytes,0) DESC", (item["id"],)).fetchall()
    for r in rows:
        if Path(r["path"]).exists():
            return r["path"]
    return None


def command_for(ident, db=None):
    """Resolve the command that would launch `ident`. Never spawns anything."""
    db = db or connect()
    item = _item(db, ident)
    system = (item["system"] or "").lower()
    template = emulators().get(system)
    if not template:
        raise LookupError(
            f"no emulator configured for {system!r} — add it to emulators.yaml, e.g.\n"
            f"  emulators:\n    {system}: 'C:/RetroArch/retroarch.exe -L core.dll \"{{rom}}\"'")
    # MAME is the odd one out and the reason {set} exists: it takes a machine name and finds
    # the roms itself via its rompath, so handing it a file path launches nothing. The
    # catalog already stores that name -- external_id is "arcade/<setname>".
    setname = (item["external_id"] or "").split("/", 1)[-1] or item["title"]
    rom = rom_for(db, item)
    # Only insist on a file when the template actually wants one. A rompath-driven emulator
    # needs no path from us, and refusing to launch it for lack of one would be nonsense.
    if "{rom}" in template and not rom:
        raise LookupError(f"{item['title']!r} has no file on disk to launch")
    # A configured emulator whose binary or libretro core is not actually installed fails
    # far away from here: RetroArch opens, finds no core, and closes again, which reaches the
    # owner as "it didn't work". Name the missing file instead, while there is still context.
    # Split on quotes and spaces rather than pattern-matching paths: a template mixes
    # quoted and bare arguments, and a regex over Windows paths is all backslash escaping
    # for no benefit.
    for token in template.replace(chr(34), " ").replace(chr(39), " ").split():
        # Absolute paths only. A bare `mame.exe` or `retroarch` is resolved through PATH by
        # the shell, and we cannot tell from here whether it will be found — guessing would
        # refuse to launch emulators that work perfectly well.
        if not token.lower().endswith((".exe", ".dll")):
            continue
        if not (token[1:3] in (":/", ":" + chr(92)) or token.startswith("/")):
            continue
        if Path(token).exists():
            continue
            continue
        what = "libretro core" if token.lower().endswith(".dll") else "emulator"
        extra = (" Download it in RetroArch: Online Updater -> Core Downloader."
                 if token.lower().endswith(".dll") else "")
        raise LookupError(f"{system}: the {what} is not installed — {token} does not "
                          f"exist.{extra}")
    command = template.replace("{set}", setname)
    if rom:
        command = command.replace("{rom}", rom)
    return {"item": item["id"], "title": item["title"], "system": system,
            "set": setname, "rom": rom, "command": command}


def launch(ident, db=None, record=True):
    """Start the emulator and return once it has been spawned.

    Detached on purpose: the emulator outlives this process, so `romcom play` returns
    immediately instead of blocking a terminal for the length of a play session.
    """
    db = db or connect()
    plan = command_for(ident, db=db)
    args = plan["command"] if os.name == "nt" else shlex.split(plan["command"])
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, shell=(os.name == "nt"), close_fds=True, creationflags=flags,
                     cwd=_workdir(plan["command"]))
    if record:
        with db:
            db.execute("UPDATE items SET play_status=CASE WHEN play_status='UNPLAYED'"
                       " THEN 'PLAYED' ELSE play_status END, last_played=?, "
                       "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (datetime.now().isoformat(timespec="seconds"), plan["item"]))
            db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,'played',?)",
                       (plan["item"], plan["command"][:400]))
    return plan


def set_keep(ident, on=True, db=None):
    """Mark an item for export, or unmark it. This is the marker `organize --keep-only` uses."""
    db = db or connect()
    item = _item(db, ident)
    with db:
        db.execute("UPDATE items SET keep=?, play_status=?, updated_at=CURRENT_TIMESTAMP"
                   " WHERE id=?", (1 if on else 0, "KEEP" if on else "SKIP", item["id"]))
        db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",
                   (item["id"], "keep" if on else "unkeep", item["title"][:200]))
    return {"item": item["id"], "title": item["title"], "keep": bool(on)}


def kept(system=None, db=None):
    """What is currently marked for export, with its on-disk size."""
    db = db or connect()
    sql = ("SELECT i.id, i.title, i.system, SUM(COALESCE(f.bytes,0)) bytes, COUNT(f.path) files"
           " FROM items i LEFT JOIN files f ON f.matched_item_id=i.id WHERE i.keep=1")
    params = []
    if system:
        sql += " AND i.system=?"
        params.append(system)
    sql += " GROUP BY i.id ORDER BY i.system, i.title"
    rows = [dict(r) for r in db.execute(sql, params)]
    return {"items": rows, "count": len(rows),
            "bytes": sum(r["bytes"] or 0 for r in rows),
            "gb": round(sum(r["bytes"] or 0 for r in rows) / (1024 ** 3), 2)}


# --------------------------------------------------------------- the session-0 handoff
#
# Everything below exists because of one Windows fact: a service runs in session 0 whatever
# account it uses, and session 0 cannot put a window on the desktop. The web app therefore
# queues a resolved command and `romcom agent`, running in the owner's session, executes it.

AGENT_HEARTBEAT_KEY = "launch_agent_beat"
# How long an emulator must survive before we believe it started something.
EARLY_EXIT_SECS = 4.0
AGENT_STALE_SECS = 30


def _workdir(command):
    """Where to run an emulator from: its own directory.

    Emulators write beside themselves — MAME drops cfg/, nvram/ and diff/ into the working
    directory — and inheriting the agent's cwd meant those landed in the repository. Running
    from the binary's own folder is also what the emulators expect for their relative paths.
    """
    for token in command.replace(chr(34), " ").replace(chr(39), " ").split():
        if token.lower().endswith(".exe") and Path(token).exists():
            return str(Path(token).parent)
    return None


def request_launch(ident, db=None):
    """Queue a launch. Resolves the command up front so a bad request fails in the UI, where
    someone is looking, rather than silently in the agent."""
    db = db or connect()
    plan = command_for(ident, db=db)
    # Arcade needs its roms staged before the emulator is told to look for them.
    if plan["system"] == "arcade":
        plan["staged"] = ensure_arcade_set(plan["set"], db=db)
    with db:
        cur = db.execute(
            "INSERT INTO launch_requests(item_id,title,system,command) VALUES(?,?,?,?)",
            (plan["item"], plan["title"], plan["system"], plan["command"]))
    return plan | {"request": cur.lastrowid, "status": "PENDING"}


def launch_request(rid, db=None):
    row = (db or connect()).execute("SELECT * FROM launch_requests WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def _beat(db):
    with db:
        db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)"
                   " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                   " updated_at=CURRENT_TIMESTAMP",
                   (AGENT_HEARTBEAT_KEY, datetime.now().isoformat(timespec="seconds")))


def agent_status(db=None):
    """Whether an agent is alive, so the UI can say 'start the agent' instead of queuing
    launches into a void."""
    db = db or connect()
    row = db.execute("SELECT value FROM app_settings WHERE key=?", (AGENT_HEARTBEAT_KEY,)).fetchone()
    if not row:
        return {"running": False, "last_beat": None, "age_secs": None}
    try:
        age = (datetime.now() - datetime.fromisoformat(row["value"])).total_seconds()
    except ValueError:
        return {"running": False, "last_beat": row["value"], "age_secs": None}
    return {"running": age < AGENT_STALE_SECS, "last_beat": row["value"], "age_secs": round(age, 1)}


def agent_once(db=None):
    """Execute every pending request. Returns how many were started.

    Claims each row before spawning, so two agents cannot launch the same game twice and a
    crash mid-launch leaves the row as RUNNING rather than replaying it forever.
    """
    db = db or connect()
    _beat(db)
    started = 0
    while True:
        row = db.execute("SELECT * FROM launch_requests WHERE status='PENDING'"
                         " ORDER BY id LIMIT 1").fetchone()
        if not row:
            return started
        with db:
            claimed = db.execute(
                "UPDATE launch_requests SET status='RUNNING', started_at=CURRENT_TIMESTAMP"
                " WHERE id=? AND status='PENDING'", (row["id"],)).rowcount
        if not claimed:
            continue
        try:
            args = row["command"] if os.name == "nt" else shlex.split(row["command"])
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            # Output goes to a file, not a pipe: a pipe on a long-lived GUI process fills its
            # buffer and wedges the emulator. A file costs nothing and is there when needed.
            log = Path(tempfile.gettempdir()) / f"romcom-launch-{row['id']}.log"
            fh = open(log, "wb")
            proc = subprocess.Popen(args, shell=(os.name == "nt"), close_fds=True,
                                    creationflags=flags, stdout=fh, stderr=subprocess.STDOUT,
                                    cwd=_workdir(row["command"]))
            # A spawn that succeeds proves nothing: MAME with a missing romset prints
            # "NOT FOUND", exits 0, and vanishes — which reported as RUNNING with no error
            # while the owner saw nothing happen at all. An emulator that quits this fast did
            # not start a game, whatever it returned.
            rc = None
            for _ in range(int(EARLY_EXIT_SECS * 10)):
                rc = proc.poll()
                if rc is not None:
                    break
                time.sleep(0.1)
            fh.close()
            if rc is not None:
                tail = ""
                try:
                    tail = log.read_text(errors="replace").strip().splitlines()
                    tail = " | ".join(tail[-4:])
                except OSError:
                    pass
                with db:
                    db.execute("UPDATE launch_requests SET status='FAILED', error=? WHERE id=?",
                               (f"exited after {EARLY_EXIT_SECS}s or less (code {rc}). "
                                f"{tail}"[:500], row["id"]))
                continue
            started += 1
            with db:
                db.execute("UPDATE items SET play_status=CASE WHEN play_status='UNPLAYED'"
                           " THEN 'PLAYED' ELSE play_status END, last_played=?,"
                           " updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (datetime.now().isoformat(timespec="seconds"), row["item_id"]))
                db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,'played',?)",
                           (row["item_id"], row["command"][:400]))
        except Exception as e:
            with db:
                db.execute("UPDATE launch_requests SET status='FAILED', error=? WHERE id=?",
                           (f"{type(e).__name__}: {e}"[:500], row["id"]))


def agent_loop(interval=1.0, on_event=None):
    """Poll for queued launches until interrupted. This is `romcom agent`."""
    import time
    db = connect()
    if on_event:
        on_event("ready", {"interval": interval})
    while True:
        try:
            n = agent_once(db)
            if n and on_event:
                on_event("launched", {"count": n})
        except Exception as e:
            if on_event:
                on_event("error", {"message": f"{type(e).__name__}: {e}"})
        time.sleep(interval)
