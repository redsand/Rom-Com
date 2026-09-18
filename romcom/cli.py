import argparse
from datetime import datetime
from .db import connect
from .seed import seed
from .planner import bulk_plan
from .scanner import scan
from . import indexer, sab

def fmt(n):
    x=float(n or 0)
    for u in ["B","KB","MB","GB","TB"]:
        if x<1024: return f"{x:.1f} {u}"
        x/=1024
    return f"{x:.1f} PB"

def cmd_status(_):
    db=connect(); total=db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    print(f"Cataloged: {total}")
    for r in db.execute("SELECT status,COUNT(*) c FROM items GROUP BY status ORDER BY c DESC"):
        print(f"{r['status']:14} {r['c']}")

def cmd_missing(a):
    db=connect(); q="SELECT * FROM items WHERE wanted=1 AND status NOT IN ('COMPLETE','VERIFIED','INSTALLED')"; p=[]
    if a.system: q+=" AND system=?"; p.append(a.system)
    for r in db.execute(q+" ORDER BY series,series_number,title",p): print(f"{r['id']:12} {r['system'] or '-':10} {r['title']}")

def cmd_series(a):
    db=connect()
    for r in db.execute("SELECT * FROM items WHERE lower(series)=lower(?) ORDER BY series_number",(a.name,)):
        print(f"{r['series_number']:02d}  {r['status']:10}  {r['title']} ({r['year']})")

def cmd_search(a):
    db=connect(); r=db.execute("SELECT * FROM items WHERE id=?",(a.item_id,)).fetchone()
    if not r: raise SystemExit("unknown item")
    if not r["authorized"]: raise SystemExit("item is not marked authorized; search/acquire blocked")
    for i,x in enumerate(indexer.search(r["title"]),1): print(f"{i:2}. {fmt(x['size']):>10}  {x['title']}")

def cmd_acquire(a):
    db=connect(); r=db.execute("SELECT * FROM items WHERE id=?",(a.item_id,)).fetchone()
    if not r or not r["authorized"]: raise SystemExit("unknown or unauthorized item")
    results=indexer.search(r["title"])
    if a.result<1 or a.result>len(results): raise SystemExit("invalid result number")
    x=results[a.result-1]; resp=sab.add_url(x["url"],f"ROMCOM__{r['id']}"); ids=resp.get("nzo_ids",[]); nzo=ids[0] if ids else None
    with db:
        db.execute("UPDATE items SET status='QUEUED' WHERE id=?",(r["id"],))
        db.execute("INSERT INTO jobs(entity_type,entity_id,nzo_id,result_title,bytes,status,queued_at) VALUES('item',?,?,?,?,?,?)",(r["id"],nzo,x["title"],x["size"],"QUEUED",datetime.now().isoformat(timespec="seconds")))
    print(f"Queued {r['title']} as {nzo}")

def cmd_sync(_):
    db=connect(); hist={x.get("nzo_id"):x for x in sab.history() if x.get("nzo_id")}; que={x.get("nzo_id"):x for x in sab.queue() if x.get("nzo_id")}
    with db:
        for j in db.execute("SELECT * FROM jobs WHERE status NOT IN ('COMPLETE','FAILED')").fetchall():
            n=j["nzo_id"]
            if n in que:
                st=(que[n].get("status") or "QUEUED").upper(); db.execute("UPDATE jobs SET status=? WHERE id=?",(st,j["id"])); db.execute("UPDATE items SET status=? WHERE id=?",(st,j["entity_id"]))
            elif n in hist:
                st=(hist[n].get("status") or "UNKNOWN").upper(); mapped="COMPLETE" if st=="COMPLETED" else ("FAILED" if st=="FAILED" else st)
                db.execute("UPDATE jobs SET status=?,completed_at=? WHERE id=?",(mapped,datetime.now().isoformat(timespec="seconds"),j["id"])); db.execute("UPDATE items SET status=? WHERE id=?",(mapped,j["entity_id"]))
    print("SABnzbd state synchronized")

def main():
    p=argparse.ArgumentParser(prog="romcom"); s=p.add_subparsers(dest="cmd",required=True)
    s.add_parser("init").set_defaults(fn=lambda a: (connect(),print("Database initialized")))
    s.add_parser("seed").set_defaults(fn=lambda a: print(f"Seeded catalog; {seed()} items total"))
    s.add_parser("status").set_defaults(fn=cmd_status)
    m=s.add_parser("missing"); m.add_argument("--system"); m.set_defaults(fn=cmd_missing)
    se=s.add_parser("series"); se.add_argument("name"); se.set_defaults(fn=cmd_series)
    bp=s.add_parser("bulk-plan"); bp.set_defaults(fn=lambda a:[print(f"{x['coverage_score']:8.2f}  {x['id']}  {x['title']}") for x in bulk_plan()] or None)
    q=s.add_parser("search"); q.add_argument("item_id"); q.set_defaults(fn=cmd_search)
    ac=s.add_parser("acquire"); ac.add_argument("item_id"); ac.add_argument("--result",type=int,required=True); ac.set_defaults(fn=cmd_acquire)
    s.add_parser("sync").set_defaults(fn=cmd_sync)
    sc=s.add_parser("scan"); sc.add_argument("path"); sc.set_defaults(fn=lambda a: print(f"Scanned {scan(a.path)} files"))
    a=p.parse_args(); a.fn(a)
