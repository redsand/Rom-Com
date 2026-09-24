"""Rebuilding MAME sets from a flat dump."""
from pathlib import Path

from romcom.db import connect
from romcom import mameset

DAT = """<?xml version="1.0"?><datafile>
<machine name="galaga" sourcefile="namco/galaga.cpp">
<description>Galaga</description>
<rom name="gg1_1b.3p" size="4096" crc="aaaa1111" sha1="1111111111111111111111111111111111111111"/>
<rom name="prom-2.5c" size="256" crc="bbbb2222" sha1="2222222222222222222222222222222222222222"/>
<device_ref name="namco54"/>
</machine>
<machine name="namco54" isdevice="yes" runnable="no">
<description>Namco 54xx</description>
<rom name="54xx.bin" size="1024" crc="cccc3333" sha1="3333333333333333333333333333333333333333"/>
</machine>
<machine name="broken" sourcefile="x.cpp">
<description>Broken</description>
<rom name="missing.rom" size="256" crc="dddd4444" sha1="4444444444444444444444444444444444444444"/>
</machine>
<machine name="other" sourcefile="x.cpp">
<description>Other</description>
<rom name="prom-2.5c" size="256" crc="bbbb2222" sha1="2222222222222222222222222222222222222222"/>
</machine>
</datafile>"""


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "m.db"))
    from romcom.config import invalidate
    invalidate()
    dat = tmp_path / "arcade.dat"
    dat.write_text(DAT, encoding="utf-8")
    monkeypatch.setattr(mameset, "dat_path", lambda: dat)
    flat = tmp_path / "flat"
    flat.mkdir()
    db = connect()
    # A flattened dump: collisions get renamed, and one physical file serves several sets.
    disk = {"gg1_1b.3p": "aaaa1111", "prom-2.5c_7": "bbbb2222", "54xx.bin_3": "cccc3333"}
    with db:
        for name, crc in disk.items():
            (flat / name).write_bytes(b"x" * 16)
            db.execute("INSERT INTO files(path,bytes,crc32,sha1) VALUES(?,?,?,?)",
                       (str(flat / name), 16, crc, {"aaaa1111": "1" * 40, "bbbb2222": "2" * 40,
                                                    "cccc3333": "3" * 40}[crc]))
    return db, tmp_path


def test_a_set_is_written_under_the_names_mame_expects(monkeypatch, tmp_path):
    """A flattened dump renames collisions — `prom-2.5c` arrives as `prom-2.5c_7` — and MAME
    looks for the canonical name only, so copying the on-disk name produces a set it rejects."""
    db, tmp = _setup(monkeypatch, tmp_path)
    r = mameset.build(["galaga"], tmp / "out", db=db)
    assert r["sets"]["galaga"]["complete"] is True
    assert (tmp / "out" / "galaga" / "prom-2.5c").exists()
    assert not (tmp / "out" / "galaga" / "prom-2.5c_7").exists()


def test_device_sets_come_along_uninvited(monkeypatch, tmp_path):
    """galaga will not boot without namco54's roms; MAME says so as
    `54xx.bin - NOT FOUND (namco54)`. An export that ignores device_ref ships games that
    refuse to start."""
    db, tmp = _setup(monkeypatch, tmp_path)
    r = mameset.build(["galaga"], tmp / "out", db=db)
    assert "namco54" in r["sets"] and r["sets"]["namco54"]["complete"]
    assert (tmp / "out" / "namco54" / "54xx.bin").exists()


def test_one_file_can_serve_two_sets(monkeypatch, tmp_path):
    """The reason this is built from the dat rather than from files.matched_item_id: that
    column records a single owner, so a shared prom left every set but one incomplete."""
    db, tmp = _setup(monkeypatch, tmp_path)
    r = mameset.build(["galaga", "other"], tmp / "out", db=db)
    assert r["sets"]["galaga"]["complete"] and r["sets"]["other"]["complete"]
    assert (tmp / "out" / "galaga" / "prom-2.5c").exists()
    assert (tmp / "out" / "other" / "prom-2.5c").exists()


def test_a_missing_rom_is_reported_not_hidden(monkeypatch, tmp_path):
    db, tmp = _setup(monkeypatch, tmp_path)
    with db:
        db.execute("DELETE FROM files WHERE crc32='bbbb2222'")
    r = mameset.build(["galaga"], tmp / "out", db=db)
    assert r["sets"]["galaga"]["complete"] is False
    assert "prom-2.5c" in r["sets"]["galaga"]["missing"]


def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    db, tmp = _setup(monkeypatch, tmp_path)
    r = mameset.build(["galaga"], tmp / "out", db=db, dry_run=True)
    assert r["sets"]["galaga"]["complete"] is True
    assert not (tmp / "out").exists()


def test_no_dat_degrades_instead_of_failing(monkeypatch, tmp_path):
    db, tmp = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mameset, "dat_path", lambda: None)
    r = mameset.build(["galaga"], tmp / "out", db=db)
    assert r["copied"] == 0 and "no MAME dat" in r["error"]


def test_playable_is_assembly_not_status(monkeypatch, tmp_path):
    """Status is the wrong question for arcade.

    A MAME set is built from chips scattered through a flat dump, so an item with no matched
    file of its own can be perfectly playable while a VERIFIED one is missing a chip another
    set claimed. In the real library 4,484 arcade items are VERIFIED but cannot be assembled
    — each one offering a Play button whose only outcome was MAME refusing to start — and 119
    are CATALOGED but build fine.
    """
    db, tmp = _setup(monkeypatch, tmp_path)
    with db:
        # galaga's roms are on disk; `other` needs a prom that is not.
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id)"
                   " VALUES('g','Galaga','arcade','CATALOGED','antopisa','arcade/galaga')")
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id)"
                   " VALUES('o','Broken','arcade','VERIFIED','antopisa','arcade/broken')")
    ok = mameset.assemblable(db)
    assert "galaga" in ok and "broken" not in ok
    mameset.refresh_playable(db)
    rows = {r["id"]: r["playable"] for r in db.execute("SELECT id,playable FROM items")}
    assert rows["g"] == 1, "CATALOGED but assemblable must be playable"
    assert rows["o"] == 0, "VERIFIED but unassemblable must not be"


def test_devices_are_never_offered_as_playable(monkeypatch, tmp_path):
    db, tmp = _setup(monkeypatch, tmp_path)
    with db:
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id,is_device)"
                   " VALUES('d','Namco 54xx','arcade','VERIFIED','antopisa','arcade/namco54',1)")
    mameset.refresh_playable(db)
    assert db.execute("SELECT playable FROM items WHERE id='d'").fetchone()["playable"] == 0
