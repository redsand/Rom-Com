import json
from .db import connect

def summary():
    db=connect()
    total=db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    wanted=db.execute("SELECT COUNT(*) c FROM items WHERE wanted=1").fetchone()["c"]
    by_status={r["status"]:r["c"] for r in db.execute("SELECT status,COUNT(*) c FROM items GROUP BY status")}
    by_system=[dict(r) for r in db.execute("""SELECT COALESCE(system,'unknown') system,COUNT(*) total,
      SUM(CASE WHEN wanted=1 THEN 1 ELSE 0 END) wanted,
      SUM(CASE WHEN status IN ('VERIFIED','NORMALIZED','INSTALLED','TESTED') THEN 1 ELSE 0 END) satisfied
      FROM items GROUP BY system ORDER BY system""")]
    return {"cataloged":total,"wanted":wanted,"by_status":by_status,"by_system":by_system}

def render_text():
    s=summary(); lines=[f"Cataloged: {s['cataloged']}",f"Wanted:    {s['wanted']}","", "By status:"]
    lines += [f"  {k:14} {v}" for k,v in sorted(s["by_status"].items())]
    lines += ["","By system:"]
    for x in s["by_system"]:
        pct=(100*x["satisfied"]/x["wanted"]) if x["wanted"] else 0
        lines.append(f"  {x['system']:16} {x['satisfied']:5}/{x['wanted']:<5} {pct:6.1f}%")
    return "\n".join(lines)
