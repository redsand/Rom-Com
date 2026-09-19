"""Tool registry for the assistant — one registry, used by every consumer.

**The one-registry rule** is the anti-drift guarantee for the whole feature: the in-app
agent resolves tools here, and (Stage 6) the MCP server serializes this same dict, so an
external client sees exactly what the assistant can do and nothing more. Adding a tool in
one place can never leave the other behind.

Every tool is a `Tool` with a risk level:

* `low` — read-only. Never gated.
* `medium` — writes or network, but narrow and reversible. Runs without a prompt. Network
  tools (a live indexer search) are `medium` on purpose: read-only, and they *are* the
  assistant's core power.
* `high` — bulk, destructive, or moves files on disk. `dispatch` refuses these unless the
  call carries `confirm: true`, and the agent surfaces an approval card first (Stage 3).

There is no delete tool anywhere in Rom-Com's API, so there is none here — enforced by
omission rather than by a check that could be edited out.

**Output discipline.** Results are JSON and truncated to ~1,500 chars for the model's
context, always reporting `total` alongside the rows actually returned, so the agent
narrows its filter or pages instead of silently believing it saw everything. That matters:
a local model will happily summarize 50 of 7,553 rows as "that's all of them."
"""
import json
import re
import time

from .config import load_yaml
from .db import connect
from .status import LIFECYCLE

RESULT_CAP = 1500
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class Ctx:
    """Adapter for state that lives inside `create_app`'s closure, passed in rather than
    imported (importing web.py from here would be circular). Defaults are inert so tests
    can build one with no app at all."""

    def __init__(self, job_status=None, jobs_active=None, launch_job=None, launch_watcher=None,
                 arm_acquire=None, set_settings=None):
        self.job_status = job_status or (lambda kind: {"running": False})
        self.jobs_active = jobs_active or (lambda: {})
        self.launch_job = launch_job or (lambda kind, params: False)
        self.launch_watcher = launch_watcher or (lambda on: False)
        # `_maybe_auto_acquire` and the Settings whitelist live in web.py and must stay
        # there: the first owns the "arm the pipeline" rule, the second is the only thing
        # that knows which keys are writable. Re-implementing either here would let the
        # assistant write something the Settings tab refuses.
        self.arm_acquire = arm_acquire or (lambda: False)
        self.set_settings = set_settings or (lambda values: {"error": "settings are not writable here"})


class Tool:
    def __init__(self, name, description, parameters, fn, risk="low", ctx=None, confirm=None):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.fn = fn
        self.risk = risk
        # The Tool carries the closure state it needs, so dispatch stays
        # `dispatch(reg, name, args)` — no ctx threaded through every call site.
        self.ctx = ctx or Ctx()
        # For `high` risk tools: the sentence on the confirm card. It runs *instead of* the
        # tool, so it must be read-only — that is the whole point of asking.
        self.confirm = confirm

    def schema(self):
        """Ollama's tool shape (OpenAI-compatible)."""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}


# ------------------------------------------------------------------- result shaping

