"""Remove the duplicate copies of a game that waste disk space in the library.

Every download source names files differently (the site's downloadName, the URL's last
segment, whatever SABnzbd picked), so one game fetched more than once — or once per
region/variant catalog entry that arms separately — arrives as several files with
different names and the same bytes. Nothing else in the repair tooling touches them:
`dedupe` merges catalog *rows*, `audit` deletes stale *records*, and both leave content
files alone. The export no longer copies the extras onto a card (organizer's canonical
pick), but the bytes still sit in the download directory.

Identity here is content: a strong hash (sha1, md5 as fallback — crc32 alone proves
nothing). A file with neither is never treated as a duplicate of anything, and neither is
anything with content=0 (artwork and junk). Two catalog items sharing content are
*reported*, not deleted: the rows describe different entries and deleting a file would
silently un-match one of them. The copies that ARE deleted: same-content siblings under
one item, and a `local` adopted item's copy of content a catalog entry also holds —
exactly what `prune_adopted_ghosts` cleans up when a re-scan re-points a file, done here
for the file itself. Deletion is opt-in (`--apply`), preceded by a manifest in backups/
recording every path and hash, and every removal is an event on the item.
"""
import json
from datetime import datetime
from pathlib import Path

from .db import connect


def _rows(db, systems=None):
    """Every content file carrying a strong hash — the only rows identity can be proven
    for. Files with content=0 (artwork/junk) and unhashable files are excluded."""
    q = """SELECT f.path, f.bytes, f.sha1, f.md5, f.match_method, f.matched_item_id item_id,
                  i.system, i.title, i.wanted, i.keep, i.status, i.catalog_source
           FROM files f JOIN items i ON i.id=f.matched_item_id
           WHERE COALESCE(f.content,1)=1 AND (f.sha1 IS NOT NULL OR f.md5 IS NOT NULL)"""
    args = []
    if systems:
        q += " AND lower(COALESCE(i.system,'')) IN (%s)" % ",".join("?" * len(systems))
        args = [s.lower() for s in systems]
    return db.execute(q, args).fetchall()


def _rank(r):
    """Lower sorts first, so the winner is g[0]. Deliberately total, so the choice never
    depends on row order: the hash-verified copy (proof, per scanner's match methods)
    beats the owner's verdict (wanted/keep), which beats an adopted `local` item's copy
    of what a catalog entry also holds; then the larger file, then path order."""
    return (0 if str(r["match_method"] or "").startswith("hash") else 1,
            0 if (r["wanted"] or r["keep"]) else 1,
            1 if (r["catalog_source"] or "") == "local" else 0,
            -(r["bytes"] or 0),
            r["path"])


def _content_groups(db, systems=None):
    """Content hash -> every file holding it, best copy first."""
    groups = {}
    for r in _rows(db, systems):
        groups.setdefault(r["sha1"] or r["md5"], []).append(dict(r))
    for g in groups.values():
        g.sort(key=_rank)
    return groups


def same_item_groups(db=None, systems=None):
    """Same-content siblings under ONE item, winner first — always safe to thin, the
    winner is byte-identical and stays."""
    out = []
    for g in _content_groups(db or connect(), systems).values():
        by_item = {}
        for r in g:
            by_item.setdefault(r["item_id"], []).append(r)
        out.extend(files for files in by_item.values() if len(files) > 1)
    return out


def cross_item_groups(db=None, systems=None):
    """Identical content owned by DIFFERENT items, winner first. Mostly `local` adoptions
    of a file a catalog entry also holds, or variant entries that each fetched the one
    dump a source had."""
    out = []
    for g in _content_groups(db or connect(), systems).values():
        if len({r["item_id"] for r in g}) > 1:
            out.append(g)
    return out


