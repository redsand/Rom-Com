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


# ---------------------------------------------------------------- patch entries

PATCH_DAT = """<?xml version="1.0"?><datafile>
<game name="0003 - 03D56334 to 0F3E05F9"><rom name="a.ips" size="4" crc="aaaaaaaa"/></game>
<game name="0003 - 0F3E05F9 to 03D56334"><rom name="b.ips" size="4" crc="bbbbbbbb"/></game>
<game name="Some Hack (v1.0).bps"><rom name="c.bps" size="4" crc="cccccccc"/></game>
<game name="Yoshi Touch &amp; Go (USA, Australia)"><rom name="y.nds" size="4" crc="03d56334"/></game>
</datafile>"""


def test_import_skips_patch_entries_but_keeps_the_game(tmp_path, monkeypatch):
    """A patch DAT names each entry for the transformation, not the game -- importing one
    put 5,040 undownloadable rows on nds/gba that duplicated titles already catalogued.
    The real game in the same file must still come through."""
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    p = tmp_path / "patch.dat"; p.write_text(PATCH_DAT, encoding="utf-8")
    r = import_dat(p, "nds", "testsource")
    assert r["items"] == 1 and r["patches"] == 3
    db = connect()
    assert [x["title"] for x in db.execute("SELECT title FROM items")] == ["Yoshi Touch & Go (USA, Australia)"]
    # Skipped entries must leave nothing behind -- no alias, no hash pointing at a ghost.
    assert db.execute("SELECT COUNT(c) c FROM (SELECT 1 c FROM aliases)").fetchone()["c"] == 1
    assert db.execute("SELECT COUNT(*) c FROM file_hashes").fetchone()["c"] == 1


def test_is_patch_entry_rejects_real_titles_containing_to():
    """The false-positive direction is the dangerous one: a game wrongly judged a patch is
    silently absent from the catalog, with nothing to notice it by."""
    from romcom.catalog import is_patch_entry
    for real in ("007 - Blood Stone (USA)",
                 "Goomba V2.2 - Back to Earth 3D (PD) [C]",
                 "PocketNES V9.98 - Chip to Dale no Daisakusen (J)",
                 "Final Fantasy Crystal Chronicles - Ring of Fates (Europe) (En,Fr,De,Es)",
                 "2153 - 295A54EB to 3AFD677"):      # 7 hex digits -- not a checksum pair
        assert not is_patch_entry(real), real
    for patch in ("2153 - 295A54EB to 3AFD6777", "295A54EB to 3AFD6777",
                  "deadbeef to cafebabe", "Translation.ips", "Hack.xdelta"):
        assert is_patch_entry(patch), patch


def test_is_patch_entry_checks_the_external_name_too(tmp_path, monkeypatch):
    """A DAT can carry a tidy <description> over a patch-shaped entry name. The name is
    what identifies the entry, so either form being patch-shaped disqualifies it."""
    from romcom.catalog import is_patch_entry
    assert is_patch_entry("Nice Looking Description", "0003 - 03D56334 to 0F3E05F9")
