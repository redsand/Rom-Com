"""Reconcile the FOUND→VERIFIED gap.

A FOUND item has a file on disk that matched by NAME but never hash-verified. The scanner
already tries every hash it can (file, and inner zip members), so a FOUND item is FOUND for
one of two reasons, and they need opposite handling:

- **Unverifiable** — the catalog has NO hash for this item (whole systems like dos, windows,
  amiga, scummvm, romhacks carry no No-Intro/Redump hashes). There is no truth to check
  against, so FOUND is the correct terminal state — not a problem to fix.

- **Mismatch** — the catalog DOES have a hash for this item, but the on-disk file didn't
  match it. That means the file is a different dump/region/hack than the catalog entry (or
  the name-match grabbed the wrong file). These are the real gap: candidates to re-acquire
  the correct dump.

reconcile() classifies the FOUND pile so the two are visible and countable; it does not
change anything. `reacquire_mismatches()` is the opt-in action: it flips the mismatch items
back to MISSING so the acquirer fetches the catalogued dump.
"""
from .db import connect

_HAS_HASH = "EXISTS(SELECT 1 FROM file_hashes fh WHERE fh.item_id=i.id)"


def reconcile(db=None):
    """Classify FOUND items into unverifiable (no catalog hash) vs mismatch (has a catalog
    hash the on-disk file didn't match), per system. Read-only."""
    db = db or connect()
    rows = db.execute(f"""SELECT COALESCE(i.system,'unknown') system,
          SUM(CASE WHEN {_HAS_HASH} THEN 1 ELSE 0 END) mismatch,
          SUM(CASE WHEN {_HAS_HASH} THEN 0 ELSE 1 END) unverifiable
        FROM items i WHERE i.status='FOUND' GROUP BY i.system""").fetchall()
    by_system = [dict(r) for r in rows]
    mism = sorted((r for r in by_system if r["mismatch"]), key=lambda x: -x["mismatch"])
    unver = sorted((r for r in by_system if r["unverifiable"]), key=lambda x: -x["unverifiable"])
    return {
        "found_total": sum(r["mismatch"] + r["unverifiable"] for r in by_system),
        "mismatch_total": sum(r["mismatch"] for r in by_system),
        "unverifiable_total": sum(r["unverifiable"] for r in by_system),
        "mismatch_by_system": [{"system": r["system"], "count": r["mismatch"]} for r in mism],
        "unverifiable_by_system": [{"system": r["system"], "count": r["unverifiable"]} for r in unver],
    }


def reacquire_mismatches(system=None, db=None):
    """Flip mismatch FOUND items (a catalog hash exists but the file didn't match) back to
    MISSING so the acquirer fetches the catalogued dump. Optionally scope to one system.
    Returns the number re-armed. The wrong file stays on disk until the right one overwrites
    it in the scan — nothing is deleted here."""
    db = db or connect()
    # NB: an UPDATE has no table alias, so this EXISTS references items.id (not the i.id the
    # reconcile() SELECT uses).
    q = ("UPDATE items SET status='MISSING',updated_at=CURRENT_TIMESTAMP WHERE status='FOUND' "
         "AND EXISTS(SELECT 1 FROM file_hashes fh WHERE fh.item_id=items.id)")
    args = []
    if system:
        q += " AND system=?"; args.append(system)
    with db:
        cur = db.execute(q, args)
    return cur.rowcount
