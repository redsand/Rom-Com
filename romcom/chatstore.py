"""Chat persistence: sessions, messages, the tool audit log.

Stage 2 scope is deliberately narrow — sessions and messages only. Facts, embedded chunks
and approvals are added by the stages that use them (§D/§B of the plan), so nothing here
carries speculative columns or dead helpers.

**Storage round-trips exactly into Ollama's message shape**, which is the one thing worth
being careful about:

| stored row | rebuilt as |
|---|---|
| `role='user'` | `{"role": "user", "content": …}` |
| `role='assistant'` | `{"role": "assistant", "content": …, "tool_calls": json(tool_args)}` |
| `role='tool'` | `{"role": "tool", "content": …, "tool_name": tool_name}` |

Ollama matches a tool result to its call **by name, not by id**, so no call id needs
storing — `tool_name` alone is sufficient and the schema stays as designed in Stage 0.
`thinking` is kept for the UI's reasoning bubble but is never replayed into the model's
context: re-feeding a model its own prior reasoning measurably degrades local models and
inflates the prompt for nothing.
"""
import json
from .db import connect

# Tool output is the bulk of a tool-heavy thread and the least valuable part to replay.
# Capped at insert, so the stored copy (which is what context and any future summarizer
# read) never carries a 40 KB `library_summary`. The in-flight turn still reasons over the
# full result — only the persisted copy is trimmed.
TOOL_RESULT_CAP = 2000


def _now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _truncate(text, cap=TOOL_RESULT_CAP):
    text = text or ""
    if len(text) <= cap:
        return text
    return text[:cap] + f" …[truncated, {len(text)} chars total]"


# ------------------------------------------------------------------------------- sessions

def new_session(title=None):
    db = connect()
    with db:
        cur = db.execute("INSERT INTO chat_sessions(title,updated_at) VALUES(?,?)",
                         ((title or "").strip()[:120] or None, _now()))
    return cur.lastrowid


def session(sid):
    return connect().execute("SELECT * FROM chat_sessions WHERE id=?", (sid,)).fetchone()


def sessions(limit=50):
    return [dict(r) for r in connect().execute(
        "SELECT s.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) messages,"
        " (SELECT content FROM chat_messages m WHERE m.session_id=s.id AND m.role='user'"
        "  ORDER BY m.id LIMIT 1) first_user"
        " FROM chat_sessions s ORDER BY s.updated_at DESC, s.id DESC LIMIT ?", (int(limit),))]


def touch(sid, title=None):
    db = connect()
    with db:
        if title:
            db.execute("UPDATE chat_sessions SET updated_at=?, title=COALESCE(NULLIF(title,''),?) WHERE id=?",
                       (_now(), title[:120], sid))
        else:
            db.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (_now(), sid))


def delete_session(sid):
    """Removes the thread and its messages. Approvals and tool-log rows keep their session
    id as a dangling reference on purpose — they are an audit trail, and an audit trail
    that disappears when someone tidies up their chat list is worthless."""
    db = connect()
    with db:
        db.execute("DELETE FROM chat_messages WHERE session_id=?", (sid,))
        cur = db.execute("DELETE FROM chat_sessions WHERE id=?", (sid,))
    return cur.rowcount


# ------------------------------------------------------------------------------- messages

def append(sid, role, content="", thinking=None, tool_name=None, tool_args=None):
    """One message. Returns its id."""
    if role == "tool":
        content = _truncate(content)
    db = connect()
    with db:
        cur = db.execute(
            "INSERT INTO chat_messages(session_id,role,content,thinking,tool_name,tool_args)"
            " VALUES(?,?,?,?,?,?)",
            (sid, role, content or "",
             thinking or None, tool_name or None,
             json.dumps(tool_args) if tool_args is not None else None))
    return cur.lastrowid


