from .db import connect

SATISFIED=("VERIFIED","NORMALIZED","INSTALLED","TESTED")

def bulk_plan():
    db=connect()
    rows=db.execute("""SELECT v.id,v.title,v.estimated_bytes,
      COUNT(vc.item_id) covered,
      COALESCE(SUM(CASE WHEN i.wanted=1 AND i.status NOT IN ('VERIFIED','NORMALIZED','INSTALLED','TESTED','EXCLUDED') THEN 1 ELSE 0 END),0) missing
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

def next_individuals(limit=None):
    db=connect()
    q="""SELECT i.* FROM items i
      WHERE i.wanted=1 AND i.authorized=1
      AND i.status NOT IN ('QUEUED','DOWNLOADING','DOWNLOADED','VERIFIED','NORMALIZED','INSTALLED','TESTED','EXCLUDED')
      AND NOT EXISTS (
        SELECT 1 FROM volume_covers vc JOIN volumes v ON v.id=vc.volume_id
        WHERE vc.item_id=i.id AND v.authorized=1
        AND v.status IN ('CATALOGED','FOUND','QUEUED','DOWNLOADING')
      )
      ORDER BY i.system,i.title"""
    if limit: q+=f" LIMIT {int(limit)}"
    return [dict(r) for r in db.execute(q)]
