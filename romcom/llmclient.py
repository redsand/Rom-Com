"""Ollama client for the assistant: streaming chat with tool calls, embeddings, model pick.

`romcom/llm.py` already talks to Ollama, and this deliberately does NOT replace it. That
module is a single-purpose, non-streaming, fail-open salvage step for the acquirer; this one
is a general client for an interactive agent. They share `llm_base` and `llm_timeout` so
there is exactly one place to point at the server.

Three details about Ollama's wire format are load-bearing, and all three were verified
against a live 0.34.0 daemon rather than assumed:

1. **Native `/api/chat` streams NDJSON, not SSE.** One complete JSON object per line. The
   OpenAI-compatible `data:`-framed shape lives on a different endpoint (`/v1/chat/
   completions`) and is not what we use.

2. **`tool_calls[0].function.arguments` arrives as an already-parsed dict**, not a JSON
   string the way OpenAI-shaped APIs send it. Calling `json.loads()` on it is the single
   easiest porting bug here, so `_assemble_tool_calls` accepts both shapes.

3. **`thinking` is its own field** next to `content` on each delta, so reasoning streams to
   its own UI bubble without parsing markers out of the text.

The final frame carries `eval_count` / `prompt_eval_count` / `total_duration`, which is where
the token-usage readout comes from — no extra request needed.
"""
import time
import json
import threading
import time

import requests

from .config import settings

# Capability lookups cost one /api/show per model, and resolve_model may walk the whole tag
# list looking for a tool-capable model — so cache them. The model list itself is cheap.
_CAPS = {"t": 0.0, "vals": {}}
_CAPS_TTL = 300.0
_LOCK = threading.Lock()


def _base():
    return settings()["llm_base"]


def _timeout():
    return settings()["llm_timeout"]


def _post(path, body, timeout=None, stream=False):
    return requests.post(f"{_base()}{path}", json=body, stream=stream,
                         timeout=timeout or _timeout())


def models():
    """Every model the server knows, as `[{name, bytes}]`. One request."""
    r = requests.get(f"{_base()}/api/tags", timeout=_timeout())
    r.raise_for_status()
    return [{"name": m.get("name", ""), "bytes": m.get("size", 0)}
            for m in (r.json().get("models") or [])]


def capabilities(model):
    """The capability list for one model ('completion', 'tools', 'thinking', 'vision', …)."""
    now = time.monotonic()
    with _LOCK:
        if now - _CAPS["t"] < _CAPS_TTL and model in _CAPS["vals"]:
            return _CAPS["vals"][model]
    try:
        r = _post("/api/show", {"model": model}, timeout=max(10.0, _timeout()))
        r.raise_for_status()
        caps = r.json().get("capabilities") or []
    except Exception:
        caps = []
    with _LOCK:
        if now - _CAPS["t"] >= _CAPS_TTL:
            _CAPS["vals"] = {}
        _CAPS.update(t=now)
        _CAPS["vals"][model] = caps
    return caps


def supports_tools(model):
    return "tools" in capabilities(model)


def resolve_model():
    """Which model to talk to.

    Deliberately not "whatever llm_model says": that default is a CLOUD model
    (`deepseek-v4.1-flash:cloud`), so inheriting it would make the assistant require the
    network and a cloud account. This walks a locally-present tool-capable model instead.
    Order: the configured chat_model, then llm_model if it is actually installed here, then
    the first installed model that advertises `tools`.
    """
    s = settings()
    want = (s.get("chat_model") or "").strip()
    if want:
        return want
    installed = [m["name"] for m in models()]
    legacy = (s.get("llm_model") or "").strip()
    if legacy and legacy in installed and supports_tools(legacy):
        return legacy
    for name in installed:
        if supports_tools(name):
            return name
    raise RuntimeError(
        "no tool-capable Ollama model is installed — pull one (e.g. `ollama pull qwen3:14b`) "
        "or set ROMCOM_CHAT_MODEL")


# ---------------------------------------------------------------------------- streaming

