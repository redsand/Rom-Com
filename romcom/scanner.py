from pathlib import Path
import hashlib, re, zipfile, zlib
from .db import connect, NOT_CONTENT_EXTS
from .status import promote, own

CHUNK=1024*1024

def digest_file(path):
    md5=hashlib.md5(); sha1=hashlib.sha1(); crc=0
    with open(path,"rb") as f:
        while True:
            b=f.read(CHUNK)
            if not b: break
            md5.update(b); sha1.update(b); crc=zlib.crc32(b,crc)
    return f"{crc & 0xffffffff:08x}",md5.hexdigest(),sha1.hexdigest()

def _norm(s):
    s=Path(s).stem.lower()
    s=re.sub(r"[\(\[].*?[\)\]]"," ",s)
    return re.sub(r"[^a-z0-9]+"," ",s).strip()

def _name_index(db):
    """One normalized-name -> item_id lookup, built once per scan (the alias table is ~1M rows)."""
    idx={}
    for r in db.execute("SELECT item_id,alias FROM aliases"):
        n=_norm(r["alias"])
        if n: idx.setdefault(n,r["item_id"])
    for r in db.execute("SELECT id,title FROM items WHERE wanted=1"):
        n=_norm(r["title"])
        if n: idx[n]=r["id"]  # wanted titles win over generic aliases
    return idx

def _zip_member_matches(db,p):
    """Match a zip by its members. Zip central directories store each member's CRC32 of the
    UNCOMPRESSED data, so candidates are found without decompressing; only candidate members
    are decompressed to confirm via md5/sha1 when the catalog has them."""
    strong_items=[]; weak=[]
    members=0
    try:
        with zipfile.ZipFile(p) as z:
            infos=[x for x in z.infolist() if not x.is_dir() and x.file_size]
            members=len(infos)
            for info in infos:
                crc=f"{info.CRC & 0xffffffff:08x}"
                cand=db.execute("SELECT item_id FROM file_hashes WHERE algorithm='crc' AND digest=? LIMIT 1",(crc,)).fetchone()
                if not cand: continue
                md5=hashlib.md5(); sha1=hashlib.sha1()
                with z.open(info) as fh:
                    while True:
                        b=fh.read(CHUNK)
                        if not b: break
                        md5.update(b); sha1.update(b)
                strong=db.execute("""SELECT item_id FROM file_hashes
                  WHERE (algorithm='sha1' AND digest=?) OR (algorithm='md5' AND digest=?) LIMIT 1""",
                  (sha1.hexdigest(),md5.hexdigest())).fetchone()
                if strong: strong_items.append(strong["item_id"])
                else: weak.append(cand["item_id"])
    except (zipfile.BadZipFile,OSError):
        pass
    # A CRC32 hit alone can be a collision (MAME chunk zips vs a million catalog hashes).
    # Accept it only for small No-Intro-style zips, or when two members agree on the same item.
    items=strong_items[:]
    for it,n in {w:weak.count(w) for w in weak}.items():
        if n>=2 or members<=4: items.append(it)
    return items

