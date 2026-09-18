from romcom.db import connect
from romcom.config import settings, invalidate
from romcom.web import create_app


def env_db(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ROMCOM_SAB_CATEGORY", "Games")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    invalidate()
    return connect()


def test_env_values_apply(monkeypatch, tmp_path):
    env_db(monkeypatch, tmp_path)
    assert settings()["sab_category"] == "Games"
    assert settings()["acquire_batch_max"] == 0


def test_db_override_beats_env(monkeypatch, tmp_path):
    db = env_db(monkeypatch, tmp_path)
    with db:
        db.execute("INSERT INTO app_settings(key,value) VALUES('sab_category','tv')")
    invalidate()
    assert settings()["sab_category"] == "tv"


def test_empty_override_falls_back_to_env(monkeypatch, tmp_path):
    db = env_db(monkeypatch, tmp_path)
    with db:
        db.execute("INSERT INTO app_settings(key,value) VALUES('sab_category','')")
    invalidate()
    assert settings()["sab_category"] == "Games"  # from .env


def test_numeric_and_bool_coercion(monkeypatch, tmp_path):
    db = env_db(monkeypatch, tmp_path, ROMCOM_ACQUIRE_POLL="45")
    with db:
        db.executemany("INSERT INTO app_settings(key,value) VALUES(?,?)",
                       [("acquire_batch_max", "25"), ("acquire_poll", "5"), ("sab_verify_ssl", "false")])
    invalidate()
    s = settings()
    assert s["acquire_batch_max"] == 25 and s["acquire_poll"] == 5.0
    assert s["sab_verify_ssl"] is False


def test_parallel_default_and_coercion(monkeypatch, tmp_path):
    env_db(monkeypatch, tmp_path)
    assert settings()["acquire_parallel"] == 3  # default: 3 downloads in flight
    env_db(monkeypatch, tmp_path, ROMCOM_ACQUIRE_PARALLEL="0")
    assert settings()["acquire_parallel"] == 0  # 0 = unlimited
    env_db(monkeypatch, tmp_path, ROMCOM_ACQUIRE_PARALLEL="junk")
    assert settings()["acquire_parallel"] == 3  # bad value falls back, no crash


def test_bad_numeric_falls_back(monkeypatch, tmp_path):
    db = env_db(monkeypatch, tmp_path)
    with db:
        db.execute("INSERT INTO app_settings(key,value) VALUES('acquire_batch_max','lots')")
    invalidate()
    assert settings()["acquire_batch_max"] == 0  # default, not a crash


def test_settings_sources(monkeypatch, tmp_path):
    monkeypatch.delenv("ROMCOM_DOWNLOAD_DIR", raising=False)
    env_db(monkeypatch, tmp_path, ROMCOM_ACQUIRE_BATCH_MAX="15")
    c = create_app().test_client()
    d = c.get("/api/settings").get_json()
    assert d["sources"]["sab_category"] == "env"        # monkeypatched env var
    assert d["sources"]["acquire_batch_max"] == "env"
    assert d["sources"]["download_dir"] == "default"   # set nowhere

    d = c.post("/api/settings", json={"download_dir": "H:/Games"}).get_json()
    assert d["sources"]["download_dir"] == "ui"        # a UI save takes over

    d = c.post("/api/settings", json={"download_dir": ""}).get_json()
    assert d["sources"]["download_dir"] == "default"  # cleared override falls back


def test_settings_roundtrip(monkeypatch, tmp_path):
    env_db(monkeypatch, tmp_path)
    c = create_app().test_client()
    r = c.post("/api/settings", json={"sab_category": "tv", "acquire_batch_max": "25"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["settings"]["sab_category"] == "tv" and d["settings"]["acquire_batch_max"] == 25
    assert set(d["overrides"]) == {"sab_category", "acquire_batch_max"}

    d = c.get("/api/settings").get_json()  # persists and is visible on reload
    assert d["settings"]["sab_category"] == "tv"

    r = c.post("/api/settings", json={"nope": "x"})
    assert r.status_code == 400

    d = c.post("/api/settings", json={"sab_category": ""}).get_json()  # "" clears the override
    assert "sab_category" not in d["overrides"]
    assert d["settings"]["sab_category"] == "Games"  # env value is back in charge


def test_settings_test_endpoint(monkeypatch, tmp_path):
    """The Settings 'Test connections' button probes all three sources, and API
    keys never leak into the error detail (request errors echo the full URL)."""
    env_db(monkeypatch, tmp_path, NZB_API_KEY="sekret", SAB_API_KEY="sabsekret")
    monkeypatch.setattr("romcom.indexer.ping", lambda: True)
    monkeypatch.setattr("romcom.sab.queue", lambda: [])
    monkeypatch.setattr("romcom.webdl.test", lambda: True)
    c = create_app().test_client()
    d = c.post("/api/settings/test").get_json()
    assert set(d) == {"indexer", "sabnzbd", "romsgames"}
    assert all(v["ok"] for v in d.values())

    def boom():
        raise RuntimeError("404 for url https://api.example/api?t=search&apikey=sekret&limit=1")
    monkeypatch.setattr("romcom.indexer.ping", boom)
    d = c.post("/api/settings/test").get_json()
    assert d["indexer"]["ok"] is False
    assert "sekret" not in d["indexer"]["detail"] and "***" in d["indexer"]["detail"]
    assert d["sabnzbd"]["ok"] is True  # one failure doesn't mask the rest