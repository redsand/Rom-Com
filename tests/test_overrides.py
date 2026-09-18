from pathlib import Path
from romcom.db import connect
from romcom.seed import apply_overrides

def test_settable_fields_exist(tmp_path,monkeypatch):
    monkeypatch.setenv("ROMCOM_DB",str(tmp_path/"db.sqlite"))
    db=connect()
    with db:
        db.execute("INSERT INTO items(id,title) VALUES('x','X')")
        db.execute("UPDATE items SET authorized=1,wanted=0 WHERE id='x'")
    r=connect().execute("SELECT authorized,wanted FROM items WHERE id='x'").fetchone()
    assert r["authorized"]==1 and r["wanted"]==0
