import argparse, json, sys
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

def cmd_dupes(a):
    from . import dupes
    r=dupes.cleanup(apply=a.apply, systems=a.system or None)
    print(json.dumps({k:v for k,v in r.items() if k!="sample"},indent=2))
    if r["sample"]:
        print("largest duplicates:")
        for d in r["sample"]:
            print(f'  {d["path"]} ({d["bytes"]} bytes, same as {d["kept"]})')
    if not a.apply and r["duplicate_copies"]:
        gb=1024**3
        print(f'{r["duplicate_copies"]} copy/copies would be reclaimed '
              f'({r["reclaimable_bytes"]/gb:.1f} GB); re-run with --apply')

def cmd_prune(a):
    from . import prune
    r=prune.cleanup(apply=a.apply, systems=a.system or None, per_region=a.per_region)
    print(json.dumps({k:v for k,v in r.items() if k!="sample"},indent=2))
    if r["sample"]:
        print("largest removals:")
        for d in r["sample"]:
            print(f'  [{d["system"]}] {d["title"]} ({d["reason"]}, {d["files"]} file(s))')
    if not a.apply and r["items"]:
        gb=1024**3
        print(f'{r["items"]} item(s) would go ({r["junk"]} junk, {r["pre_releases"]} pre-release, '
              f'{r["variants"]} variants, {r["bytes"]/gb:.1f} GB); re-run with --apply --system <system>')

def cmd_cache(a):
    from . import searchcache
    if a.action=="clear":
        print(f"Cleared {searchcache.clear(a.source)} cached search(es)")
    else:
        print(json.dumps(searchcache.stats(),indent=2))

def cmd_reconcile(a):
    from .reconcile import (reconcile, reacquire_mismatches, wrong_downloads, stub_discs,
                            unclaimed_downloads)
    if a.unclaimed_downloads:
        r = unclaimed_downloads(apply=a.apply)
        for b in r["items"]:
            print(f"{b['system']:10} {b['title'][:50]:50} <- {b['path']}")
        print(f"{r['count']} downloaded game(s) paired with their file"
              + (" — claimed" if r["applied"] else " — run again with --apply to claim them"))
        return
    if a.stub_discs:
        r = stub_discs(apply=a.apply)
        for b in r["items"]:
            print(f"{b['system']:10} {b['bytes'] / 1e6:6.2f} MB  {b['title'][:50]:50} <- {b['path']}")
        print(f"{r['count']} disc game(s) whose file is a stub"
              + (" — let go and re-armed" if r["applied"] else " — run again with --apply to let go of them"))
        return
    if a.wrong_downloads:
        r = wrong_downloads(apply=a.apply)
        for b in r["items"]:
            print(f"{b['system']:10} {b['title'][:50]:50} {b['bytes'] // (1 << 20):>7} MB  {b['why'][:90]}")
        print(f"{r['count']} wrong download(s), {r['bytes'] / (1 << 30):.1f} GB"
              + (" no other game uses — let go and re-armed" if r["applied"]
                 else " no other game uses — run again with --apply to let go of them"))
        if r["applied"]:
            print("files nothing claims (safe to delete):")
            for b in r["items"]:
                for f in b["orphans"]:
                    print("  " + f)
    elif a.reacquire:
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

def cmd_fixnames(a):
    from .fixnames import fix
    r = fix(apply=a.apply)
    print(json.dumps({k: v for k, v in r.items() if k != "examples"}, indent=2))
    if not a.apply and r["found"]:
        from pathlib import Path as _P
        for e in r["examples"]:
            print(f'   {_P(e["from"]).name}  ->  {_P(e["to"]).name}')
        print("re-run with --apply to rename them")

def cmd_playlists(a):
    from .playlists import build
    r = build(systems=a.system or None, keep_only=a.keep_only, replace=a.replace)
    if r.get("error"):
        print(r["error"]); return 1
    print(f'wrote {r["total"]:,} entries into {r["dest"]}')
    for system, n in sorted(r["written"].items(), key=lambda kv: -kv[1]):
        print(f'   {system:<14} {n:>7,}')
    for system, n in (r.get("unloadable") or {}).items():
        print(f'   {system:<14} {n:>7,} listed but wear a decoy extension — RetroArch will not load them')
    for system, why in r.get("skipped", {}).items():
        print(f'   {system:<14} skipped: {why}')

