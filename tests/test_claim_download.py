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


def test_a_disc_in_an_archive_is_unpacked_to_launch(tmp_path, monkeypatch):
    """RetroArch opens an archive's FIRST file: an uploader's .url ahead of the .cdi meant
    Flycast got a web shortcut. The playable disc is unpacked and launched instead."""
    import zipfile
    from romcom import player, config
    z = tmp_path / "Millionnaire.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Replayers.url", "[InternetShortcut]")
        f.writestr("Game (US).cdi", b"cdi" * 1000)
    monkeypatch.setattr(config, "settings", lambda: {"download_dir": str(tmp_path)})
    got = player.unpacked_disc(str(z), "dreamcast")
    assert got.name == "Game (US).cdi" and got.read_bytes() == b"cdi" * 1000
    assert player.unpacked_disc(str(tmp_path / "Mario.zip"), "nes") is None   # carts: as is


def test_a_released_stub_is_not_paired_back(tmp_path):
    """stub_discs let go of a 1 MB 'disc'; pairing downloads by size put it straight back."""
    db = connect()
    _item(db, "lh", "Love Hina - Smile Again (Japan)")
    f = tmp_path / "Love Hina - Smile Again (Japan).zip"
    f.write_bytes(b"z" * 970_000)
    scan(tmp_path, adopt=False)
    done = datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")
    with db:
        db.execute("INSERT INTO jobs(entity_type,entity_id,result_title,bytes,status,source,"
                   "queued_at,completed_at) VALUES('item','lh','x',?,'DOWNLOADED','cdromance',?,?)",
                   (f.stat().st_size, done, done))
        db.execute("INSERT INTO events(item_id,event,detail) VALUES('lh','stub-disc',?)",
                   (f"970000 bytes cannot be a dreamcast disc: {f}",))
    assert unclaimed_downloads(db=db)["count"] == 0
