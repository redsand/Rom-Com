"""MCP *client*: external MCP servers' tools merged into the assistant's registry.

Hand-rolled JSON-RPC 2.0 over streamable HTTP — no SDK, matching how `llmclient` talks to
Ollama. Only the tools primitive is implemented; resources/prompts/sampling/roots are not
part of this app's problem.

Three deliberate scoping choices, each for a reason:

* **Streamable HTTP only.** stdio servers would unlock the filesystem/git/sqlite ecosystem,
  but they mean a Flask process babysitting long-lived subprocesses — a lifecycle problem
  that deserves its own testing stage rather than riding along with this one.
* **Config mtime polling, not a filesystem watcher.** `watchdog` is a dependency and Windows
  FS events are famously unreliable; `os.stat` at the start of each turn is cheap and exact
  for a file that changes when a human edits it. Disabling a server takes effect next turn,
  with no restart.
* **A failed server degrades to "no external tools from it", never to a failed turn.** The
  in-app assistant's own tools must keep working when someone's MCP server is down.
"""
import json
import os
import threading

import requests

from . import chattools
from .config import settings

TIMEOUT = 20.0
PROTOCOL = "2025-06-18"

_lock = threading.Lock()
_cache = {"path": None, "mtime": None, "servers": [], "checked": False}
# Tool *definitions* are cached against the config version. Re-listng every server on every
# turn would add an HTTP round trip to each turn for a list that only changes when a human
# edits the config — and would make every turn's latency depend on someone else's server
# being up. Only `tools/call` does live I/O.
_tools_cache = {"key": None, "tools": {}}


# ----------------------------------------------------------------------------- config

def _read_config():
    """Servers from `mcp_servers_path`, or from an inline JSON blob in the same env var.

    A path that does not exist is not an error — it is the default state of a fresh install,
    and a chat turn must never fail because someone has no MCP servers configured."""
    path = settings().get("mcp_servers_path") or ""
    if path and not os.path.exists(path):
        # The env var may hold the JSON itself rather than a path to it.
        raw = (path or "").strip()
        if raw.startswith("{"):
            try:
                return json.loads(raw).get("servers") or []
            except ValueError:
                return []
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("servers") or []
    except Exception:
        return []


def servers(force=False):
    """Configured servers, re-read when the config file's mtime changes.

    `force` bypasses the check — used by the status route so the UI can report the truth
    without waiting for a turn.
    """
    path = settings().get("mcp_servers_path") or ""
    try:
        mtime = os.path.getmtime(path) if path and os.path.exists(path) else None
    except OSError:
        mtime = None
    with _lock:
        if not force and _cache["checked"] and (_cache["path"], _cache["mtime"]) == (path, mtime):
            return _cache["servers"]
        _cache.update(path=path, mtime=mtime, servers=_read_config(), checked=True)
        return _cache["servers"]


def _enabled():
    return [s for s in servers() if s.get("enabled", True) and s.get("url")]


def status():
    """What the UI reports: each configured server and whether it answered."""
    out = []
    for s in servers(force=True):
        name = s.get("name") or "unnamed"
        if not s.get("enabled", True) or not s.get("url"):
            out.append({"name": name, "url": s.get("url"), "state": "disabled", "tools": 0})
            continue
        try:
            tools = _rpc(s, "tools/list")
            out.append({"name": name, "url": s["url"], "state": "ok",
                        "tools": len(tools.get("tools") or [])})
        except Exception as e:
            out.append({"name": name, "url": s.get("url"), "state": "error",
                        "tools": 0, "error": f"{type(e).__name__}: {e}"})
    return out


# ---------------------------------------------------------------------------- transport

def _headers(server):
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    h.update(server.get("headers") or {})
    return h


def _parse(resp):
    """A streamable-HTTP response is *either* a plain JSON body or a `data:`-framed SSE
    stream. Unlike Ollama — whose native endpoint is always NDJSON — this is exactly where
    SSE framing applies, so both shapes are accepted."""
    if "text/event-stream" in (resp.headers.get("Content-Type") or ""):
        for line in resp.text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            msg = json.loads(payload)
            if "id" in msg:                 # the actual reply; notifications are id-less
                return msg
        raise RuntimeError("no JSON-RPC reply in the event stream")
    return resp.json()


def _rpc(server, method, params=None, notify=False):
    """One JSON-RPC 2.0 round trip, including the MCP handshake.

    The handshake is re-sent per call rather than held in a session: these are stateless
    servers reached over HTTP, the cost is one extra request, and a cached session id that
    the server has since dropped is a whole class of confusing failure avoided."""
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if not notify:
        body["id"] = 1
    url = server["url"]
    if not notify:
        _post(url, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                               "clientInfo": {"name": "romcom", "version": "1"}}},
              server)
    resp = _post(url, body, server)
    if notify:
        return {}
    msg = _parse(resp)
    if msg.get("error"):
        err = msg["error"]
        raise RuntimeError(err.get("message") if isinstance(err, dict) else str(err))
    return msg.get("result") or {}


