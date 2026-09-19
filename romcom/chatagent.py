"""The agent loop: assemble context, call the model, run its tools, repeat, stream it all.

A turn is bounded on three independent axes, because a local model looping is the normal
failure mode rather than an exotic one:

* **Iterations** — at most `MAX_ITERATIONS` model calls per turn.
* **Time** — a wall-clock deadline; each request gets only the time remaining, so a slow
  model can't extend a turn indefinitely.
* **Stuck detection** — the same call with the same arguments repeated `STUCK_REPEAT` times
  consecutively stops the turn. Local models do this: they re-issue a call that returned
  something unhelpful, forever. Counting *consecutive* identical calls (not total) means a
  legitimate repeat — paging a list, re-checking health after an action — stays allowed.

Cancellation is a `threading.Event` checked between rounds and passed to nothing else: the
SSE generator's `GeneratorExit` sets it, so closing the browser tab stops the turn at the
next boundary rather than leaving a model generating into a dead socket.
"""
import json
import threading
import time

from . import chattools, chatstore, llmclient

MAX_ITERATIONS = 8
TURN_TIMEOUT = 120.0        # seconds of wall clock for the whole turn
STUCK_REPEAT = 3            # identical consecutive calls before we stop
HISTORY_DEFAULT = 20

_SYSTEM = """You are the assistant built into Rom-Com, a personal ROM library manager. \
You are not a chat toy: you have tools that read the real library and you are expected to \
use them. Answer from tool output, never from memory of similar questions.

THE LIBRARY
Items are ROM titles (one game, or one entry of a multicart). Volumes are bundles that \
cover many items. Statuses are a progression, and the distinctions matter:

- CATALOGED / MISSING — known from a catalog, nothing on disk. These are what acquisition
  searches for.
- FOUND — a file matched by *name* only, not yet verified.
- QUEUED / DOWNLOADING — in the download client.
- DOWNLOADED — the download finished. This DOES NOT mean the right file arrived, and does
  not mean it is verified.
- VERIFIED — hash-matched against the catalog. NORMALIZED / INSTALLED / TESTED are further
  along. VERIFIED and beyond are the only statuses that mean "confirmed good".
- FAILED / MANUAL / EXCLUDED — side states. EXCLUDED is the deliberate "don't want it back".

"On disk" means a real content file is matched to the item (artwork and manuals don't count).
Status only ever advances, so a wrong DOWNLOADED is permanent until someone reconciles it —
that is what `library_audit` exists to find.

HOW TO WORK
- For any question about counts or coverage, call a tool. Start with `library_summary` or
  `list_items`.
- Before filtering by a system or status, call `facets` so you use the exact spelling rather
  than a guess.
- Every list tool returns `total` as well as the rows returned, and may say `truncated` or
  carry a `note`. If `total` is larger than what you received, say so and page or narrow the
  filter — never present a partial list as complete.
- For "why is this stuck / what went wrong / why is this downloaded but absent", call
  `library_audit` and read its explanation.
- Search tools hit the network and are rate-limited. Use them deliberately, not in a loop.
- If a tool's result is ambiguous or errors, fix your call and try again — a tool error is
  information, not a dead end.
- Never invent an item id, title, or number. If you don't know, look it up.
- Be concise and concrete. Lead with the answer, then the supporting numbers. No preamble.

ACTING
You can change the library, not just read it. Most writes run immediately: marking one item
authorized or wanted, queueing one specific release, running a scan, starting an acquisition
pass, syncing the ledger.

Some calls are gated and will come back with `requires_approval`:
- Bulk changes (bulk_update_items, set_series, mark_all_owned)
- organize_library — it moves real files on disk
- set_setting and watcher_toggle — they change how the library behaves

When a call comes back `requires_approval`, that is not an error and not a failure. Nothing
has changed. Say plainly what you are asking to do and why, then stop and wait — the owner
sees a confirmation card and the call resumes by itself. Never re-issue a gated call hoping
for a different answer, and never claim something happened that has not: the tool text says
NOT RUN for a reason.
If the owner declines, that decision is final. Do not ask again for the same call; say what
you would do differently or ask what they would prefer.
"""


def system_prompt():
    return _SYSTEM.strip()


