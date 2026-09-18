from pathlib import Path
from .config import settings
from .db import connect
from . import sab

def run(check_sab=True):
    s=settings(); results=[]
    try:
        db=connect()
        db.execute("SELECT 1").fetchone()
        results.append(("database",True,s["db"]))
    except Exception as e:
        results.append(("database",False,str(e)))
    results.append(("nzb-config",bool(s["nzb_url"] and s["nzb_key"]),s["nzb_url"] or "not configured"))
    results.append(("sab-config",bool(s["sab_url"] and s["sab_key"]),s["sab_url"] or "not configured"))
    if check_sab and s["sab_url"] and s["sab_key"]:
        try:
            sab.queue()
            results.append(("sab-api",True,"reachable"))
        except Exception as e:
            results.append(("sab-api",False,str(e)))
    return results
