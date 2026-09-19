"""A hermetic baseline for the suite.

`config.py` calls `load_dotenv` at import, so anything the owner puts in `.env` lands in
`os.environ` for the whole test process. That was invisible until the login was switched on:
`ROMCOM_WEB_USER`/`ROMCOM_WEB_PASS` in `.env` made `webauth.configured()` true in every test,
and 37 tests that expect an open app started getting 401 where they asserted 404 or 200. The
failure had nothing to do with those tests — the suite simply was not isolated from the
machine it ran on.

So the baseline is pinned here, before each test, rather than left to whatever `.env` holds.
Tests that want auth, the assistant, or MCP *on* set the vars themselves via `monkeypatch`,
which applies after this fixture and therefore wins. The rule is that a test states its own
preconditions instead of inheriting the developer's.

`ROMCOM_DB` is pinned as a floor for a blunter reason: without it a test that forgets to set
a database would open the real `romcom.db` — 2 GB of the owner's actual collection — and
write to it.
"""
import pytest

from romcom.config import invalidate

# Cleared so the defaults in `_env_settings()` are what a test sees unless it says otherwise.
LEAKY = (
    "ROMCOM_WEB_USER", "ROMCOM_WEB_PASS", "ROMCOM_MCP_KEY",
    "ROMCOM_CHAT_ENABLED", "ROMCOM_MCP_ENABLED",
    # A real `mcp-servers.json` in the repo root is a normal thing for the owner to have, and
    # without this every test that builds a registry would try to reach those servers.
    "ROMCOM_MCP_SERVERS",
)


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch, tmp_path_factory):
    for name in LEAKY:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROMCOM_MCP_SERVERS", str(tmp_path_factory.mktemp("no-servers") / "mcp-servers.json"))
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path_factory.mktemp("db-floor") / "floor.db"))
    invalidate()
    yield
    invalidate()
