import zipfile
from romcom.catalog import parse_dat, detect_system, import_dats
from romcom.db import connect

XML_DAT = """<?xml version="1.0"?>
<!DOCTYPE datafile PUBLIC "-//Logiqx//DTD ROM Management Datafile//EN" "http://www.logiqx.com/Dats/datafile.dtd">
<datafile><header><name>Sony - PlayStation</name><author>redump.org</author><homepage>redump.org</homepage></header>
<game name="Example (USA)"><description>Example Game</description>
<rom name="x.cue" size="4" crc="1234abcd"/><rom name="x.bin" size="9" crc="9999aaaa" md5="0123456789abcdef0123456789abcdef"/></game>
</datafile>"""

CM_DAT = """clrmamepro (
\tname "ScummVM AGI"
\tdescription "ScummVM - AGI Engine Games"
\tauthor Gruby
)
game (
\tname 13thdisciple
\tdescription "13th Disciple, The (DOS/Fanmade)[v1.01][!]"
\tyear 2005
\tsourcefile agi
\trom ( name LOGDIR size 615 crc 0c186a8c md5 58e3ec1b9ac1a79901c472aaa59db832 )
\trom ( name OBJECT size 128 crc 27e82f4f )
)"""

DOS_DAT = """DOSCenter (
\tName:The DOS Collection .dat
\tAuthor: hargle
)
game (
\tname "10th Frame (1987)(Access Software, Inc.) [Sports][!].zip"
\tfile ( name BOWL.EXE size 167580 date 1987/04/20 16:54:10 crc EDB3AE2E )
)"""

def test_parse_xml():
    header, games = parse_dat(XML_DAT)
    assert header["name"] == "Sony - PlayStation"
    assert games[0]["title"] == "Example Game"
    assert ("md5", "0123456789abcdef0123456789abcdef") in games[0]["hashes"]
    assert len(games[0]["hashes"]) == 3

def test_parse_clrmamepro():
    header, games = parse_dat(CM_DAT)
    assert header["name"] == "ScummVM AGI"
    g = games[0]
    assert g["external"] == "13thdisciple" and g["sourcefile"] == "agi" and g["year"] == 2005
    assert ("crc", "0c186a8c") in g["hashes"] and len(g["hashes"]) == 3

def test_parse_doscenter():
    _, games = parse_dat(DOS_DAT)
    # Unquoted date with a space must not break hash extraction
    assert games[0]["hashes"] == [("crc", "edb3ae2e")]

def test_parse_truncated_xml():
    truncated = XML_DAT.rsplit("</datafile>", 1)[0]
    _, games = parse_dat(truncated)
    assert games[0]["title"] == "Example Game"

def test_detect_system():
    cases = {
        "Sony - PlayStation": "ps1",
        "Sony - PlayStation 2": "ps2",
        "Sony - PlayStation 3": None,
        "Sony - PlayStation - BIOS Images": None,
        "Nintendo - Super Nintendo Entertainment System": "snes",
        "Nintendo - Nintendo Entertainment System (Headered)": "nes",
        "Sega - Mega CD & Sega CD": "segacd",
        "NEC - PC Engine CD & TurboGrafx CD": "pcenginecd",
        "NEC - PC-98 series": "pc98",
        "Commodore - Amiga CD32": "amigacd32",
        "Commodore - Amiga CDTV": None,
        "Philips - CD-i": "cdi",
        "Atari - Jaguar CD Interactive Multimedia System": None,
        "progetto-SNAPS - Cabinets": None,
        "The DOS Collection .dat": "dos",
        "ScummVM AGI": "scummvm",
        "Super Mario World Hacks - All": "romhacks",
        "Fujitsu - FM-Towns": "fmtowns",
    }
    for name, want in cases.items():
        assert detect_system([name]) == want, name

def test_import_dats_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    dats = tmp_path / "dats"; dats.mkdir()
    with zipfile.ZipFile(dats / "Sony - PlayStation (2022).zip", "w") as z:
        z.writestr("Sony - PlayStation - Datfile.dat", XML_DAT)
    (dats / "unknown.dat").write_text("clrmamepro ( name \"Weird Emulator\" )\ngame ( name foo rom ( crc 12345678 ) )")
    # ScummVM merge target must pre-exist
    db = connect()
    with db:
        db.execute("""INSERT INTO items(id,title,system,catalog_source,external_id)
          VALUES('scummvm-scummvm-x','13th Disciple','scummvm','scummvm','agi:13thdisciple')""")
    with zipfile.ZipFile(dats / "ScummVM AGI (v1.60).zip", "w") as z:
        z.writestr("ScummVM - AGI Engine Games.dat", CM_DAT)

    r = import_dats(dats)
    assert r["files"] == 3 and r["dats"] == 3
    assert r["items"] == 1 and r["scummvm_matched"] == 1
    assert len(r["skipped"]) == 1 and r["skipped"][0]["reason"] == "unrecognized system"
    ps1 = db.execute("SELECT * FROM items WHERE system='ps1'").fetchone()
    assert ps1["catalog_source"] == "redump" and ps1["wanted"] == 0
    assert ps1["external_id"] == "ps1/Example (USA)"
    merged = db.execute("SELECT COUNT(*) c FROM file_hashes WHERE item_id='scummvm-scummvm-x'").fetchone()["c"]
    assert merged == 3
    # Re-import is idempotent
    r2 = import_dats(dats)
    assert r2["items"] == 1
    assert db.execute("SELECT COUNT(*) c FROM items WHERE system='ps1'").fetchone()["c"] == 1