def cmd_audit(a):
    from .audit import audit
    r = audit(fix=a.fix)
    print(json.dumps({k: v for k, v in r.items() if k != "examples"}, indent=2))
    if not a.fix and r["status_changes"]:
        print(f'{r["status_changes"]} claim(s) disagree with the evidence; re-run with --fix')

def cmd_prove(a):
    from .mameset import prove
    r = prove(progress=lambda n, t: print(f"  verifying {n}/{t}..."))
    if r.get("error"):
        print(r["error"]); return 1
    from .mameset import record_proof
    rec = record_proof(r) if a.record else {}
    print(json.dumps({"checked": r["checked"], "good": r["good_count"],
                      "best_available": r["best_count"], "bad": r["bad_count"],
                      "runnable": len(r["runnable"]), **rec}, indent=2))
    for s, why in list((r.get("bad") or {}).items())[:10]:
        print(f"  {s}: {why}")

def cmd_displays(a):
    from . import player
    ds = player.displays()
    if not ds:
        print("no displays detected"); return
    for d in ds:
        star = "  (primary)" if d["primary"] else ""
        print(f'  {d["index"]}  {d["device"]:<15} {d["name"]}{star}')
    print()
    print("  set it in emulators.yaml:  display: <index>")

def cmd_playable(a):
    from .mameset import refresh_playable
    print(json.dumps({"playable": refresh_playable()}, indent=2))


def cmd_romset(a):
    from .organizer import arcade_card, romset
    last = [0]
    def progress(i, total, name):
        if i == 0 or i >= last[0] + 200 or i >= total:
            last[0] = i
            print(f"[{i}/{total}] {name}")
    if a.profile == "card":
        r = arcade_card(a.dest, progress=progress, arcade_status=a.status,
                        exclude_labels=_exclude(a.exclude_labels), versions=a.versions,
                        prune=not a.keep_dropped, artwork=not a.no_artwork)
        for core, rep in r["cores"].items():
            print(core, json.dumps({k: rep.get(k) for k in ("complete", "bytes", "clones_dropped",
                                                            "deferred", "pruned_files", "media",
                                                            "error", "stopped")}, indent=2))
        return
    r = romset(a.profile, a.dest, leave_bytes=int(float(a.leave_gb) * 1024 ** 3),
               progress=progress, arcade_status=a.status,
               exclude_labels=_exclude(a.exclude_labels), versions=a.versions,
               prune=not a.keep_dropped, artwork=not a.no_artwork,
               defer_to=[c for c in (a.defer_to or "").split(",") if c] or None)
    print(json.dumps({k: v for k, v in r.items() if k != "sets"}, indent=2))

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

def _exclude(text):
    """--exclude-labels: None keeps the default leave-out, 'none' an empty one."""
    if text is None:
        return None
    return [] if text.strip().lower() == "none" else [x.strip() for x in text.split(",") if x.strip()]

def cmd_labels(a):
    from . import labels
    if a.action == "refresh":
        print(json.dumps(labels.refresh(), indent=2))
    elif a.action == "counts":
        print(json.dumps(labels.counts(held_only=not a.all), indent=2))
    else:
        if not (a.item and a.label):
            print("labels add|remove|clear needs --item and --label"); return 1
        labels.set_override(a.item, a.label, None if a.action == "clear" else a.action)
        print(json.dumps(labels.for_items([a.item]), indent=2))

