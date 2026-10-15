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


def wrong_downloads(apply=False, db=None):
    """Website downloads that were never the game asked for: another title sharing a few
    words, or the same title for another platform (a 538 MB Wii ISO filed as a DS game).
    Judged by the same two checks that now guard every download — indexer.plausible on
    the result's name and size, verify.check_download on what is on disk. Hash-verified
    files are proof and never judged.

    `apply` lets go of each wrong file (the item goes back to MISSING, so the acquirer —
    guarded now — fetches the right one; its ledger already refuses the bad url) and
    records why. Nothing is deleted: the report lists the files and their size, for the
    owner to remove."""
    from pathlib import Path
    from .indexer import plausible, CART_MAX_BYTES
    from .verify import check_download
    db = db or connect()
    by_name = None
    bad = {}
    for j in db.execute("""SELECT j.entity_id, j.result_title, j.bytes, i.title, i.system, i.status
                           FROM jobs j JOIN items i ON i.id=j.entity_id
                           WHERE j.entity_type='item' AND j.status='DOWNLOADED' AND j.nzo_id IS NULL
                             AND i.status IN ('DOWNLOADED','FOUND')"""):
        files = [r["path"] for r in db.execute(
            "SELECT path FROM files WHERE matched_item_id=? AND COALESCE(match_method,'') NOT LIKE 'hash%'",
            (j["entity_id"],))]
        if not files:
            # Marked downloaded, yet no file claims the item: the scanner gave the file to a
            # same-named game on another console. Find it by the name it was saved under.
            if by_name is None:
                by_name = {}
                for r in db.execute("SELECT path FROM files"):
                    by_name.setdefault(Path(r["path"]).name.lower(), []).append(r["path"])
            files = by_name.get((j["result_title"] or "").lower(), [])
        why = None
        cap = CART_MAX_BYTES.get((j["system"] or "").lower())
        if cap and (j["bytes"] or 0) > cap:
            why = f"{(j['bytes'] or 0) >> 20} MB is bigger than any {j['system']} cartridge"
        elif not plausible(j["title"], j["system"], j["result_title"], j["bytes"] or 0):
            why = f"'{j['result_title']}' is another game"
        for f in files:
            if why is None and Path(f).exists():
                ok, reason = check_download(f, j["system"])
                why = None if ok else reason
        if why and files:
            bad[j["entity_id"]] = {"title": j["title"], "system": j["system"], "why": why,
                                   "files": files}
    total = 0
    for iid, b in bad.items():
        # A file can be wrong here and right elsewhere (a PS1 disc fetched for a Dreamcast
        # game, since matched to the PS1 one): only a file nothing else claims is the
        # owner's to delete.
        b["orphans"] = [f for f in b["files"] if Path(f).exists() and not db.execute(
            "SELECT 1 FROM files WHERE path=? AND matched_item_id IS NOT NULL "
            "AND matched_item_id<>?", (f, iid)).fetchone()]
        b["bytes"] = sum(Path(f).stat().st_size for f in b["orphans"])
        total += b["bytes"]
    if apply and bad:
        with db:
            for iid, b in bad.items():
                db.executemany("UPDATE files SET matched_item_id=NULL, match_method=NULL "
                               "WHERE path=? AND matched_item_id=?", [(f, iid) for f in b["files"]])
                db.execute("UPDATE items SET status='MISSING', updated_at=CURRENT_TIMESTAMP "
                           "WHERE id=? AND status IN ('DOWNLOADED','FOUND')", (iid,))
                db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",
                           (iid, "wrong-download", f"{b['why']}: {'; '.join(b['files'])[:400]}"))
    return {"count": len(bad), "bytes": total, "applied": bool(apply),
            "items": sorted(bad.values(), key=lambda b: -b["bytes"])}


def stub_discs(apply=False, db=None):
    """Disc games whose only file is an archive too small to hold the disc (see
    sniff.stub_disc): a download that stopped after its first megabyte, matched by the
    one track every disc of that console shares, or a cartridge rom filed as a Virtual
    Console title. `apply` lets go of each file, sends a game left with no file back to
    MISSING so the acquirer fetches a whole copy, and records why. Nothing is deleted."""
    from pathlib import Path
    from . import sniff
    db = db or connect()
    q = f"""SELECT f.path, f.bytes, f.match_method, i.id, i.title, i.system, i.status FROM files f
            JOIN items i ON i.id=f.matched_item_id
            WHERE lower(i.system) IN ({','.join('?' * len(sniff.DISC_SYSTEMS))})
              AND f.path NOT LIKE '%|%'
              AND (lower(f.path) LIKE '%.zip' OR lower(f.path) LIKE '%.7z'
                   OR lower(f.path) LIKE '%.rar')"""
    bad = [dict(r) for r in db.execute(q, sniff.DISC_SYSTEMS)
           if sniff.stub_disc(r["bytes"], r["system"], f"{r['title']} {Path(r['path']).name}")]
    if apply and bad:
        with db:
            for b in bad:
                db.execute("UPDATE files SET matched_item_id=NULL, match_method=NULL "
                           "WHERE path=? AND matched_item_id=?", (b["path"], b["id"]))
                left = db.execute("SELECT 1 FROM files WHERE matched_item_id=?",
                                  (b["id"],)).fetchone()
                if not left:
                    db.execute("UPDATE items SET status='MISSING', updated_at=CURRENT_TIMESTAMP "
                               "WHERE id=? AND status IN ('DOWNLOADED','FOUND','VERIFIED',"
                               "'NORMALIZED')", (b["id"],))
                db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",
                           (b["id"], "stub-disc",
                            f"{b['bytes']} bytes cannot be a {b['system']} disc: {b['path']}"[:400]))
    return {"count": len(bad), "applied": bool(apply), "items": bad}
