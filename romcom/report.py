import json
from .db import connect

# Artwork/support files (snapshots, manuals, videos) shouldn't make an item count as "on disk".
_NOT_CONTENT=("png","jpg","jpeg","gif","bmp","ico","pdf","mp4","avi","txt","nfo","xml","html")
_ON_DISK="""SELECT DISTINCT matched_item_id item_id FROM files
  WHERE matched_item_id IS NOT NULL AND """ + " AND ".join(
  f"lower(path) NOT LIKE '%.{e}'" for e in _NOT_CONTENT)

def summary():
    db=connect()
    total=db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    wanted=db.execute("SELECT COUNT(*) c FROM items WHERE wanted=1").fetchone()["c"]
    on_disk=db.execute(f"SELECT COUNT(*) c FROM ({_ON_DISK})").fetchone()["c"]
    by_status={r["status"]:r["c"] for r in db.execute("SELECT status,COUNT(*) c FROM items GROUP BY status")}
    by_system=[dict(r) for r in db.execute(f"""SELECT COALESCE(i.system,'unknown') system,COUNT(*) total,
      SUM(CASE WHEN i.wanted=1 THEN 1 ELSE 0 END) wanted,
      SUM(CASE WHEN i.status IN ('VERIFIED','NORMALIZED','INSTALLED','TESTED') THEN 1 ELSE 0 END) satisfied,
      SUM(CASE WHEN od.item_id IS NOT NULL THEN 1 ELSE 0 END) have
      FROM items i LEFT JOIN ({_ON_DISK}) od ON od.item_id=i.id
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