def scan(root,name_match=True,progress=None,rehash=False,adopt=True):
    db=connect(); root=Path(root); count=matched=verified=reused=0
    paths=[root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    idx=_name_index(db) if name_match else {}
    recent=[]
    def _stats():
        return {"matched":matched,"verified":verified,"reused":reused,"recent":list(recent)}
    for i,p in enumerate(paths):
        if progress: progress(i,len(paths),p.name,_stats())
        st=p.stat()
        prev=db.execute("SELECT bytes,mtime,crc32,md5,sha1,matched_item_id FROM files WHERE path=?",(str(p),)).fetchone()
        if prev and not rehash and prev["bytes"]==st.st_size and prev["mtime"]==st.st_mtime and prev["sha1"]:
            crc,md5,sha1=prev["crc32"],prev["md5"],prev["sha1"]; reused+=1
        else:
            crc,md5,sha1=digest_file(p)
        match=db.execute("""SELECT item_id FROM file_hashes
          WHERE (algorithm='sha1' AND digest=?) OR (algorithm='md5' AND digest=?) OR (algorithm='crc' AND digest=?)
          LIMIT 1""",(sha1,md5,crc)).fetchone()
        item_id=match["item_id"] if match else None
        method="hash" if item_id else None
        extra=[]
        if not item_id and p.suffix.lower()==".zip":
            zitems=_zip_member_matches(db,p)
            if zitems:
                item_id=zitems[0]; method="hash-zip"; extra=[x for x in set(zitems) if x!=item_id]
        if not item_id and name_match:
            item_id=idx.get(_norm(p.name)); method="filename-exact" if item_id else None
        if item_id:
            matched+=1
            hit=db.execute("SELECT title,system FROM items WHERE id=?",(item_id,)).fetchone()
            if hit:
                recent.append(f"{hit['title']} — {hit['system'] or '?'} [{method}]")
                if len(recent)>10: recent.pop(0)
            if method.startswith("hash"):
                for it in [item_id]+extra: promote(db,it,"VERIFIED")
                verified+=1
            else:
                promote(db,item_id,"FOUND")
            # Only record the match trail when it's actually new/changed. Re-scanning a
            # directory of already-matched, unchanged files must not re-insert a scan-match
            # event per file per pass — that grew the events table into the millions and
            # flooded the WAL. own() is a no-op write when the item is already owned.
            newly = prev is None or prev["matched_item_id"] != item_id
            for it in [item_id]+extra:
                own(db,it)  # having the file means we want and authorize the item
                if newly:
                    db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",(it,"scan-match",f"{method}: {p}"))
        content=0 if p.suffix.lower().lstrip(".") in NOT_CONTENT_EXTS else 1
        db.execute("""INSERT INTO files(path,bytes,mtime,crc32,md5,sha1,matched_item_id,match_method,content,scanned_at)
          VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
          ON CONFLICT(path) DO UPDATE SET bytes=excluded.bytes,mtime=excluded.mtime,crc32=excluded.crc32,
          md5=excluded.md5,sha1=excluded.sha1,matched_item_id=excluded.matched_item_id,
          match_method=excluded.match_method,content=excluded.content,scanned_at=CURRENT_TIMESTAMP""",
          (str(p),st.st_size,st.st_mtime,crc,md5,sha1,item_id,method,content)); count+=1
        if count%200==0: db.commit()  # interrupted scans keep their progress; unchanged files resume via reuse
    db.commit()
    out={"files":count,"matched":matched,"verified":verified,"reused":reused}
    if adopt:
        base=_stats()
        prog=(lambda i,t,n,s=None: progress(i,t,f"cataloging unmatched: {n}",(base|s) if s else base)) if progress else None
        a=adopt_unmatched(root=root,progress=prog)
        out|={"adopted":a["adopted"],"adopt_skipped":a["skipped"],
              "adopted_by_system":a["by_system"],"skipped_exts":a["skipped_exts"]}
    return out

# Extensions that pin down a system on their own; ambiguous ones (.bin/.iso/.cue/.zip) rely on folder names.
EXT_SYSTEM={
 ".nes":"nes",".sfc":"snes",".smc":"snes",".gb":"gb",".gbc":"gbc",".gba":"gba",
 ".n64":"n64",".z64":"n64",".v64":"n64",".vb":"virtualboy",".nds":"nds",".3ds":"3ds",".cia":"3ds",
 ".md":"genesis",".gen":"genesis",".smd":"genesis",".sms":"mastersystem",".gg":"gamegear",".32x":"32x",
 ".pce":"pcengine",".ws":"wonderswan",".wsc":"wonderswancolor",".ngp":"neogeopocket",".ngc":"neogeopocketcolor",
 ".a26":"atari2600",".a52":"atari5200",".a78":"atari7800",".lnx":"lynx",
 ".d64":"c64",".t64":"c64",".crt":"c64",".tap":"c64",".adf":"amiga",".ipf":"amiga",".lha":"amiga",
 ".st":"atarist",".stx":"atarist",".msx":"msx",".vpk":"vita",
}

def _known_systems():
    from .config import load_yaml
    cfg=load_yaml("catalogs.yaml")
    known={s for meta in cfg.get("catalogs",{}).values() for s in meta.get("systems",[])}
    return known|set(cfg.get("custom",{}).get("systems",[]))

def _detect_file_system(p,known,detect_system):
    for part in reversed(p.parent.parts):
        if re.sub(r"[^a-z0-9]+","",part.lower()) in known:
            return re.sub(r"[^a-z0-9]+","",part.lower())
    ext=EXT_SYSTEM.get(p.suffix.lower())
    if ext: return ext
    return detect_system(list(reversed(p.parent.parts))[:4])

def adopt_unmatched(root=None,progress=None):
    """Create catalog entries (source 'local') for scanned files that matched nothing, so
    downloaded content is never dropped from the library or the SD-card export."""
    from .catalog import detect_system
    known=_known_systems()
    db=connect()
    q="SELECT * FROM files WHERE matched_item_id IS NULL"; params=[]
    if root:
        q+=" AND path LIKE ?"; params=[str(Path(root))+"%"]
    rows=db.execute(q,params).fetchall()
    adopted=skipped=0; by_system={}; skipped_exts={}
    for i,r in enumerate(rows):
        p=Path(r["path"])
        if progress and i%100==0: progress(i,len(rows),p.name,{"adopted":adopted,"adopt_skipped":skipped})
        system=_detect_file_system(p,known,detect_system)
        if not system:
            skipped+=1
            ext=p.suffix.lower() or "(no extension)"
            skipped_exts[ext]=skipped_exts.get(ext,0)+1
            continue
        digest=r["sha1"] or r["md5"] or r["crc32"] or ""
        existing=db.execute("SELECT id FROM items WHERE catalog_source='local' AND external_id=?",(digest,)).fetchone()
        if existing:
            item_id=existing["id"]
        else:
            item_id=f"local-{system}-{digest[:12]}"
            db.execute("""INSERT INTO items(id,title,system,wanted,authorized,status,catalog_source,external_id)
              VALUES(?,?,?,1,1,'FOUND','local',?)""",(item_id,p.stem,system,digest))
        own(db,item_id)  # covers items adopted by an earlier scan (EXCLUDED ones stay excluded)
        db.execute("INSERT OR IGNORE INTO aliases(item_id,alias) VALUES(?,?)",(item_id,p.stem))
        for alg,d in (("crc",r["crc32"]),("md5",r["md5"]),("sha1",r["sha1"])):
            if d: db.execute("INSERT OR IGNORE INTO file_hashes(item_id,algorithm,digest) VALUES(?,?,?)",(item_id,alg,d))
        db.execute("UPDATE files SET matched_item_id=?,match_method='adopted' WHERE path=?",(item_id,r["path"]))
        adopted+=1; by_system[system]=by_system.get(system,0)+1
        if adopted%500==0: db.commit()
    db.commit()
    if progress: progress(len(rows),len(rows),"done")
    return {"unmatched":len(rows),"adopted":adopted,"skipped":skipped,
            "by_system":by_system,"skipped_exts":dict(sorted(skipped_exts.items(),key=lambda x:-x[1])[:12])}
