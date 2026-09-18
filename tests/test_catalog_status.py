from romcom.db import connect
from romcom.catalog_status import catalog_status

def test_catalog_status_returns_targets(tmp_path,monkeypatch):
    monkeypatch.setenv("ROMCOM_DB",str(tmp_path/"db.sqlite"))
    connect()
    rows=catalog_status()
    assert any(r["source"]=="nointro" and r["system"]=="snes" for r in rows)
