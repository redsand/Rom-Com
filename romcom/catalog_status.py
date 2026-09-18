from .config import load_yaml
from .db import connect

def catalog_status():
    cfg=load_yaml("catalogs.yaml").get("catalogs",{})
    db=connect(); rows=[]
    for source,meta in cfg.items():
        if not meta.get("enabled",False): continue
        for system in meta.get("systems",[]):
            count=db.execute("SELECT COUNT(*) c FROM items WHERE catalog_source=? AND system=?",(source,system)).fetchone()["c"]
            rows.append({"source":source,"system":system,"count":count,"loaded":count>0})
    return rows

def render():
    rows=catalog_status()
    lines=[]
    missing=0
    for r in rows:
        ok=r["loaded"]; missing+=0 if ok else 1
        lines.append(f"{'OK' if ok else 'MISSING':7} {r['source']:10} {r['system']:18} {r['count']:7}")
    lines.append("")
    lines.append(f"Catalog targets: {len(rows)}; missing imports: {missing}")
    return "\n".join(lines), missing
