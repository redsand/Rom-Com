"""Master-login gate for the whole web UI — one user, one password, session cookie.

Everything in this app mutates a real library: a stray tab can mark thousands of items
owned, or point the acquire watcher at a source. The app binds to 127.0.0.1 by default, so
this is not defending against the internet — it is defending against a second local user, a
shared screen, or a mis-click in a browser that happens to be open.

Two deliberate design choices:

1. **Credentials live outside the settings system entirely.** `ROMCOM_WEB_USER` /
   `ROMCOM_WEB_PASS` are read here with `os.getenv` and are NOT keys in
   `config._env_settings()`. A key absent from that dict is invisible to `settings()`, so it
   can never be copied into `app_settings`, can never be returned by `GET /api/settings`, and
   can never be written from the Settings tab. The settings-precedence system *is* the
   security boundary — which is why nothing has to be masked back out of the payload later.
   The same reasoning keeps `ROMCOM_MCP_KEY` out of it.

2. **Sessions are rows in `web_sessions`, not signed cookies.** A signed cookie needs a
   stable `SECRET_KEY` that itself has to be persisted somewhere (the same problem with worse
   ergonomics), cannot be revoked on logout, and needs versioning before any claim can be
   added. A random token in the database is revocable, inspectable, and survives a restart
   for free. The token is stored **hashed**: the plaintext exists only in the user's cookie,
   so a copy of the database yields nothing usable.

Unconfigured means unprotected, exactly as the app runs today — that is the default, and it
is what the entire existing test suite inherits because those tests never set these vars.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from flask import jsonify, request
from .db import connect

COOKIE = "romcom_sess"
TTL_HOURS = 24
# Renew when less than half the life is left rather than on every request: the UI polls
# several endpoints continuously, and a write per request would contend with the acquire
# watcher for the write lock for no benefit. Half-life still means an active user never
# re-logs, while an abandoned token dies within a day.
RENEW_BELOW_HOURS = TTL_HOURS / 2

# The static shell has to load for a login form to exist at all, and the auth routes have to
# be reachable before there is a session. Everything else under /api/* is gated.
PUBLIC_PATHS = {"/", "/app.js", "/style.css", "/favicon.ico",
                "/api/auth/login", "/api/auth/status", "/api/auth/session",
                "/api/auth/logout"}

# `/mcp` is gated, but not *here*. It carries its own credential (`mcp_key_ok`, never open),
# and the two do not overlap: an MCP client authenticates with ROMCOM_MCP_KEY, which is not a
# UI session token, so letting this gate run first means a perfectly authorized MCP client
# gets a 401 from the login gate and its own (correct) auth check never executes. That is
# exactly the combination the owner asked for — login on *and* MCP reachable — so the gate
# has to stand aside rather than be satisfied.
SELF_GATING_PATHS = {"/mcp"}


def _cred(name):
    """Read a credential at call time — never at import, so a test (or a .env edit followed
    by a restart) always sees the current value."""
    return os.getenv(name) or ""


def configured():
    """True when a master login is set up. Both halves are required: a username with no
    password is a misconfiguration, not "protection with a blank password"."""
    return bool(_cred("ROMCOM_WEB_USER") and _cred("ROMCOM_WEB_PASS"))


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def issue_token(user):
    """Mint a session token, persist its hash, and return the plaintext (shown once)."""
    token = secrets.token_hex(32)
    now = _now()
    db = connect()
    with db:
        db.execute("INSERT INTO web_sessions(token_hash,user,created_at,expires_at,last_seen)"
                   " VALUES(?,?,?,?,?)",
                   (_hash(token), user, _stamp(now), _stamp(now + timedelta(hours=TTL_HOURS)),
                    _stamp(now)))
    return token


def _presented(req):
    """The token this request carries — cookie first (the browser), then a bearer header so
    a script or an MCP client can share the same session store."""
    tok = req.cookies.get(COOKIE)
    if tok:
        return tok
    auth = req.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def current_user(req=None):
    """The authenticated user for this request, or None. Also performs sliding renewal, and
    deletes the row once it is past its expiry."""
    tok = _presented(req or request)
    if not tok:
        return None
    db = connect()
    row = db.execute("SELECT * FROM web_sessions WHERE token_hash=?", (_hash(tok),)).fetchone()
    if not row:
        return None
    now = _now()
    expires = _parse(row["expires_at"])
    if expires is None or expires < now:
        with db:  # expired: clear it out rather than leaving dead rows to accumulate
            db.execute("DELETE FROM web_sessions WHERE token_hash=?", (row["token_hash"],))
        return None
    if expires - now < timedelta(hours=RENEW_BELOW_HOURS):
        with db:
            db.execute("UPDATE web_sessions SET expires_at=?,last_seen=? WHERE token_hash=?",
                       (_stamp(now + timedelta(hours=TTL_HOURS)), _stamp(now), row["token_hash"]))
    else:
        with db:
            db.execute("UPDATE web_sessions SET last_seen=? WHERE token_hash=?",
                       (_stamp(now), row["token_hash"]))
    return row["user"]


def revoke(token):
    if not token:
        return
    db = connect()
    with db:
        db.execute("DELETE FROM web_sessions WHERE token_hash=?", (_hash(token),))


def _same(a, b):
    """Constant-time compare. Both sides are encoded so a non-ASCII password can't raise,
    and compare_digest needs bytes-or-ASCII-str of equal type."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def mcp_key_ok(req):
    """Authentication for POST /mcp. MCP clients cannot do a browser login, so this accepts
    ROMCOM_MCP_KEY via Bearer or X-Romcom-Key — or a normal UI session token, so the in-app
    assistant and a user's script share one path. Unlike the UI gate this is NEVER open: an
    unset key means nobody gets in."""
    key = _cred("ROMCOM_MCP_KEY")
    auth = req.headers.get("Authorization", "")
    presented = auth[7:].strip() if auth.lower().startswith("bearer ") else \
        req.headers.get("X-Romcom-Key", "").strip()
    if key and presented and _same(presented, key):
        return True
    return current_user(req) is not None