def _assemble_tool_calls(state, chunk):
    """Merge a streamed `tool_calls` fragment into `state`, keyed by call index.

    Arguments are normally a dict (see the module docstring) but some builds stream them as
    a JSON string, so both are handled: a dict replaces, a string appends and is parsed once
    the call is complete.
    """
    for tc in chunk or []:
        fn = tc.get("function") or {}
        idx = fn.get("index")
        if idx is None:
            idx = tc.get("index", 0)
        slot = state.setdefault(idx, {"id": tc.get("id"), "name": "", "args_raw": "",
                                      "args": None, "text": ""})
        if tc.get("id"):
            slot["id"] = tc["id"]
        if fn.get("name"):
            slot["name"] = fn["name"]
        args = fn.get("arguments")
        if isinstance(args, dict):
            slot["args"] = args               # already parsed — do NOT json.loads this
        elif isinstance(args, str):
            slot["args_raw"] += args          # streamed in pieces; parse when the turn ends
            slot["text"] = slot["args_raw"]


def _finalize_tool_calls(state):
    out = []
    for idx in sorted(state):
        slot = state[idx]
        args = slot["args"]
        if args is None:
            raw = (slot["args_raw"] or "").strip()
            try:
                args = json.loads(raw) if raw else {}
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        out.append({"id": slot["id"], "name": slot["name"], "arguments": args})
    return out


# Upstream hiccups worth one more try. Matched on the message text because Ollama relays
# a cloud provider's failure as an error string rather than a status code once the NDJSON
# stream has started.
_TRANSIENT_MARKERS = ("internal server error", "service unavailable", "bad gateway",
                      "gateway timeout", "temporarily unavailable", "overloaded",
                      "connection reset", "connection aborted", "timed out",
                      "502", "503", "504")
_TRANSIENT_ATTEMPTS = 3
_TRANSIENT_BACKOFF = 1.5


def _transient(exc):
    """True for an upstream failure worth retrying rather than surfacing."""
    if isinstance(exc, _ContextOverflow):
        return False
    m = str(exc).lower()
    return any(t in m for t in _TRANSIENT_MARKERS)


def chat(messages, tools=None, on_event=None, model=None, stream=True,
         temperature=None, timeout=None, prune_on_overflow=True):
    """One turn against Ollama.

    `on_event(name, payload)` is called with ("token", text) and ("thinking", text) as the
    stream arrives — the caller forwards those to the SSE stream. Returns
    `{"content", "thinking", "tool_calls", "usage", "model"}`.

    On a context-overflow error from the server the turn is retried ONCE against heavily
    pruned messages, mirroring the fail-soft posture of `llm.py`: the assistant should
    degrade rather than die because a tool returned too much.
    """
    body = {"model": model or resolve_model(), "messages": messages, "stream": stream}
    if tools:
        body["tools"] = tools
    if temperature is not None:
        body["options"] = {"temperature": temperature}

    # A transient upstream failure must not end the turn. Cloud-hosted models return
    # things like "Internal Server Error (ref: ...)" mid-stream, and that reached the UI
    # as a dead turn carrying an opaque reference number and no way forward.
    #
    # Retried only while nothing has been emitted yet: once tokens have reached the
    # browser, running the turn again would append a second answer to the first.
    for attempt in range(_TRANSIENT_ATTEMPTS):
        emitted = []

        def watch(name, payload, _e=emitted):
            if name in ("token", "thinking", "tool_call"):
                _e.append(1)
            if on_event:
                on_event(name, payload)

        try:
            return _run(body, watch, timeout)
        except _ContextOverflow:
            if not prune_on_overflow:
                raise
            # One retry, with tool payloads reduced to stubs. Losing old tool output
            # beats losing the whole conversation.
            body["messages"] = prune(messages, aggressive=True)
            return _run(body, watch, timeout)
        except Exception as e:
            if emitted or attempt == _TRANSIENT_ATTEMPTS - 1 or not _transient(e):
                raise
            time.sleep(_TRANSIENT_BACKOFF * (attempt + 1))


class _ContextOverflow(RuntimeError):
    pass


def _overflow(msg):
    m = (msg or "").lower()
    return ("context length" in m or "context window" in m or "too large" in m
            or "exceeds" in m or "too many tokens" in m)