def history(sid, limit=None):
    """Messages oldest-first, as plain dicts. `limit` keeps the most recent N."""
    db = connect()
    if limit:
        rows = db.execute("SELECT * FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                          (sid, int(limit))).fetchall()
        rows = list(reversed(rows))
    else:
        rows = db.execute("SELECT * FROM chat_messages WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    return [dict(r) for r in rows]


def to_messages(rows):
    """Rebuild stored rows into Ollama's message shape (see the module docstring)."""
    out = []
    for r in rows:
        role = r.get("role")
        m = {"role": role, "content": r.get("content") or ""}
        if role == "assistant" and r.get("tool_args"):
            try:
                m["tool_calls"] = json.loads(r["tool_args"])
            except (TypeError, ValueError):
                m["tool_calls"] = []
        if role == "tool":
            m["tool_name"] = r.get("tool_name") or ""
        out.append(m)
    return out


def set_thinking(mid, text):
    """Backfill a message's reasoning once its stream finishes."""
    db = connect()
    with db:
        db.execute("UPDATE chat_messages SET thinking=? WHERE id=?", (text or None, mid))


def drop_after(sid, mid):
    """Remove messages after `mid` — used when a turn is abandoned mid-stream so a
    half-written assistant reply is not replayed as context on the next turn."""
    db = connect()
    with db:
        cur = db.execute("DELETE FROM chat_messages WHERE session_id=? AND id>?", (sid, mid))
    return cur.rowcount


# --------------------------------------------------------------------------- approvals

def request_approval(session_id, tool, arguments, summary):
    """Record a gated call awaiting the owner's decision. Persisted rather than held in
    memory so an approval survives a server restart — the assistant asked, and the answer
    to a question that outlives the process should still count."""
    db = connect()
    with db:
        cur = db.execute(
            "INSERT INTO chat_approvals(session_id,tool,arguments,summary) VALUES(?,?,?,?)",
            (session_id, tool, json.dumps(arguments or {}, default=str)[:4000],
             (summary or "")[:500]))
    return cur.lastrowid


def approval(aid):
    return connect().execute("SELECT * FROM chat_approvals WHERE id=?", (aid,)).fetchone()


def decide_approval(aid, approved):
    """Flip a *pending* approval. Returns the updated row, or None if it was already
    decided — the `status='pending'` predicate is what makes "approve executes once" true
    under a double-click or two open tabs, rather than a check the caller must remember."""
    db = connect()
    with db:
        cur = db.execute(
            "UPDATE chat_approvals SET status=?,decided_at=? WHERE id=? AND status='pending'",
            ("approved" if approved else "declined", _now(), aid))
    return approval(aid) if cur.rowcount else None


def spend_approval(aid):
    """Claim an approved request for exactly one run, atomically.

    The `status='approved'` predicate in the UPDATE *is* the mutex: an approved row is
    consumed the moment it is claimed, so a second caller — a double-click, a retry, a
    second process — finds nothing to claim rather than running the call again. Without
    this, an approval id stays a standing permission for whatever it named.
    """
    db = connect()
    with db:
        cur = db.execute("UPDATE chat_approvals SET status='used' WHERE id=? AND status='approved'",
                         (aid,))
    return approval(aid) if cur.rowcount else None


def record_approval_result(aid, result):
    db = connect()
    with db:
        db.execute("UPDATE chat_approvals SET result=? WHERE id=?",
                   (json.dumps(result, default=str)[:2000], aid))


def approvals(sid=None, status=None, limit=50):
    q, p = "SELECT * FROM chat_approvals WHERE 1=1", []
    if sid is not None:
        q += " AND session_id=?"; p.append(sid)
    if status:
        q += " AND status=?"; p.append(status)
    return [dict(r) for r in connect().execute(
        q + " ORDER BY id DESC LIMIT ?", p + [int(limit)])]


def pending_approvals(sid=None):
    return approvals(sid=sid, status="pending")


# ------------------------------------------------------------------------------- audit

def log_call(session_id, tool, arguments, ok, risk=None, result_summary=""):
    """One row per dispatch. Errors in the log must never take down the call it is
    recording, so this is best-effort."""
    try:
        db = connect()
        with db:
            db.execute("INSERT INTO chat_tool_log(session_id,tool,arguments,ok,risk,result_summary)"
                       " VALUES(?,?,?,?,?,?)",
                       (session_id, tool,
                        json.dumps(arguments or {}, default=str)[:2000],
                        1 if ok else 0, risk, (result_summary or "")[:500]))
    except Exception:
        pass


def recent_calls(session_id=None, limit=100):
    db = connect()
    if session_id:
        rows = db.execute("SELECT * FROM chat_tool_log WHERE session_id=? ORDER BY id DESC LIMIT ?",
                          (session_id, int(limit)))
    else:
        rows = db.execute("SELECT * FROM chat_tool_log ORDER BY id DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in rows]
