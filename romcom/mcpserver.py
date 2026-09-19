"""MCP *server*: Rom-Com's own tools, exposed as JSON-RPC 2.0 at `/mcp`.

**The anti-drift guarantee is that this module owns no tool list.** `tools/list` serializes
`chattools.build_registry(ctx)` — the same artifact the in-app assistant runs against — so an
external client sees exactly what the owner's assistant can do and nothing more. Adding a tool
in one place cannot leave the other stale, because there is only one place.

Stateless: no session id is issued or required, because every request is authenticated on its
own credentials and there is no per-session state worth keeping. That also means a client
crash cannot leave a half-open session the server waits on.

**The confirm contract is the same in both directions.** A `high`-risk tool is *listed* (a
client should be able to discover that `organize_library` exists) but refuses unless the
caller passes `"confirm": true` in its arguments, which is then stripped before dispatch. An
external client cannot get the agent's interactive confirm card, so the honest equivalent is
an explicit, documented assertion by the caller — not a silent bypass and not a tool that
simply cannot be reached.
"""
from flask import jsonify, request

from . import chattools, chatstore, webauth

PROTOCOL = "2025-06-18"
SERVER_INFO = {"name": "rom-com", "version": "1"}


def _result(rid, result):
    return jsonify({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return jsonify({"jsonrpc": "2.0", "id": rid, "error": err}), 200


def _tool_descriptor(tool):
    """MCP's tool shape. `inputSchema` is a JSON Schema, which is what `Tool.parameters`
    already is — the reverse of the conversion the client does."""
    desc = tool.description
    if tool.risk == "high":
        desc += ("  [Requires confirmation: pass \"confirm\": true in the arguments. Without "
                 "it this call is refused and nothing happens.]")
    return {"name": tool.name, "description": desc,
            "inputSchema": tool.parameters or {"type": "object", "properties": {}}}


def _content(payload):
    """The MCP content envelope. The tool's own JSON is the text block; `isError` is set from
    the envelope so a client can tell a refusal from a result without parsing prose."""
    import json
    return [{"type": "text", "content": json.dumps(payload, default=str)}]


def handle(body, registry):
    """One JSON-RPC request. Returns a Flask response — `None` means "accepted, no reply"
    (a notification), which the route turns into a 204."""
    if not isinstance(body, dict):
        return _error(None, -32600, "invalid request: expected a JSON object")
    rid = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    # JSON-RPC: a request with no `id` is a notification, and the server must not reply. The
    # check is on the *absence of an id*, not on the method name — replying to something no
    # client is waiting for is what corrupts request/response matching on the other end.
    if "id" not in body or method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "initialize":
        return _result(rid, {"protocolVersion": PROTOCOL,
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": SERVER_INFO})
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": [_tool_descriptor(t)
                                       for t in sorted(registry.values(), key=lambda x: x.name)]})
    if method != "tools/call":
        return _error(rid, -32601, f"method not found: {method}")

    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return _error(rid, -32602, "arguments must be an object")
    tool = registry.get(name)
    if not tool:
        return _error(rid, -32602, f"unknown tool: {name}",
                      {"available": sorted(registry)})

    if tool.risk == "high":
        # `confirm` is the caller's explicit assertion, and it never reaches the tool — the
        # tool's own schema does not have that field, and a tool writing an unknown argument
        # into an UPDATE is exactly the kind of surprise this prevents.
        if str(args.get("confirm", "")).strip().lower() not in ("1", "true", "yes", "on"):
            return _error(
                rid, -32000,
                f"{name} is a high-risk tool and needs explicit confirmation",
                {"requires_confirmation": True,
                 "hint": f"Re-send with the arguments plus \"confirm\": true. It changes "
                         f"things that need a human decision. Nothing has run."})
        args = {k: v for k, v in args.items() if k != "confirm"}
        chatstore.log_call(None, name, args, True, "high", "confirmed by MCP caller")
        result = chattools._invoke(tool, args, None)
    else:
        result = chattools.dispatch(registry, name, args, session_id=None)

    if result.get("requires_approval"):
        # Cannot happen for a non-high tool, but if a future gate is added the caller must
        # not be handed a silent success.
        return _error(rid, -32000, result.get("error") or "this call needs confirmation",
                      {"requires_confirmation": True})
    return _result(rid, {"content": _content(result), "isError": not result.get("ok")})


def register(app, ctx):
    @app.post("/mcp")
    def mcp_endpoint():
        # Never open, unlike the UI gate: this endpoint exists to be driven programmatically,
        # so an unset ROMCOM_MCP_KEY must mean nobody gets in rather than everybody.
        if not webauth.mcp_key_ok(request):
            return jsonify({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32001, "message": "unauthorized"}}), 401
        body = request.get_json(silent=True)
        if body is None:
            return _error(None, -32700, "parse error: body was not JSON")
        if isinstance(body, list):
            return _error(None, -32600, "batch requests are not supported")
        registry = chattools.build_registry(ctx)
        resp = handle(body, registry)
        if resp is None:
            return "", 204
        return resp