def cmd_ps3(a):
    from pathlib import Path
    from . import ps3
    if a.action == "refresh":
        print(json.dumps(ps3.refresh(), indent=2))
    elif a.action == "status":
        print(json.dumps(ps3.status(), indent=2))
    elif a.action == "firmware":
        print(json.dumps(ps3.firmware(refresh=True), indent=2))
    elif a.action == "inspect":
        if not a.iso:
            print("ps3 inspect needs an .iso"); return 1
        print(json.dumps(ps3.inspect(a.iso), indent=2))
    else:
        if not a.iso:
            print("ps3 decrypt needs an .iso (and optionally an output path)"); return 1
        info = ps3.inspect(a.iso)
        if not info.get("ps3"):
            print(f"{a.iso} is not a PS3 disc image"); return 1
        if not info["already_decrypted"] and not info["fits"]:
            print("no key in the local mirror opens this disc (try: romcom ps3 refresh)"); return 1
        out = a.out or str(Path(a.iso).with_name(Path(a.iso).stem + " (decrypted).iso"))
        key = None if info["already_decrypted"] else info["key"]["key"]
        print(json.dumps(ps3.decrypt(a.iso, out, key) if key else {"already": True}, indent=2))

def cmd_organize(a):
    from .organizer import organize
    print(json.dumps(organize(a.dest, systems=a.system or None,
                             wanted_only=a.wanted_only, keep_only=a.keep_only, sources=a.source or None,
                             rating_min=int(settings().get("export_rating_min", 0) or 0),
                             fill=a.fill, dry_run=a.dry_run,
                             arcade_status=a.arcade_status,
                             exclude_labels=_exclude(a.exclude_labels),
                             layout=a.layout, chd=not a.no_chd), indent=2))


def cmd_artwork(a):
    from . import artwork
    if a.action == "fetch":
        last = [0]
        def progress(i, total, msg):
            if i >= last[0] + 500 or i >= total:
                last[0] = i
                print(msg)
        r = artwork.fetch(systems=a.system or None, kinds=a.kind or artwork.DEFAULT_KINDS,
                          refresh=a.refresh, progress=progress)
    elif a.action == "export":
        if not a.dest:
            print("artwork export needs a card folder, e.g. romcom artwork export J:\\")
            return 1
        r = artwork.export_media(a.dest, kinds=a.kind or artwork.DEFAULT_KINDS,
                                 media_dir=a.media_dir, systems=a.system or None)
    else:
        r = artwork.coverage()
    print(json.dumps(r, indent=2))


def cmd_maintain(a):
    from . import maintain
    if a.list:
        for n, w in maintain.STEPS:
            print(f"  {n:9} {w}")
        return
    r = maintain.run(steps=a.step or None,
                     progress=lambda i, t, n: print(f"[{i}/{t}] {n}", flush=True))
    print(json.dumps(r, indent=2, default=str))


def cmd_discs(a):
    from . import discs
    r = discs.compact(apply=a.apply, convert=a.convert is not None,
                      limit=a.convert or None,
                      progress=lambda i, t, n: print(f"[{i}/{t}] {n}", flush=True))
    print(json.dumps(r, indent=2, default=str))
    if not a.apply:
        print(f"{r['chds']} disc archive(s) would be replaced by their CHD "
              f"({r['planned_bytes'] / 1e9:.1f} GB) — run again with --apply")


def cmd_chd_cache(a):
    from .organizer import precache_chds
    excl = _exclude(a.exclude_labels)
    if a.profile:
        from . import exportplan
        prof = next((p for p in exportplan.list_profiles()
                     if a.profile in (p["id"], p["name"])), None)
        if not prof:
            print(f"no export profile named {a.profile!r}")
            return
        excl = prof.get("exclude_labels")
    r = precache_chds(a.system, exclude_labels=excl,
                      progress=lambda i, t, n: print(f"[{i + 1}/{t}] {n}", flush=True))
    print(json.dumps(r, indent=2))


def cmd_arcade_fetch(a):
    from . import arcadefetch
    sets = list(a.sets or [])
    if a.broken:
        sets += arcadefetch.broken_fixable(a.core)
    if not sets:
        print("name the sets to fetch, or --broken for every owned broken set on --core"); return 1
    if a.limit:
        sets = sets[:a.limit]
    r = arcadefetch.fetch_sets(sets, progress=print, core=a.core or "mame")
    for x in r["sets"]:
        if x["chips"] or x["disks"] or x["missing"] or x["errors"]:
            print(f"{x['set']}: +{len(x['chips'])} chip(s) +{len(x['disks'])} CHD(s)"
                  + (f" from {', '.join(x['sources'][:3])}" if x["sources"] else "")
                  + (f"; still missing {', '.join(x['missing'][:6])}" if x["missing"] else "")
                  + (f"; errors {'; '.join(x['errors'][:2])}" if x["errors"] else ""))
    print(json.dumps({k: r.get(k) for k in ("chips", "disks", "dest", "scan", "health")}, indent=2))

