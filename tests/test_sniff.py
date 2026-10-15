"""A disc's own stamp vetoes a filename guess: a PC game or a PS1 bin/cue that shares a
PS3 title's name must not become that PS3 game (it did — 11 of the library's 13 'PS3
games' were PC DVDs, Xbox 360 folders and other consoles' discs)."""
import zipfile
from pathlib import Path

from romcom import sniff
from romcom.db import connect
from romcom.scanner import scan


def _iso(path, at, stamp):
    head = bytearray(sniff.HEAD)
    head[at:at + len(stamp)] = stamp
    path.write_bytes(bytes(head))
    return path


def _ps_iso(path, sysid):
    head = bytearray(sniff.HEAD)
    head[0x8001:0x8006] = b"CD001"
    head[0x8008:0x8008 + len(sysid)] = sysid
    path.write_bytes(bytes(head))
    return path


def _zip(path, names):
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, b"x")
    return path


def test_disc_stamps_name_their_console(tmp_path):
    ps3 = sniff.evidence(_iso(tmp_path / "a.iso", 0x800, b"PlayStation3"))
    assert sniff.consistent(ps3, "ps3") is True and sniff.consistent(ps3, "ps2") is False
    wii = sniff.evidence(_iso(tmp_path / "b.iso", 0x18, bytes.fromhex("5D1C9EA3")))
    assert sniff.consistent(wii, "wii") is True and sniff.consistent(wii, "ps3") is False
    ps2 = sniff.evidence(_ps_iso(tmp_path / "c.iso", b"PLAYSTATION"))
    assert sniff.consistent(ps2, "ps2") is True and sniff.consistent(ps2, "ps3") is False
    psp = sniff.evidence(_ps_iso(tmp_path / "d.iso", b"PSP GAME"))
    assert sniff.consistent(psp, "psp") is True
    blank = sniff.evidence(_iso(tmp_path / "e.iso", 0x8001, b"BEA01"))   # a plain UDF DVD
    assert sniff.consistent(blank, "ps3") is False


def test_archive_listings_say_what_the_disc_is(tmp_path):
    pc = sniff.evidence(_zip(tmp_path / "pc.zip", ["game/setup.exe", "game/data.cab"]))
    assert sniff.consistent(pc, "ps3") is False
    cd = sniff.evidence(_zip(tmp_path / "cd.zip", ["Ape Escape (USA).bin", "Ape Escape (USA).cue"]))
    assert sniff.consistent(cd, "ps1") is True and sniff.consistent(cd, "ps3") is False
    god = sniff.evidence(_zip(tmp_path / "x.zip", ["CoD4/415607E6/00007000/5E60", "CoD4/415607E6/00007000/5E60.data/Data0000"]))
    assert sniff.consistent(god, "ps3") is False
    dc = sniff.evidence(_zip(tmp_path / "dc.zip", ["disc.gdi", "track01.bin", "track03.bin"]))
    assert sniff.consistent(dc, "psp") is False and sniff.consistent(dc, "ps1") is False
    jb = sniff.evidence(_zip(tmp_path / "jb.zip", ["G/PS3_GAME/PARAM.SFO", "G/PS3_GAME/USRDIR/EBOOT.BIN"]))
    assert sniff.consistent(jb, "ps3") is True
    pbp = sniff.evidence(_zip(tmp_path / "k1.zip", ["K-1/EBOOT.PBP"]))
    assert sniff.consistent(pbp, "ps1") is True and sniff.consistent(pbp, "ps3") is False
    assert sniff.consistent(sniff.evidence(tmp_path / "nothing.chd"), "ps1") is None
    assert sniff.consistent(cd, "nes") is None          # cartridge systems aren't judged


