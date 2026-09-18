import os
from pathlib import Path
from romcom.catalog import import_dat
from romcom.db import connect

DAT="""<?xml version="1.0"?><datafile><game name="Example (USA)"><description>Example Game</description><year>1994</year><rom name="x.bin" size="4" crc="1234abcd" md5="0123456789abcdef0123456789abcdef" sha1="0123456789abcdef0123456789abcdef01234567"/></game></datafile>"""

def test_import_dat(tmp_path,monkeypatch):
    monkeypatch.setenv("ROMCOM_DB",str(tmp_path/"test.db"))
    p=tmp_path/"test.dat"; p.write_text(DAT)
    r=import_dat(p,"snes","testsource")
    assert r["items"]==1
    db=connect()
    item=db.execute("SELECT * FROM items").fetchone()
    assert item["title"]=="Example Game"
    assert db.execute("SELECT COUNT(*) c FROM file_hashes").fetchone()["c"]==3
