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
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from .config import load_yaml
from .db import connect

PLAY_STATUSES = ("UNPLAYED", "PLAYED", "KEEP", "SKIP")


def emulators():
    """system -> command template, from emulators.yaml. `{rom}` is substituted."""
    return (load_yaml("emulators.yaml") or {}).get("emulators") or {}


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
    rom = rom_for(db, item)
    if not rom:
        raise LookupError(f"{item['title']!r} has no file on disk to launch")
    return {"item": item["id"], "title": item["title"], "system": system,
            "rom": rom, "command": template.replace("{rom}", rom)}


def launch(ident, db=None, record=True):
    """Start the emulator and return once it has been spawned.

    Detached on purpose: the emulator outlives this process, so `romcom play` returns
    immediately instead of blocking a terminal for the length of a play session.
    """
    db = db or connect()
    plan = command_for(ident, db=db)
    args = plan["command"] if os.name == "nt" else shlex.split(plan["command"])
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, shell=(os.name == "nt"), close_fds=True, creationflags=flags)
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
AGENT_STALE_SECS = 30


def request_launch(ident, db=None):
    """Queue a launch. Resolves the command up front so a bad request fails in the UI, where
    someone is looking, rather than silently in the agent."""
    db = db or connect()
    plan = command_for(ident, db=db)
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
            subprocess.Popen(args, shell=(os.name == "nt"), close_fds=True, creationflags=flags)
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
