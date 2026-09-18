from pathlib import Path
import os, sqlite3, time, yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Overrides live in the app_settings table so the web UI can edit them without
# touching .env; the cache keeps the hot path (every indexer/SAB call) off the disk.
_CACHE = {"t": 0.0, "vals": {}}
_CACHE_TTL = 3.0

def invalidate():
    """Drop the override cache (called after the UI saves settings)."""
    _CACHE["t"] = 0.0

# Environment variable feeding each settable key — the Settings UI uses this to
# attribute every value's source: a UI override, the .env/environment, or a default.
ENV_VARS = {
    "nzb_url": "NZB_API_URL",
    "nzb_key": "NZB_API_KEY",
    "sab_url": "SAB_URL",
    "sab_key": "SAB_API_KEY",
    "sab_verify_ssl": "SAB_VERIFY_SSL",
    "sab_category": "ROMCOM_SAB_CATEGORY",
    "download_dir": "ROMCOM_DOWNLOAD_DIR",
    "acquire_poll": "ROMCOM_ACQUIRE_POLL",
    "acquire_max_wait_min": "ROMCOM_ACQUIRE_MAX_WAIT_MIN",
    "acquire_batch_max": "ROMCOM_ACQUIRE_BATCH_MAX",
}

def _env_settings():
    return {
        "db": os.getenv("ROMCOM_DB", str(ROOT / "romcom.db")),
        "nzb_url": os.getenv("NZB_API_URL", "").rstrip("?"),
        "nzb_key": os.getenv("NZB_API_KEY", ""),
        "sab_url": os.getenv("SAB_URL", "").rstrip("?"),
        "sab_key": os.getenv("SAB_API_KEY", ""),
        "sab_verify_ssl": os.getenv("SAB_VERIFY_SSL", "true"),
        "sab_category": os.getenv("ROMCOM_SAB_CATEGORY", "odin"),
        "download_dir": (os.getenv("ROMCOM_DOWNLOAD_DIR") or "").strip().strip('"'),
        "acquire_poll": os.getenv("ROMCOM_ACQUIRE_POLL", "30"),
        "acquire_max_wait_min": os.getenv("ROMCOM_ACQUIRE_MAX_WAIT_MIN", "240"),
        "acquire_batch_max": os.getenv("ROMCOM_ACQUIRE_BATCH_MAX", "0"),
    }

def _overrides(db_path):
    now = time.monotonic()
    if now - _CACHE["t"] < _CACHE_TTL:
        return _CACHE["vals"]
    vals = {}
    try:
        con = sqlite3.connect(db_path, timeout=5)
        try:
            vals = {r[0]: r[1] for r in con.execute("SELECT key,value FROM app_settings")}
        finally:
            con.close()
    except Exception:  # missing db/table — env values apply until first migration
        vals = {}
    _CACHE.update(t=now, vals=vals)
    return vals

def settings():
    s = _env_settings()
    for k, v in _overrides(s["db"]).items():
        if k in s and v not in (None, ""):
            s[k] = v
    try: s["acquire_poll"] = float(s["acquire_poll"])
    except (TypeError, ValueError): s["acquire_poll"] = 30.0
    try: s["acquire_max_wait_min"] = float(s["acquire_max_wait_min"])
    except (TypeError, ValueError): s["acquire_max_wait_min"] = 240.0
    try: s["acquire_batch_max"] = int(s["acquire_batch_max"])
    except (TypeError, ValueError): s["acquire_batch_max"] = 0
    s["sab_verify_ssl"] = str(s["sab_verify_ssl"]).strip().lower() not in ("0", "false", "no", "off")
    return s

def load_yaml(name):
    p = ROOT / name
    if not p.exists(): return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}