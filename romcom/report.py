import json
from .db import connect

# "On disk" means a real content file matched an item (artwork/manuals/videos excluded).
# The exclusion is precomputed into files.content (see db.NOT_CONTENT_EXTS).
_ON_DISK="SELECT DISTINCT matched_item_id item_id FROM files WHERE matched_item_id IS NOT NULL AND content=1"

def _load_on_disk(db):
    """Materialize the on-disk item ids into an indexed temp table ONCE, so joins against
    it use a primary-key lookup. Joining items directly against the DISTINCT subquery lets
    SQLite re-evaluate it per item (200k×) — turning a ~100ms dashboard into a 40s hang."""
    db.execute("DROP TABLE IF EXISTS temp._od")
    db.execute("CREATE TEMP TABLE _od(id TEXT PRIMARY KEY)")
    db.execute(f"INSERT OR IGNORE INTO _od(id) {_ON_DISK}")
    return db.execute("SELECT COUNT(*) c FROM _od").fetchone()["c"]

def summary():
    db=connect()
    total=db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    wanted=db.execute("SELECT COUNT(*) c FROM items WHERE wanted=1").fetchone()["c"]
    on_disk=_load_on_disk(db)
    by_status={r["status"]:r["c"] for r in db.execute("SELECT status,COUNT(*) c FROM items GROUP BY status")}
    by_system=[dict(r) for r in db.execute("""SELECT COALESCE(i.system,'unknown') system,COUNT(*) total,
      SUM(CASE WHEN i.wanted=1 THEN 1 ELSE 0 END) wanted,
      SUM(CASE WHEN i.status IN ('VERIFIED','NORMALIZED','INSTALLED','TESTED') THEN 1 ELSE 0 END) satisfied,
      SUM(CASE WHEN od.id IS NOT NULL THEN 1 ELSE 0 END) have
      FROM items i LEFT JOIN _od od ON od.id=i.id
      GROUP BY i.system ORDER BY i.system""")]
    return {"cataloged":total,"wanted":wanted,"on_disk":on_disk,"by_status":by_status,"by_system":by_system}

def render_text():
    s=summary(); lines=[f"Cataloged: {s['cataloged']}",f"Wanted:    {s['wanted']}","", "By status:"]
    lines += [f"  {k:14} {v}" for k,v in sorted(s["by_status"].items())]
    lines += ["","By system:"]
    for x in s["by_system"]:
        pct=(100*x["satisfied"]/x["wanted"]) if x["wanted"] else 0
        lines.append(f"  {x['system']:16} {x['satisfied']:5}/{x['wanted']:<5} {pct:6.1f}%")
    return "\n".join(lines)
