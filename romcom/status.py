LIFECYCLE = [
    "CATALOGED","MISSING","FOUND","QUEUED","DOWNLOADING","DOWNLOADED",
    "EXTRACTED","VERIFIED","NORMALIZED","INSTALLED","TESTED","FAILED",
    "MANUAL","EXCLUDED"
]
RANK = {name: i for i, name in enumerate(LIFECYCLE)}
# FAILED/MANUAL/EXCLUDED are side states and should not prevent explicit updates.
RANK.update({"FAILED": 0, "MANUAL": 0, "EXCLUDED": 99})

# Fully reconciled: the file is on disk and hash-verified against the catalog.
SATISFIED = ("VERIFIED", "NORMALIZED", "INSTALLED", "TESTED")
# The file is in hand — matched, downloaded, or verified. Items in one of these states
# are never "missing" and the acquisition pipeline never downloads them again.
OWNED = ("FOUND", "DOWNLOADED") + SATISFIED
# Still nothing on disk: the only states worth searching for (see acquirer.QUEUEABLE).
# FOUND/DOWNLOADED are on disk awaiting verification, not absent.
MISSING = ("CATALOGED", "MISSING")

def promote(db, item_id, new_status):
    row=db.execute("SELECT status FROM items WHERE id=?",(item_id,)).fetchone()
    if not row: return
    old=row["status"]
    if old=="EXCLUDED": return
    # Strictly greater: advancing to the SAME status is a no-op. This matters at scale —
    # a re-scan of a directory of already-matched files would otherwise re-UPDATE every
    # item (bumping updated_at) on every pass, a flood of pointless writes that bloats the
    # WAL. Only a real status advance writes.
    if RANK.get(new_status,0) > RANK.get(old,0):
        db.execute("UPDATE items SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(new_status,item_id))

def own(db, item_id):
    """A file we already have is part of the intended collection: flag the item wanted
    and authorized so coverage, the missing list, and the planner all agree with what is
    actually on the shelf. Raises the two flags only — status, notes and play state stay
    put — and leaves an explicitly EXCLUDED item alone, since exclusion is the deliberate
    "don't want this back" switch."""
    db.execute("""UPDATE items SET wanted=1,authorized=1,updated_at=CURRENT_TIMESTAMP
      WHERE id=? AND status<>'EXCLUDED' AND (wanted=0 OR authorized=0)""",(item_id,))

def own_all(db):
    """Same rule applied to the whole library (CLI `mark-owned`, Library tab button).
    Returns the number of items that changed."""
    cur=db.execute(f"""UPDATE items SET wanted=1,authorized=1,updated_at=CURRENT_TIMESTAMP
      WHERE status IN {OWNED} AND (wanted=0 OR authorized=0)""")
    return cur.rowcount
