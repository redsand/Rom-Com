"""Chat HTTP surface, including this codebase's first SSE endpoint.

**Why a worker thread + queue.** `run_turn` is synchronous: it blocks while the model
generates, and its `emit` callback fires from deep inside the streaming loop. A Flask
generator can only produce values when it is resumed, so it cannot yield from inside that
call stack. Running the turn on a thread and having `emit` push onto a `queue.Queue` lets
the generator drain and yield as tokens arrive — which is the whole point of streaming,
since the alternative (run to completion, then dump the text) would show nothing until the
turn finished.

**Cancellation has two paths.** A client disconnect raises `GeneratorExit` at the `yield`,
whose `finally` sets the cancel event; the Stop button sets the same event via
`POST /api/chat/cancel`. Both stop the turn at its next boundary. Only one turn may run per
session at a time — two would interleave writes into the same history and produce a
conversation the model never actually saw.
"""
import json
import os
import queue
import threading

from flask import Response, jsonify, request

from . import chatagent, chatstore, chattools, mcpclient
from .config import settings

# session_id -> cancel Event for the turn currently running on it.
_RUNNING = {}
_RUN_LOCK = threading.Lock()


def _claim(sid):
    """Reserve a session for one turn. Returns False if a turn is already in flight."""
    if sid is None:
        return True
    with _RUN_LOCK:
        if sid in _RUNNING:
            return False
        _RUNNING[sid] = threading.Event()
        return True


def _release(sid):
    if sid is None:
        return
    with _RUN_LOCK:
        _RUNNING.pop(sid, None)


def _cancel_event(sid):
    with _RUN_LOCK:
        return _RUNNING.get(sid)


def _frame(name, payload):
    return f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"


def _sse(sid, work):
    """Run `work(emit)` on a daemon thread and stream what it emits.

    Shared by the turn route and the approval route: both are long, both must stream, and
    both must release the session claim even when the client vanishes mid-stream (the
    `GeneratorExit` at the `yield` is what sets the cancel event)."""

    def gen():
        q = queue.Queue()

        def emit(name, payload):
            q.put((name, payload))

        def run():
            try:
                work(emit)
            except Exception as e:
                # The agent reports model and tool failure on the stream itself; this is the
                # backstop for anything that escaped before the loop existed at all (an
                # unresolvable model, say) so the UI never sees a bare 500.
                emit("error", {"message": f"{type(e).__name__}: {e}"})
            finally:
                q.put(None)

        threading.Thread(target=run, daemon=True).start()
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                yield _frame(*item)
        finally:
            ev = _cancel_event(sid)
            if ev:
                ev.set()          # client went away mid-turn
            _release(sid)

    resp = Response(gen(), mimetype="text/event-stream")
    # Deliberately NOT `direct_passthrough`. That flag hands the raw iterable to the WSGI
    # server without encoding or framing it, while the headers still advertise
    # `Transfer-Encoding: chunked`. Frames are `str`, so the server received str where WSGI
    # requires bytes: it wrote nothing, closed the connection, and the browser saw a 200 with
    # a zero-byte chunked body — surfacing as "NetworkError when attempting to fetch
    # resource" rather than as anything resembling a server error. Letting Werkzeug encode
    # and chunk the generator is also what keeps the stream unbuffered, so nothing is lost.
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"   # defeat any proxy buffering
    # `Connection` is hop-by-hop and belongs to the server, not the app; setting it here
    # fought with the server's own value and produced two of them.
    return resp


