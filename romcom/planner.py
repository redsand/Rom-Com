from .db import connect
from .status import MISSING, SATISFIED

def bulk_plan():
    db=connect()
    # `missing` counts covered titles with nothing on disk yet — a bundle is only worth
    # downloading for what it actually adds, not for the titles it covers that we own.
    rows=db.execute(f"""SELECT v.id,v.title,v.estimated_bytes,
      COUNT(vc.item_id) covered,
      COALESCE(SUM(CASE WHEN i.wanted=1 AND i.status IN {MISSING} THEN 1 ELSE 0 END),0) missing
      FROM volumes v LEFT JOIN volume_covers vc ON vc.volume_id=v.id
      LEFT JOIN items i ON i.id=vc.item_id
      WHERE v.authorized=1 AND v.status NOT IN ('QUEUED','DOWNLOADING','DOWNLOADED','VERIFIED')
      GROUP BY v.id""").fetchall()
    out=[]
    for r in rows:
        b=r["estimated_bytes"] or 0; gb=b/(1024**3) if b else 0
        score=(r["missing"]/gb) if gb else float(r["missing"] or 0)
        out.append(dict(r)|{"coverage_score":score})
    return sorted(out,key=lambda x:(x["coverage_score"],x["missing"]),reverse=True)

# What is still worth acquiring: armed items with nothing on disk. FOUND/DOWNLOADED
# items are already downloaded and waiting for a scan — they are not picks, and a
# FAILED or MANUAL item stays out until it is deliberately retried from the drawer.
_NEXT_WHERE=f"""i.wanted=1 AND i.authorized=1
      AND i.status IN {MISSING}
      AND NOT EXISTS (
        SELECT 1 FROM volume_covers vc JOIN volumes v ON v.id=vc.volume_id
        WHERE vc.item_id=i.id AND v.authorized=1
        AND v.status IN ('CATALOGED','FOUND','QUEUED','DOWNLOADING')
      )"""

def next_individuals(limit=None):
    db=connect()
    q=f"""SELECT i.* FROM items i
      WHERE {_NEXT_WHERE}
      ORDER BY i.system,i.title"""
    if limit: q+=f" LIMIT {int(limit)}"
    return [dict(r) for r in db.execute(q)]

def next_picks(search=None,system=None,limit=50,offset=0):
    """Same eligibility as next_individuals, with a title/id filter and paging for the Acquire tab."""
    db=connect()
    where=_NEXT_WHERE; args=[]
    if search:
        where+=" AND (i.title LIKE ? COLLATE NOCASE OR i.id LIKE ? COLLATE NOCASE)"
        args+=[f"%{search}%"]*2
    if system:
        where+=" AND i.system=?"; args.append(system)
    total=db.execute(f"SELECT COUNT(*) c FROM items i WHERE {where}",args).fetchone()["c"]
    rows=db.execute(f"""SELECT i.* FROM items i
      WHERE {where}
      ORDER BY i.system,i.title LIMIT ? OFFSET ?""",(*args,int(limit),int(offset)))
    return [dict(r) for r in rows],total
