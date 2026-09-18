import argparse, json
from datetime import datetime
from .db import connect
from .seed import seed
from .planner import bulk_plan, next_individuals
from .scanner import scan
from .catalog import import_dat, import_scummvm
from .report import render_text, summary
from .doctor import run as doctor_run
from .manage import set_series, export_csv, import_csv
from .catalog_status import render as render_catalog_status
from . import indexer, sab

def fmt(n):
    x=float(n or 0)
    for u in ["B","KB","MB","GB","TB"]:
        if x<1024: return f"{x:.1f} {u}"
        x/=1024
    return f"{x:.1f} PB"

def entity(db,ident):
    i=db.execute("SELECT 'item' kind,id,title,authorized,status FROM items WHERE id=?",(ident,)).fetchone()
    if i: return "item",i
    v=db.execute("SELECT 'volume' kind,id,title,authorized,status FROM volumes WHERE id=?",(ident,)).fetchone()
    if v: return "volume",v
    raise SystemExit("unknown item/volume")

def cmd_status(_): print(render_text())

def cmd_missing(a):
    db=connect(); q="SELECT * FROM items WHERE wanted=1 AND status NOT IN ('VERIFIED','NORMALIZED','INSTALLED','TESTED','EXCLUDED')"; p=[]
    if a.system: q+=" AND system=?"; p.append(a.system)
    for r in db.execute(q+" ORDER BY series,series_number,title",p): print(f"{r['id']:34} {r['status']:12} {r['system'] or '-':12} {r['title']}")

def cmd_series(a):
    db=connect()
    rows=db.execute("SELECT * FROM items WHERE lower(series)=lower(?) ORDER BY series_number",(a.name,)).fetchall()
    if not rows: raise SystemExit("series not found")
    for r in rows: print(f"{r['series_number']:02d}  {r['status']:12}  {'AUTH' if r['authorized'] else '----'}  {r['title']} ({r['year']})")

def search_results(ident):
    db=connect(); kind,_=entity(db,ident)
    try: results,e=indexer.search_entity(db,kind,ident)
    except PermissionError as ex: raise SystemExit(str(ex))
    return kind,e,results

def cmd_search(a):
    _,_,results=search_results(a.ident)
    for i,x in enumerate(results,1): print(f"{i:2}. {x['score']:6.1f} {fmt(x['size']):>10}  {x['title']}")

def cmd_acquire(a):
    db=connect(); kind,e,results=search_results(a.ident)
    if a.result<1 or a.result>len(results): raise SystemExit("invalid result number")
    x=results[a.result-1]; resp=sab.add_url(x["url"],f"ROMCOM__{e['id']}",priority=1 if kind=="volume" else 0)
    ids=resp.get("nzo_ids",[]); nzo=ids[0] if ids else None
    table="items" if kind=="item" else "volumes"
    with db:
        db.execute(f"UPDATE {table} SET status='QUEUED' WHERE id=?",(e["id"],))
        db.execute("""INSERT INTO jobs(entity_type,entity_id,nzo_id,result_title,result_url,bytes,status,queued_at)
          VALUES(?,?,?,?,?,?,'QUEUED',?)""",(kind,e["id"],nzo,x["title"],x["url"],x["size"],datetime.now().isoformat(timespec="seconds")))
    print(f"Queued {e['title']} as {nzo}")

def cmd_sync(_):
    db=connect(); hist={x.get("nzo_id"):x for x in sab.history() if x.get("nzo_id")}; que={x.get("nzo_id"):x for x in sab.queue() if x.get("nzo_id")}
    with db:
        jobs=db.execute("SELECT * FROM jobs WHERE status NOT IN ('DOWNLOADED','FAILED')").fetchall()
        for j in jobs:
            n=j["nzo_id"]; table="items" if j["entity_type"]=="item" else "volumes"
            if n in que:
                st=(que[n].get("status") or "QUEUED").upper()
                mapped="DOWNLOADING" if st not in ("QUEUED","PAUSED") else "QUEUED"
                db.execute("UPDATE jobs SET status=? WHERE id=?",(mapped,j["id"])); db.execute(f"UPDATE {table} SET status=? WHERE id=?",(mapped,j["entity_id"]))
            elif n in hist:
                st=(hist[n].get("status") or "UNKNOWN").upper()
                mapped="DOWNLOADED" if st=="COMPLETED" else ("FAILED" if st=="FAILED" else st)
                db.execute("UPDATE jobs SET status=?,completed_at=? WHERE id=?",(mapped,datetime.now().isoformat(timespec="seconds"),j["id"]))
                db.execute(f"UPDATE {table} SET status=? WHERE id=?",(mapped,j["entity_id"]))
                if j["entity_type"]=="volume" and mapped=="DOWNLOADED":
                    for c in db.execute("SELECT item_id FROM volume_covers WHERE volume_id=?",(j["entity_id"],)):
                        db.execute("""UPDATE items SET status='FOUND' WHERE id=? AND status IN ('CATALOGED','MISSING','FAILED')""",(c["item_id"],))
    print("SABnzbd state synchronized; downloaded content still requires scan/verification")

