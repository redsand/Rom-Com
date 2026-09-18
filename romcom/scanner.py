from pathlib import Path
import hashlib, os, zlib
from .db import connect

CHUNK=1024*1024

def digest_file(path):
    md5=hashlib.md5(); sha1=hashlib.sha1(); crc=0
    with open(path,"rb") as f:
        while True:
            b=f.read(CHUNK)
            if not b: break
            md5.update(b); sha1.update(b); crc=zlib.crc32(b,crc)
    return f"{crc & 0xffffffff:08x}",md5.hexdigest(),sha1.hexdigest()

def scan(root):
    db=connect(); root=Path(root); count=0
    with db:
        for p in root.rglob("*"):
            if not p.is_file(): continue
            st=p.stat(); crc,md5,sha1=digest_file(p)
            match=db.execute("SELECT item_id FROM hashes WHERE (algorithm='sha1' AND digest=?) OR (algorithm='md5' AND digest=?) OR (algorithm='crc32' AND digest=?) LIMIT 1",(sha1,md5,crc)).fetchone()
            db.execute("""INSERT INTO files(path,bytes,mtime,crc32,md5,sha1,matched_item_id)
                          VALUES(?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET bytes=excluded.bytes,mtime=excluded.mtime,
                          crc32=excluded.crc32,md5=excluded.md5,sha1=excluded.sha1,matched_item_id=excluded.matched_item_id""",
                       (str(p),st.st_size,st.st_mtime,crc,md5,sha1,match["item_id"] if match else None)); count+=1
    return count
