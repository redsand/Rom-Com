"""Make the catalog's claims match the evidence, without losing anything.

An archive that overstates what it holds is worse than a smaller honest one: every count,
every missing list and every export decision is built on these statuses, and a VERIFIED item
with nothing behind it quietly corrupts all of them. 2,580 items claimed to be in hand while
owning no file at all, and 3.5% of recorded file paths no longer existed on disk.

The rule here is archival, not tidying: **no catalog entry is ever deleted**. An item whose
file has gone is not lost knowledge — it is a game we know about and do not currently hold,
which is exactly what CATALOGED means. Only the file *records* are pruned, and only when the
file is genuinely absent, because a record of a file that is not there is the lie.

Evidence, strongest first:

  VERIFIED   a file exists AND its hash matches one the catalog holds for this item.
             This is proof of correct content, not merely of a file with the right name.
  FOUND      a file exists and is associated, but nothing proves it is the right dump.
  CATALOGED  nothing on disk. Known, not held.

Arcade is judged differently and deliberately so: a MAME set is assembled on demand from
chips scattered through a flat dump, so ownership is "can this be built", which `playable`
already answers.
"""
from .db import connect

IN_HAND = ("FOUND", "DOWNLOADED", "VERIFIED", "NORMALIZED", "INSTALLED", "TESTED")
# Deliberate states that evidence must not override. EXCLUDED is the owner's decision;
# FAILED and MANUAL record history that a status sweep has no business rewriting.
UNTOUCHABLE = ("EXCLUDED", "FAILED", "MANUAL")


def _missing_paths(db, progress=None):
    """Recorded files that are no longer on disk."""
    import os
    gone, n = [], 0
    rows = db.execute("SELECT path FROM files").fetchall()
    for (path,) in rows:
        n += 1
        if progress and n % 50000 == 0:
            progress(n, len(rows))
        if not os.path.exists(path):
            gone.append(path)
    return gone


def evidence(db=None):
    """Per item: what the files actually prove. Returns {item_id: 'VERIFIED'|'FOUND'}."""
    db = db or connect()
    # A hash match means the bytes on disk are the dump the catalog describes.
    proven = {r["id"] for r in db.execute("""
        SELECT DISTINCT i.id FROM items i
        JOIN files f ON f.matched_item_id = i.id
        JOIN file_hashes h ON h.item_id = i.id
        WHERE (h.algorithm='sha1' AND lower(f.sha1)  = lower(h.digest))
           OR (h.algorithm='md5'  AND lower(f.md5)   = lower(h.digest))
           OR (h.algorithm='crc'  AND lower(f.crc32) = lower(h.digest))""")}
    held = {r["id"] for r in db.execute("""
        SELECT DISTINCT i.id FROM items i JOIN files f ON f.matched_item_id = i.id""")}
    return {i: ("VERIFIED" if i in proven else "FOUND") for i in held}


def audit(fix=False, db=None, progress=None):
    """Compare every claim with the evidence. Reports; only changes anything when `fix`."""
    db = db or connect()
    gone = _missing_paths(db, progress=progress)
    if fix and gone:
        with db:
            for i in range(0, len(gone), 800):
                chunk = gone[i:i + 800]
                q = ",".join("?" * len(chunk))
                # The record goes; the catalog entry it pointed at does not.
                db.execute(f"DELETE FROM files WHERE path IN ({q})", chunk)

    ev = evidence(db)
    rows = db.execute("""SELECT id, system, status, COALESCE(playable,0) playable
                         FROM items WHERE status NOT IN (?,?,?)""", UNTOUCHABLE).fetchall()
    changes = []
    for r in rows:
        if (r["system"] or "").lower() == "arcade":
            # Three outcomes, because a partial set is neither playable nor nothing.
            # Saying CATALOGED for a machine whose chips are half present would throw
            # away the fact that we hold some of it, which an archive should not do.
            want = ("VERIFIED" if r["playable"]
                    else ("FOUND" if r["id"] in ev else "CATALOGED"))
        else:
            want = ev.get(r["id"], "CATALOGED")
        if want != r["status"]:
            changes.append({"id": r["id"], "from": r["status"], "to": want,
                            "system": r["system"]})
    if fix and changes:
        with db:
            for i in range(0, len(changes), 500):
                for ch in changes[i:i + 500]:
                    db.execute("UPDATE items SET status=?, updated_at=CURRENT_TIMESTAMP"
                               " WHERE id=?", (ch["to"], ch["id"]))
                    db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",
                               (ch["id"], "audit",
                                f"{ch['from']} -> {ch['to']} (status now matches the evidence)"))

    demotions = [c for c in changes if c["from"] in IN_HAND and c["to"] not in IN_HAND]
    promotions = [c for c in changes if c["to"] in IN_HAND and c["from"] not in IN_HAND]
    by_system = {}
    for c in changes:
        by_system[c["system"] or "?"] = by_system.get(c["system"] or "?", 0) + 1
    return {"applied": bool(fix),
            "stale_file_records": len(gone),
            "status_changes": len(changes),
            "overclaimed": len(demotions),
            "understated": len(promotions),
            "by_system": dict(sorted(by_system.items(), key=lambda kv: -kv[1])[:10]),
            "examples": changes[:10],
            "items_deleted": 0}