def install(app):
    """Register the gate and the auth routes on the app factory's app."""
    @app.before_request
    def _gate():
        # Unconfigured = the app behaves exactly as it did before this module existed.
        if not configured():
            return None
        if request.path in PUBLIC_PATHS or request.path in SELF_GATING_PATHS:
            return None
        if current_user() is not None:
            return None
        # JSON, never a redirect: every consumer of this app (including the fetch wrapper in
        # app.js and the assistant itself) speaks JSON, and a 302 to a login page would be
        # silently followed by fetch and parsed as the wrong thing.
        return jsonify({"error": "authentication required"}), 401

    @app.post("/api/auth/login")
    def api_login():
        if not configured():
            return jsonify({"ok": True, "user": None, "configured": False})
        body = request.get_json(force=True, silent=True) or {}
        user = str(body.get("username") or "")
        pw = str(body.get("password") or "")
        # Both compared even when the username is wrong, so timing doesn't reveal which half
        # failed. compare_digest is not short-circuiting on content, but the two calls are
        # ordered so the password is always checked.
        user_ok = _same(user, _cred("ROMCOM_WEB_USER"))
        pw_ok = _same(pw, _cred("ROMCOM_WEB_PASS"))
        if not (user_ok and pw_ok):
            return jsonify({"error": "invalid username or password"}), 401
        token = issue_token(user)
        resp = jsonify({"ok": True, "user": user, "configured": True})
        resp.set_cookie(COOKIE, token, httponly=True, samesite="Lax", path="/",
                        max_age=TTL_HOURS * 3600)
        return resp

    @app.post("/api/auth/logout")
    def api_logout():
        revoke(_presented(request))
        resp = jsonify({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/auth/status")
    def api_auth_status():
        """Public: the login overlay needs to know whether to show itself at all."""
        return jsonify({"configured": configured()})

    @app.get("/api/auth/session")
    def api_auth_session():
        """Public and self-reporting, so the UI can check on boot whether the cookie it
        already has is still good without tripping the gate."""
        return jsonify({"configured": configured(), "user": current_user()})
