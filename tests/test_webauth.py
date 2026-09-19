"""Master-login gate: protecting the app, and — just as important — not protecting it when
unconfigured, since that is the documented default and what every other test relies on."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from romcom.db import connect
from romcom.web import create_app

# Distinctive on purpose: a short username like "tim" is a substring of "timeout", which
# IS a settings key, so a naive "not in body" assertion would fail for the wrong reason.
USER, PW = "zaphod-beeblebrox", "hunter2-correct-horse"


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def make_client(monkeypatch, tmp_path, configure=True):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    if configure:
        monkeypatch.setenv("ROMCOM_WEB_USER", USER)
        monkeypatch.setenv("ROMCOM_WEB_PASS", PW)
    else:
        monkeypatch.delenv("ROMCOM_WEB_USER", raising=False)
        monkeypatch.delenv("ROMCOM_WEB_PASS", raising=False)
    connect()
    return create_app().test_client()


def login(client, user=USER, pw=PW):
    return client.post("/api/auth/login", json={"username": user, "password": pw})


# --------------------------------------------------------------------------- unconfigured

def test_unconfigured_leaves_the_app_exactly_as_it_was(monkeypatch, tmp_path):
    """The default. Every existing test in this suite runs in this state, so if this
    regresses the whole app breaks — the gate must be inert, not merely permissive."""
    c = make_client(monkeypatch, tmp_path, configure=False)
    assert c.get("/api/summary").status_code == 200
    assert c.get("/api/settings").status_code == 200
    assert c.get("/api/facets").status_code == 200
    assert c.get("/").status_code == 200
    # login is a no-op rather than an error, so the UI can call it unconditionally
    r = login(c)
    assert r.status_code == 200 and r.get_json()["configured"] is False


def test_status_reports_whether_a_login_exists(monkeypatch, tmp_path):
    assert make_client(monkeypatch, tmp_path, configure=False).get("/api/auth/status").get_json() \
        == {"configured": False}
    assert make_client(monkeypatch, tmp_path, configure=True).get("/api/auth/status").get_json() \
        == {"configured": True}


def test_username_without_password_is_not_protection(monkeypatch, tmp_path):
    """Half-configured is a mistake, not 'protection with a blank password' — it must not
    silently lock the owner out of their own library."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ROMCOM_WEB_USER", USER)
    monkeypatch.delenv("ROMCOM_WEB_PASS", raising=False)
    connect()
    assert create_app().test_client().get("/api/summary").status_code == 200


# ----------------------------------------------------------------------------- gating

@pytest.mark.parametrize("method,path", [
    ("get", "/api/summary"), ("get", "/api/settings"), ("get", "/api/facets"),
    ("get", "/api/items"), ("get", "/api/plan"), ("get", "/api/jobs"),
    ("get", "/api/acquire/health"), ("get", "/api/doctor"),
    ("post", "/api/settings"), ("post", "/api/items/bulk"), ("post", "/api/scan"),
    ("post", "/api/auto-acquire"), ("post", "/api/library/own"),
])
def test_every_api_route_requires_login(monkeypatch, tmp_path, method, path):
    """Not a sample — the point is that no route was forgotten, because the gate is a
    before_request hook rather than a per-route decorator."""
    c = make_client(monkeypatch, tmp_path)
    r = getattr(c, method)(path, json={})
    assert r.status_code == 401, path
    assert r.get_json()["error"] == "authentication required"


def test_the_static_shell_stays_open(monkeypatch, tmp_path):
    """The login form lives in the shell — gating index.html would make logging in
    impossible (a 401 JSON page instead of a form)."""
    c = make_client(monkeypatch, tmp_path)
    assert c.get("/").status_code == 200
    assert c.get("/app.js").status_code == 200
    assert c.get("/style.css").status_code == 200


def test_a_refusal_is_json_never_a_redirect(monkeypatch, tmp_path):
    """fetch() follows a 302 transparently and hands the caller the login page as if it
    were the API response. Every consumer here speaks JSON, so 401 JSON is the only shape
    that fails honestly."""
    c = make_client(monkeypatch, tmp_path)
    r = c.get("/api/summary")
    assert r.status_code == 401
    assert r.headers["Content-Type"].startswith("application/json")


# ------------------------------------------------------------------------------ login

