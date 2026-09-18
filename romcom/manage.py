import csv
from pathlib import Path
from .db import connect

FIELDS=["id","title","system","series","series_number","year","authorized","wanted","status",
        "preferred_runtime","support_level","region","language","play_status","notes",
        "catalog_source","external_id"]

BOOL_FIELDS={"authorized","wanted"}
EDITABLE=set(FIELDS)-{"id","title","catalog_source","external_id","series_number","year"}

def set_series(name,field,value):
    if field not in EDITABLE:
        raise ValueError(f"field is not editable: {field}")
    if field in BOOL_FIELDS:
        value=1 if str(value).lower() in ("1","true","yes","on") else 0
    db=connect()
    with db:
        cur=db.execute(f"UPDATE items SET {field}=?,updated_at=CURRENT_TIMESTAMP WHERE lower(series)=lower(?)",(value,name))
    return cur.rowcount

def export_csv(path):
    db=connect(); rows=db.execute("SELECT "+",".join(FIELDS)+" FROM items ORDER BY system,series,series_number,title").fetchall()
    p=Path(path)
    with p.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS); w.writeheader()
        for r in rows: w.writerow(dict(r))
    return len(rows)

def import_csv(path):
    db=connect(); changed=0; unknown=0
    with open(path,newline="",encoding="utf-8-sig") as f, db:
        for row in csv.DictReader(f):
            item_id=(row.get("id") or "").strip()
            if not item_id or not db.execute("SELECT 1 FROM items WHERE id=?",(item_id,)).fetchone():
                unknown+=1; continue
            fields=[]; values=[]
            for key in EDITABLE:
                if key not in row or row[key]=="" or key=="title": continue
                v=row[key]
                if key in BOOL_FIELDS: v=1 if str(v).lower() in ("1","true","yes","on") else 0
                fields.append(f"{key}=?"); values.append(v)
            if fields:
                values.append(item_id)
                db.execute(f"UPDATE items SET {','.join(fields)},updated_at=CURRENT_TIMESTAMP WHERE id=?",values)
                changed+=1
    return {"changed":changed,"unknown":unknown}
