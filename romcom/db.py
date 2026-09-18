import sqlite3
from .config import settings

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS items (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, system TEXT, series TEXT,
 series_number INTEGER, year INTEGER, authorized INTEGER NOT NULL DEFAULT 0,
 wanted INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'CATALOGED',
 preferred_runtime TEXT, source TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
 item_id TEXT NOT NULL, alias TEXT NOT NULL,
 UNIQUE(item_id, alias), FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS hashes (
 item_id TEXT NOT NULL, algorithm TEXT NOT NULL, digest TEXT NOT NULL,
 UNIQUE(algorithm, digest), FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS volumes (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, authorized INTEGER NOT NULL DEFAULT 0,
 search_query TEXT, estimated_bytes INTEGER, status TEXT NOT NULL DEFAULT 'CATALOGED'
);
CREATE TABLE IF NOT EXISTS volume_covers (
 volume_id TEXT NOT NULL, item_id TEXT NOT NULL,
 PRIMARY KEY(volume_id,item_id),
 FOREIGN KEY(volume_id) REFERENCES volumes(id) ON DELETE CASCADE,
 FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
 nzo_id TEXT, result_title TEXT, bytes INTEGER, status TEXT, queued_at TEXT, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS files (
 path TEXT PRIMARY KEY, bytes INTEGER, mtime REAL, crc32 TEXT, md5 TEXT, sha1 TEXT, matched_item_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_system ON items(system);
CREATE INDEX IF NOT EXISTS idx_items_series ON items(series);
"""

def connect():
    db = sqlite3.connect(settings()["db"])
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db