def register(app, ctx):
    def _registry():
        """Native tools plus any external MCP tools.

        `merge` returns a *copy*: the native registry is also what `/mcp` serves, so adding
        external tools in place would make Rom-Com re-advertise another server's tools as its
        own — a loop that grows a level per hop.
        """
        return mcpclient.merge(chattools.build_registry(ctx))

    @app.post("/api/chat/stream")
    def api_chat_stream():
        body = request.get_json(force=True, silent=True) or {}
        if not settings()["chat_enabled"]:
            # A JSON error, not an empty stream: the tab renders a disabled state from this
            # and the reason is legible instead of looking like a broken connection.
            return jsonify({"error": "the assistant is disabled — set ROMCOM_CHAT_ENABLED=true "
                                     "and restart"}), 400
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify({"error": "message is required"}), 400
        sid = body.get("session_id")
        if sid is not None:
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                return jsonify({"error": "session_id must be an integer"}), 400
            if not chatstore.session(sid):
                return jsonify({"error": f"no such session: {sid}"}), 404
        if not _claim(sid):
            return jsonify({"error": "a turn is already running on this session"}), 409
        model = (body.get("model") or "").strip() or None
        registry = _registry()
        return _sse(sid, lambda emit: chatagent.run_turn(
            sid, message, registry, emit, cancel=_cancel_event(sid), model=model))

    @app.post("/api/chat/approve/<int:aid>")
    def api_chat_approve(aid):
        """Answer a confirmation card. Streams like a turn because it *is* one: the decision
        is appended to the history as a tool result and the agent loop continues from there."""
        body = request.get_json(force=True, silent=True) or {}
        approve = str(body.get("approve", "")).strip().lower() in ("1", "true", "yes", "on")
        row = chatstore.approval(aid)
        if not row:
            return jsonify({"error": f"no such approval: {aid}"}), 404
        if row["status"] != "pending":
            return jsonify({"error": f"approval {aid} was already {row['status']}"}), 409
        sid = row["session_id"]
        if not _claim(sid):
            return jsonify({"error": "a turn is already running on this session"}), 409
        registry = _registry()
        model = (body.get("model") or "").strip() or None
        return _sse(sid, lambda emit: chatagent.resolve_approval(
            aid, approve, registry, emit, cancel=_cancel_event(sid), model=model))

    @app.get("/api/chat/approvals/<int:sid>")
    def api_chat_approvals(sid):
        return jsonify(chatstore.approvals(sid=sid, status=request.args.get("status") or None))

    @app.get("/api/chat/memory")
    def api_chat_memory():
        """What the assistant durably remembers, and what it can recall. Read-only, and the
        owner's window into a store that otherwise only the agent can see — memory nobody can
        inspect is memory nobody can correct."""
        q = (request.args.get("q") or "").strip()
        return jsonify({"facts": chatstore.facts(limit=200),
                        "chunks": chatstore.chunk_count(),
                        "recall": chatstore.recall(q, k=10) if q else []})

    @app.get("/api/chat/mcp")
    def api_chat_mcp():
        """External MCP servers and whether they answered. The assistant's own catalog is at
        /api/chat/tools; this is the other direction, and it is separate because a server
        being down is normal and must not look like a broken tool catalog."""
        return jsonify({"enabled": bool(settings().get("mcp_enabled")),
                        "servers": mcpclient.status() if settings().get("mcp_enabled") else [],
                        "key_set": bool(os.getenv("ROMCOM_MCP_KEY"))})

    @app.post("/api/chat/cancel")
    def api_chat_cancel():
        body = request.get_json(force=True, silent=True) or {}
        try:
            sid = int(body.get("session_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "session_id is required"}), 400
        ev = _cancel_event(sid)
        if ev:
            ev.set()
        return jsonify({"cancelled": bool(ev)})

    @app.get("/api/chat/sessions")
    def api_chat_sessions():
        return jsonify(chatstore.sessions(limit=request.args.get("limit", 50, type=int) or 50))

    @app.get("/api/chat/session/<int:sid>")
    def api_chat_session(sid):
        s = chatstore.session(sid)
        if not s:
            return jsonify({"error": f"no such session: {sid}"}), 404
        return jsonify({"session": dict(s), "messages": chatstore.history(sid),
                        "running": bool(_cancel_event(sid))})

    @app.delete("/api/chat/session/<int:sid>")
    def api_chat_delete(sid):
        return jsonify({"deleted": chatstore.delete_session(sid)})

    @app.get("/api/chat/status")
    def api_chat_status():
        """What the Assistant tab needs to render its header before the first turn: is the
        feature on, which model will be used, and is Ollama actually answering. Every probe
        is best-effort — a status call must not 500 because the model server is down."""
        from . import llmclient
        out = {"enabled": bool(settings()["chat_enabled"]),
               "configured_model": settings()["chat_model"],
               "embed_model": settings()["chat_embed_model"],
               "base": settings()["llm_base"], "reachable": False, "model": None,
               "models": [], "error": None}
        try:
            out["models"] = llmclient.models()
            out["reachable"] = True
            out["model"] = llmclient.resolve_model()
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
        return jsonify(out)

    @app.get("/api/chat/tools")
    def api_chat_tools():
        """The catalog, so the UI (and the owner) can see exactly what the assistant can
        reach. Also the anti-drift check for the MCP server in Stage 6."""
        return jsonify([{"name": t.name, "risk": t.risk, "description": t.description}
                        for t in _registry().values()])
