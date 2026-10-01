"""Export profiles and the card-fill planner behind the export card's smart help.

A profile is one card's recipe: where it mounts, which platforms go on it, how big it is,
and which curation gates apply. Profiles live in the database rather than the browser so a
card set up on one machine looks the same from any other.

The planner answers the question an owner actually has in front of an empty card: what is
the most of my library this card can hold? That is a 0/1 knapsack over platforms (a platform
goes on whole or not at all), solved exactly; with a few dozen platforms and the sizes
bucketed to 64 MiB it is a few hundred thousand steps.
"""
import json
import uuid
from .db import connect
from .organizer import export_systems

GB = 1000 ** 3              # card makers' gigabyte: a "256 GB" card is 256e9 bytes
GIB = 1024 ** 3
_UNIT = 64 * 1024 ** 2      # knapsack granularity; sizes round UP, so a plan never overshoots


def reserve_for(total_bytes):
    """What an export always leaves free on a card of this size. Mirrors the organizer's
    rule so the plan and the real run agree on what "fits" means."""
    return max(GIB, int(total_bytes) // 100)


# ------------------------------------------------------------------------------ profiles
def _clean(p):
    """Only the fields a profile means, with types fixed — the browser is not trusted to
    send a well-formed one."""
    gates = p.get("gates") or {}
    cap = p.get("capacity_gb")
    return {"name": str(p.get("name") or "").strip()[:80] or "Untitled card",
            "path": str(p.get("path") or "").strip(),
            "systems": sorted({str(s) for s in (p.get("systems") or []) if s}),
            "fill": bool(p.get("fill")),
            "capacity_gb": float(cap) if cap not in (None, "", 0, "0") else None,
            "gates": {"wanted_only": bool(gates.get("wanted_only")),
                      "keep_only": bool(gates.get("keep_only")),
                      "rating_min": max(0, min(10, int(gates.get("rating_min") or 0)))}}


def list_profiles():
    return [{"id": r["id"], **json.loads(r["data"])}
            for r in connect().execute("SELECT id,data FROM export_profiles ORDER BY name")]


def save_profile(p, pid=None):
    data = _clean(p)
    pid = pid or uuid.uuid4().hex[:12]
    db = connect()
    with db:
        db.execute("""INSERT INTO export_profiles(id,name,data) VALUES(?,?,?)
          ON CONFLICT(id) DO UPDATE SET name=excluded.name,data=excluded.data,
          updated_at=CURRENT_TIMESTAMP""", (pid, data["name"], json.dumps(data)))
    return {"id": pid, **data}


def delete_profile(pid):
    db = connect()
    with db:
        return db.execute("DELETE FROM export_profiles WHERE id=?", (pid,)).rowcount > 0


# ------------------------------------------------------------------------------ planner
def best_fill(sizes, room, forced=()):
    """The platforms to add to `forced` that use the most of `room` bytes.

    `sizes` maps platform -> bytes. Exact 0/1 knapsack on 64 MiB buckets, sizes rounded up
    so the true total never exceeds `room`. Platforms of unknown/zero size are left out:
    they cost nothing but the plan cannot vouch for them either."""
    forced = set(forced)
    room -= sum(sizes.get(s, 0) for s in forced)
    if room <= 0:
        return {"add": [], "add_bytes": 0, "room_after_forced": max(room, 0)}
    cap = room // _UNIT
    items = [(s, b, -(-b // _UNIT)) for s, b in sorted(sizes.items())
             if s not in forced and b > 0]
    # reach[c] = index of the item that first made total c reachable; -2 marks the empty set.
    # Walking c downwards means reach[c - w] still reflects only earlier items, so each
    # item is used at most once and the parent chain reconstructs a valid subset.
    reach = [-1] * (cap + 1)
    reach[0] = -2
    for i, (_, _, w) in enumerate(items):
        if w > cap:
            continue
        for c in range(cap, w - 1, -1):
            if reach[c] == -1 and reach[c - w] != -1:
                reach[c] = i
    c = max(c for c in range(cap + 1) if reach[c] != -1)
    add = []
    while c > 0:
        i = reach[c]
        add.append(items[i][0])
        c -= items[i][2]
    add.sort()
    return {"add": add, "add_bytes": sum(sizes[s] for s in add), "room_after_forced": room}


def plan(capacity_bytes=None, selected=(), gates=None):
    """Smart help for a card of `capacity_bytes`.

    Plans against the card's whole size, not its current free space: what is already on
    the card is usually an earlier run of this same recipe, which a re-run skips, so
    counting it against free space would call a top-up "won't fit" when it fits. The real
    export still checks actual free space before it writes.

    Returns per-platform sizes under the gates, the selection's total against the usable
    space (capacity less the reserve), the best fill from scratch, the best way to fill what
    the selection leaves, and for each platform too big for the card on its own, what its
    curated subset (kept or rated 7+) would take instead."""
    gates = gates or {}
    rows = export_systems(wanted_only=gates.get("wanted_only"),
                          keep_only=gates.get("keep_only"),
                          rating_min=gates.get("rating_min"))
    sizes = {r["system"]: r["bytes"] or 0 for r in rows}
    if not capacity_bytes:
        return {"systems": rows, "usable_bytes": None}
    usable = max(0, int(capacity_bytes) - reserve_for(capacity_bytes))
    selected = [s for s in selected if s in sizes]
    sel_bytes = sum(sizes[s] for s in selected)

    too_big = []
    big = [s for s, b in sizes.items() if b > usable]
    if big:
        curated = {r["system"]: r["bytes"] or 0
                   for r in export_systems(wanted_only=gates.get("wanted_only"),
                                           keep_only=True, rating_min=7)}
        too_big = [{"system": s, "bytes": sizes[s], "curated_bytes": curated.get(s, 0),
                    "curated_fits": 0 < curated.get(s, 0) <= usable} for s in sorted(big)]

    return {"systems": rows, "usable_bytes": usable,
            "selected_bytes": sel_bytes, "selected_fits": sel_bytes <= usable,
            "best": best_fill(sizes, usable),
            "top_up": best_fill(sizes, usable, forced=selected) if selected else None,
            "too_big": too_big}
