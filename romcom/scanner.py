from pathlib import Path
import hashlib, re, zlib
from .db import connect
from .status import promote

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

def _name_match(db,path):
    n=_norm(path.name)
    if not n: return None
    rows=db.execute("SELECT id,title FROM items WHERE wanted=1").fetchall()
    for r in rows:
        if _norm(r["title"])==n: return r["id"]
    for r in db.execute("SELECT item_id,alias FROM aliases"):
        if _norm(r["alias"])==n: return r["item_id"]
    return None

def scan(root,name_match=True):
    db=connect(); root=Path(root); count=matched=verified=0
    with db:
        for p in root.rglob("*"):
            if not p.is_file(): continue
            st=p.stat(); crc,md5,sha1=digest_file(p)
            match=db.execute("""SELECT item_id FROM file_hashes
              WHERE (algorithm='sha1' AND digest=?) OR (algorithm='md5' AND digest=?) OR (algorithm='crc' AND digest=?)
              LIMIT 1""",(sha1,md5,crc)).fetchone()
            item_id=match["item_id"] if match else None
            method="hash" if item_id else None
            if not item_id and name_match:
                item_id=_name_match(db,p); method="filename-exact" if item_id else None
            if item_id:
                matched+=1
                if method=="hash":
                    promote(db,item_id,"VERIFIED"); verified+=1
                else:
                    promote(db,item_id,"FOUND")
                db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",(item_id,"scan-match",f"{method}: {p}"))
            db.execute("""INSERT INTO files(path,bytes,mtime,crc32,md5,sha1,matched_item_id,match_method,scanned_at)
              VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
              ON CONFLICT(path) DO UPDATE SET bytes=excluded.bytes,mtime=excluded.mtime,crc32=excluded.crc32,
              md5=excluded.md5,sha1=excluded.sha1,matched_item_id=excluded.matched_item_id,
              match_method=excluded.match_method,scanned_at=CURRENT_TIMESTAMP""",
              (str(p),st.st_size,st.st_mtime,crc,md5,sha1,item_id,method)); count+=1
    return {"files":count,"matched":matched,"verified":verified}
