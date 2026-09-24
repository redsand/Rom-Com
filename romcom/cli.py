import argparse, json
from .db import connect
from .seed import seed
from .planner import bulk_plan, next_individuals
from .scanner import scan
from .catalog import import_dat, import_dats, import_scummvm
from .report import render_text, summary
from .doctor import run as doctor_run
from .manage import set_series, export_csv, import_csv
from .catalog_status import render as render_catalog_status
from .status import MISSING, own_all
from . import indexer, actions
from .config import settings

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
    db=connect(); q=f"SELECT * FROM items WHERE wanted=1 AND status IN {MISSING}"; p=[]
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
    nzo=actions.queue_result(db,kind,e,results[a.result-1])
    print(f"Queued {e['title']} as {nzo}")

def cmd_sync(_):
    r=actions.sync()
    print(f"SABnzbd state synchronized ({r['tracked']} tracked, {r['updated']} updated); downloaded content still requires scan/verification")

def cmd_auto_acquire(a):
    from .acquirer import auto_acquire
    def prog(i,total,name,stats=None):
        extra=" ".join(f"{k}={v}" for k,v in (stats or {}).items() if isinstance(v,(int,float)))
        print(f"[{i}/{total}] {name}  {extra}",flush=True)
    print(json.dumps(auto_acquire(progress=prog,poll_interval=a.poll,max_wait_minutes=a.max_wait,batch_max=a.max_batch,parallel=a.parallel,watch=a.watch),indent=2))

def cmd_webdl(a):
    from . import webdl
    db=connect(); e=db.execute("SELECT * FROM items WHERE id=?",(a.ident,)).fetchone()
    if not e: raise SystemExit(f"unknown item: {a.ident}")
    if not e["authorized"]: raise SystemExit("item is not marked authorized")
    dest=a.out or settings()["download_dir"]
    if not dest: raise SystemExit("no destination: set ROMCOM_DOWNLOAD_DIR or pass --out")
    results=webdl.search(e["title"],e["system"])
    for i,x in enumerate(results[:10],1): print(f"{i:2}. {x['score']:6.1f}  {x['title']}  [{x.get('console','?')}]")
    pick=None
    if a.result:
        pick=results[a.result-1] if 0<a.result<=len(results) else None
        if not pick: raise SystemExit("invalid result number")
    else:
        pick=next((x for x in results if x["score"]>=20 and x.get("url")),None)
        if not pick: raise SystemExit("no direct match scoring >= 20; rerun with --result N to override")
    print(f"downloading: {pick['title']} …")
    path=webdl.fetch(pick,dest)
    print(f"saved {path}")
    with db: db.execute("UPDATE items SET status='DOWNLOADED' WHERE id=?",(e["id"],))
    print(f"{e['id']}: status=DOWNLOADED — run `romcom scan \"{dest}\"` to match it into the library")

def cmd_web(a):
    from .web import serve
    serve(host=a.host,port=a.port,open_browser=not a.no_browser)

def cmd_dedupe(a):
    from . import dedupe
    r=dedupe.dedupe(dry_run=not a.apply)
    print(json.dumps({k:v for k,v in r.items() if k!="pairs"},indent=2))
    if not a.apply and r["removed"]:
        print(f'{r["removed"]} row(s) would be merged away; re-run with --apply')

def cmd_cache(a):
    from . import searchcache
    if a.action=="clear":
        print(f"Cleared {searchcache.clear(a.source)} cached search(es)")
    else:
        print(json.dumps(searchcache.stats(),indent=2))

def cmd_reconcile(a):
    from .reconcile import reconcile, reacquire_mismatches
    if a.reacquire:
        print(f"Re-armed {reacquire_mismatches(a.system)} mismatch item(s) as MISSING for re-acquisition")
    else:
        print(json.dumps(reconcile(),indent=2))

def cmd_vimm(a):
    from . import vimm
    print("Opening Vimm in a browser — solve any Cloudflare challenge, then it captures the session…")
    ok=vimm.capture()
    print("Vimm session captured — downloads can now reuse it headlessly."
          if ok else "Capture timed out. Rerun and solve the challenge in the window that opens.")

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

def cmd_mark_owned(_):
    db=connect()
    with db: n=own_all(db)
    print(f"Marked {n} item(s) we already have as wanted & authorized")

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

def cmd_import_dats(a):
    def prog(i,total,name):
        if i<total: print(f"[{i+1}/{total}] {name}",flush=True)
    r=import_dats(a.path,system=a.system,source=a.source,wanted=a.wanted,progress=prog)
    if a.json: print(json.dumps(r,indent=2)); return
    print(f"\nFiles: {r['files']}  Dats: {r['dats']}  Items: {r['items']}  Hashes: {r['hashes']}  ScummVM matched: {r['scummvm_matched']}")
    print(f"Imported: {len(r['imported'])}  Skipped: {len(r['skipped'])}  Errors: {len(r['errors'])}")
    for x in r["skipped"]: print(f"  SKIP {x['file']}: {x.get('header') or x.get('dat') or ''} ({x['reason']})")
    for x in r["errors"]: print(f"  ERR  {x['file']}: {x['error']}")
