"""Actions shared by the CLI and the web UI: queueing downloads and syncing SABnzbd state."""
from datetime import datetime
from .db import connect
from . import sab

def queue_result(db, kind, e, result):
    """Send a search result to SABnzbd and record the job. Returns the nzo id (or None)."""
    resp = sab.add_url(result["url"], f"ROMCOM__{e['id']}", priority=1 if kind == "volume" else 0)
    ids = resp.get("nzo_ids", [])
    nzo = ids[0] if ids else None
    table = "items" if kind == "item" else "volumes"
    with db:
        db.execute(f"UPDATE {table} SET status='QUEUED' WHERE id=?", (e["id"],))
        db.execute("""INSERT INTO jobs(entity_type,entity_id,nzo_id,result_title,result_url,bytes,status,queued_at)
          VALUES(?,?,?,?,?,?,'QUEUED',?)""",
          (kind, e["id"], nzo, result["title"], result["url"], result.get("size") or 0,
           datetime.now().isoformat(timespec="seconds")))
    return nzo

def sync(db=None):
    """Pull queue/history state from SABnzbd into jobs and entity statuses."""
    if db is None:
        db = connect()
    hist = {x.get("nzo_id"): x for x in sab.history() if x.get("nzo_id")}
    que = {x.get("nzo_id"): x for x in sab.queue() if x.get("nzo_id")}
    updated = 0
    with db:
        jobs = db.execute("SELECT * FROM jobs WHERE status NOT IN ('DOWNLOADED','FAILED')").fetchall()
        for j in jobs:
            n = j["nzo_id"]; table = "items" if j["entity_type"] == "item" else "volumes"
            if n in que:
                st = (que[n].get("status") or "QUEUED").upper()
                mapped = "DOWNLOADING" if st not in ("QUEUED", "PAUSED") else "QUEUED"
                if mapped != j["status"]: updated += 1
                db.execute("UPDATE jobs SET status=? WHERE id=?", (mapped, j["id"]))
                db.execute(f"UPDATE {table} SET status=? WHERE id=?", (mapped, j["entity_id"]))
            elif n in hist:
                st = (hist[n].get("status") or "UNKNOWN").upper()
                mapped = "DOWNLOADED" if st == "COMPLETED" else ("FAILED" if st == "FAILED" else st)
                updated += 1
                db.execute("UPDATE jobs SET status=?,completed_at=? WHERE id=?",
                           (mapped, datetime.now().isoformat(timespec="seconds"), j["id"]))
                db.execute(f"UPDATE {table} SET status=? WHERE id=?", (mapped, j["entity_id"]))
                if j["entity_type"] == "volume" and mapped == "DOWNLOADED":
                    for c in db.execute("SELECT item_id FROM volume_covers WHERE volume_id=?", (j["entity_id"],)):
                        db.execute("""UPDATE items SET status='FOUND' WHERE id=? AND status IN ('CATALOGED','MISSING','FAILED')""",
                                   (c["item_id"],))
    return {"tracked": len(jobs), "updated": updated}
