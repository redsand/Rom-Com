from pathlib import Path
import os, yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

def load_yaml(name):
    p = ROOT / name
    if not p.exists(): return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}

def settings():
    return {
        "db": os.getenv("ROMCOM_DB", str(ROOT / "romcom.db")),
        "nzb_url": os.getenv("NZB_API_URL", "").rstrip("?"),
        "nzb_key": os.getenv("NZB_API_KEY", ""),
        "sab_url": os.getenv("SAB_URL", "").rstrip("?"),
        "sab_key": os.getenv("SAB_API_KEY", ""),
        "sab_verify_ssl": os.getenv("SAB_VERIFY_SSL", "true").strip().lower() not in ("0", "false", "no", "off"),
        "sab_category": os.getenv("ROMCOM_SAB_CATEGORY", "odin"),
    }