def _fit(data, cap=RESULT_CAP):
    """Shrink `data` until its JSON fits the budget, returning what was dropped.

    Trimming a list rather than slicing the JSON text keeps the payload parseable, so the
    model sees well-formed data and an explicit `note` about what is missing."""
    note = None
    for _ in range(12):
        text = json.dumps(data, default=str)
        if len(text) <= cap:
            return data, text, note
        target = None
        if isinstance(data, dict):
            lists = [(len(v), k) for k, v in data.items() if isinstance(v, list) and v]
            if lists:
                target = max(lists)[1]
        elif isinstance(data, list):
            target = None
            keep = max(1, len(data) // 2)
            note = f"showing {keep} of {len(data)}"
            data = data[:keep]
            continue
        if target is None:
            if isinstance(data, str):
                data = data[:cap] + " …"
                continue
            return data, text[:cap] + " …[truncated]", note
        kept = max(1, len(data[target]) // 2)
        note = f"{target}: showing {kept} of {len(data[target])}"
        data = dict(data, **{target: data[target][:kept]})
    return data, json.dumps(data, default=str)[:cap], note


def _limit(args, key="limit", default=DEFAULT_LIMIT):
    try:
        n = int(args.get(key) or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_LIMIT))


def _bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _checked_path(path, must_exist=True):
    """The same rule the web routes use (`_checked_path` in web.py): a quoted path is
    accepted, an empty one is not, and a job that reads a directory must name one that
    exists. A tool that skipped this could queue a job guaranteed to fail."""
    p = (path or "").strip().strip('"')
    if not p:
        return None
    from pathlib import Path
    if must_exist and not Path(p).exists():
        return None
    return p


def _item_where(args):
    """The Library tab's filter semantics, in one place: `list_items` counts with this and
    `bulk_update_items` writes with it, so the number the assistant reports and the number
    it actually changes can never disagree."""
    from .status import MISSING, SATISFIED
    q, p = "SELECT * FROM items WHERE 1=1", []
    if args.get("system"):
        q += " AND system=?"; p.append(args["system"])
    if args.get("status"):
        q += " AND status=?"; p.append(args["status"])
    if args.get("series"):
        q += " AND lower(series)=lower(?)"; p.append(args["series"])
    if args.get("q"):
        q += " AND (title LIKE ? OR series LIKE ? OR id LIKE ?)"
        like = f"%{args['q']}%"; p += [like, like, like]
    view = args.get("view") or "all"
    if view == "missing":
        q += f" AND wanted=1 AND status IN {MISSING}"
    elif view == "wanted":
        q += " AND wanted=1"
    elif view == "satisfied":
        q += f" AND status IN {SATISFIED}"
    return q, p


def _entity_by_id(ident):
    """Items first, then volumes — the same order `_entity` uses in web.py."""
    db = connect()
    row = db.execute("SELECT * FROM items WHERE id=?", (ident,)).fetchone()
    if row:
        return db, "item", row
    row = db.execute("SELECT * FROM volumes WHERE id=?", (ident,)).fetchone()
    return db, ("volume" if row else None), row


# ------------------------------------------------------------------- read tools

def _library_summary(args, ctx):
    from .report import summary
    from .status import SATISFIED
    db = connect()
    active = db.execute("SELECT COUNT(*) c FROM jobs WHERE status IN ('QUEUED','DOWNLOADING')").fetchone()["c"]
    satisfied = db.execute(f"SELECT COUNT(*) c FROM items WHERE wanted=1 AND status IN {SATISFIED}").fetchone()["c"]
    s = summary()
    return {"cataloged": s["cataloged"], "wanted": s["wanted"], "on_disk": s["on_disk"],
            "satisfied": satisfied, "active_jobs": active,
            "by_status": s["by_status"], "by_system": s["by_system"]}


def _catalog_status(args, ctx):
    from .catalog_status import catalog_status
    rows = catalog_status()
    missing = [r for r in rows if not r["loaded"]]
    return {"targets": len(rows), "missing_imports": len(missing), "rows": rows,
            "empty_targets": [dict(r) for r in missing]}


def _facets(args, ctx):
    db = connect()
    systems = [r["system"] for r in db.execute(
        "SELECT DISTINCT system FROM items WHERE system IS NOT NULL ORDER BY system")]
    cfg = load_yaml("catalogs.yaml")
    known = {s for meta in cfg.get("catalogs", {}).values() for s in meta.get("systems", [])}
    known |= set(cfg.get("custom", {}).get("systems", []))
    return {"systems": systems, "statuses": LIFECYCLE, "all_systems": sorted(known | set(systems))}


# ---------------------------------------------------------------------------- memory
#
# Memory is written by explicit tool call rather than extracted after every turn: extraction
# would be a second model call per turn on a local model, and the owner can only audit and
# correct what the agent chose to record in the open.

# A credential the owner pastes into chat must not become a permanent injection. This is the
# same guard as the settings scrubber: refuse outright rather than storing and masking later,
# because a stored secret is already in the database.
_SECRETISH = re.compile(r"(?i)\b(api[_-]?key|apikey|password|passwd|secret|token|bearer)\b\s*[:=]?\s*\S")


def _remember_fact(args, ctx):
    from . import chatstore
    key = (args.get("key") or "").strip()
    value = (args.get("value") or "").strip()
    if not key or not value:
        return {"error": "both key and value are required"}
    if _SECRETISH.search(value):
        return {"error": "refusing to store what looks like a credential. Do not put API keys, "
                         "passwords or tokens into memory — they belong in .env, not in a "
                         "conversation or the database."}
    if len(key) > 120:
        return {"error": "key is too long; use a short stable identifier like 'snes_folder'"}
    existed = chatstore.fact(key) is not None
    row = chatstore.remember_fact(key, value, source=args.get("source"))
    return {"saved": key, "updated": existed, "value": row["value"] if row else value,
            "note": f"remembered '{key}'" + (" (replaced the previous value)" if existed else "")}


def _recall_memory(args, ctx):
    from . import chatstore
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    k = min(int(args.get("k") or 5), 20)
    hits = chatstore.recall(query, k=k)
    return {"query": query, "hits": len(hits), "results": hits,
            "note": None if hits else "nothing in memory is close enough to that — this is a "
                                      "gap in what has been recorded, not a search failure"}


def _list_facts(args, ctx):
    from . import chatstore
    rows = chatstore.facts()
    return {"total": len(rows), "facts": rows}


def _forget_fact(args, ctx):
    from . import chatstore
    key = (args.get("key") or "").strip()
    n = chatstore.forget_fact(key)
    if not n:
        return {"error": f"no remembered fact with key {key!r}"}
    return {"forgotten": key}


def _list_items(args, ctx):
    """Same filter semantics as the Library tab (`_item_filter` in web.py) so the assistant
    and the UI can never disagree about what "missing on NES" means."""
    q, p = _item_where(args)
    db = connect()
    total = db.execute(f"SELECT COUNT(*) c FROM ({q})", p).fetchone()["c"]
    limit, offset = _limit(args), max(0, int(args.get("offset") or 0))
    rows = db.execute(q + " ORDER BY system,series,series_number,title LIMIT ? OFFSET ?",
                      p + [limit, offset]).fetchall()
    return {"total": total, "returned": len(rows), "offset": offset,
            "view": args.get("view") or "all", "items": [dict(r) for r in rows]}


def _get_item(args, ctx):
    ident = (args.get("ident") or "").strip()
    if not ident:
        return {"error": "ident is required (an item or volume id, or a title to search)"}
    db = connect()
    row = db.execute("SELECT * FROM items WHERE id=?", (ident,)).fetchone()
    kind = "item"
    if not row:
        row = db.execute("SELECT * FROM volumes WHERE id=?", (ident,)).fetchone()
        kind = "volume"
    if not row:
        # Fall back to a title lookup — an id is awkward for a user to quote in chat.
        row = db.execute("SELECT * FROM items WHERE title=? COLLATE NOCASE", (ident,)).fetchone()
        if row:
            kind = "item"
        else:
            hits = db.execute("SELECT * FROM items WHERE title LIKE ? COLLATE NOCASE LIMIT 5",
                              (f"%{ident}%",)).fetchall()
            if len(hits) == 1:
                row, kind = hits[0], "item"
            elif hits:
                return {"ambiguous": True, "matches": [dict(h) for h in hits],
                        "note": "more than one title matches; call again with an exact id"}
            else:
                return {"error": f"no item or volume matches {ident!r}"}
    out = {"kind": kind, "entity": dict(row)}
    if kind == "item":
        out["aliases"] = [r["alias"] for r in db.execute("SELECT alias FROM aliases WHERE item_id=?", (row["id"],))]
        out["files"] = [dict(r) for r in db.execute(
            "SELECT path,bytes,crc32,match_method,content,scanned_at FROM files"
            " WHERE matched_item_id=? LIMIT ?", (row["id"], DEFAULT_LIMIT))]
        out["in_volumes"] = [dict(r) for r in db.execute(
            "SELECT v.id,v.title,v.status FROM volumes v JOIN volume_covers vc ON vc.volume_id=v.id"
            " WHERE vc.item_id=?", (row["id"],))]
    else:
        out["queries"] = [r["query"] for r in db.execute(
            "SELECT query FROM volume_search WHERE volume_id=?", (row["id"],))]
        out["covers"] = [dict(r) for r in db.execute(
            "SELECT i.id,i.title,i.system,i.status FROM items i JOIN volume_covers vc ON vc.item_id=i.id"
            " WHERE vc.volume_id=? LIMIT ?", (row["id"], DEFAULT_LIMIT))]
    out["events"] = [dict(r) for r in db.execute(
        "SELECT event,detail,created_at FROM events WHERE item_id=? ORDER BY id DESC LIMIT 15",
        (row["id"],))] if kind == "item" else []
    return out


def _library_audit(args, ctx):
    """Items whose status claims a file is in hand but nothing in `files` matches them.

    This is the population behind the recurring "why is this DOWNLOADED with no file?"
    question: `acquirer` marks the *requested* item DOWNLOADED when a download completes
    without confirming what actually arrived, and `status.promote` only ever advances, so a
    false DOWNLOADED is permanent and silently excluded from re-acquisition. Read-only —
    the fix (gate DOWNLOADED on a real match, reconcile the existing rows) is a separate
    change; this is how the assistant *explains* the population instead.
    """
    from .status import OWNED, SATISFIED
    db = connect()
    owned = tuple(OWNED)
    ph = "(" + ",".join("?" * len(owned)) + ")"
    by_status = {r["status"]: r["c"] for r in db.execute(
        f"SELECT status,COUNT(*) c FROM items WHERE status IN {ph} GROUP BY status", owned)}
    no_file = db.execute(
        f"""SELECT i.id,i.title,i.system,i.status,i.source,i.updated_at FROM items i
            WHERE i.status IN {ph}
              AND NOT EXISTS (SELECT 1 FROM files f WHERE f.matched_item_id=i.id)
            ORDER BY i.system,i.title LIMIT ?""", owned + (DEFAULT_LIMIT,)).fetchall()
    total_no_file = db.execute(
        f"""SELECT COUNT(*) c FROM items i WHERE i.status IN {ph}
            AND NOT EXISTS (SELECT 1 FROM files f WHERE f.matched_item_id=i.id)""",
        owned).fetchone()["c"]
    by_system = [dict(r) for r in db.execute(
        f"""SELECT COALESCE(i.system,'unknown') system, COUNT(*) missing FROM items i
            WHERE i.status IN {ph}
              AND NOT EXISTS (SELECT 1 FROM files f WHERE f.matched_item_id=i.id)
            GROUP BY i.system ORDER BY missing DESC""", owned)]
    attribution = [dict(r) for r in db.execute(
        "SELECT COALESCE(match_method,'none') match_method, COUNT(*) files FROM files"
        " WHERE matched_item_id IS NOT NULL GROUP BY match_method ORDER BY files DESC")]
    # Claims to be hash-verified, but only a filename ever matched it.
    sat = tuple(SATISFIED)
    ph2 = "(" + ",".join("?" * len(sat)) + ")"
    satisfied_no_hash = db.execute(
        f"""SELECT COUNT(*) c FROM items i WHERE i.status IN {ph2}
            AND NOT EXISTS (SELECT 1 FROM files f WHERE f.matched_item_id=i.id
                            AND f.match_method LIKE 'hash%')""", sat).fetchone()["c"]
    return {"claimed_in_hand_by_status": by_status,
            "in_hand_with_no_matching_file": total_no_file,
            "by_system": by_system,
            "sample": [dict(r) for r in no_file],
            "file_attribution": attribution,
            "satisfied_without_a_hash_match": satisfied_no_hash,
            "explanation": (
                "A status of DOWNLOADED/FOUND without a `files` row means the pipeline "
                "believed it fetched the item but no file on disk was ever matched to it. "
                "Because statuses only advance, these are excluded from future acquisition "
                "until reconciled: re-scan the download directory, or reset them to MISSING "
                "so the pipeline retries.")}


def _acquire_plan(args, ctx):
    from .planner import bulk_plan, next_individuals
    from . import acquirer
    summ = acquirer.armed_summary()
    vols = bulk_plan()
    return {"eligible": summ["eligible"], "cooling": summ["cooling"],
            "volumes": vols[:20], "volumes_total": len(vols),
            "next_individuals": next_individuals(DEFAULT_LIMIT)}


def _next_picks(args, ctx):
    from .planner import next_picks
    items, total = next_picks(search=args.get("search") or None, system=args.get("system") or None,
                              limit=_limit(args), offset=max(0, int(args.get("offset") or 0)))
    return {"total": total, "returned": len(items), "items": items}


def _download_ledger(args, ctx):
    db = connect()
    limit = _limit(args)
    q = "SELECT j.*, COALESCE(i.title,v.title,j.entity_id) entity_title FROM jobs j" \
        " LEFT JOIN items i ON j.entity_type='item' AND i.id=j.entity_id" \
        " LEFT JOIN volumes v ON j.entity_type='volume' AND v.id=j.entity_id"
    p = []
    if args.get("status"):
        q += " WHERE j.status=?"; p.append(args["status"])
    total = db.execute(f"SELECT COUNT(*) c FROM ({q})", p).fetchone()["c"]
    rows = db.execute(q + " ORDER BY j.id DESC LIMIT ?", p + [limit]).fetchall()
    mix = [dict(r) for r in db.execute(
        "SELECT COALESCE(source,'sab') source, status, COUNT(*) c FROM jobs GROUP BY source,status"
        " ORDER BY c DESC")]
    return {"total": total, "returned": len(rows), "jobs": [dict(r) for r in rows],
            "source_status_mix": mix}


def _watcher_health(args, ctx):
    """Same staleness math as GET /api/acquire/health, so the assistant's answer and the
    header chip can never disagree."""
    from .config import settings
    j = ctx.job_status("acquire") or {}
    beat = j.get("last_beat")
    secs = round(time.monotonic() - beat, 1) if beat else None
    watch_on = bool(settings()["acquire_watch"])
    stale = bool(j.get("running") and secs is not None
                 and secs > max(120.0, settings()["acquire_poll"] * 3))
    state = ("off" if not watch_on else
             ("stale" if stale else "alive") if j.get("running") else "recovering")
    return {"watch_on": watch_on, "running": bool(j.get("running")), "state": state,
            "stale": stale, "last_beat_secs": secs, "error": j.get("error"),
            "current": j.get("current"), "stats": j.get("stats")}


def _job_status(args, ctx):
    kinds = ("import", "scan", "organize", "adopt", "acquire")
    out = {k: {kk: vv for kk, vv in (ctx.job_status(k) or {}).items()
               if kk in ("running", "done", "total", "current", "error")} for k in kinds}
    return {"active": ctx.jobs_active(), "jobs": out}


def _doctor(args, ctx):
    from .doctor import run
    return {"checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in run()]}


def _search_title(args, ctx):
    """Search the indexer (plus direct sources) for an item or volume by id."""
    from . import indexer
    ident = (args.get("ident") or "").strip()
    if not ident:
        return {"error": "ident is required"}
    db = connect()
    kind = "item" if db.execute("SELECT 1 FROM items WHERE id=?", (ident,)).fetchone() else "volume"
    if kind == "volume" and not db.execute("SELECT 1 FROM volumes WHERE id=?", (ident,)).fetchone():
        return {"error": f"no item or volume with id {ident!r}"}
    try:
        results, e = indexer.search_entity(db, kind, ident)
    except PermissionError as ex:
        return {"error": str(ex), "hint": "the item must be marked authorized before it can be searched"}
    except KeyError:
        return {"error": f"unknown {kind}: {ident}"}
    limit = _limit(args)
    return {"kind": kind, "title": e["title"], "result_count": len(results),
            "results": results[:limit]}


# ------------------------------------------------------------------- act tools

def _set_item_flags(args, ctx):
    """One item, one whitelisted field. The only write allowed without a prompt, kept
    narrow on purpose: it is how the assistant resolves "don't want that one" or "search
    for this one" without a confirm card stopping every small correction."""
    from .web import ITEM_FIELDS, VOLUME_FIELDS
    ident = (args.get("ident") or "").strip()
    field = (args.get("field") or "").strip()
    if not ident or not field:
        return {"error": "ident and field are required"}
    db, kind, row = _entity_by_id(ident)
    if not kind:
        return {"error": f"no item or volume with id {ident!r} — use get_item or list_items "
                         f"to find the exact id first"}
    allowed = ITEM_FIELDS if kind == "item" else VOLUME_FIELDS
    if field not in allowed:
        return {"error": f"field must be one of: {', '.join(sorted(allowed))}"}
    value = args.get("value")
    if field in ("authorized", "wanted"):
        value = 1 if _bool(value) else 0
    before = row[field]
    table = "items" if kind == "item" else "volumes"
    with db:
        db.execute(f"UPDATE {table} SET {field}=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (value, ident))
    armed = bool(ctx.arm_acquire()) if kind == "item" and field in ("authorized", "wanted") else False
    return {"updated": {kind: row["title"], "id": row["id"], "field": field,
                        "from": before, "to": value},
            "auto_acquire_started": armed,
            "note": None if armed else "wanted/authorized set; the pipeline was not started"
                    if field in ("authorized", "wanted") else None}


def _acquire_item(args, ctx):
    """Send one item's search result to the download client."""
    from . import actions
    ident = (args.get("ident") or "").strip()
    url = (args.get("url") or "").strip()
    if not ident or not url:
        return {"error": "ident and url are required — get the url from search_title"}
    db, kind, row = _entity_by_id(ident)
    if not kind:
        return {"error": f"no item or volume with id {ident!r}"}
    if not row["authorized"]:
        # The same human-control seam the Acquire tab enforces: the owner authorizes, the
        # assistant acts. Authorizing is itself a tool call, so this is a detour, not a wall.
        return {"error": f"{row['title']} is not marked authorized. Set authorized=true on it "
                         f"first — the library is not supposed to download things you have not "
                         f"approved."}
    nzo = actions.queue_result(db, kind, row, {"url": url,
                                              "title": args.get("title") or row["title"],
                                              "size": args.get("size") or 0})
    return {"queued": row["title"], "nzo_id": nzo, "status": "QUEUED",
            "note": "the download client accepted it; use download_ledger to follow it"}


def _launch_job(ctx, kind, params):
    if not ctx.launch_job(kind, params):
        return {"error": f"a {kind} job is already running — check job_status before retrying"}
    return {"started": kind, "params": params}


def _run_scan(args, ctx):
    path = _checked_path(args.get("path"))
    if not path:
        return {"error": f"path not found: {args.get('path') or '(empty)'} — pass a directory "
                         f"that exists on this machine"}
    return _launch_job(ctx, "scan", {"path": path,
                                     "name_match": _bool(args.get("name_match"), True),
                                     "adopt": _bool(args.get("adopt"), True)})


def _run_import(args, ctx):
    path = _checked_path(args.get("path"))
    if not path:
        return {"error": f"path not found: {args.get('path') or '(empty)'}"}
    return _launch_job(ctx, "import", {"path": path, "system": args.get("system") or None,
                                       "source": args.get("source") or None,
                                       "wanted": _bool(args.get("wanted"))})


def _run_adopt(args, ctx):
    return _launch_job(ctx, "adopt", {"path": (args.get("path") or "").strip()})


def _run_auto_acquire(args, ctx):
    return _launch_job(ctx, "acquire", {})


def _sync_downloads(args, ctx):
    from . import actions
    out = actions.sync()
    return dict(out, note="pulled queue and history from the download client into the ledger")


def _defer_item(args, ctx):
    """Sink an item to the back of the queue after a fruitless search — the same
    acquire-skip event the Acquire tab writes, so `next_picks` (least-recently-tried
    first) genuinely moves on instead of re-hitting the same empty search."""
    ident = (args.get("ident") or "").strip()
    if not ident:
        return {"error": "ident is required"}
    db, kind, row = _entity_by_id(ident)
    if kind != "item":
        return {"error": f"no item with id {ident!r}"}
    with db:
        db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,'acquire-skip',?)",
                   (row["id"], args.get("reason") or "assistant: deferred after a failed search"))
    return {"deferred": {"id": row["id"], "title": row["title"]},
            "effect": "next_picks sinks it and the watcher cools it briefly"}


# --------------------------------------------------------------- confirm-gated tools

_SECRET_HINTS = ("key", "pass", "token", "secret")


def _scrub(values):
    """Never let a credential reach the model's context or the stored transcript. A tool
    result is both, so the value is masked before it is returned, not just before display."""
    return {k: ("***" if v and any(h in k.lower() for h in _SECRET_HINTS) else v)
            for k, v in (values or {}).items()}


def _bulk_update(args, ctx):
    field = (args.get("field") or "").strip()
    if field not in ("wanted", "authorized", "status"):
        return {"error": "field must be wanted, authorized, or status"}
    value = args.get("value")
    if field in ("wanted", "authorized"):
        value = 1 if _bool(value) else 0
    q, p = _item_where(args.get("filters") or {})
    db = connect()
    with db:
        cur = db.execute(f"UPDATE items SET {field}=?,updated_at=CURRENT_TIMESTAMP "
                         f"WHERE id IN (SELECT id FROM ({q}))", [value] + p)
    armed = bool(ctx.arm_acquire()) if field in ("wanted", "authorized") else False
    return {"updated": cur.rowcount, "field": field, "value": value,
            "filters": args.get("filters") or {}, "auto_acquire_started": armed}


def _bulk_confirm(args):
    q, p = _item_where(args.get("filters") or {})
    n = connect().execute(f"SELECT COUNT(*) c FROM ({q})", p).fetchone()["c"]
    return (f"Set {args.get('field')} = {args.get('value')} on {n} item(s)"
            f"{' matching ' + json.dumps(args['filters'], default=str) if args.get('filters') else ' (no filter — the whole catalog)'}")


def _set_series(args, ctx):
    from .manage import set_series
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    try:
        count = set_series(name, args.get("field"), args.get("value"))
    except ValueError as e:
        return {"error": str(e)}
    field = args.get("field")
    armed = bool(ctx.arm_acquire()) if field in ("authorized", "wanted") else False
    return {"updated": count, "series": name, "field": field, "value": args.get("value"),
            "auto_acquire_started": armed}


def _set_series_confirm(args):
    n = connect().execute("SELECT COUNT(*) c FROM items WHERE lower(series)=lower(?)",
                          ((args.get("name") or "").strip(),)).fetchone()["c"]
    return (f"Set {args.get('field')} = {args.get('value')} on all {n} item(s) in the series "
            f"{args.get('name')!r}")


def _mark_owned(args, ctx):
    """Match the /api/library/own route exactly — same rule, and the same deliberate
    absence of an auto-acquire launch: everything this touches is already on disk, so the
    pipeline has nothing to fetch for it."""
    from .status import own_all
    db = connect()
    with db:
        updated = own_all(db)
    return {"updated": updated,
            "note": "everything already on disk now counts as wanted & authorized"}


def _mark_owned_confirm(args):
    db = connect()
    n = db.execute("SELECT COUNT(*) c FROM items WHERE wanted=0 OR authorized=0").fetchone()["c"]
    return f"Mark every item that has a file on disk as wanted & authorized ({n} item(s) are not both today)"


def _organize(args, ctx):
    path = _checked_path(args.get("path"), must_exist=False)
    if not path:
        return {"error": "destination path is required"}
    systems = [s for s in (args.get("systems") or []) if s]
    return _launch_job(ctx, "organize", {"path": path, "systems": systems})


def _organize_confirm(args):
    systems = ", ".join([s for s in (args.get("systems") or []) if s]) or "every system"
    return (f"MOVE files on disk into {args.get('path')!r} ({systems}) — this tool relocates "
            f"real files, so it is the one that most deserves a look before it runs")


def _watcher_toggle(args, ctx):
    on = _bool(args.get("on"))
    return ctx.launch_watcher(on)


def _watcher_confirm(args):
    if _bool(args.get("on")):
        return "Turn the always-on acquisition watcher ON — it will download matching items continuously"
    return "Turn the always-on acquisition watcher OFF — downloads stop until it is turned back on"


def _set_settings(args, ctx):
    values = args.get("values")
    if not isinstance(values, dict) or not values:
        return {"error": "values must be an object of setting name → new value"}
    out = ctx.set_settings(values)
    if isinstance(out, dict) and out.get("error"):
        return out
    return {"saved": _scrub(values), "note": "some settings only take effect on restart"}


def _set_settings_confirm(args):
    values = args.get("values") or {}
    return (f"Change {len(values)} source setting(s): "
            f"{_scrub(values) if values else '(nothing given)'}")


# ------------------------------------------------------------------- registry

def build_registry(ctx=None):
    ctx = ctx or Ctx()
    tools = [
        Tool("library_summary",
             "Overall library state: how many items are cataloged, wanted, on disk, and the "
             "breakdown by lifecycle status and by system. Call this FIRST for any question "
             "about overall progress, coverage or 'how am I doing'.",
             _obj({}), _library_summary),
        Tool("catalog_status",
             "Which DAT catalogs are imported, per system, and which are missing/empty. Use for "
             "'is the NES catalog loaded?' or 'which systems have no catalog yet?'.",
             _obj({}), _catalog_status),
        Tool("facets",
             "The legal filter values: every system name present, and every valid lifecycle "
             "status. Call this before filtering by a system or status so the exact spelling is "
             "correct instead of guessed.",
             _obj({}), _facets),
        Tool("list_items",
             "List catalog items with filters, paging and a total count. Same filters as the "
             "Library tab. Use for 'show me NES items', 'which titles are missing', 'what's "
             "marked FAILED'. Always read `total` — it may exceed what was returned.",
             _obj({"q": dict(_STR, description="substring of title, series or id"),
                   "system": dict(_STR, description="exact system name, e.g. 'Nintendo Entertainment System'"),
                   "series": dict(_STR, description="exact series name"),
                   "status": dict(_STR, description="one lifecycle status, e.g. MISSING, DOWNLOADED"),
                   "view": dict(_STR, enum=["all", "missing", "wanted", "satisfied"],
                                description="missing = wanted with nothing on disk"),
                   "limit": dict(_INT, description=f"max rows (default {DEFAULT_LIMIT}, cap {MAX_LIMIT})"),
                   "offset": dict(_INT, description="skip this many rows")},
                  ["view"]), _list_items),
        Tool("get_item",
             "Full detail for one item or volume: status, flags, aliases, matched files on disk "
             "with how each was matched, which volumes cover it, and its recent event history. "
             "Accepts an exact id or a title (a title must match exactly one item).",
             _obj({"ident": dict(_STR, description="item/volume id, or a title")}, ["ident"]),
             _get_item),
        Tool("library_audit",
             "Diagnostic: items whose status claims a file is in hand (DOWNLOADED/FOUND/VERIFIED…) "
             "but no file on disk is matched to them, grouped by system, plus how files were "
             "attributed (hash vs filename only). Use this for 'why is this item stuck?', 'why is "
             "something DOWNLOADED with no file?', and any 'what went wrong' question. Read-only.",
             _obj({}), _library_audit),
        Tool("acquire_plan",
             "What the acquisition pipeline would do next: how many armed items are eligible now "
             "vs held back by a cooldown, ranked volume bundles, and the next individual picks.",
             _obj({}), _acquire_plan),
        Tool("next_picks",
             "The individual items the pipeline would search next, paged and filterable, "
             "least-recently-tried first.",
             _obj({"search": dict(_STR, description="title or id substring"),
                   "system": dict(_STR), "limit": dict(_INT), "offset": dict(_INT)}),
             _next_picks),
        Tool("download_ledger",
             "The acquisition ledger: recent download jobs with their status, plus a source × "
             "status summary. This is the record of what was actually fetched and what happened "
             "to it.",
             _obj({"status": dict(_STR, description="filter to one job status, e.g. QUEUED, FAILED, DOWNLOADED"),
                   "limit": dict(_INT)}), _download_ledger),
        Tool("watcher_health",
             "Liveness of the always-on acquisition watcher: is it on, is the thread running, how "
             "long since its last heartbeat, and is it stale. Call this whenever downloads seem "
             "to have stopped — 'is the watcher alive?' is the first question for that.",
             _obj({}), _watcher_health),
        Tool("job_status",
             "Status of the background jobs (import, scan, organize, adopt, acquire): which are "
             "running, their progress and any error.",
             _obj({}), _job_status),
        Tool("doctor",
             "Connectivity and configuration health checks: database, indexer config and API, "
             "SABnzbd config and API. Use when a source appears to be failing.",
             _obj({}), _doctor),
        Tool("search_title",
             "Search the indexer (plus the direct download sources) live for one item or volume by "
             "id, and return ranked candidate releases with sizes. This is a real, rate-limited "
             "network search — use it to answer 'can this be found right now?' or to gather URLs "
             "for a later acquire. The item must be authorized.",
             _obj({"ident": dict(_STR, description="item or volume id to search for")}, ["ident"]),
             _search_title, risk="medium"),

        # --- act: narrow, reversible, no prompt -----------------------------
        Tool("set_item_flags",
             "Change one field on ONE item or volume — most often authorized or wanted, which "
             "is how you arm or hold back a title. This is the normal way to act on a single "
             "item. Call get_item first if you are not certain of the exact id.",
             _obj({"ident": dict(_STR, description="exact item or volume id"),
                   "field": dict(_STR, description="authorized, wanted, status, system, region, "
                                                   "language, preferred_runtime, notes, play_status "
                                                   "(items) or authorized, status (volumes)"),
                   "value": dict(description="the new value; booleans may be true/false")},
                  ["ident", "field", "value"]), _set_item_flags, risk="medium"),
        Tool("acquire_item",
             "Queue one specific search result for download, using a url that search_title "
             "returned. The item must already be authorized — authorize it first if it is not. "
             "Prefer this over bulk operations when the owner named a specific title.",
             _obj({"ident": dict(_STR, description="exact item or volume id"),
                   "url": dict(_STR, description="the result url, from search_title"),
                   "title": dict(_STR, description="the release title, for the ledger"),
                   "size": dict(_INT, description="bytes, if known")},
                  ["ident", "url"]), _acquire_item, risk="medium"),
        Tool("run_scan",
             "Scan an existing directory of files into the library, matching them to catalog "
             "items by hash and (optionally) by name. Use for 'rescan my downloads folder' or "
             "after files have been added by hand.",
             _obj({"path": dict(_STR, description="directory that already exists on this machine"),
                   "name_match": dict(_BOOL, description="also match by filename (default true)"),
                   "adopt": dict(_BOOL, description="adopt unmatched files (default true)")},
                  ["path"]), _run_scan, risk="medium"),
        Tool("run_adopt",
             "Adopt unmatched files — files on disk that no catalog item claims — starting from "
             "an optional root directory. Use when scanning leaves files unaccounted for.",
             _obj({"path": dict(_STR, description="root directory; omit for the configured default")}),
             _run_adopt, risk="medium"),
        Tool("run_import_dats",
             "Import a DAT catalog file so its titles become known items. Use when a system has "
             "no catalog yet, or after downloading a fresh DAT. Check catalog_status first.",
             _obj({"path": dict(_STR, description="path to the .dat/.xml file"),
                   "system": dict(_STR, description="system name for the catalog"),
                   "source": dict(_STR, description="catalog source label, e.g. no-intro"),
                   "wanted": dict(_BOOL, description="mark imported items as wanted")},
                  ["path"]), _run_import, risk="medium"),
        Tool("start_auto_acquire",
             "Start one acquisition pass: search the indexers for eligible armed items and send "
             "what it finds to the download client. This is the manual 'run it now' button — it "
             "runs one pass and stops. Use it after arming items, not in a loop.",
             _obj({}), _run_auto_acquire, risk="medium"),
        Tool("sync_downloads",
             "Pull the download client's queue and history into the job ledger, updating item "
             "statuses. Use when the ledger looks stale or a finished download never showed up.",
             _obj({}), _sync_downloads, risk="medium"),
        Tool("defer_item",
             "Sink one item to the back of the acquisition queue after a search found nothing "
             "usable, so the pipeline moves on instead of re-trying the same empty search.",
             _obj({"ident": dict(_STR, description="exact item id"),
                   "reason": dict(_STR, description="why it is being deferred")},
                  ["ident"]), _defer_item, risk="medium"),

        # --- confirm-gated: bulk, disk-moving, or config --------------------
        Tool("bulk_update_items",
             "Change a field on MANY items at once, selected by the same filters as list_items. "
             "This needs the owner's confirmation before it runs — the call returns a card "
             "describing how many items it would change, and does not change anything itself.",
             _obj({"filters": dict(type="object", description="same filters as list_items: "
                                                              "q, system, status, series, view"),
                   "field": dict(_STR, enum=["wanted", "authorized", "status"]),
                   "value": dict(description="the new value")},
                  ["field", "value"]), _bulk_update, risk="high", confirm=_bulk_confirm),
        Tool("set_series",
             "Change a field on every item in a series at once. Needs confirmation.",
             _obj({"name": dict(_STR, description="series name"),
                   "field": dict(_STR, description="the field to change"),
                   "value": dict(description="the new value")},
                  ["name", "field", "value"]), _set_series, risk="high", confirm=_set_series_confirm),
        Tool("mark_all_owned",
             "Mark everything already on disk as wanted & authorized, so coverage and the "
             "missing list agree with the collection. Affects the whole library, so it needs "
             "confirmation.",
             _obj({}), _mark_owned, risk="high", confirm=_mark_owned_confirm),
        Tool("organize_library",
             "Move and rename files on disk into a destination directory, organised by system. "
             "The only tool that relocates real files, and therefore always confirmed.",
             _obj({"path": dict(_STR, description="destination directory"),
                   "systems": dict(type="array", items=_STR,
                                   description="limit to these systems; omit for all")},
                  ["path"]), _organize, risk="high", confirm=_organize_confirm),
        Tool("watcher_toggle",
             "Turn the always-on acquisition watcher on or off. It downloads continuously while "
             "on, so both directions need confirmation — a silent stop is worse than a prompt.",
             _obj({"on": dict(_BOOL, description="true to turn the watcher on, false to stop it")},
                  ["on"]), _watcher_toggle, risk="high", confirm=_watcher_confirm),
        Tool("set_setting",
             "Change library settings (indexer, download client, acquisition pacing and so on). "
             "Needs confirmation. Credential values are masked in the confirmation and in this "
             "conversation — never ask the owner to type a key into chat.",
             _obj({"values": dict(type="object",
                                  description="setting name → new value, e.g. {\"acquire_batch_max\": \"5\"}")},
                  ["values"]), _set_settings, risk="high", confirm=_set_settings_confirm),

        # --- memory --------------------------------------------------------
        Tool("remember_fact",
             "Record something durable that should still be true in a later conversation: a "
             "preference, a decision, a naming convention, the path to something they keep "
             "asking about. Use a short stable key ('snes_folder') so re-remembering the same "
             "thing updates it rather than piling up near-duplicates. Facts are injected into "
             "every future turn, so record *conclusions*, not narration — and never record a "
             "credential, key or password.",
             _obj({"key": dict(_STR, description="short stable identifier, e.g. 'snes_folder'"),
                   "value": dict(_STR, description="what to remember"),
                   "source": dict(_STR, description="where this came from (optional)")},
                  ["key", "value"]), _remember_fact),
        Tool("recall_memory",
             "Search your memory of past conversations and notes by meaning, not keyword. Use "
             "when the owner refers to something from an earlier session ('the folder we talked "
             "about'), or before asking them to repeat a preference.",
             _obj({"query": dict(_STR, description="what to look for"),
                   "k": dict(_INT, description="how many hits (default 5)")},
                  ["query"]), _recall_memory),
        Tool("list_facts",
             "Everything you have recorded in durable memory. Use to check what you already "
             "know, or to find the key of a fact that is now out of date.",
             _obj({}), _list_facts),
        Tool("forget_fact",
             "Delete one durable memory by its key. Use when a remembered fact is wrong or no "
             "longer applies — leaving it would keep injecting a falsehood into every turn.",
             _obj({"key": dict(_STR, description="the fact's key, from list_facts")}, ["key"]),
             _forget_fact),
    ]
    for t in tools:
        t.ctx = ctx
    return {t.name: t for t in tools}


# ------------------------------------------------------------------- dispatch

def _confirm_summary(tool, args):
    if tool.confirm:
        try:
            return tool.confirm(args)
        except Exception as e:
            return f"{tool.name} ({type(e).__name__} while describing it: {e})"
    return f"{tool.name} with {json.dumps(args, default=str)[:300]}"


def _canon(text):
    """Canonical form of a stored arguments blob, so two spellings of the same call compare
    equal."""
    try:
        return json.dumps(json.loads(text or "{}"), sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(text)


def _gate(tool, args, session_id, approval_id):
    """The confirm contract for `high`-risk tools, in one place for both MCP directions and
    the in-app agent — so there is no second path around it.

    Without an approval: record the request and hand back the card to show. With one: the
    approval must be *this* tool and *these* arguments. Comparing them rather than trusting
    the caller is what stops an approval for one call being spent on another.
    """
    from . import chatstore
    if not approval_id:
        summary = _confirm_summary(tool, args)
        aid = chatstore.request_approval(session_id, tool.name, args, summary)
        chatstore.log_call(session_id, tool.name, args, False, tool.risk,
                           f"awaiting approval #{aid}")
        return {"ok": False, "requires_approval": True, "approval_id": aid,
                "summary": summary, "risk": tool.risk,
                "error": f"NOT RUN — this needs the owner's confirmation first (approval #{aid}). "
                         f"Nothing has changed. Tell them what you are asking to do and stop; "
                         f"the call will resume by itself once they answer."}
    row = chatstore.approval(approval_id)
    if not row or row["status"] != "approved":
        # Distinguish "already spent" from "never approved": the model should be able to tell
        # a call that has already run from one the owner refused.
        if row and row["status"] == "used":
            return {"ok": False, "risk": tool.risk,
                    "error": f"approval {approval_id} has already been used"}
        return {"ok": False, "risk": tool.risk,
                "error": f"approval {approval_id} is not an approved request"}
    if row["tool"] != tool.name or _canon(row["arguments"]) != _canon(json.dumps(args, default=str)):
        return {"ok": False, "risk": tool.risk,
                "error": f"approval {approval_id} does not match this call"}
    # Spending it here, atomically, is what stops an approval id being a standing permission:
    # the row is consumed by the run it authorises and by no other.
    if not chatstore.spend_approval(approval_id):
        return {"ok": False, "risk": tool.risk,
                "error": f"approval {approval_id} has already been used"}
    return None


def _invoke(tool, args, session_id, name=None):
    """The single implementation of "run a tool and shape the result" — reached by the
    gated path and the ungated one alike, so there is no second way to execute a tool."""
    from . import chatstore
    name = name or tool.name
    try:
        result = tool.fn(args, tool.ctx)
    except Exception as e:
        chatstore.log_call(session_id, name, args, False, tool.risk, f"{type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "risk": tool.risk}
    data, text, note = _fit(result)
    ok = not (isinstance(result, dict) and result.get("error"))
    chatstore.log_call(session_id, name, args, ok, tool.risk, note or text[:200])
    return {"ok": ok, "data": data, "truncated": bool(note), "note": note, "risk": tool.risk}


def dispatch(registry, name, args, session_id=None, approval_id=None):
    """Run one tool call and return the envelope the agent loop and the SSE stream consume.

    `{"ok": True, "data": …, "truncated": bool, "note": str|None, "risk": str}`
    or `{"ok": False, "error": str, "risk": str}`
    or `{"ok": False, "requires_approval": True, "approval_id": int, "summary": str}`.

    A tool raising is a normal outcome, not a crash: the error is handed back to the model so
    it can correct its own call, and the turn continues.
    """
    tool = registry.get(name)
    if not tool:
        return {"ok": False, "error": f"unknown tool: {name}", "available": sorted(registry)}
    if not isinstance(args, dict):
        args = {}
    if tool.risk == "high":
        refusal = _gate(tool, args, session_id, approval_id)
        if refusal:
            return refusal
    return _invoke(tool, args, session_id)


def apply_approval(registry, approval_id):
    """Run an approved call. The tool and its arguments come from the stored row, never from
    the caller — that is what makes an approval un-reusable for anything else. Spending the
    row before running it means the permission is consumed exactly once even if two callers
    race for it."""
    from . import chatstore
    row = chatstore.spend_approval(approval_id)
    if not row:
        existing = chatstore.approval(approval_id)
        if not existing:
            return {"ok": False, "error": f"no such approval: {approval_id}"}
        return {"ok": False, "error": f"approval {approval_id} is {existing['status']} — only a "
                                      f"freshly approved request can be run, and each runs once"}
    tool = registry.get(row["tool"])
    if not tool:
        return {"ok": False, "error": f"unknown tool: {row['tool']}"}
    args = json.loads(row["arguments"] or "{}") if row["arguments"] else {}
    result = _invoke(tool, args, row["session_id"])
    chatstore.record_approval_result(approval_id, result)
    return result


def declined(tool_name, args=None, session_id=None):
    """The envelope for a call the owner refused. Handed back through the same channel as a
    tool result, so the model sees its own call answered rather than left dangling."""
    from . import chatstore
    chatstore.log_call(session_id, tool_name, args or {}, False, "high", "declined by owner")
    return {"ok": False, "risk": "high",
            "error": f"the owner DECLINED {tool_name}. It did not run and nothing changed. "
                     f"Do not ask for the same call again — say what you would do instead, or "
                     f"ask what they would prefer."}