def test_the_scanner_moves_a_vetoed_name_match_to_the_right_console(monkeypatch, tmp_path):
    monkeypatch.setattr(sniff, "STUB_BYTES", 0)     # the fixtures are toy-sized discs
    db = connect()
    with db:
        db.execute("INSERT INTO items(id,system,title,status) VALUES('p3','ps3','Ape Escape (Europe)','CATALOGED')")
        db.execute("INSERT INTO items(id,system,title,status) VALUES('p1','ps1','Ape Escape (USA)','CATALOGED')")
        db.execute("INSERT INTO items(id,system,title,status,wanted) VALUES('pc','ps3','Bejeweled 3 (Europe)','CATALOGED',1)")
    roms = tmp_path / "roms"
    roms.mkdir()
    _zip(roms / "Ape Escape.zip", ["Ape Escape (USA)/Ape Escape (USA).bin", "Ape Escape (USA)/Ape Escape (USA).cue"])
    # A file the old scanner had already matched by name: the rescan has to let go of it.
    pc = _zip(roms / "Bejeweled 3.zip", ["Bejeweled 3/Bejeweled3.exe"])
    st = pc.stat()
    with db:
        db.execute("INSERT INTO files(path,bytes,mtime,matched_item_id,match_method) VALUES(?,?,?,'pc','filename-exact')",
                   (str(pc), st.st_size, st.st_mtime))
        db.execute("UPDATE items SET status='FOUND',authorized=1 WHERE id='pc'")
    scan(roms, adopt=False)
    got = {r["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: r["matched_item_id"]
           for r in db.execute("SELECT path, matched_item_id FROM files")}
    assert got["Ape Escape.zip"] == "p1"
    assert got["Bejeweled 3.zip"] is None
    st = {r["id"]: (r["status"], r["wanted"]) for r in db.execute("SELECT id,status,wanted FROM items")}
    assert st["p1"][0] == "FOUND" and st["p3"][0] == "CATALOGED"
    assert st["pc"] == ("CATALOGED", 0)


def test_a_vetoed_disc_two_lands_on_disc_two_and_dreamcast_images_on_dreamcast(monkeypatch, tmp_path):
    monkeypatch.setattr(sniff, "STUB_BYTES", 0)     # the fixtures are toy-sized discs
    db = connect()
    with db:
        for iid, sysname, title in (("x", "psp", "Xenogears (Europe) (Unl)"),
                                    ("x1", "ps1", "Xenogears (USA) (Disc 1)"),
                                    ("x2", "ps1", "Xenogears (USA) (Disc 2)"),
                                    ("w", "wii", "MDK2 (USA) (WiiWare)"),
                                    ("d", "dreamcast", "MDK2 (USA)")):
            db.execute("INSERT INTO items(id,system,title,status) VALUES(?,?,?,'CATALOGED')",
                       (iid, sysname, title))
    roms = tmp_path / "roms"
    roms.mkdir()
    _zip(roms / "Xenogears (Japan) (Disc 2).zip", ["Xenogears (Disc 2).bin", "Xenogears (Disc 2).cue"])
    _zip(roms / "MDK2.zip", ["MDK2.gdi", "track01.bin", "track03.bin"])
    scan(roms, adopt=False)
    got = {r["path"].replace("\\", "/").rsplit("/", 1)[-1]: r["matched_item_id"]
           for r in db.execute("SELECT path, matched_item_id FROM files")}
    assert got == {"Xenogears (Japan) (Disc 2).zip": "x2", "MDK2.zip": "d"}


def test_a_website_result_must_be_the_same_game_and_fit_the_cartridge():
    """Real wrong fetches from the archive source: a PC game for an NES demo, a 28 MB PC
    adventure for an NES cart. Spelling and numbering differences still pass."""
    from romcom.indexer import plausible
    assert not plausible("Age of Darkness (World) (Demo 3)", "nes", "World Of Warcraft 3.85.rar", 7_800_000)
    assert not plausible("Black Dragon (Korea)", "nes", "The Black Dragon Case.zip", 28_000_000)
    assert plausible("Mega Man 2 (USA)", "nes", "Megaman 2.zip", 100_000)
    assert plausible("Street Fighter II - The World Warrior (USA)", "snes",
                     "Street Fighter 2 The World Warrior.zip", 2_000_000)
    assert plausible("Crazy Taxi 2 (Japan) (En,Ja)", "dreamcast", "Crazy Taxi 2 (USA).zip", 142_000_000)


def test_a_download_for_the_wrong_platform_is_refused(tmp_path):
    from romcom.verify import check_download
    iso = _zip(tmp_path / "Bratz - 4 Real.zip", ["bratz 4 real.iso"])
    ok, why = check_download(iso, "nds")
    assert not ok and "disc image" in why
    big = tmp_path / "Black Dragon Case.zip"
    with zipfile.ZipFile(big, "w", zipfile.ZIP_STORED) as z:
        z.writestr("game.nes", b"\0" * (5 << 20))
    ok, why = check_download(big, "nes")
    assert not ok and "cartridge" in why
    pc = _zip(tmp_path / "Call of Duty Black Ops.zip", ["main/iw_00.iwd", "BlackOps.exe"])
    assert check_download(pc, "ps3")[0] is False
    good = _zip(tmp_path / "Mega Man 2.zip", ["Mega Man 2 (USA).nes"])
    assert check_download(good, "nes")[0] is True


def test_a_disc_converts_once_and_later_cards_copy_it(monkeypatch, tmp_path):
    from romcom import esde
    monkeypatch.setenv("ROMCOM_CHD_CACHE", str(tmp_path / "cache"))
    from romcom.config import invalidate
    invalidate()
    src = tmp_path / "Crazy Taxi (USA).gdi"
    src.write_text("disc")
    runs = []
    def convert(src_, names, main, target, system):
        runs.append(target)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"CHD")
    monkeypatch.setattr(esde, "_convert_chd", convert)
    esde.make_chd(src, None, src.name, tmp_path / "card1" / "Crazy Taxi.chd", "dreamcast")
    esde.make_chd(src, None, src.name, tmp_path / "card2" / "Crazy Taxi.chd", "dreamcast")
    assert len(runs) == 1 and str(runs[0]).startswith(str(tmp_path / "cache"))
    assert (tmp_path / "card2" / "Crazy Taxi.chd").read_bytes() == b"CHD"


def test_a_scan_steps_over_partial_and_vanished_files(tmp_path, monkeypatch):
    """A live download folder holds .part files and files renamed mid-scan; neither may
    fail the scan (one vanished .chd.part failed the watcher's whole sweep)."""
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "929jbb02.chd.part").write_bytes(b"x")
    gone = roms / "gone.zip"
    gone.write_bytes(b"x")
    import romcom.scanner as sc
    real_digest = sc.digest_file
    monkeypatch.setattr(sc, "digest_file", lambda p: (_ for _ in ()).throw(FileNotFoundError(p))
                        if Path(p).name == "gone.zip" else real_digest(p))
    r = scan(roms, adopt=False)
    assert r["files"] == 0
