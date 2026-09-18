from romcom.db import connect
from romcom.manage import set_series, export_csv, import_csv

def test_series_and_csv_management(tmp_path,monkeypatch):
    monkeypatch.setenv("ROMCOM_DB",str(tmp_path/"db.sqlite"))
    db=connect()
    with db:
        db.execute("INSERT INTO items(id,title,series) VALUES('a','A','Nancy Drew')")
        db.execute("INSERT INTO items(id,title,series) VALUES('b','B','Nancy Drew')")
    assert set_series("Nancy Drew","authorized","true")==2
    p=tmp_path/"out.csv"
    assert export_csv(p)==2
    text=p.read_text()
    text=text.replace("1,1,CATALOGED", "1,0,CATALOGED")
    p.write_text(text)
    result=import_csv(p)
    assert result["changed"]==2
