import sqlite3
from romcom.db import connect

def test_existing_database_migrates(tmp_path,monkeypatch):
    p=tmp_path/"old.db"
    db=sqlite3.connect(p)
    db.execute("CREATE TABLE items(id TEXT PRIMARY KEY,title TEXT NOT NULL,authorized INTEGER DEFAULT 0,wanted INTEGER DEFAULT 1,status TEXT DEFAULT 'CATALOGED')")
    db.commit(); db.close()
    monkeypatch.setenv("ROMCOM_DB",str(p))
    db=connect()
    cols={r["name"] for r in db.execute("PRAGMA table_info(items)")}
    assert "catalog_source" in cols
    assert "play_status" in cols