def _plan(db, systems=None):
    """(deletions, kept_cross_item) — the redundant copies and the ones only reported.
    A deletion records `kept`: the surviving copy its bytes are identical to."""
    deletions, kept_cross = {}, []
    for g in _content_groups(db, systems).values():
        keep = g[0]
        by_item = {}
        for r in g:
            by_item.setdefault(r["item_id"], []).append(r)
        for files in by_item.values():
            head = files[0]
            # One copy per item remains. A `local` adoption of content a catalog entry
            # holds is removable; two catalog items sharing content are report-only —
            # the rows describe different entries and the file matches one of them.
            head_removed = head is not keep and (
                (head["catalog_source"] or "") == "local"
                and (keep["catalog_source"] or "") != "local"
                and head["status"] != "EXCLUDED")
            if head is not keep:
                if head_removed:
                    deletions[head["path"]] = {**head, "kept": keep["path"]}
                else:
                    kept_cross.append({**head, "kept": keep["path"]})
            # A second copy under the same item: whichever copy of this item survives.
            survivor = keep["path"] if (head is keep or head_removed) else head["path"]
            for r in files[1:]:
                deletions[r["path"]] = {**r, "kept": survivor}
        # The winner is the copy that stays — never a deletion, never the last one.
        deletions.pop(keep["path"], None)
    return deletions, kept_cross


def audit(db=None, systems=None):
    """What a cleanup would remove, per system, without touching anything."""
    db = db or connect()
    deletions, kept_cross = _plan(db, systems)
    by_system = {}
    for d in deletions.values():
        s = by_system.setdefault(d["system"] or "unknown", {"files": 0, "bytes": 0})
        s["files"] += 1
        s["bytes"] += d["bytes"] or 0
    return {"duplicate_copies": len(deletions),
            "reclaimable_bytes": sum(d["bytes"] or 0 for d in deletions.values()),
            "cross_item_kept": len(kept_cross),
            "by_system": by_system,
            "sample": sorted(deletions.values(), key=lambda d: -(d["bytes"] or 0))[:6]}


def cleanup(apply=False, systems=None, db=None):
    """Delete the redundant copies. Reports what it would do unless `apply`; deletes
    nothing but a copy whose identical twin is recorded as staying."""
    db = db or connect()
    deletions, kept_cross = _plan(db, systems)
    report = {"applied": bool(apply), "duplicate_copies": len(deletions),
              "reclaimable_bytes": sum(d["bytes"] or 0 for d in deletions.values()),
              "cross_item_kept": len(kept_cross),
              "deleted": 0, "bytes_freed": 0, "local_items_pruned": 0,
              "errors": [], "sample": sorted(deletions.values(),
                                             key=lambda d: -(d["bytes"] or 0))[:6]}
    if not apply or not deletions:
        return report

    # The manifest precedes the deletions (fixnames' rule): every path and hash, so a
    # removal is reversible by record even though it is not reversible by recycle bin.
    Path("backups").mkdir(exist_ok=True)
    manifest = Path("backups") / f"dupes-removed-{datetime.now():%Y%m%d-%H%M%S}.json"
    manifest.write_text(json.dumps({
        "at": datetime.now().isoformat(timespec="seconds"),
        "note": "copies removed as duplicates; 'kept' is the identical file that remains",
        "removed": [{"path": d["path"], "bytes": d["bytes"], "sha1": d["sha1"],
                     "md5": d["md5"], "item": d["item_id"], "system": d["system"],
                     "title": d["title"], "kept": d["kept"]}
                    for d in sorted(deletions.values(), key=lambda d: d["path"])]},
        indent=1), encoding="utf-8")
    report["manifest"] = str(manifest)

    for path, d in deletions.items():
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            report["errors"].append({"path": path, "error": str(e)})
            continue
        with db:
            db.execute("DELETE FROM files WHERE path=?", (path,))
            db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,'dupe-removed',?)",
                       (d["item_id"], f"copy kept at {d['kept']}"))
        report["deleted"] += 1
        report["bytes_freed"] += d["bytes"] or 0

    # A `local` item exists for one reason: to represent a file. One whose last file a
    # catalog entry also holds represents nothing now — the same prune scan() runs.
    with db:
        report["local_items_pruned"] = db.execute(
            """DELETE FROM items WHERE catalog_source='local' AND status<>'EXCLUDED'
               AND NOT EXISTS (SELECT 1 FROM files f WHERE f.matched_item_id = items.id)"""
        ).rowcount
    return report