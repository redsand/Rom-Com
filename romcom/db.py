import sqlite3
from .config import settings

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
 queued_at TEXT, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS files (
 path TEXT PRIMARY KEY, bytes INTEGER, mtime REAL, crc32 TEXT, md5 TEXT, sha1 TEXT,
 matched_item_id TEXT, match_method TEXT, scanned_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, event TEXT NOT NULL,
 detail TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

MIGRATIONS = {
 "items": {
   "catalog_source":"TEXT","external_id":"TEXT","support_level":"TEXT",
   "region":"TEXT","language":"TEXT","play_status":"TEXT NOT NULL DEFAULT 'UNPLAYED'",
   "updated_at":"TEXT DEFAULT CURRENT_TIMESTAMP"
 },
 "volumes": {
   "min_bytes":"INTEGER","max_bytes":"INTEGER","updated_at":"TEXT DEFAULT CURRENT_TIMESTAMP"
 },
 "jobs": {"result_url":"TEXT"},
 "files": {"match_method":"TEXT","scanned_at":"TEXT DEFAULT CURRENT_TIMESTAMP"}
}

INDEXES = [
 "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_external ON items(catalog_source,external_id)",
 "CREATE INDEX IF NOT EXISTS idx_items_status ON items(status)",
 "CREATE INDEX IF NOT EXISTS idx_items_system ON items(system)",
 "CREATE INDEX IF NOT EXISTS idx_items_series ON items(series)",
 "CREATE INDEX IF NOT EXISTS idx_file_hash_lookup ON file_hashes(algorithm,digest)",
 "CREATE INDEX IF NOT EXISTS idx_jobs_nzo ON jobs(nzo_id)"
]

def _columns(db,table):
    return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}

def migrate(db):
    for table, cols in MIGRATIONS.items():
        existing=_columns(db,table)
        for name,decl in cols.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    for sql in INDEXES:
        db.execute(sql)
    db.commit()

def connect():
    db=sqlite3.connect(settings()["db"])
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    migrate(db)
    return db
