from .config import load_yaml
from .db import connect

def catalog_status():
    """Every system the catalog config declares, plus everything already imported that it
    doesn't declare.

    This used to iterate `catalogs:` alone. The `custom:` block and anything undeclared were
    therefore invisible: 205,907 items across 20 systems — 64% of the library, including every
    arcade, dos, nds, vita and ps3 row — never appeared, and the view gave no hint it was
    omitting them. Coverage that can silently drop most of the library is worse than none, so
    an imported system with no home in the config is now reported as `undeclared` rather than
    skipped.
    """
    cfg=load_yaml("catalogs.yaml")
    db=connect()
    counts={}; totals={}
    for r in db.execute("SELECT catalog_source src, system, COUNT(*) c FROM items GROUP BY 1,2"):
        counts[(r["src"],r["system"])]=r["c"]
        totals[r["system"]]=totals.get(r["system"],0)+r["c"]

    rows=[]; claimed=set(); covered=set()
    def add(source,system,count,loaded,declared):
        rows.append({"source":source,"system":system,"count":count,"loaded":loaded,"declared":declared})
        if count: covered.add((source,system))

    for source,meta in cfg.get("catalogs",{}).items():
        if not meta.get("enabled",False): continue
        for system in meta.get("systems",[]):
            n=counts.get((source,system),0)
            add(source,system,n,n>0,"catalogs")
            claimed.add(system)

    # `custom:` systems are filled by hand or by a one-off dat rather than by a named upstream
    # catalog, so there is no expected source to check against — report whichever source
    # actually imported them, and a bare zero row when nothing has.
    for system in cfg.get("custom",{}).get("systems",[]):
        if system in claimed: continue
        claimed.add(system)
        srcs=sorted(s for (s,sy),n in counts.items() if sy==system and n)
        if not srcs:
            add("custom",system,0,False,"custom")
        for s in srcs:
            add(s or "unknown",system,counts[(s,system)],True,"custom")

    # Imported but mentioned nowhere in the config — ps3 arrived this way, through an explicit
    # system override in the import UI. Surfacing it is the entire point of the change.
    for system in sorted(set(totals)-claimed):
        for (s,sy),n in sorted(counts.items()):
            if sy==system and n:
                add(s or "unknown",system,n,True,"undeclared")

    # Finally, any source/system pair still unaccounted for. A declared system is only checked
    # against the one source that declares it, so items imported under a different source fell
    # through even after the passes above — psp is declared under redump, and its 6,774
    # nointro-sourced rows were invisible, as were 25,649 `local` gba rows. 48,637 items in all.
    # Whatever the config expected, these are really in the library and have to be visible.
    for (s,sy),n in sorted(counts.items()):
        if n and (s,sy) not in covered:
            add(s or "unknown",sy,n,True,"extra-source")
    return rows

def render():
    rows=catalog_status()
    lines=[]
    missing=0
    for r in rows:
        ok=r["loaded"]; missing+=0 if ok else 1
        tag="" if r.get("declared")=="catalogs" else f"  [{r.get('declared')}]"
        lines.append(f"{'OK' if ok else 'MISSING':7} {r['source']:10} {r['system']:18} {r['count']:7}{tag}")
    lines.append("")
    lines.append(f"Catalog targets: {len(rows)}; missing imports: {missing}")
    return "\n".join(lines), missing