def cmd_import_scummvm(a): print(f"Imported {import_scummvm(a.url,not a.catalog_only)} ScummVM compatibility entries")
def cmd_scan(a):
    def prog(i,total,name,stats=None):
        if i%100==0 and total:
            extra=f"  matched={stats.get('matched',0)} verified={stats.get('verified',0)} adopted={stats.get('adopted',0)}" if stats else ""
            print(f"[{i}/{total}]{extra}  {name}",flush=True)
    print(json.dumps(scan(a.path,not a.no_name_match,adopt=not a.no_adopt,recursive=not a.no_recursive,progress=prog),indent=2))

def cmd_adopt(a):
    from .scanner import adopt_unmatched
    print(json.dumps(adopt_unmatched(root=a.root),indent=2))

def cmd_playable(a):
    from .mameset import refresh_playable
    print(json.dumps({"playable": refresh_playable()}, indent=2))

def cmd_cores(a):
    from . import cores
    if a.install:
        r = cores.install(systems=a.system or None, all_systems=a.all,
                          progress=lambda c, s: print(f"  downloading {c} ({s})..."))
        print(f'installed {r["installed"]} core(s) into {r["dest"]}')
        for f in r["failed"]:
            print(f'  FAILED {f["core"]}: {f["error"]}')
        return
    st = cores.status()
    print(f'RetroArch cores folder: {st["cores_dir"]}')
    print(f'{st["missing_count"]} core(s) missing, blocking {st["games_blocked"]:,} games')
    print()
    for r in st["rows"]:
        mark = "installed" if r["installed"] else "MISSING"
        print(f'  {r["system"]:<13} {r["games"]:>7,} games  {mark:<10} {str(r["name"] or "-")}')
        if r["why"]:
            print(f'      {r["confidence"]}: {r["why"]}')
        if r["alternative"]:
            print(f'      alternative: {r["alternative"]}')
    if st["missing_count"]:
        print()
        print("  install them all with:  python -m romcom cores --install")

def cmd_agent(a):
    from . import player
    def ev(name, payload):
        if name == "ready":
            print(f"launch agent ready (polling every {payload['interval']}s) — leave this running")
        elif name == "launched":
            print(f"launched {payload['count']} game(s)")
        else:
            print(f"{name}: {payload}")
    try:
        player.agent_loop(interval=a.interval, on_event=ev)
    except KeyboardInterrupt:
        print("agent stopped")

def cmd_play(a):
    from . import player
    try:
        plan = player.command_for(a.game) if a.show else player.launch(a.game)
    except LookupError as e:
        print(e); return 1
    print(json.dumps(plan, indent=2))

def cmd_keep(a):
    from . import player
    if a.list:
        k = player.kept(system=a.system)
        print(json.dumps({"count": k["count"], "gb": k["gb"],
                          "items": [i["title"] for i in k["items"][:40]]}, indent=2))
        return
    try:
        print(json.dumps(player.set_keep(a.game, on=not a.no), indent=2))
    except LookupError as e:
        print(e); return 1