def _post(url, body, server):
    r = requests.post(url, json=body, headers=_headers(server), timeout=TIMEOUT)
    r.raise_for_status()
    return r


def call(server_name, tool, args):
    """Invoke one external tool and return the raw JSON-RPC `result`.

    Deliberately faithful at this layer: the transport's job is to report what came back, and
    reshaping the MCP content envelope belongs to the tool adapter (`_unwrap`), which is what
    turns someone else's server into something the agent loop treats like a native tool."""
    server = next((s for s in _enabled() if (s.get("name") or "") == server_name), None)
    if not server:
        raise RuntimeError(f"no enabled MCP server named {server_name!r}")
    return _rpc(server, "tools/call", {"name": tool, "arguments": args or {}})


# ------------------------------------------------------------------------- tool merge

def _safe(name):
    """Ollama's tool-name parser dislikes the sister app's dotted names, so a server name is
    folded to underscores. Anything else that survives is not a legal tool name."""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in str(name or "")).strip("_")


def tool_name(server_name, tool):
    return f"mcp_{_safe(server_name)}_{_safe(tool)}"


def external_tools(force=False):
    """`{name: Tool}` for every enabled server's tools, cached per config version.

    A server that fails to list contributes nothing rather than raising: one unreachable MCP
    server must not cost the assistant its own tools for the turn.
    """
    # Refresh the config *before* deriving the key from it. Reading `_cache` cold gives a key
    # of (None, None, ()) that no later call can match, so the first merge after a config
    # change listed every server twice and stored the staleness for the next turn to find.
    servers()
    with _lock:
        key = (_cache["path"], _cache["mtime"], tuple(
            sorted((s.get("name") or "", s.get("url") or "") for s in _cache["servers"])))
        if not force and _tools_cache["key"] == key:
            return _tools_cache["tools"]
    out = {}
    for s in _enabled():
        name = s.get("name") or ""
        try:
            listed = _rpc(s, "tools/list").get("tools") or []
        except Exception:
            continue
        for t in listed:
            tname = t.get("name")
            if not tname:
                continue
            full = tool_name(name, tname)
            out[full] = chattools.Tool(
                full,
                (t.get("description") or f"{tname} (via MCP server {name})")[:1000],
                t.get("inputSchema") or {"type": "object", "properties": {}},
                _make_fn(name, tname),
                # Config-raisable per server: a server that can write to something
                # irreversible is the owner's call, not ours to assume.
                risk=s.get("risk", "medium"))
    with _lock:
        _tools_cache.update(key=key, tools=out)
    return out


def _unwrap(result):
    """Strip MCP's content envelope for the one case where it carries no information.

    MCP wraps every tool result in a content list. A single text block is by far the common
    case, and leaving it wrapped hands the model a JSON string inside a JSON string — the
    payload arrives escaped, which costs tokens on every quote and invites the model to parse
    prose instead of reading data. Anything else (multiple blocks, images, embedded resources)
    is passed through untouched rather than guessed at.
    """
    if isinstance(result, dict):
        blocks = result.get("content")
        if (isinstance(blocks, list) and len(blocks) == 1
                and isinstance(blocks[0], dict) and blocks[0].get("type") == "text"
                and isinstance(blocks[0].get("content"), str)):
            text = blocks[0]["content"]
            try:
                return json.loads(text)     # most servers return JSON; give the model structure
            except ValueError:
                return text
    return result


def _make_fn(server_name, tool):
    def fn(args, ctx):
        try:
            return _unwrap(call(server_name, tool, args))
        except Exception as e:
            # Shape it like a native tool error so the model corrects course instead of the
            # turn dying on someone else's server.
            return {"error": f"MCP {server_name}/{tool} failed: {type(e).__name__}: {e}"}
    return fn


def merge(registry):
    """The registry a turn actually runs against: native tools plus external ones.

    Returns a new dict — the native registry is built fresh per turn and shared with the
    in-app tool catalog, so mutating it in place would leak external tools into `/mcp`'s
    own `tools/list` and make Rom-Com re-serve another server's tools as its own.
    """
    if not settings().get("mcp_enabled"):
        return dict(registry)
    try:
        ext = external_tools()
    except Exception:
        return dict(registry)
    merged = dict(registry)
    merged.update(ext)
    return merged
