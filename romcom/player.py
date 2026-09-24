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
