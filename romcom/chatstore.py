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
        # Its summary chunk goes too, or recall keeps surfacing a conversation the owner
        # deliberately got rid of.
        db.execute("DELETE FROM chat_memory_chunks WHERE kind='session_summary' AND ref_id=?",
                   (str(sid),))
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


# ------------------------------------------------------------------------------- facts
#
# Durable facts are written by an explicit `remember_fact` tool call, not extracted after
# every turn. Extraction would mean a *second* model call per turn on a local model; the
# owner's "advanced memory" is served by the agent choosing what to remember, which is also
# the only version the owner can audit and correct.

def remember_fact(key, value, source=None, confidence=1.0):
    """Keyed UPSERT. Re-remembering a key refreshes `updated_at` rather than duplicating —
    'the owner's preferred SNES folder' should have one current answer, not five."""
    key = (key or "").strip()[:120]
    if not key:
        return None
    db = connect()
    with db:
        db.execute(
            "INSERT INTO chat_facts(key,value,source,confidence,updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source,"
            " confidence=excluded.confidence, updated_at=excluded.updated_at",
            (key, str(value)[:4000], source, float(confidence), _now()))
    return fact(key)


def fact(key):
    row = connect().execute("SELECT * FROM chat_facts WHERE key=?", ((key or "").strip()[:120],)).fetchone()
    return dict(row) if row else None