def _run(body, on_event, timeout):
    out = {"content": "", "thinking": "", "tool_calls": [], "usage": {}, "model": body["model"]}
    tc_state = {}
    with _post("/api/chat", body, timeout=timeout, stream=body.get("stream", True)) as r:
        if r.status_code >= 400:
            detail = ""
            try:
                detail = (r.json() or {}).get("error", "")
            except Exception:
                detail = r.text[:300]
            if _overflow(detail):
                raise _ContextOverflow(detail)
            raise RuntimeError(f"ollama {r.status_code}: {detail or r.reason}")
        if not body.get("stream", True):
            data = r.json() or {}
            if data.get("error"):
                if _overflow(data["error"]):
                    raise _ContextOverflow(data["error"])
                raise RuntimeError(data["error"])
            msg = data.get("message") or {}
            out["content"] = msg.get("content") or ""
            out["thinking"] = msg.get("thinking") or msg.get("reasoning") or ""
            _assemble_tool_calls(tc_state, msg.get("tool_calls"))
            out["usage"] = _usage(data)
            out["tool_calls"] = _finalize_tool_calls(tc_state)
            return out

        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue                      # NDJSON: blank lines are framing, skip them
            try:
                d = json.loads(line)
            except ValueError:
                continue                      # a partial line can't happen per-line, but be safe
            if d.get("error"):
                if _overflow(d["error"]):
                    raise _ContextOverflow(d["error"])
                raise RuntimeError(d["error"])
            msg = d.get("message") or {}
            think = msg.get("thinking") or msg.get("reasoning") or msg.get("reasoning_content") or ""
            if think:
                out["thinking"] += think
                if on_event: on_event("thinking", think)
            text = msg.get("content") or ""
            if text:
                out["content"] += text
                if on_event: on_event("token", text)
            if msg.get("tool_calls"):
                _assemble_tool_calls(tc_state, msg["tool_calls"])
            if d.get("done"):
                out["usage"] = _usage(d)
                break
    out["tool_calls"] = _finalize_tool_calls(tc_state)
    return out


def _usage(d):
    return {"eval_count": d.get("eval_count"), "prompt_eval_count": d.get("prompt_eval_count"),
            "prompt_eval_cached_count": d.get("prompt_eval_cached_count"),
            "total_duration": d.get("total_duration"), "done_reason": d.get("done_reason")}


# ------------------------------------------------------------------------------ pruning

def _chars(msgs):
    return sum(len(m.get("content") or "") + len(str(m.get("tool_calls") or "")) for m in msgs)


def prune(messages, budget_chars=64000, aggressive=False):
    """Trim a message list to fit the context, oldest first.

    The system prompt and **the in-flight turn** are never dropped — losing either would
    make the model answer the wrong question or forget its instructions. The in-flight turn
    is everything from the last user message onward. Protecting a fixed "last N" instead
    would happily protect a *stale* tool result from a previous turn while dropping the
    conversation that explains what is even being asked.

    Tool results go before conversation does, since an old tool payload is the least useful
    thing in the window. Aggressive mode (the overflow retry) stubs every tool result down
    to its first line.
    """
    if not messages:
        return messages
    msgs = [dict(m) for m in messages]
    if aggressive:
        for m in msgs:
            if m.get("role") == "tool" and len(m.get("content") or "") > 200:
                m["content"] = (m["content"] or "")[:200] + " …[trimmed]"
    head = [m for m in msgs[:1] if m.get("role") == "system"]
    body = msgs[len(head):]
    # The in-flight turn is everything from the last user message onward: the current
    # question plus whatever tool exchanges it has already produced.
    start = max((i for i, m in enumerate(body) if m.get("role") == "user"), default=len(body) - 1)
    middle, tail = body[:start], body[start:]
    # Drop whole tool exchanges first, then oldest conversation.
    middle = [m for m in middle if m.get("role") != "tool"]
    while middle and _chars(head + middle + tail) > budget_chars:
        middle.pop(0)
    return head + middle + tail


# --------------------------------------------------------------------------- embeddings

def embed(texts, model=None):
    """Vectors for a list of strings via `/api/embed` (the current endpoint, not the legacy
    `/api/embeddings`). Returns `(vectors, dim, model)` — the dimension is reported so the
    caller can hold its dimension lock without guessing."""
    s = settings()
    name = model or s["chat_embed_model"]
    if isinstance(texts, str):
        texts = [texts]
    r = _post("/api/embed", {"model": name, "input": list(texts)}, timeout=max(30.0, _timeout()))
    r.raise_for_status()
    vecs = (r.json() or {}).get("embeddings") or []
    dim = len(vecs[0]) if vecs else 0
    if not dim:
        raise RuntimeError(f"embedding model {name!r} returned no vectors")
    return vecs, dim, name
