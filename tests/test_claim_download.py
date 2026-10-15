"""A file the acquirer fetched belongs to the game it was fetched for, whatever the site
called it."""
import os
from datetime import datetime

from romcom.acquirer import claim_download
from romcom.db import connect
from romcom.reconcile import unclaimed_downloads
from romcom.scanner import scan


def _item(db, iid, title, system="dreamcast"):
    with db:
        db.execute("INSERT INTO items(id,title,system,status,wanted,authorized) "
                   "VALUES(?,?,?,'DOWNLOADED',1,1)", (iid, title, system))


def test_a_claimed_download_survives_a_rescan(tmp_path):
    db = connect()
    _item(db, "m", "Who Wants to Beat Up a Millionaire (USA)")
    f = tmp_path / "[CDI] Who Wants to Beat Up a Millionnaire (US)[DCCM].7z"
    f.write_bytes(b"7z" + b"x" * 6_000_000)
    claim_download(db, "m", f)
    scan(tmp_path, adopt=False)
    row = db.execute("SELECT matched_item_id, match_method, sha1 FROM files WHERE path=?",
                     (str(f),)).fetchone()
    assert row["matched_item_id"] == "m" and row["match_method"] == "downloaded"
    assert row["sha1"]                                    # hashed by the scan all the same


def test_past_downloads_are_paired_by_size_time_and_name(tmp_path):
    db = connect()
    _item(db, "m", "Who Wants to Beat Up a Millionaire (USA)")
    _item(db, "q", "Q", "amiga")                          # a junk one-letter catalog title
    f = tmp_path / "[CDI] Who Wants to Beat Up a Millionnaire (US)[DCCM].7z"
    f.write_bytes(b"7z" + b"x" * 6_000_000)
    junk = tmp_path / "Famicom Q-R.rar"
    junk.write_bytes(b"r" * 1234)
    other = tmp_path / "Unrelated Game.7z"                 # same size, wrong time and name
    other.write_bytes(b"y" * 777)
    scan(tmp_path, adopt=False)
    os.utime(other, (0, 0))
    done = datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")
    with db:
        db.execute("UPDATE files SET mtime=0 WHERE path=?", (str(other),))
        for iid, size in (("m", f.stat().st_size), ("q", 1234)):
            db.execute("INSERT INTO jobs(entity_type,entity_id,result_title,bytes,status,source,"
                       "queued_at,completed_at) VALUES('item',?,?,?,'DOWNLOADED','cdromance',?,?)",
                       (iid, "x", size, done, done))
    r = unclaimed_downloads(apply=True, db=db)
    assert [x["item_id"] for x in r["items"]] == ["m"]
    assert db.execute("SELECT matched_item_id FROM files WHERE path=?",
                      (str(f),)).fetchone()[0] == "m"
