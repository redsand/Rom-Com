LIFECYCLE = [
    "CATALOGED","MISSING","FOUND","QUEUED","DOWNLOADING","DOWNLOADED",
    "EXTRACTED","VERIFIED","NORMALIZED","INSTALLED","TESTED","FAILED",
    "MANUAL","EXCLUDED"
]
RANK = {name: i for i, name in enumerate(LIFECYCLE)}
# FAILED/MANUAL/EXCLUDED are side states and should not prevent explicit updates.
RANK.update({"FAILED": 0, "MANUAL": 0, "EXCLUDED": 99})

def promote(db, item_id, new_status):
    row=db.execute("SELECT status FROM items WHERE id=?",(item_id,)).fetchone()
    if not row: return
    old=row["status"]
    if old=="EXCLUDED": return
    if RANK.get(new_status,0) >= RANK.get(old,0):
        db.execute("UPDATE items SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(new_status,item_id))