def _assemble(sid, registry):
    """Build the message list for a turn: system → (memory) → (summary) → verbatim window.

    Stage 2 has neither memory nor summary, so the two insertion points below are empty by
    design; Stage 4 fills them without disturbing this ordering.
    """
    from .config import settings
    msgs = [{"role": "system", "content": system_prompt()}]
    memory = _memory_block(sid)          # Stage 4: recalled facts/chunks
    if memory:
        msgs.append({"role": "system", "content": memory})
    summary = _summary_block(sid)        # Stage 4: rolling summary
    if summary:
        msgs.append({"role": "system", "content": summary})
    keep = int(settings().get("chat_history_max") or HISTORY_DEFAULT)
    rows = [r for r in chatstore.history(sid, limit=keep) if r["role"] != "system"]
    # Start the window on a user turn. Taking the last N messages can land mid-exchange,
    # leaving a dangling answer with no question — which reads to the model as if it had
    # said that unprompted.
    while rows and rows[0]["role"] != "user":
        rows.pop(0)
    msgs += chatstore.to_messages(rows)
    return msgs


def _memory_block(sid):
    return ""      # Stage 4


def _summary_block(sid):
    row = chatstore.session(sid)
    return (row["summary"] or "") if row else ""    # populated in Stage 4


def resolve_approval(aid, approve, registry, emit, cancel=None, model=None):
    """Decide a pending approval and continue the conversation that paused on it.

    Either way the outcome is appended as a tool result, so the model always sees its own
    call answered — a tool call left dangling with no reply is a shape local models handle
    badly. Approved, that result is the tool's own; declined, it says so in as many words.
    """
    from . import chattools
    row = chatstore.approval(aid)
    if not row:
        emit("error", {"message": f"no such approval: {aid}"})
        return None
    sid = row["session_id"]
    decided = chatstore.decide_approval(aid, approve)
    if not decided:
        # Already answered — a second click, or two tabs racing. Nothing runs twice.
        emit("error", {"message": f"approval {aid} was already {row['status']}"})
        return None
    try:
        args = json.loads(row["arguments"] or "{}") if row["arguments"] else {}
    except (TypeError, ValueError):
        args = {}
    if approve:
        result = chattools.apply_approval(registry, aid)
    else:
        result = chattools.declined(row["tool"], args, sid)
        chatstore.record_approval_result(aid, result)

    emit("tool_start", {"name": row["tool"], "args": args})
    emit("tool_result", {"name": row["tool"], "ok": result.get("ok"),
                         "data": result.get("data"), "error": result.get("error"),
                         "truncated": result.get("truncated", False),
                         "note": result.get("note"), "risk": result.get("risk"),
                         "approved": bool(approve)})
    if not sid:
        # An approval with no conversation behind it (a programmatic caller): there is
        # nothing to resume, and appending a session-less tool row would litter the log.
        return {"session_id": None, "content": "", "stopped": None, "paused": False,
                "approval_result": result}
    chatstore.append(sid, "tool", tool_text(result), tool_name=row["tool"])
    return resume_turn(sid, registry, emit, cancel, model)


def tool_text(result):
    """The tool message the model sees. `risk` is dropped — it is our bookkeeping."""
    if result.get("ok"):
        payload = {"ok": True, "data": result.get("data")}
        if result.get("note"):
            payload["note"] = result["note"]
        if result.get("truncated"):
            payload["truncated"] = True
        return json.dumps(payload, default=str)
    return json.dumps({"ok": False, "error": result.get("error")}, default=str)


def _title_from(text):
    t = " ".join((text or "").split())
    return t[:80] or None


def run_turn(sid, message, registry, emit, cancel=None, model=None):
    """Start a turn from a user message, then hand off to `_loop`.

    Events: start, token, thinking, tool_start, tool_result, approval_required, error, done.
    """
    if not sid:
        sid = chatstore.new_session(_title_from(message))
    user_mid = chatstore.append(sid, "user", message)
    chatstore.touch(sid, title=_title_from(message))
    emit("start", {"session_id": sid, "user_message_id": user_mid})
    return _loop(sid, registry, emit, cancel, model)


def resume_turn(sid, registry, emit, cancel=None, model=None):
    """Continue a turn that stopped for approval.

    The approval's outcome is already in the history — as a tool result, exactly where the
    model left its own call waiting — so there is no new user message. That is the point:
    re-feeding it as a *user* turn would make the model read the owner's decision as
    something the owner typed, and it would answer the wrong question.
    """
    emit("start", {"session_id": sid, "user_message_id": None})
    return _loop(sid, registry, emit, cancel, model)


