import json
from .config import load_yaml
from .db import connect

def seed_series(db):
    data=load_yaml("series.yaml").get("series",{})
    for _,series in data.items():
        for idx,row in enumerate(series.get("items",[]),1):
            item_id,title,year=row
            db.execute("""INSERT INTO items(id,title,system,series,series_number,year,status,catalog_source,external_id)
              VALUES(?,?,?,?,?,?,?,'series',?)
              ON CONFLICT(id) DO UPDATE SET title=excluded.title,series=excluded.series,
              series_number=excluded.series_number,year=excluded.year""",
              (item_id,title,"windows",series["title"],idx,year,"CATALOGED",item_id))

def seed_volumes(db):
    for v in load_yaml("volumes.yaml").get("volumes",[]):
        db.execute("""INSERT INTO volumes(id,title,authorized,estimated_bytes,min_bytes,max_bytes)
          VALUES(?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET title=excluded.title,authorized=excluded.authorized,
          estimated_bytes=excluded.estimated_bytes,min_bytes=excluded.min_bytes,max_bytes=excluded.max_bytes""",
          (v["id"],v["title"],int(bool(v.get("authorized"))),v.get("estimated_bytes"),
           v.get("min_bytes"),v.get("max_bytes")))
        db.execute("DELETE FROM volume_search WHERE volume_id=?",(v["id"],))
        for q in v.get("search",[]):
            db.execute("INSERT OR IGNORE INTO volume_search(volume_id,query) VALUES(?,?)",(v["id"],q))
        db.execute("DELETE FROM volume_covers WHERE volume_id=?",(v["id"],))
        for item_id in v.get("covers",[]):
            if db.execute("SELECT 1 FROM items WHERE id=?",(item_id,)).fetchone():
                db.execute("INSERT OR IGNORE INTO volume_covers(volume_id,item_id) VALUES(?,?)",(v["id"],item_id))

def seed():
    db=connect()
    with db:
        seed_series(db); seed_volumes(db)
    return db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
