from pathlib import Path
from .config import settings
from .db import connect
from . import sab, indexer

def run(check_sab=True, check_nzb=True):
    s=settings(); results=[]
    try:
        db=connect()
        db.execute("SELECT 1").fetchone()
        results.append(("database",True,s["db"]))
    except Exception as e:
        results.append(("database",False,str(e)))
    results.append(("nzb-config",bool(s["nzb_url"] and s["nzb_key"]),s["nzb_url"] or "not configured"))
    results.append(("sab-config",bool(s["sab_url"] and s["sab_key"]),s["sab_url"] or "not configured"))
    if check_nzb and s["nzb_url"] and s["nzb_key"]:
        try:
            indexer.ping()
            results.append(("nzb-api",True,"authenticated/reachable"))
        except Exception as e:
            results.append(("nzb-api",False,str(e)))
    if check_sab and s["sab_url"] and s["sab_key"]:
        try:
            sab.queue()
            results.append(("sab-api",True,"reachable"))
        except Exception as e:
            results.append(("sab-api",False,str(e)))
    try:
        from . import cores
        bs = cores.bios_status()
        gaps = [f"{r['label']}: missing " + " or ".join(s for slot in r["missing"] for s in slot)
                for r in bs["rows"] if r["missing"] and not r.get("optional")]
        # A missing BIOS is a report, not an outage: the games are on the card and the
        # file just needs finding once. "ok" here means nothing unplayable is lurking.
        detail = "; ".join(gaps[:3]) + (f" and {len(gaps) - 3} more" if len(gaps) > 3 else "")
        results.append(("console-bios", not gaps,
                        detail if gaps else "every console's BIOS is in the library"))
    except Exception as e:
        results.append(("console-bios",False,str(e)))
    return results