def facts(limit=100):
    rows = connect().execute(
        "SELECT * FROM chat_facts ORDER BY updated_at DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in rows]


def forget_fact(key):
    db = connect()
    with db:
        cur = db.execute("DELETE FROM chat_facts WHERE key=?", ((key or "").strip()[:120],))
    return cur.rowcount


# --------------------------------------------------------------------------- embeddings
#
# Cosine over a personal library's few thousand chunks is a sub-10 ms pure-Python loop once
# each row's norm is precomputed — numpy would be a dependency for no measurable gain.
#
# Every row stores the model that produced it and its dimension, and `recall` filters on the
# model. That is the dimension lock: switching to `bge-m3` (1024-dim) cannot silently cosine
# against 768-dim `nomic-embed-text` rows, because the mismatch is refused at write time
# rather than producing meaningless scores at read time.

EMBED_CACHE_MAX = 500
EMBED_CACHE_TTL = 1800          # seconds
_embed_cache = {}               # sha256(text) -> (vector, model, dim, monotonic)

# The recall floor is deliberately 0.45 and not the 0.35 the plan inherited from the sister
# app. That number was measured against a different embed model, and `nomic-embed-text` has a
# much higher baseline: measured live, genuinely-relevant pairs score 0.69 and *unrelated*
# English sentences still score 0.30-0.38. At 0.35 an off-topic question ("what is the weather
# in Paris?") pulled a library chunk into the turn — memory becoming noise on every message.
# 0.45 sits in the measured gap. Re-measure before changing it.
RECALL_MIN_SCORE = 0.45


def _cache_get(key):
    import time
    hit = _embed_cache.get(key)
    if not hit:
        return None
    vec, model, dim, at = hit
    if time.monotonic() - at > EMBED_CACHE_TTL:
        _embed_cache.pop(key, None)
        return None
    return vec, model, dim


def _cache_put(key, vec, model, dim):
    import time
    if len(_embed_cache) >= EMBED_CACHE_MAX:
        oldest = min(_embed_cache, key=lambda k: _embed_cache[k][3])
        _embed_cache.pop(oldest, None)
    _embed_cache[key] = (vec, model, dim, time.monotonic())


def _vector(blob):
    import array
    a = array.array("f")
    a.frombytes(blob)
    return a


def _norm(vec):
    return sum(float(x) * float(x) for x in vec) ** 0.5


def embed_cached(texts):
    """Embed with an in-process LRU. Returns `(vectors, dim, model)`; raises on an Ollama
    failure, because a caller that asked for vectors must not silently receive none.

    The cache key is **(model, text)**, not the text alone. Keying on text alone looks
    harmless — the dimension lock at insert would catch a model swap that changes the vector
    size — but a same-dimension swap (two different 768-dim models) would serve vectors from
    the old model straight out of the cache, and those are not comparable. The model name is
    part of the identity of a vector.
    """
    import hashlib
    from . import llmclient
    from .config import settings
    if isinstance(texts, str):
        texts = [texts]
    model_hint = settings().get("chat_embed_model") or ""
    keys = [hashlib.sha256(f"{model_hint}\x00{t}".encode("utf-8", "replace")).hexdigest()
            for t in texts]
    out = [None] * len(texts)
    missing = []
    for i, k in enumerate(keys):
        hit = _cache_get(k)
        if hit:
            out[i] = hit[0]
        else:
            missing.append(i)
    if missing:
        vecs, dim, model = llmclient.embed([texts[i] for i in missing])
        if len(vecs) < len(missing):
            raise RuntimeError("embedding model returned fewer vectors than inputs")
        for slot, i in enumerate(missing):
            out[i] = vecs[slot]
            _cache_put(keys[i], vecs[slot], model, dim)
        return out, dim, model
    # Fully cached: every hit carries the same model+dim, but they may have come from
    # different calls, so report the most recently used one's.
    vec, model, dim = _cache_get(keys[0])
    return out, dim, model


def store_chunk(kind, text, ref_id=None):
    """Embed and store one memory chunk. Returns the new row id, or None if `text` is blank.

    Refuses a vector whose dimension disagrees with what this model already holds — a loud
    failure the owner can see, rather than a store quietly containing two incompatible
    vector spaces."""
    text = (text or "").strip()
    if not text:
        return None
    vecs, dim, model = embed_cached([text[:8000]])
    vec = vecs[0]
    db = connect()
    existing = db.execute(
        "SELECT dim FROM chat_memory_chunks WHERE model=? LIMIT 1", (model,)).fetchone()
    if existing and existing["dim"] != dim:
        raise RuntimeError(
            f"embedding dimension changed for {model}: stored {existing['dim']}-dim, "
            f"got {dim}-dim. Refusing to mix vector spaces — clear chat_memory_chunks "
            f"for this model (or switch chat_embed_model back) and re-ingest.")
    import array
    blob = array.array("f", vec).tobytes()
    with db:
        cur = db.execute(
            "INSERT INTO chat_memory_chunks(kind,ref_id,text,embedding,dim,model,norm)"
            " VALUES(?,?,?,?,?,?,?)",
            (kind, None if ref_id is None else str(ref_id)[:120], text[:8000], blob, dim,
             model, _norm(vec)))
    return cur.lastrowid


def remember_chunks(kind, texts, ref_id=None):
    """Bulk form for summarizer output. One embedding call, one transaction."""
    texts = [t for t in (texts or []) if (t or "").strip()]
    if not texts:
        return []
    vecs, dim, model = embed_cached([t[:8000] for t in texts])
    db = connect()
    existing = db.execute(
        "SELECT dim FROM chat_memory_chunks WHERE model=? LIMIT 1", (model,)).fetchone()
    if existing and existing["dim"] != dim:
        raise RuntimeError(
            f"embedding dimension changed for {model}: stored {existing['dim']}-dim, got "
            f"{dim}-dim. Refusing to mix vector spaces.")
    import array
    ids = []
    with db:
        for i, t in enumerate(texts):
            cur = db.execute(
                "INSERT INTO chat_memory_chunks(kind,ref_id,text,embedding,dim,model,norm)"
                " VALUES(?,?,?,?,?,?,?)",
                (kind, None if ref_id is None else str(ref_id)[:120], t[:8000],
                 array.array("f", vecs[i]).tobytes(), dim, model, _norm(vecs[i])))
            ids.append(cur.lastrowid)
    return ids


def recall(query, k=5, min_score=None, kind=None):
    """Top-`k` chunks by cosine similarity, above `min_score`, same model only.

    Returns `[{id, kind, ref_id, text, score}]`. Never raises for an Ollama failure: recall is
    an enhancement to a turn, and a turn that dies because the embedding model is down is
    strictly worse than a turn with no recalled context."""
    if min_score is None:
        min_score = RECALL_MIN_SCORE
    try:
        if not (query or "").strip():
            return []
        vecs, dim, model = embed_cached([query[:8000]])
    except Exception:
        return []
    q = vecs[0]
    qn = _norm(q) or 1.0
    db = connect()
    sql = "SELECT id,kind,ref_id,text,embedding,dim,norm FROM chat_memory_chunks WHERE model=?"
    params = [model]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    scored = []
    for r in db.execute(sql, params).fetchall():
        if r["dim"] != dim:          # belt and braces alongside the insert-time refusal
            continue
        v = _vector(r["embedding"])
        if len(v) != dim:
            continue
        dot = 0.0
        for a, b in zip(v, q):
            dot += float(a) * float(b)
        score = dot / ((r["norm"] or 1.0) * qn)
        if score >= min_score:
            scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = [{"id": r["id"], "kind": r["kind"], "ref_id": r["ref_id"], "text": r["text"],
            "score": round(s, 4)} for s, r in scored[:int(k)]]
    if out:
        with db:
            db.executemany("UPDATE chat_memory_chunks SET last_accessed=? WHERE id=?",
                           [(_now(), o["id"]) for o in out])
    return out


def chunk_count():
    return connect().execute("SELECT COUNT(*) c FROM chat_memory_chunks").fetchone()["c"]


def forget_chunks(ref_id=None, kind=None):
    """Drop chunks by ref (a session id) or kind — used when a session is deleted, so its
    summary stops surfacing in recall for a conversation that no longer exists."""
    db = connect()
    sql, params = "DELETE FROM chat_memory_chunks WHERE 1=1", []
    if ref_id is not None:
        sql += " AND ref_id=?"; params.append(str(ref_id))
    if kind is not None:
        sql += " AND kind=?"; params.append(kind)
    with db:
        cur = db.execute(sql, params)
    return cur.rowcount


# ------------------------------------------------------------------------- summarization
#
# Trigger 30 / keep 20 / re-summarize every 15. The sister app uses 40/30/15; sized down
# because Rom-Com turns are tool-heavy (so 30 messages is closer to 6 exchanges than 15) and
# local inference is slow, so each extra summarization call is expensive. Re-summarizing
# every 15 rather than every turn past the trigger keeps it to roughly one call per five
# exchanges.
#
# The verbatim window is what the model actually reasons over; the summary is the safety net
# that keeps a long thread from silently losing its beginning. Tool results are capped at
# insert (TOOL_RESULT_CAP), so a 40 KB library_summary never reaches this prompt.

SUMMARY_TRIGGER = 30        # unsummarized messages before we compress
SUMMARY_KEEP = 20           # always left verbatim after the summary
SUMMARY_EVERY = 15          # new messages before re-summarizing again

_SUMMARY_PROMPT = """Compress this conversation transcript into a durable memory note for \
yourself. Keep, in this order:

1. What the owner asked for and what was concluded — the answers, not the narration.
2. Concrete facts learned about their library: item ids, titles, systems, counts, and any \
decision they made (things they approved, declined, or said they wanted).
3. Anything still open or unresolved at the end.

Drop: tool-call mechanics, JSON payloads, restated pleasantries, and reasoning. Write \
compact prose and short bullet lists, under 400 words. Do not invent anything that is not \
in the transcript. If an earlier summary is given, fold it in rather than repeating it."""


def _unsummarized(sid):
    """Messages newer than the summarization watermark, oldest first."""
    row = connect().execute("SELECT summarized_to FROM chat_sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        return []
    return [m for m in history(sid) if m["id"] > (row["summarized_to"] or 0)]


def maybe_summarize(sid, model=None, emit=None):
    """Fold the oldest unsummarized messages into `chat_sessions.summary`.

    Returns the new summary text, or None if nothing was due or the model was unavailable.
    **Never raises and never advances the watermark on failure** — a summary that failed to
    generate must be retried on the next turn, not silently skipped forever.

    Called *before* the turn's model call: the summary is input to that call, and doing it
    afterwards would mean the first exchange past the trigger sees a stale window.
    """
    def say(name, payload):
        if emit:
            emit(name, payload)
    try:
        pending = _unsummarized(sid)
        if len(pending) < SUMMARY_TRIGGER:
            return None
        # Everything except the newest SUMMARY_KEEP messages gets compressed.
        batch = pending[:-SUMMARY_KEEP] if len(pending) > SUMMARY_KEEP else pending
        if not batch:
            return None
        # `session()` hands back a sqlite3.Row, so this is a subscript, not `.get()`.
        srow = session(sid)
        prior = (srow["summary"] if srow else None) or ""
        transcript = "\n".join(
            f"[{m['role']}] {m['content']}" for m in batch if (m["content"] or "").strip())
        if not transcript.strip():
            # Nothing but empty rows: advance the watermark so it can't wedge the trigger.
            _set_summary(sid, prior, batch[-1]["id"])
            return None

        from . import llmclient
        msgs = [{"role": "system", "content": _SUMMARY_PROMPT}]
        if prior:
            msgs.append({"role": "system", "content": f"Earlier summary:\n{prior}"})
        msgs.append({"role": "user", "content": transcript})
        say("summarizing", {"messages": len(batch)})
        turn = llmclient.chat(msgs, tools=None, model=model)
        text = (turn.get("content") or "").strip()
        if not text:
            return None
        _set_summary(sid, text, batch[-1]["id"])
        # The summary is also recalled across sessions — "what did we decide about the SNES
        # folder" should work from a new thread.
        try:
            store_chunk("session_summary", f"Session {sid}: {text}", ref_id=sid)
        except Exception:
            pass
        return text
    except Exception as e:
        say("error", {"message": f"summarization skipped: {type(e).__name__}: {e}"})
        return None


def _set_summary(sid, text, watermark):
    db = connect()
    with db:
        db.execute("UPDATE chat_sessions SET summary=?, summarized_to=? WHERE id=?",
                   ((text or "")[:8000], int(watermark), sid))
