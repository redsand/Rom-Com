from .config import load_yaml
from .db import connect

def seed():
    data = load_yaml("series.yaml").get("series", {})
    db = connect()
    with db:
        for series_id, series in data.items():
            for idx, row in enumerate(series.get("items", []), 1):
                item_id, title, year = row
                db.execute("""INSERT INTO items(id,title,system,series,series_number,year,status)
                              VALUES(?,?,?,?,?,?,?)
                              ON CONFLICT(id) DO UPDATE SET title=excluded.title,series=excluded.series,
                              series_number=excluded.series_number,year=excluded.year""",
                           (item_id,title,"windows",series["title"],idx,year,"CATALOGED"))
    return db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