def cmd_set(a):
    db=connect(); kind,_=entity(db,a.ident); table="items" if kind=="item" else "volumes"
    allowed={"authorized","status"}
    if kind=="item": allowed|={"wanted","preferred_runtime","notes","play_status","system","region","language"}
    if a.field not in allowed: raise SystemExit(f"field must be one of: {', '.join(sorted(allowed))}")
    value=a.value
    if a.field in ("authorized","wanted"):
        value=1 if a.value.lower() in ("1","true","yes","on") else 0
    with db:
        db.execute(f"UPDATE {table} SET {a.field}=? WHERE id=?",(value,a.ident))
    print(f"{a.ident}: {a.field}={value}")

def cmd_doctor(a):
    bad=False
    for name,ok,detail in doctor_run(not a.no_sab):
        print(f"{'OK' if ok else 'FAIL':4} {name:12} {detail}")
        bad=bad or not ok
    if bad: raise SystemExit(1)

def cmd_set_series(a):
    try: count=set_series(a.name,a.field,a.value)
    except ValueError as e: raise SystemExit(str(e))
    print(f"Updated {count} items in series {a.name}")

def cmd_export_csv(a):
    print(f"Exported {export_csv(a.path)} items to {a.path}")

def cmd_import_csv(a):
    print(json.dumps(import_csv(a.path),indent=2))

def cmd_catalog_status(a):
    text,missing=render_catalog_status()
    print(text)
    if a.strict and missing: raise SystemExit(2)

def cmd_import_dat(a): print(json.dumps(import_dat(a.path,a.system,a.source,not a.catalog_only),indent=2))
def cmd_import_scummvm(a): print(f"Imported {import_scummvm(a.url,not a.catalog_only)} ScummVM compatibility entries")
def cmd_scan(a): print(json.dumps(scan(a.path,not a.no_name_match),indent=2))

def main():
    p=argparse.ArgumentParser(prog="romcom"); s=p.add_subparsers(dest="cmd",required=True)
    s.add_parser("init").set_defaults(fn=lambda a:(connect(),print("Database initialized/migrated")))
    s.add_parser("seed").set_defaults(fn=lambda a:print(f"Seeded configuration; {seed()} items total"))
    s.add_parser("status").set_defaults(fn=cmd_status)
    m=s.add_parser("missing"); m.add_argument("--system"); m.set_defaults(fn=cmd_missing)
    se=s.add_parser("series"); se.add_argument("name"); se.set_defaults(fn=cmd_series)
    bp=s.add_parser("bulk-plan"); bp.set_defaults(fn=lambda a:[print(f"{x['coverage_score']:8.2f} {x['missing']:5} missing  {x['id']}  {x['title']}") for x in bulk_plan()])
    ni=s.add_parser("next"); ni.add_argument("--limit",type=int,default=25); ni.set_defaults(fn=lambda a:[print(f"{x['id']:34} {x['title']}") for x in next_individuals(a.limit)])
    q=s.add_parser("search"); q.add_argument("ident"); q.set_defaults(fn=cmd_search)
    ac=s.add_parser("acquire"); ac.add_argument("ident"); ac.add_argument("--result",type=int,required=True); ac.set_defaults(fn=cmd_acquire)
    s.add_parser("sync").set_defaults(fn=cmd_sync)
    sc=s.add_parser("scan"); sc.add_argument("path"); sc.add_argument("--no-name-match",action="store_true"); sc.set_defaults(fn=cmd_scan)
    d=s.add_parser("import-dat"); d.add_argument("path"); d.add_argument("--system",required=True); d.add_argument("--source",default="dat"); d.add_argument("--catalog-only",action="store_true"); d.set_defaults(fn=cmd_import_dat)
    sv=s.add_parser("import-scummvm"); sv.add_argument("--url",default="https://www.scummvm.org/compatibility"); sv.add_argument("--catalog-only",action="store_true"); sv.set_defaults(fn=cmd_import_scummvm)
    r=s.add_parser("report"); r.add_argument("--json",action="store_true"); r.set_defaults(fn=lambda a:print(json.dumps(summary(),indent=2) if a.json else render_text()))
    st=s.add_parser("set"); st.add_argument("ident"); st.add_argument("field"); st.add_argument("value"); st.set_defaults(fn=cmd_set)
    ss=s.add_parser("set-series"); ss.add_argument("name"); ss.add_argument("field"); ss.add_argument("value"); ss.set_defaults(fn=cmd_set_series)
    ec=s.add_parser("export-csv"); ec.add_argument("path"); ec.set_defaults(fn=cmd_export_csv)
    ic=s.add_parser("import-csv"); ic.add_argument("path"); ic.set_defaults(fn=cmd_import_csv)
    dr=s.add_parser("doctor"); dr.add_argument("--no-sab",action="store_true"); dr.set_defaults(fn=cmd_doctor)
    cs=s.add_parser("catalog-status"); cs.add_argument("--strict",action="store_true"); cs.set_defaults(fn=cmd_catalog_status)
    a=p.parse_args(); a.fn(a)

if __name__=="__main__": main()