def _loop(sid, registry, emit, cancel=None, model=None):
    """The model/tool cycle, shared by a new turn and a resumed one.

    **Every turn ends with exactly one `done`**, whatever went wrong — cancelled, timed out,
    stuck, declined, or capped. That is the rule the UI leans on to re-enable its input, so
    there is no path where the composer stays disabled forever. `error` carries the
    explanation and is always followed by that terminal `done`; `done.stopped` says whether
    the turn stopped early and why.

    Never raises for model or tool failure: the failure is reported on the stream, because a
    broken turn must leave the app usable.
    """
    cancel = cancel or threading.Event()
    deadline = time.monotonic() + TURN_TIMEOUT

    messages = _assemble(sid, registry)
    tools = [t.schema() for t in registry.values()]
    prior_calls = []
    usage_total = {}
    content, thinking = "", ""
    stopped = None      # a reason string when the turn ended early
    paused = False      # waiting on the owner's approval (Stage 3), not a failure

    try:
        for _iteration in range(MAX_ITERATIONS):
            if cancel.is_set():
                stopped = "cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stopped = f"turn timed out after {int(TURN_TIMEOUT)}s"
                break

            def on_event(name, text):
                emit("token" if name == "token" else "thinking", {"text": text})

            turn = llmclient.chat(messages, tools=tools, on_event=on_event, model=model,
                                  timeout=min(remaining, TURN_TIMEOUT))
            for k, v in (turn.get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    usage_total[k] = usage_total.get(k, 0) + v
            content += turn.get("content") or ""
            thinking += turn.get("thinking") or ""
            calls = [c for c in (turn.get("tool_calls") or []) if c.get("name")]

            if not calls:
                break

            # Record the model's own tool-call turn so the next round sees a consistent
            # conversation (Ollama matches results back by tool name, not by id).
            chatstore.append(sid, "assistant", turn.get("content") or "", tool_args=calls)
            messages.append({"role": "assistant", "content": turn.get("content") or "",
                             "tool_calls": calls})

            for call in calls:
                sig = (call["name"], json.dumps(call.get("arguments") or {}, sort_keys=True, default=str))
                prior_calls.append(sig)
                if len(set(prior_calls[-STUCK_REPEAT:])) == 1 and len(prior_calls) >= STUCK_REPEAT:
                    stopped = (f"stopped: the same call to {call['name']} repeated "
                               f"{STUCK_REPEAT} times with identical arguments")
                    break

                emit("tool_start", {"name": call["name"], "args": call.get("arguments") or {}})
                result = chattools.dispatch(registry, call["name"], call.get("arguments") or {},
                                            session_id=sid)
                emit("tool_result", {"name": call["name"], "ok": result.get("ok"),
                                     "data": result.get("data"), "error": result.get("error"),
                                     "truncated": result.get("truncated", False),
                                     "note": result.get("note"), "risk": result.get("risk")})
                if result.get("requires_approval"):
                    # The turn ends here by design, and deliberately writes *no* tool row: the
                    # model's call stays open and `resolve_approval` answers it with what the
                    # owner decided. Writing "not run" now and the outcome later would put two
                    # replies under one call — a shape Ollama matches by name, not by id.
                    emit("approval_required", {"approval_id": result.get("approval_id"),
                                               "tool": call["name"],
                                               "args": call.get("arguments") or {},
                                               "summary": result.get("summary")})
                    paused = True
                    break
                text = tool_text(result)
                chatstore.append(sid, "tool", text, tool_name=call["name"])
                messages.append({"role": "tool", "content": text, "tool_name": call["name"]})
            if stopped or paused:
                break
        else:
            stopped = f"stopped after {MAX_ITERATIONS} tool rounds without a final answer"
    except Exception as e:
        stopped = f"{type(e).__name__}: {e}"

    if stopped:
        emit("error", {"message": stopped})
    # A stopped or paused turn that produced nothing gets no row: an empty assistant message
    # would otherwise be replayed as context and read as "the model said nothing here".
    assistant_mid = None
    if content.strip() or not (stopped or paused):
        assistant_mid = chatstore.append(sid, "assistant", content, thinking=thinking or None)
    chatstore.touch(sid)
    emit("done", {"assistant_message_id": assistant_mid, "usage": usage_total,
                  "stopped": stopped, "paused": paused})
    return {"session_id": sid, "assistant_message_id": assistant_mid, "content": content,
            "usage": usage_total, "stopped": stopped, "paused": paused}