def test_bad_credentials_are_refused(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    assert login(c, USER, "wrong").status_code == 401
    assert login(c, "nobody", PW).status_code == 401
    assert c.get("/api/summary").status_code == 401


def test_login_unlocks_every_tab(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    assert login(c).status_code == 200
    for path in ("/api/summary", "/api/settings", "/api/facets", "/api/items"):
        assert c.get(path).status_code == 200, path


def test_a_bearer_token_works_too(monkeypatch, tmp_path):
    """So a script or the MCP server can share the same session store as the browser."""
    c = make_client(monkeypatch, tmp_path)
    login(c)
    token = c.get_cookie("romcom_sess").value
    fresh = create_app().test_client()
    assert fresh.get("/api/summary").status_code == 401
    r = fresh.get("/api/summary", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_the_session_survives_a_restart(monkeypatch, tmp_path):
    """The token is a row, not process memory — otherwise every code tweak (and this app is
    restarted constantly) would log the owner out mid-task."""
    c = make_client(monkeypatch, tmp_path)
    login(c)
    token = c.get_cookie("romcom_sess").value
    restarted = create_app().test_client()   # a brand-new app object
    restarted.set_cookie("romcom_sess", token)
    assert restarted.get("/api/summary").status_code == 200


# ------------------------------------------------------------------------- session life

def test_expired_sessions_are_refused_and_cleaned_up(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    login(c)
    db = connect()
    past = _stamp(datetime.now(timezone.utc) - timedelta(hours=1))
    with db:
        db.execute("UPDATE web_sessions SET expires_at=?", (past,))
    assert c.get("/api/summary").status_code == 401
    # and the dead row is removed rather than left to accumulate
    assert db.execute("SELECT COUNT(*) c FROM web_sessions").fetchone()["c"] == 0


def test_an_active_session_renews_itself(monkeypatch, tmp_path):
    """Sliding renewal, so a working session never expires underneath the owner."""
    c = make_client(monkeypatch, tmp_path)
    login(c)
    db = connect()
    soon = _stamp(datetime.now(timezone.utc) + timedelta(hours=1))   # past half-life
    with db:
        db.execute("UPDATE web_sessions SET expires_at=?", (soon,))
    assert c.get("/api/summary").status_code == 200
    after = db.execute("SELECT expires_at FROM web_sessions").fetchone()["expires_at"]
    renewed = datetime.strptime(after, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert renewed > datetime.now(timezone.utc) + timedelta(hours=23)


def test_a_fresh_session_is_not_rewritten_on_every_request(monkeypatch, tmp_path):
    """Renewal must not mean a DB write per request: the UI polls several endpoints
    continuously and would contend with the acquire watcher for the write lock."""
    c = make_client(monkeypatch, tmp_path)
    login(c)
    db = connect()
    before = db.execute("SELECT expires_at FROM web_sessions").fetchone()["expires_at"]
    for _ in range(3):
        c.get("/api/summary")
    after = db.execute("SELECT expires_at FROM web_sessions").fetchone()["expires_at"]
    assert before == after


def test_logout_revokes_the_token(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    login(c)
    token = c.get_cookie("romcom_sess").value
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/summary").status_code == 401
    # and the revoked token can't be replayed even if the cookie is put back
    replayed = create_app().test_client()
    replayed.set_cookie("romcom_sess", token)
    assert replayed.get("/api/summary").status_code == 401


def test_session_check_reports_login_state_without_tripping_the_gate(monkeypatch, tmp_path):
    c = make_client(monkeypatch, tmp_path)
    assert c.get("/api/auth/session").get_json() == {"configured": True, "user": None}
    login(c)
    assert c.get("/api/auth/session").get_json()["user"] == USER


# ------------------------------------------------------------------ secrets stay secrets

def test_the_password_is_never_in_the_settings_payload(monkeypatch, tmp_path):
    """The whole reason the credentials are kept out of config._env_settings(): a key that
    is not in that dict cannot reach app_settings, so it cannot be echoed here — no masking
    step exists to be forgotten."""
    c = make_client(monkeypatch, tmp_path)
    login(c)
    body = c.get("/api/settings").data
    assert PW.encode() not in body
    assert USER.encode() not in body
    assert b"ROMCOM_WEB_PASS" not in body
    assert "web_pass" not in c.get("/api/settings").get_json()["settings"]


def test_the_token_is_stored_hashed(monkeypatch, tmp_path):
    """A copy of the database must not yield a usable session."""
    c = make_client(monkeypatch, tmp_path)
    login(c)
    token = c.get_cookie("romcom_sess").value
    stored = connect().execute("SELECT token_hash FROM web_sessions").fetchone()["token_hash"]
    assert stored != token
    assert len(stored) == 64   # sha256 hex


# --------------------------------------------------------- background work is unaffected

def test_the_watcher_still_starts_with_auth_on(monkeypatch, tmp_path):
    """The gate is a before_request hook, so it never sees the watcher: that runs in a
    thread calling functions directly. If this ever broke, turning on the login would
    silently stop all downloads — the worst possible failure for this app."""
    from romcom import acquirer
    from romcom.config import invalidate
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ROMCOM_WEB_USER", USER)
    monkeypatch.setenv("ROMCOM_WEB_PASS", PW)
    monkeypatch.setenv("ROMCOM_ACQUIRE_WATCH", "true")
    connect()
    started = threading.Event()

    def stub(progress=None, **kwargs):
        started.set()

    monkeypatch.setattr("romcom.acquirer.auto_acquire", stub)
    app = create_app()
    invalidate()
    app.start_watcher_if_on()
    assert started.wait(timeout=5), "watcher did not start behind the auth gate"