def cmd_organize(a):
    from .organizer import organize
    print(json.dumps(organize(a.dest, systems=a.system or None,
                             wanted_only=a.wanted_only, keep_only=a.keep_only, sources=a.source or None,
                             dry_run=a.dry_run), indent=2))

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
    aa=s.add_parser("auto-acquire"); aa.add_argument("--poll",type=float,default=None); aa.add_argument("--max-wait",type=float,dest="max_wait",default=None); aa.add_argument("--max-batch",type=int,dest="max_batch",default=None); aa.add_argument("--parallel",type=int,default=None); aa.add_argument("--watch",action="store_true"); aa.set_defaults(fn=cmd_auto_acquire)
    wd=s.add_parser("webdl"); wd.add_argument("ident"); wd.add_argument("--result",type=int); wd.add_argument("--out"); wd.set_defaults(fn=cmd_webdl)
    sc=s.add_parser("scan"); sc.add_argument("path"); sc.add_argument("--no-name-match",action="store_true"); sc.add_argument("--no-adopt",action="store_true"); sc.add_argument("--no-recursive",action="store_true"); sc.set_defaults(fn=cmd_scan)
    pb=s.add_parser("playable", help="recompute which arcade sets can be assembled")
    pb.set_defaults(fn=cmd_playable)
    cr=s.add_parser("cores", help="which libretro cores this library needs, and fetch them")
    cr.add_argument("--install", action="store_true", help="download the missing ones")
    cr.add_argument("--all", action="store_true", help="every system, not just those with files")
    cr.add_argument("--system", action="append", help="limit to these systems")
    cr.set_defaults(fn=cmd_cores)
    ag=s.add_parser("agent", help="run the launch agent in your session (needed for the web Play button)")
    ag.add_argument("--interval", type=float, default=1.0)
    ag.set_defaults(fn=cmd_agent)
    pl=s.add_parser("play", help="launch a game in its emulator (runs in YOUR session)")
    pl.add_argument("game", help="item id, or part of a title")
    pl.add_argument("--show", action="store_true", help="print the command, launch nothing")
    pl.set_defaults(fn=cmd_play)
    kp=s.add_parser("keep", help="mark a game for export to the card")
    kp.add_argument("game", nargs="?", help="item id, or part of a title")
    kp.add_argument("--no", action="store_true", help="unmark instead")
    kp.add_argument("--list", action="store_true", help="show what is marked, and its size")
    kp.add_argument("--system")
    kp.set_defaults(fn=cmd_keep)
    og=s.add_parser("organize"); og.add_argument("dest"); og.add_argument("--system",action="append")
    og.add_argument("--source",action="append",help="only items from this catalog_source (e.g. antopisa)")
    og.add_argument("--wanted-only",action="store_true",help="only items marked wanted")
    og.add_argument("--keep-only",action="store_true",help="only items marked keep")
    og.add_argument("--dry-run",action="store_true",help="report what would be copied, copy nothing")
    og.set_defaults(fn=cmd_organize)
    ad=s.add_parser("adopt"); ad.add_argument("--root"); ad.set_defaults(fn=cmd_adopt)
    d=s.add_parser("import-dat"); d.add_argument("path"); d.add_argument("--system",required=True); d.add_argument("--source",default="dat"); d.add_argument("--catalog-only",action="store_true"); d.set_defaults(fn=cmd_import_dat)
    ds=s.add_parser("import-dats"); ds.add_argument("path"); ds.add_argument("--system"); ds.add_argument("--source"); ds.add_argument("--wanted",action="store_true"); ds.add_argument("--json",action="store_true"); ds.set_defaults(fn=cmd_import_dats)
    sv=s.add_parser("import-scummvm"); sv.add_argument("--url",default="https://www.scummvm.org/compatibility"); sv.add_argument("--catalog-only",action="store_true"); sv.set_defaults(fn=cmd_import_scummvm)
    r=s.add_parser("report"); r.add_argument("--json",action="store_true"); r.set_defaults(fn=lambda a:print(json.dumps(summary(),indent=2) if a.json else render_text()))
    st=s.add_parser("set"); st.add_argument("ident"); st.add_argument("field"); st.add_argument("value"); st.set_defaults(fn=cmd_set)
    ss=s.add_parser("set-series"); ss.add_argument("name"); ss.add_argument("field"); ss.add_argument("value"); ss.set_defaults(fn=cmd_set_series)
    mo=s.add_parser("mark-owned"); mo.set_defaults(fn=cmd_mark_owned)
    ec=s.add_parser("export-csv"); ec.add_argument("path"); ec.set_defaults(fn=cmd_export_csv)
    ic=s.add_parser("import-csv"); ic.add_argument("path"); ic.set_defaults(fn=cmd_import_csv)
    dr=s.add_parser("doctor"); dr.add_argument("--no-sab",action="store_true"); dr.set_defaults(fn=cmd_doctor)
    dd=s.add_parser("dedupe"); dd.add_argument("--apply",action="store_true"); dd.set_defaults(fn=cmd_dedupe)
    w=s.add_parser("web"); w.add_argument("--host",default="127.0.0.1"); w.add_argument("--port",type=int,default=8927); w.add_argument("--no-browser",action="store_true"); w.set_defaults(fn=cmd_web)
    cs=s.add_parser("catalog-status"); cs.add_argument("--strict",action="store_true"); cs.set_defaults(fn=cmd_catalog_status)
    cc=s.add_parser("cache"); cc.add_argument("action",choices=["stats","clear"],nargs="?",default="stats"); cc.add_argument("--source"); cc.set_defaults(fn=cmd_cache)
    vm=s.add_parser("vimm"); vm.add_argument("action",choices=["capture"],nargs="?",default="capture"); vm.set_defaults(fn=cmd_vimm)
    rc=s.add_parser("reconcile"); rc.add_argument("--reacquire",action="store_true"); rc.add_argument("--system"); rc.set_defaults(fn=cmd_reconcile)
    a=p.parse_args(); a.fn(a)

if __name__=="__main__": main()
