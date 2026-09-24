import sqlite3
from romcom import db as dbmod
from romcom.db import connect

CHAT_TABLES = {"chat_sessions", "chat_messages", "chat_facts", "chat_memory_chunks",
               "chat_approvals", "chat_tool_log", "web_sessions"}

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

def test_chat_tables_arrive_without_disturbing_existing_ones(tmp_path,monkeypatch):
    """The assistant's tables are new tables, so an existing database gains them on the
    next connect() with no ALTER against anything already there. That matters because the
    first connect() after this change is the user's live 2 GB database, with the acquire
    watcher writing to it — an ALTER on `items` would be a schema rewrite under a live
    writer, and a dropped column would lose catalog data."""
    p=tmp_path/"old.db"
    db=sqlite3.connect(p)
    db.execute("CREATE TABLE items(id TEXT PRIMARY KEY,title TEXT NOT NULL,authorized INTEGER DEFAULT 0,"
               "wanted INTEGER DEFAULT 1,status TEXT DEFAULT 'CATALOGED')")
    db.execute("INSERT INTO items(id,title) VALUES('nes/x','X')")
    before={r[1] for r in db.execute("PRAGMA table_info(items)")}  # r[0] is cid, r[1] is the name
    db.commit(); db.close()
    monkeypatch.setenv("ROMCOM_DB",str(p))
    db=connect()
    tables={r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert CHAT_TABLES <= tables
    after={r["name"] for r in db.execute("PRAGMA table_info(items)")}
    assert before <= after, "existing columns must survive; additions only"
    assert db.execute("SELECT title FROM items WHERE id='nes/x'").fetchone()["title"]=="X"

def test_chat_schema_is_idempotent(tmp_path,monkeypatch):
    """connect() runs the schema once per db path per process, so a second call in the
    same process can't prove re-running is safe. Force it: the real risk is a later
    restart re-executing SCHEMA against a database that already has the tables."""
    p=tmp_path/"new.db"
    monkeypatch.setenv("ROMCOM_DB",str(p))
    db=connect()
    dbmod._INITIALIZED.discard(str(p))   # pretend this is a fresh process
    db=connect()                          # re-runs SCHEMA + migrate against a populated db
    tables={r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert CHAT_TABLES <= tables
    idx={r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_chat_chunks_model" in idx


def test_rating_and_community_columns_migrate_without_losing_data(tmp_path,monkeypatch):
    """The ranking signals are new columns on `items`, so an existing database gains them
    by ALTER on the next connect(). `rating` is deliberately nullable: NULL means never
    auditioned, while 0 would itself be a verdict."""
    p=tmp_path/"old.db"
    db=sqlite3.connect(p)
    db.execute("CREATE TABLE items(id TEXT PRIMARY KEY,title TEXT NOT NULL,authorized INTEGER DEFAULT 0,"
               "wanted INTEGER DEFAULT 1,status TEXT DEFAULT 'CATALOGED',keep INTEGER DEFAULT 0)")
    db.execute("INSERT INTO items(id,title,keep) VALUES('snes/x','X',1)")
    db.commit(); db.close()
    monkeypatch.setenv("ROMCOM_DB",str(p))
    db=connect()
    cols={r["name"] for r in db.execute("PRAGMA table_info(items)")}
    assert {"rating","community_score","community_source"} <= cols
    row=db.execute("SELECT title,keep,rating FROM items WHERE id='snes/x'").fetchone()
    assert row["title"]=="X" and row["keep"]==1 and row["rating"] is None
    tables={r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"community_scores","community_state"} <= tables
    # and the raw per-source rows keep a foreign key back to items
    with db:
        db.execute("INSERT INTO community_scores(item_id,source,score,votes) VALUES('snes/x','rawg',88,40)")
    assert db.execute("SELECT score FROM community_scores WHERE item_id='snes/x'").fetchone()["score"]==88
