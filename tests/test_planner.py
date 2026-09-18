from romcom.db import connect
from romcom.planner import bulk_plan

def test_bulk_coverage_score(tmp_path,monkeypatch):
    monkeypatch.setenv("ROMCOM_DB",str(tmp_path/"test.db")); db=connect()
    with db:
        for i in range(10):
            db.execute("INSERT INTO items(id,title,authorized,status) VALUES(?,?,1,'CATALOGED')",(f"i{i}",f"Game {i}"))
        db.execute("INSERT INTO volumes(id,title,authorized,estimated_bytes) VALUES('v','Bundle',1,1073741824)")
        for i in range(10): db.execute("INSERT INTO volume_covers(volume_id,item_id) VALUES('v',?)",(f"i{i}",))
    p=bulk_plan()
    assert p[0]["missing"]==10
    assert p[0]["coverage_score"]==10
