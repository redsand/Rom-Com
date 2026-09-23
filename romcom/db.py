import sqlite3
import threading
from .config import settings

# Artwork/support extensions that shouldn't make an item count as "content on disk".
# Kept here (not in report.py) so the scanner, the migration backfill, and the reports all
# agree on one definition. `files.content` is 1 for real content, 0 for these — a stored,
# indexed flag so "what's on disk" is an index scan instead of a lower(path) NOT LIKE sweep
# over hundreds of thousands of rows on every dashboard load.
NOT_CONTENT_EXTS = ("png", "jpg", "jpeg", "gif", "bmp", "ico", "pdf", "mp4", "avi",
                    "txt", "nfo", "xml", "html")
_CONTENT_EXPR = ("CASE WHEN " + " AND ".join(f"lower(path) NOT LIKE '%.{e}'" for e in NOT_CONTENT_EXTS)
                 + " THEN 1 ELSE 0 END")

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS items (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, system TEXT, series TEXT,
 series_number INTEGER, year INTEGER, authorized INTEGER NOT NULL DEFAULT 0,
 wanted INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'CATALOGED',
 preferred_runtime TEXT, source TEXT, notes TEXT,
 catalog_source TEXT, external_id TEXT, support_level TEXT,
 region TEXT, language TEXT, play_status TEXT NOT NULL DEFAULT 'UNPLAYED',
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS aliases (
 item_id TEXT NOT NULL, alias TEXT NOT NULL,
 UNIQUE(item_id,alias), FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS file_hashes (
 item_id TEXT NOT NULL, algorithm TEXT NOT NULL, digest TEXT NOT NULL,
 UNIQUE(item_id,algorithm,digest),
 FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS volumes (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, authorized INTEGER NOT NULL DEFAULT 0,
 estimated_bytes INTEGER, status TEXT NOT NULL DEFAULT 'CATALOGED',
 min_bytes INTEGER, max_bytes INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS volume_search (
 volume_id TEXT NOT NULL, query TEXT NOT NULL,
 UNIQUE(volume_id,query), FOREIGN KEY(volume_id) REFERENCES volumes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS volume_covers (
 volume_id TEXT NOT NULL, item_id TEXT NOT NULL,
 PRIMARY KEY(volume_id,item_id),
 FOREIGN KEY(volume_id) REFERENCES volumes(id) ON DELETE CASCADE,
 FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
 nzo_id TEXT, result_title TEXT, result_url TEXT, bytes INTEGER, status TEXT,
 source TEXT, queued_at TEXT, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS files (
 path TEXT PRIMARY KEY, bytes INTEGER, mtime REAL, crc32 TEXT, md5 TEXT, sha1 TEXT,
 matched_item_id TEXT, match_method TEXT, scanned_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS web_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, params TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, event TEXT NOT NULL,
 detail TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS app_settings (
 key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS search_cache (
 source TEXT NOT NULL, cache_key TEXT NOT NULL, results TEXT NOT NULL,
 fetched_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(source, cache_key)
);
-- Assistant chat. These are NEW tables, so they need no MIGRATIONS entry: an existing
-- database picks them up on its first connect() after the upgrade, exactly how
-- app_settings arrived. Nothing here ALTERs a table the live watcher is writing to.
CREATE TABLE IF NOT EXISTS chat_sessions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, summary TEXT,
 summarized_to INTEGER NOT NULL DEFAULT 0,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
 role TEXT NOT NULL, content TEXT, thinking TEXT, tool_name TEXT, tool_args TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chat_facts (
 key TEXT PRIMARY KEY, value TEXT NOT NULL, source TEXT,
 confidence REAL DEFAULT 1.0,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT
);
-- `dim` and `model` are the dimension lock: a vector is only ever compared against
-- vectors written by the same model, so swapping embed models can never silently
-- cosine against foreign-dimension rows (it's refused at insert instead).
-- `norm` is precomputed so cosine is a bare dot product over array('f').
CREATE TABLE IF NOT EXISTS chat_memory_chunks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref_id TEXT,
 text TEXT NOT NULL, embedding BLOB NOT NULL, dim INTEGER NOT NULL,
 model TEXT NOT NULL, norm REAL NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_accessed TEXT
);
-- Text that should become a memory chunk but could not be embedded yet, because the
-- embedding model was unreachable at the moment it was produced. Without this the chunk was
-- dropped by a bare `except: pass` while the summary watermark had already advanced, so that
-- slice of conversation could never become recallable memory again -- a silent, permanent
-- hole in long-term memory whenever Ollama happened to be down.
CREATE TABLE IF NOT EXISTS chat_memory_pending (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ref_id TEXT,
 text TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chat_approvals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER,
 tool TEXT NOT NULL, arguments TEXT NOT NULL, summary TEXT,
 status TEXT NOT NULL DEFAULT 'pending', result TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_tool_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, tool TEXT NOT NULL,
 arguments TEXT, ok INTEGER, risk TEXT, result_summary TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- Web login tokens. The token is stored HASHED, so a database dump yields nothing
-- usable; the plaintext exists only in the user's cookie.
CREATE TABLE IF NOT EXISTS web_sessions (
 token_hash TEXT PRIMARY KEY, user TEXT NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL, last_seen TEXT
);
"""

# ALTER TABLE has stricter default-expression rules than CREATE TABLE.
MIGRATIONS = {
 "items": {
   "system":"TEXT","series":"TEXT","series_number":"INTEGER","year":"INTEGER",
   "authorized":"INTEGER NOT NULL DEFAULT 0","wanted":"INTEGER NOT NULL DEFAULT 1",
   "status":"TEXT NOT NULL DEFAULT 'CATALOGED'","preferred_runtime":"TEXT",
   "source":"TEXT","notes":"TEXT","catalog_source":"TEXT","external_id":"TEXT",
   "support_level":"TEXT","region":"TEXT","language":"TEXT",
   "play_status":"TEXT NOT NULL DEFAULT 'UNPLAYED'","updated_at":"TEXT"
 },
 "volumes": {"min_bytes":"INTEGER","max_bytes":"INTEGER","updated_at":"TEXT"},
 "jobs": {"result_url":"TEXT","source":"TEXT"},
 "files": {"match_method":"TEXT","scanned_at":"TEXT","content":"INTEGER"}
}

INDEXES = [
 "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_external ON items(catalog_source,external_id)",
 "CREATE INDEX IF NOT EXISTS idx_items_status ON items(status)",
 "CREATE INDEX IF NOT EXISTS idx_items_system ON items(system)",
 "CREATE INDEX IF NOT EXISTS idx_items_series ON items(series)",
 "CREATE INDEX IF NOT EXISTS idx_file_hash_lookup ON file_hashes(algorithm,digest)",
 "CREATE INDEX IF NOT EXISTS idx_jobs_nzo ON jobs(nzo_id)",
 "CREATE INDEX IF NOT EXISTS idx_files_matched ON files(matched_item_id)",
 # The events table grows into the millions (a scan-match row per matched file), and the
 # acquirer's cooldown/eligibility queries filter it by event + recency and group it by
 # item. Without these two indexes those queries full-scan millions of rows on every
 # sweep and every Acquire-tab load (measured ~1s each); with them they're range scans.
 "CREATE INDEX IF NOT EXISTS idx_events_event_created ON events(event, created_at)",
 "CREATE INDEX IF NOT EXISTS idx_events_item_event ON events(item_id, event, created_at)",
 # "what content is on disk" — the dashboard's hottest query. Indexed on the stored flag
 # so it's a range scan, not a full-table path sweep.
 "CREATE INDEX IF NOT EXISTS idx_files_content ON files(content, matched_item_id)",
 # Assistant chat. History is fetched per session and paged by id, approvals are looked
 # up pending-per-session, and recall always filters by model (the dimension lock), so
 # model is the leading column there.
 "CREATE INDEX IF NOT EXISTS idx_chat_msg_session ON chat_messages(session_id, id)",
 "CREATE INDEX IF NOT EXISTS idx_chat_chunks_model ON chat_memory_chunks(model)",
 "CREATE INDEX IF NOT EXISTS idx_chat_approvals ON chat_approvals(session_id, status)",
 "CREATE INDEX IF NOT EXISTS idx_chat_log_session ON chat_tool_log(session_id, id)",
 "CREATE INDEX IF NOT EXISTS idx_web_sessions_expires ON web_sessions(expires_at)"
]

def _columns(db,table):
    return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}

def migrate(db):
    for table,cols in MIGRATIONS.items():
        existing=_columns(db,table)
        for name,decl in cols.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    for sql in INDEXES: db.execute(sql)
    db.commit()

def backfill_content(db, batch=20000):
    """One-time fill of files.content for rows that predate the column. Batched with a
    commit per chunk so it never holds a long write lock — heavy backfills must not run in
    a request-path connect() or they contend with the live watcher (a single 349k-row
    UPDATE there stalls the whole server). Call this once at startup, before the watcher
    starts. Returns rows filled. A no-op (one cheap indexed read) once everything is set."""
    if not db.execute("SELECT 1 FROM files WHERE content IS NULL LIMIT 1").fetchone():
        return 0
    filled = 0
    while True:
        rows = [r[0] for r in db.execute(
            "SELECT rowid FROM files WHERE content IS NULL LIMIT ?", (batch,))]
        if not rows:
            break
        qs = ",".join("?" * len(rows))
        with db:
            db.execute(f"UPDATE files SET content={_CONTENT_EXPR} WHERE rowid IN ({qs})", rows)
        filled += len(rows)
    return filled

_INIT_LOCK = threading.Lock()
_INITIALIZED = set()   # db paths that have had schema+migrate applied THIS process

def connect():
    """Open a connection. Schema creation + migration run ONCE per db path per process —
    not on every connect. The acquirer opens a connection per worker per item; running
    executescript(SCHEMA)+migrate() every time was pure write-lock churn against the live
    watcher. `wal_autocheckpoint=1000` keeps the WAL from growing unbounded; a periodic
    truncating checkpoint (web.serve) is the belt-and-braces for write-heavy bursts."""
    path = settings()["db"]
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    if path not in _INITIALIZED:
        with _INIT_LOCK:
            if path not in _INITIALIZED:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA wal_autocheckpoint=1000")
                db.executescript(SCHEMA)
                migrate(db)
                _INITIALIZED.add(path)
    return db

def checkpoint(db=None):
    """Fold the WAL back into the main db and reset it (TRUNCATE). Run periodically under a
    write-heavy watcher: without it the WAL can grow to gigabytes and every read slows to a
    crawl. Returns the pragma result (busy, log_pages, checkpointed)."""
    own = db is None
    if own:
        db = connect()
    try:
        return tuple(db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    finally:
        if own:
            db.close()