def cmd_arcade_health(a):
    from . import arcadehealth
    if a.mark:
        setname, verdict = a.mark
        print(json.dumps(arcadehealth.set_verdict(
            setname, None if verdict == "clear" else verdict, core=a.core or "*",
            note=a.note), indent=2))
        return
    if not a.cached:
        if a.core:
            arcadehealth.evaluate(a.core)
        else:
            arcadehealth.evaluate_all(progress=lambda i, t, msg: print(msg))
    print(json.dumps(arcadehealth.summary(), indent=2))

def cmd_recommend(a):
    from . import recommend
    rows = recommend.top(system=a.system, n=a.n, include_played=a.include_played)
    if not rows:
        print("nothing in hand to recommend — sync community scores or acquire some games first")
        return
    for i, r in enumerate(rows, 1):
        tag = f"  [{r['system']}]" if r["system"] else ""
        year = f" ({r['year']})" if r["year"] else ""
        print(f"{i:2}. {r['title']}{year}{tag}  - {r['how']} (rank {r['rank']})")

def cmd_community(a):
    from . import community
    if a.action == "test":
        print(json.dumps(community.test(), indent=2)); return
    if a.action == "status":
        print(json.dumps(community.status(), indent=2)); return
    report = community.sync(systems=a.system or None,
                            progress=lambda done, total, slug: print(f"[{done}/{total}] {slug}"))
    print(json.dumps(report, indent=2))

