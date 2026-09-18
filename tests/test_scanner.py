import hashlib
from romcom.db import connect
from romcom.scanner import scan

def test_hash_scan_promotes_verified(tmp_path,monkeypatch):
    monkeypatch.setenv("ROMCOM_DB",str(tmp_path/"test.db")); db=connect()
    content=b"hello romcom"; sha1=hashlib.sha1(content).hexdigest()
    with db:
        db.execute("INSERT INTO items(id,title,status) VALUES('g','Game','CATALOGED')")
        db.execute("INSERT INTO file_hashes(item_id,algorithm,digest) VALUES('g','sha1',?)",(sha1,))
    root=tmp_path/"lib"; root.mkdir(); (root/"anything.bin").write_bytes(content)
    result=scan(root)
    assert result["verified"]==1
    assert connect().execute("SELECT status FROM items WHERE id='g'").fetchone()["status"]=="VERIFIED"
