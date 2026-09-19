"""Actions shared by the CLI and the web UI: queueing downloads and syncing SABnzbd state."""
from datetime import datetime, timedelta
from .db import connect
from . import sab

# A job whose nzo is missing from both the SABnzbd queue and its history was deleted
# there and can never resolve; sync gives up on it after this grace period (fresh
# queue inserts race the queue snapshot taken at the top of sync).
VANISHED_AFTER = timedelta(minutes=10)

def queue_result(db, kind, e, result):
    """Send a search result to SABnzbd and record the job. Returns the nzo id (or None)."""
    resp = sab.add_url(result["url"], f"ROMCOM__{e['id']}", priority=1 if kind == "volume" else 0)
    ids = resp.get("nzo_ids", [])
    nzo = ids[0] if ids else None
    table = "items" if kind == "item" else "volumes"
    with db:
        db.execute(f"UPDATE {table} SET status='QUEUED' WHERE id=?", (e["id"],))
        db.execute("""INSERT INTO jobs(entity_type,entity_id,nzo_id,result_title,result_url,bytes,status,source,queued_at)
          VALUES(?,?,?,?,?,?,'QUEUED','sab',?)""",
          (kind, e["id"], nzo, result["title"], result["url"], result.get("size") or 0,
           datetime.now().isoformat(timespec="seconds")))
    return nzo

def journal_direct(db, entity_id, source, result, path=None):
    """Record a completed direct download (romsgames, vimm, …) in the same jobs ledger
    SABnzbd downloads land in, so the Activity feed represents every acquisition, not
    just the ones that went through SABnzbd. Direct jobs carry no nzo_id (there is no
    SABnzbd job to poll) and a source tag naming where the file came from."""
    from pathlib import Path
    size = result.get("size") or 0
    if path:
        try: size = Path(path).stat().st_size
        except OSError: pass
    now = datetime.now().isoformat(timespec="seconds")
    with db:
        db.execute("""INSERT INTO jobs(entity_type,entity_id,nzo_id,result_title,result_url,
            bytes,status,source,queued_at,completed_at)
          VALUES('item',?,NULL,?,?,?,'DOWNLOADED',?,?,?)""",
          (entity_id, result.get("title") or entity_id, result.get("url"), size, source, now, now))

def sync(db=None):
    """Pull queue/history state from SABnzbd into jobs and entity statuses."""
    if db is None:
        db = connect()
    hist = {x.get("nzo_id"): x for x in sab.history() if x.get("nzo_id")}
    que = {x.get("nzo_id"): x for x in sab.queue() if x.get("nzo_id")}
    cutoff = (datetime.now() - VANISHED_AFTER).isoformat(timespec="seconds")
    updated = 0
    with db:
        jobs = db.execute("SELECT * FROM jobs WHERE status NOT IN ('DOWNLOADED','FAILED')").fetchall()
        for j in jobs:
            n = j["nzo_id"]; table = "items" if j["entity_type"] == "item" else "volumes"
            if j["status"] in ("QUEUED", "DOWNLOADING") and n not in que and n not in hist \
                    and (j["queued_at"] or "") < cutoff:
                # Gone from SABnzbd entirely (deleted/purged there, or the nzo was NULL):
                # it will never reach a terminal state on its own.
                db.execute("UPDATE jobs SET status='FAILED',completed_at=? WHERE id=?",
                           (datetime.now().isoformat(timespec="seconds"), j["id"]))
                db.execute(f"UPDATE {table} SET status='FAILED' WHERE id=? AND status IN ('QUEUED','DOWNLOADING')",
                           (j["entity_id"],))
                updated += 1
            elif n in que:
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