def main():
    # A Windows console speaks cp1252: one Japanese game name in a progress line used to
    # raise mid-scan and end the whole import. Unprintable characters print as '?'.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
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
    fn=s.add_parser("fix-names", help="give roms back their real extension")
    fn.add_argument("--apply", action="store_true")
    fn.set_defaults(fn=cmd_fixnames)
    pls=s.add_parser("playlists", help="write RetroArch playlists from this catalog")
    pls.add_argument("--system", action="append")
    pls.add_argument("--keep-only", action="store_true", help="only items marked keep")
    pls.add_argument("--replace", action="store_true",
                     help="overwrite RetroArch's own playlists instead of writing beside them")
    pls.set_defaults(fn=cmd_playlists)
    au=s.add_parser("audit", help="check every claim against the evidence")
    au.add_argument("--fix", action="store_true", help="apply the corrections")
    au.set_defaults(fn=cmd_audit)
    pv=s.add_parser("prove", help="have MAME verify the arcade sets it would run")
    pv.add_argument("--record", action="store_true",
                    help="write MAME's verdict into the catalog")
    pv.set_defaults(fn=cmd_prove)
    dp=s.add_parser("displays", help="list monitors games can be sent to")
    dp.set_defaults(fn=cmd_displays)
    pb=s.add_parser("playable", help="recompute which arcade sets can be assembled")
    pb.set_defaults(fn=cmd_playable)
    rs=s.add_parser("romset", help="build one core's arcade romset onto a folder, one zip per set")
    rs.add_argument("profile", choices=["card", "mame", "fbneo", "mame2003"],
                    help="which core's romset: card (FBNeo + MAME for the rest, onto a card root), mame (current MAME dat), fbneo, mame2003 (MAME 2003-Plus)")
    rs.add_argument("dest", help="target folder on the card, e.g. J:\\arcade-fbneo")
    rs.add_argument("--leave-gb", default="0",
                    help="gigabytes to leave free on the volume for what comes after")
    rs.add_argument("--status", choices=["no_broken", "works_only", "all"], default=None,
                    help="arcade health gate: skip broken sets (default), only working ones, or everything")
    rs.add_argument("--exclude-labels", default=None, help="labels to leave out, comma-separated (default fruit-machine,gambling,not-a-game; 'none' keeps everything)")
    rs.add_argument("--versions", type=int, default=1,
                    help="versions of each game to keep (best first: runs, proper English release, score); 0 keeps every clone")
    rs.add_argument("--keep-dropped", action="store_true",
                    help="leave set zips already in the folder that the build no longer chose")
    rs.add_argument("--defer-to", default="",
                    help="other cores on the same card, comma-separated: games their build carries are left out of this one (e.g. --defer-to fbneo)")
    rs.add_argument("--no-artwork", action="store_true",
                    help="skip the ES-DE gamelist and artwork copy")
    rs.set_defaults(fn=cmd_romset)
    lb=s.add_parser("labels", help="what each game is (genre, fruit-machine, hack…): refresh, counts, or your own add/remove")
    lb.add_argument("action", choices=["refresh", "counts", "add", "remove", "clear"])
    lb.add_argument("--item", help="item id, for add/remove/clear")
    lb.add_argument("--label", help="label, for add/remove/clear")
    lb.add_argument("--all", action="store_true", help="counts over the whole catalog, not just games in hand")
    lb.set_defaults(fn=cmd_labels)
    p3=s.add_parser("ps3", help="PS3 disc keys (DAT/PS3 dkeys), Sony's firmware (DAT/PS3 firmware), decrypting a disc")
    p3.add_argument("action", choices=["refresh", "status", "firmware", "inspect", "decrypt"])
    p3.add_argument("iso", nargs="?", help="a PS3 .iso, for inspect/decrypt")
    p3.add_argument("out", nargs="?", help="where decrypt writes (default '<name> (decrypted).iso' beside it)")
    p3.set_defaults(fn=cmd_ps3)
    aw=s.add_parser("artwork", help="box art / snaps for the games in hand, and ES-DE media on a card")
    aw.add_argument("action", choices=["fetch", "export", "coverage"])
    aw.add_argument("dest", nargs="?", help="card root, for export")
    aw.add_argument("--system", action="append")
    aw.add_argument("--kind", action="append", choices=["boxart", "snap", "title", "marquee"])
    aw.add_argument("--media-dir", help="ES-DE media folder (default <dest>/ES-DE/downloaded_media)")
    aw.add_argument("--refresh", action="store_true", help="re-check misses and re-read the indexes")
    aw.set_defaults(fn=cmd_artwork)
    mt=s.add_parser("maintain", help="library upkeep (the service runs it daily): import the incoming folder, attach and repair downloads, labels, artwork, duplicates")
    mt.add_argument("--step", action="append", help="run only these steps (see --list)")
    mt.add_argument("--list", action="store_true", help="list the steps")
    mt.set_defaults(fn=cmd_maintain)
    dc=s.add_parser("discs", help="keep disc games once, as CHD in the CHD library, deleting the archives they came from (after chdman verify)")
    dc.add_argument("--apply", action="store_true")
    dc.add_argument("--convert", type=int, nargs="?", const=0, default=None,
                    help="also convert discs not converted yet (at most N; no N: all)")
    dc.set_defaults(fn=cmd_discs)
    cc=s.add_parser("chd-cache", help="convert a system's discs to CHD ahead of a card export (they are kept for every later card)")
    cc.add_argument("system", nargs="+")
    cc.add_argument("--exclude-labels", default=None, help="labels to leave out, comma-separated, as the export will")
    cc.add_argument("--profile", default=None, help="take the leave-out labels from this saved export profile (name or id)")
    cc.set_defaults(fn=cmd_chd_cache)
    af=s.add_parser("arcade-fetch", help="fetch the chips, devices, bios and CHDs arcade sets lack, by setname, from archive.org's MAME items")
    af.add_argument("sets", nargs="*")
    af.add_argument("--broken", action="store_true", help="every owned set the health check calls broken for want of a download")
    af.add_argument("--core", default="mame", choices=["mame", "fbneo", "mame2003"])
    af.add_argument("--limit", type=int, default=0)
    af.set_defaults(fn=cmd_arcade_fetch)
    ah=s.add_parser("arcade-health",
                    help="judge which arcade sets will run on each core (works/partial/broken)")
    ah.add_argument("--core", choices=["mame", "fbneo", "mame2003"],
                    help="judge one core only (default: every core with a dat)")
    ah.add_argument("--cached", action="store_true",
                    help="show the last stored judgement without re-reading the dats")
    ah.add_argument("--mark", nargs=2, metavar=("SET", "VERDICT"),
                    help="record your own verdict after playing: works|partial|broken|clear "
                         "(applies to --core, or every core)")
    ah.add_argument("--note", help="why, for --mark")
    ah.set_defaults(fn=cmd_arcade_health)
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
    og.add_argument("--fill",action="store_true",help="copy until the card's free-space reserve instead of refusing when the selection will not fit")
    og.add_argument("--dry-run",action="store_true",help="report what would be copied, copy nothing")
    og.add_argument("--arcade-status",choices=["no_broken","works_only","all"],default=None,help="arcade health gate: skip broken sets (default), only working ones, or everything")
    og.add_argument("--exclude-labels",default=None,help="labels to leave out, comma-separated (default fruit-machine,gambling,not-a-game; 'none' keeps everything)")
    og.add_argument("--layout",choices=["flat","ranked"],default="flat",help="flat: every game in its ES-DE system folder (default); ranked: <system>/<score>/ folders")
    og.add_argument("--no-chd",action="store_true",help="copy/unpack disc images as they are instead of converting .cue/.gdi (and PS2 .iso) to CHD")
    og.set_defaults(fn=cmd_organize)
    rec=s.add_parser("recommend", help="the n best games to play next, in hand now")
    rec.add_argument("--system", help="one system, or omit for the whole shelf")
    rec.add_argument("--n", type=int, default=5)
    rec.add_argument("--include-played", action="store_true", help="let already-played games back in")
    rec.set_defaults(fn=cmd_recommend)
    cm=s.add_parser("community", help="crowd scores from public pools (RAWG ratings, RetroAchievements popularity)")
    cm.add_argument("action", choices=["sync","status","test"], nargs="?", default="status")
    cm.add_argument("--system", action="append", help="limit a sync to these systems")
    cm.set_defaults(fn=cmd_community)
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
    du=s.add_parser("dupes"); du.add_argument("--apply",action="store_true"); du.add_argument("--system",action="append"); du.set_defaults(fn=cmd_dupes)
    pr=s.add_parser("prune", help="drop dump-variant junk and keep the best dump per game")
    pr.add_argument("--apply",action="store_true")
    pr.add_argument("--system",action="append",help="limit to these systems (required with --apply)")
    pr.add_argument("--per-region",action="store_true",
                    help="official mode: keep the best dump per REGION per game (latest revision, no betas)")
    pr.set_defaults(fn=cmd_prune)
    w=s.add_parser("web"); w.add_argument("--host",default="127.0.0.1"); w.add_argument("--port",type=int,default=8927); w.add_argument("--no-browser",action="store_true"); w.set_defaults(fn=cmd_web)
    cs=s.add_parser("catalog-status"); cs.add_argument("--strict",action="store_true"); cs.set_defaults(fn=cmd_catalog_status)
    cc=s.add_parser("cache"); cc.add_argument("action",choices=["stats","clear"],nargs="?",default="stats"); cc.add_argument("--source"); cc.set_defaults(fn=cmd_cache)
    vm=s.add_parser("vimm"); vm.add_argument("action",choices=["capture"],nargs="?",default="capture"); vm.set_defaults(fn=cmd_vimm)
    rc=s.add_parser("reconcile"); rc.add_argument("--reacquire",action="store_true"); rc.add_argument("--system")
    rc.add_argument("--wrong-downloads",action="store_true",help="website downloads that are another game or another platform")
    rc.add_argument("--stub-discs",action="store_true",help="disc games whose only file is an archive too small to hold the disc")
    rc.add_argument("--unclaimed-downloads",action="store_true",help="downloaded games no file claims, paired with the file the acquirer saved (by exact size)")
    rc.add_argument("--apply",action="store_true",help="with --wrong-downloads / --stub-discs: let go of them and re-arm the items")
    rc.set_defaults(fn=cmd_reconcile)
    a=p.parse_args(); a.fn(a)

if __name__=="__main__": main()
