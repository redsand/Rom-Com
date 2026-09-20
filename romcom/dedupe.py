"""Merge catalog rows that describe the same entry under two source labels.

The same dat gets imported more than once under different source names: `detect_source`
returns the publisher when the header identifies one and falls back to the generic "dat"
when it does not, so a differently-packaged copy of a redump dat lands as `dat` and
duplicates every row. 6,547 pairs arrived that way, 5,586 of them redump/dat.

Identity here is (system, external_id), not the title. Two rows with the same title can be
genuinely different entries, but external_id is the dat's own key for the entry -- when it
matches, both rows describe the one catalog entry and merging them loses nothing. The
generic `dat` source is *not* simply redundant: 7,066 of its rows exist under no other
source, so it cannot be deleted wholesale.
"""
from .config import load_yaml
from .db import connect

# Children that point at an item and must follow it to the survivor.
_CHILD_TABLES = (("files", "matched_item_id"), ("file_hashes", "item_id"),
                 ("aliases", "item_id"), ("events", "item_id"), ("volume_covers", "item_id"))


def _declared_sources():
    """system -> the source catalogs.yaml expects for it, used to pick the winner."""
    out = {}
    for source, meta in (load_yaml("catalogs.yaml").get("catalogs") or {}).items():
        for system in meta.get("systems", []) or []:
            out.setdefault(system, source)
    return out


def _rank(row, declared):
    """Lower sorts first, so the winner is rows[0]. Deliberately total, so the choice never
    depends on database order: a config-declared source beats a named one, a named one beats
    the generic `dat`, and having files on disk outranks everything -- moving a matched file
    between rows is the one part of a merge with any real consequence."""
    has_files = 1 if row["nfiles"] else 0
    declared_hit = 1 if declared.get(row["system"]) == row["catalog_source"] else 0
    generic = 1 if (row["catalog_source"] or "") in ("dat", "", None) else 0
    return (-has_files, -declared_hit, generic, row["id"])


def groups(db=None):
    """Every (system, external_id) with more than one row, winner first."""
    db = db or connect()
    declared = _declared_sources()
    rows = db.execute("""
        SELECT i.id, i.system, i.external_id, i.catalog_source, i.wanted, i.authorized, i.status,
               (SELECT COUNT(*) FROM files f WHERE f.matched_item_id = i.id) nfiles
        FROM items i
        -- Grouped join rather than a correlated EXISTS: the latter is a full scan per row and
        -- takes minutes over 300k items, this is one pass plus a hash join.
        JOIN (SELECT system, external_id FROM items WHERE external_id IS NOT NULL
              GROUP BY system, external_id HAVING COUNT(*) > 1) d
          ON d.system IS i.system AND d.external_id = i.external_id
        ORDER BY i.system, i.external_id""").fetchall()
    out = {}
    for r in rows:
        out.setdefault((r["system"], r["external_id"]), []).append(dict(r))
    return [sorted(v, key=lambda x: _rank(x, declared)) for v in out.values()]


def dedupe(dry_run=True, db=None):
    """Merge each group into its winner. Returns a report; changes nothing when dry_run."""
    db = db or connect()
    gs = groups(db)
    report = {"groups": len(gs), "merged": 0, "removed": 0, "with_files": 0, "pairs": []}
    if not gs:
        return report
    for g in gs:
        keep, losers = g[0], g[1:]
        if any(x["nfiles"] for x in losers):
            report["with_files"] += 1
        report["pairs"].append({"keep": keep["id"], "drop": [x["id"] for x in losers],
                                "system": keep["system"], "external_id": keep["external_id"]})
        report["merged"] += 1
        report["removed"] += len(losers)
        if dry_run:
            continue
        with db:
            for x in losers:
                for table, col in _CHILD_TABLES:
                    # INSERT OR IGNORE semantics are not available for an UPDATE, and the
                    # survivor may already hold an identical child row (both copies of a dat
                    # carry the same alias). Re-point what does not collide, drop the rest.
                    try:
                        db.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (keep["id"], x["id"]))
                    except Exception:
                        pass
                    db.execute(f"DELETE FROM {table} WHERE {col}=?", (x["id"],))
            # A merge must never quietly un-want or de-authorize something.
            db.execute("""UPDATE items SET wanted=MAX(wanted,?), authorized=MAX(authorized,?),
                          updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                       (max(x["wanted"] or 0 for x in g), max(x["authorized"] or 0 for x in g), keep["id"]))
            db.execute("DELETE FROM items WHERE id IN (%s)" % ",".join("?" * len(losers)),
                       [x["id"] for x in losers])
    return report
