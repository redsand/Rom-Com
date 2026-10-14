"""Will an arcade set run: the dat's driver status, plus everything the set needs on disk."""
import zipfile

from romcom.db import connect
from romcom import arcadehealth, mameset

DAT = """<?xml version="1.0"?><datafile>
<machine name="good" sourcefile="capcom/cps2.cpp">
<rom name="g.bin" size="16" crc="aaaa0001" sha1="1111111111111111111111111111111111111111"/>
<rom name="never.bin" size="16" status="nodump"/>
<driver status="good"/>
</machine>
<machine name="rough" sourcefile="capcom/cps2.cpp">
<rom name="r.bin" size="16" crc="aaaa0002"/>
<driver status="imperfect"/>
</machine>
<machine name="quiet" sourcefile="pacman.c">
<rom name="q.bin" size="16" crc="aaaa0003"/>
<driver status="good" color="good" sound="imperfect" graphic="good"/>
</machine>
<machine name="wip" sourcefile="misc/wip.cpp">
<rom name="w.bin" size="16" crc="aaaa0004"/>
<driver status="preliminary"/>
</machine>
<machine name="nochip" sourcefile="capcom/cps2.cpp">
<rom name="gone.bin" size="16" crc="ffff0000"/>
<driver status="good"/>
</machine>
<machine name="olddat">
<rom name="o.bin" size="16" crc="aaaa0005"/>
</machine>
<machine name="needsdev" sourcefile="namco/galaga.cpp">
<rom name="n.bin" size="16" crc="aaaa0006"/>
<device_ref name="namco54"/>
<driver status="good"/>
</machine>
<machine name="namco54" isdevice="yes" runnable="no">
<rom name="54xx.bin" size="16" crc="ffff0054"/>
<device_ref name="loopdev"/>
</machine>
<machine name="loopdev" isdevice="yes" runnable="no">
<device_ref name="namco54"/>
</machine>
<machine name="mslug" romof="neogeo" sourcefile="neogeo/neogeo.cpp">
<rom name="m.bin" size="16" crc="aaaa0007"/>
<driver status="good"/>
</machine>
<machine name="kinst" sourcefile="midway/kinst.cpp">
<rom name="k.bin" size="16" crc="aaaa0008"/>
<disk name="kinst" sha1="9999999999999999999999999999999999999999"/>
<driver status="good"/>
</machine>
<machine name="kinstc" cloneof="kinst" romof="kinst" sourcefile="midway/kinst.cpp">
<rom name="kc.bin" size="16" crc="aaaa0009"/>
<driver status="good"/>
</machine>
</datafile>"""

CHIPS = ["aaaa0001", "aaaa0002", "aaaa0003", "aaaa0004", "aaaa0005", "aaaa0006",
         "aaaa0007", "aaaa0008", "aaaa0009"]


def _chd(path, sha1_hex, version=5):
    """A CHD header carrying `sha1_hex` where MAME keeps it for that header version."""
    at = {3: 80, 4: 48, 5: 84}[version]
    head = bytearray(124)
    head[:8] = b"MComprHD"
    head[12:16] = version.to_bytes(4, "big")
    head[at:at + 20] = bytes.fromhex(sha1_hex)
    path.write_bytes(bytes(head) + b"payload")


def _env(monkeypatch, tmp_path, dat_text=DAT):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "h.db"))
    from romcom.config import invalidate
    invalidate()
    dat = tmp_path / "arcade.dat"
    dat.write_text(dat_text, encoding="utf-8")
    monkeypatch.setattr(mameset, "dat_path", lambda profile="mame": dat)
    monkeypatch.setattr(mameset, "dat_paths", lambda profile="mame": [dat])
    flat = tmp_path / "flat"
    flat.mkdir()
    db = connect()
    with db:
        for crc in CHIPS:
            (flat / crc).write_bytes(b"x" * 16)
            db.execute("INSERT INTO files(path,bytes,crc32) VALUES(?,?,?)",
                       (str(flat / crc), 16, crc))
    return db, flat


def _add_file(db, path):
    with db:
        db.execute("INSERT INTO files(path,bytes) VALUES(?,?)", (str(path), path.stat().st_size))


def test_the_dat_driver_status_decides_a_complete_set(monkeypatch, tmp_path):
    """All chips present is not the same as runs: a preliminary driver boots to nothing,
    an imperfect one plays with flaws. MAME 2003-Plus says which part is rough."""
    db, _ = _env(monkeypatch, tmp_path)
    res = arcadehealth.evaluate("mame", db)
    assert res["good"]["status"] == "works"
    assert "never dumped" in res["good"]["reason"]          # nodump is best available, not broken
    assert res["rough"]["status"] == "partial"
    assert (res["quiet"]["status"], res["quiet"]["reason"]) == ("partial", "sound imperfect")
    assert res["wip"]["status"] == "broken" and "preliminary" in res["wip"]["reason"]
    assert res["olddat"]["status"] == "unknown"
    assert res["good"]["family"] == "capcom/cps2.cpp"


def test_missing_parts_break_a_set_and_say_which(monkeypatch, tmp_path):
    db, _ = _env(monkeypatch, tmp_path)
    res = arcadehealth.evaluate("mame", db)
    assert res["nochip"] == res["nochip"] | {"status": "broken", "reason": "missing 1 rom"}
    assert res["needsdev"]["status"] == "broken"
    assert "needs device namco54" in res["needsdev"]["reason"]
    assert res["mslug"]["status"] == "broken"
    assert "needs bios neogeo" in res["mslug"]["reason"]
    assert res["kinst"]["status"] == "broken" and "needs chd kinst" in res["kinst"]["reason"]
    assert "namco54" not in res and "loopdev" not in res     # devices are not games
    # stored, for the planner and the UI
    assert db.execute("SELECT status FROM arcade_health WHERE core='mame' AND setname='wip'"
                      ).fetchone()[0] == "broken"


def test_a_chd_is_matched_by_the_sha1_in_its_header(monkeypatch, tmp_path):
    """The file's own sha1 is not the number a dat names; the CHD header carries it."""
    db, flat = _env(monkeypatch, tmp_path)
    wrong = flat / "elsewhere" / "kinst.chd"
    wrong.parent.mkdir()
    _chd(wrong, "8" * 40)
    _add_file(db, wrong)
    assert arcadehealth.evaluate("mame", db)["kinst"]["status"] == "broken"
    right = flat / "kinst" / "kinst.chd"
    right.parent.mkdir()
    _chd(right, "9" * 40, version=4)
    _add_file(db, right)
    assert arcadehealth.evaluate("mame", db)["kinst"]["status"] == "works"


def test_the_owners_verdict_beats_the_dat(monkeypatch, tmp_path):
    db, _ = _env(monkeypatch, tmp_path)
    arcadehealth.evaluate("mame", db)
    arcadehealth.set_verdict("wip", "works", db=db)                   # every core
    arcadehealth.set_verdict("wip", "partial", core="mame", db=db)    # this core: wins
    arcadehealth.set_verdict("good", "broken", core="fbneo", db=db)   # another core only
    st = arcadehealth.statuses("mame", db)
    assert st["wip"][0] == "partial" and st["good"][0] == "works"
    arcadehealth.set_verdict("wip", None, core="mame", db=db)
    assert arcadehealth.statuses("mame", db)["wip"][0] == "works"


def test_the_gate_levels(monkeypatch, tmp_path):
    db, _ = _env(monkeypatch, tmp_path)
    arcadehealth.evaluate("mame", db)
    sets = ["good", "rough", "wip", "olddat", "neverjudged"]
    kept, held = arcadehealth.gate("mame", sets, "no_broken", db)
    assert kept == ["good", "rough", "olddat", "neverjudged"]   # unjudged is no reason to drop
    assert set(held) == {"wip"}
    assert arcadehealth.gate("mame", sets, "works_only", db)[0] == ["good", "neverjudged"]
    assert arcadehealth.gate("mame", sets, "all", db) == (sets, {})
    assert arcadehealth.gate("mame", sets, "nonsense", db)[0] == kept   # default, not open


def test_device_refs_that_loop_do_not_hang_the_builder(monkeypatch, tmp_path):
    db, _ = _env(monkeypatch, tmp_path)
    roms = mameset.set_roms(["needsdev"])
    assert set(roms) == {"needsdev", "namco54"}


def test_the_build_puts_the_chd_beside_the_zip(monkeypatch, tmp_path):
    """<set>/<disk>.chd next to <set>.zip is where MAME and RetroArch's MAME core look."""
    db, flat = _env(monkeypatch, tmp_path)
    src = flat / "kinst.chd"
    _chd(src, "9" * 40)
    _add_file(db, src)
    out = tmp_path / "card"
    r = mameset.build(["kinst"], out, db=db, zipped=True)
    assert r["sets"]["kinst"]["complete"]
    assert (out / "kinst" / "kinst.chd").read_bytes() == src.read_bytes()
    with zipfile.ZipFile(out / "kinst.zip") as z:
        assert z.namelist() == ["k.bin"]
    src.unlink()
    r = mameset.build(["kinst"], tmp_path / "card2", db=db, zipped=True)
    assert not r["sets"]["kinst"]["complete"]
    assert "kinst.chd" in r["sets"]["kinst"]["missing"]


def test_export_holds_broken_sets_back_and_a_working_clone_takes_the_slot(
        monkeypatch, tmp_path):
    """The point of the gate: a card is not spent on games that will not start. It runs
    before 'one copy per game', so kinst's working clone survives its broken parent."""
    from romcom import organizer
    db, _ = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(organizer, "_free_bytes", lambda d: 10 ** 12)
    monkeypatch.setattr(organizer, "_reserve_bytes", lambda d: 0)
    with db:
        for i, s in enumerate(["good", "wip", "kinst", "kinstc"]):
            db.execute("INSERT INTO items(id,title,system,external_id) VALUES(?,?,?,?)",
                       (f"i{i}", s, "arcade", f"arcade/{s}"))
            db.execute("UPDATE files SET matched_item_id=? WHERE crc32=?",
                       (f"i{i}", {"good": "aaaa0001", "wip": "aaaa0004", "kinst": "aaaa0008",
                                  "kinstc": "aaaa0009"}[s]))
    card = tmp_path / "sd"
    r = organizer.organize(card, systems=["arcade"], distinct_games=True)
    built = sorted(p.stem for p in (card / "arcade").glob("*.zip"))
    # kinstc won its family's one slot and shares no chip with kinst, so the broken
    # parent stays off the card (CPS3's sfiii3n once dragged a CHD-less sfiii3 along);
    # wip — nothing in its family runs — stays off too.
    assert built == ["good", "kinstc"]
    assert not r["clones_pruned"]
    held = r["arcade_held_back"]["mame"]
    assert held["count"] == 2
    assert held["by_reason"] == {"needs chd": 1, "driver preliminary — the core cannot "
                                                 "really run it": 1}
    # the planner leaves the same sets out of the arcade figure
    from romcom.organizer import export_systems
    arc = lambda **kw: next(r for r in export_systems(**kw) if r["system"] == "arcade")
    assert arc(arcade_core="mame", arcade_status="no_broken")["files"] == 2
    assert arc(arcade_core="mame", arcade_status="all")["files"] == 4


def test_a_clone_that_lists_its_merged_roms_needs_no_parent_zip(monkeypatch, tmp_path):
    """Every dat here lists a clone's shared roms itself (merge=), so the zip built from the
    clone carries them: the parent is not a dependency — but a bios still is."""
    dat = tmp_path / "d.dat"
    dat.write_text("""<datafile>
<machine name="p">
<rom name="a" crc="1"/>
</machine>
<machine name="c" cloneof="p" romof="p">
<rom name="a" merge="a" crc="1"/>
</machine>
<machine name="s" cloneof="p" romof="p">
<rom name="b" crc="2"/>
</machine>
<machine name="bios" isbios="yes">
<rom name="x" crc="3"/>
</machine>
<machine name="g" romof="bios">
<rom name="g" crc="4"/>
</machine>
<machine name="gc" cloneof="g" romof="g">
<rom name="g" merge="g" crc="4"/>
</machine>
</datafile>""", encoding="utf-8")
    machines = arcadehealth.parse(dat)
    assert arcadehealth.needed_parents(machines, "c") == []
    assert arcadehealth.needed_parents(machines, "s") == []          # shares nothing: alone
    assert arcadehealth.needed_parents(machines, "gc") == ["bios"]   # up the chain to the bios


def test_every_core_gets_its_bios_parents_not_just_mame(monkeypatch, tmp_path):
    """FBNeo's mslug lists no neogeo chips: an FBNeo card without neogeo.zip boots nothing
    on that board. The export used to build parents for the mame core only."""
    from romcom import organizer
    fb = tmp_path / "fbneo.dat"
    fb.write_text("""<datafile>
<game name="neogeo" isbios="yes">
<rom name="sp-s2.sp1" crc="aaaa0001"/>
</game>
<game name="mslug" romof="neogeo">
<rom name="201-p1.p1" crc="aaaa0002"/>
<driver status="good"/>
</game>
</datafile>""", encoding="utf-8")
    db, _ = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(mameset, "dat_path",
                        lambda profile="mame": fb if profile == "fbneo" else None)
    monkeypatch.setattr(mameset, "dat_paths",
                        lambda profile="mame": [fb] if profile == "fbneo" else [])
    monkeypatch.setattr(organizer, "_free_bytes", lambda d: 10 ** 12)
    monkeypatch.setattr(organizer, "_reserve_bytes", lambda d: 0)
    with db:
        db.execute("INSERT INTO items(id,title,system,external_id) "
                   "VALUES('m','Metal Slug','arcade','arcade/mslug')")
        db.execute("UPDATE files SET matched_item_id='m' WHERE crc32='aaaa0002'")
    organizer.organize(tmp_path / "sd", systems=["arcade"], arcade_core="fbneo")
    card = tmp_path / "sd" / "fbneo"           # ES-DE launches it with FBNeo
    assert sorted(p.stem for p in card.glob("*.zip")) == ["mslug", "neogeo"]


def test_mame2003_bios_comes_from_the_fallback_dat_in_an_export(monkeypatch, tmp_path):
    """The bios MAME 2003-Plus's own dat references but never defines is built from the
    0.78 dat beside it — the health verdict counts on that, so the export must do it."""
    from romcom import organizer
    own = tmp_path / "mame2003-plus.xml"
    own.write_text("""<datafile>
<game name="cvsgame" romof="cvs">
<rom name="g" crc="aaaa0002"/>
<driver status="good"/>
</game>
</datafile>""", encoding="utf-8")
    old = tmp_path / "MAME 0.78.dat"
    old.write_text("""<datafile>
<game name="cvs" isbios="yes">
<rom name="b" crc="aaaa0001"/>
</game>
</datafile>""", encoding="utf-8")
    db, _ = _env(monkeypatch, tmp_path)
    monkeypatch.setattr(mameset, "dat_path",
                        lambda profile="mame": own if profile == "mame2003" else None)
    monkeypatch.setattr(mameset, "dat_paths",
                        lambda profile="mame": [own, old] if profile == "mame2003" else [])
    monkeypatch.setattr(organizer, "_free_bytes", lambda d: 10 ** 12)
    monkeypatch.setattr(organizer, "_reserve_bytes", lambda d: 0)
    with db:
        db.execute("INSERT INTO items(id,title,system,external_id) "
                   "VALUES('c','CVS game','arcade','arcade/cvsgame')")
        db.execute("UPDATE files SET matched_item_id='c' WHERE crc32='aaaa0002'")
    r = organizer.organize(tmp_path / "sd", systems=["arcade"], arcade_core="mame2003")
    assert arcadehealth.statuses("mame2003", db)["cvsgame"][0] == "works"
    card = tmp_path / "sd" / "mame"            # ES-DE: MAME 2003-Plus
    assert sorted(p.stem for p in card.glob("*.zip")) == ["cvs", "cvsgame"]
    assert not r["arcade_bios_unavailable"]


def _owned(db, sets):
    with db:
        for s in sets:
            db.execute("INSERT INTO items(id,title,system,external_id,status) "
                       "VALUES(?,?,?,?,?)", (f"it-{s}", s, "arcade", f"arcade/{s}", "FOUND"))


def test_metrics_count_owned_sets_by_board_and_rank_the_fixes(monkeypatch, tmp_path):
    db, _ = _env(monkeypatch, tmp_path)
    _owned(db, ["good", "rough", "wip", "nochip", "needsdev", "mslug", "kinst"])
    arcadehealth.evaluate("mame", db)
    m = arcadehealth.metrics("mame", db)
    assert m["owned"] == 7
    assert m["counts"] | {} == {"works": 1, "partial": 1, "broken": 5, "unknown": 0,
                                "unjudged": 0}
    cps2 = next(b for b in m["boards"] if b["board"] == "cps2")
    assert (cps2["total"], cps2["works"], cps2["partial"], cps2["broken"]) == (3, 1, 1, 1)
    fixes = {(f["kind"], f["name"]): f["unlocks"] for f in m["fixes"]}
    assert fixes == {("device", "namco54"): 1, ("bios", "neogeo"): 1, ("chd", "kinst"): 1}
    assert (m["roms_missing"], m["chd_sets"], m["driver_broken"]) == (1, 1, 1)


def test_repair_previews_then_arms_the_downloads(monkeypatch, tmp_path):
    """Nothing is queued by a preview; applying arms the dependency and the game short of
    its own chips, back to MISSING so the acquirer — which only tries CATALOGED/MISSING —
    actually picks them up."""
    db, _ = _env(monkeypatch, tmp_path)
    _owned(db, ["nochip", "needsdev", "wip"])
    with db:
        db.execute("INSERT INTO items(id,title,system,external_id,status,is_device) "
                   "VALUES('it-namco54','Namco 54xx','arcade','arcade/namco54','FOUND',1)")
    arcadehealth.evaluate("mame", db)
    r = arcadehealth.repair("mame", db=db)
    assert (r["to_arm"], r["applied"]) == (2, False)
    assert db.execute("SELECT COUNT(*) FROM items WHERE wanted=1 AND authorized=1"
                      ).fetchone()[0] == 0
    r = arcadehealth.repair("mame", apply=True, db=db)
    armed = {row[0]: row[1] for row in db.execute(
        "SELECT id, status FROM items WHERE wanted=1 AND authorized=1")}
    assert armed == {"it-nochip": "MISSING", "it-namco54": "MISSING"}   # wip: no download fixes it
    assert arcadehealth.repair("mame", db=db)["to_arm"] == 0


def test_library_filters_and_sorts_by_health_on_the_chosen_core(monkeypatch, tmp_path):
    from romcom.web import create_app
    db, _ = _env(monkeypatch, tmp_path)
    _owned(db, ["good", "rough", "wip"])
    arcadehealth.evaluate("mame", db)
    c = create_app().test_client()
    got = lambda q: [(r["title"], r["health"]) for r in
                     c.get("/api/items?limit=50&system=arcade&" + q).get_json()["items"]]
    assert got("sort=health") == [("good", "works"), ("rough", "partial"), ("wip", "broken")]
    assert got("health=broken") == [("wip", "broken")]
    assert got("board=cps2&sort=title") == [("good", "works"), ("rough", "partial")]
    assert c.post("/api/arcade/verdict", json={"setname": "wip", "core": "mame",
                                               "verdict": "works"}).status_code == 200
    assert got("health=broken") == []
    assert got("health=mine") == [("wip", "works")]
    # fbneo was never judged, and the verdict above was for mame only
    assert got("core=fbneo&health=unjudged&sort=title") == [
        ("good", None), ("rough", None), ("wip", None)]
    assert c.get("/api/arcade/health?core=mame").get_json()["counts"]["works"] == 2


def test_repair_on_another_core_arms_the_mame_set_that_carries_the_missing_chip(
        monkeypatch, tmp_path):
    """The catalog fetches MAME sets only. FBNeo's pgm wants bios.u42, which MAME files
    under thegladpcb: arming that one PCB set fixes every pgm game, where re-fetching the
    games themselves would change nothing. A chip no MAME set holds stays a mismatch."""
    db, _ = _env(monkeypatch, tmp_path)
    fb = tmp_path / "fbneo.dat"
    fb.write_text("""<?xml version="1.0"?><datafile>
<game name="kov" sourcefile="pgm/d_pgm.cpp">
<rom name="k.bin" size="16" crc="aaaa0001"/>
<rom name="bios.u42" size="16" crc="b10542aa"/>
<driver status="good"/>
</game>
<game name="drgw2" sourcefile="pgm/d_pgm.cpp">
<rom name="d.bin" size="16" crc="aaaa0002"/>
<rom name="bios.u42" size="16" crc="b10542aa"/>
<driver status="good"/>
</game>
<game name="odd" sourcefile="misc/odd.cpp">
<rom name="o.bin" size="16" crc="0dd00dd0"/>
<driver status="good"/>
</game>
</datafile>""", encoding="utf-8")
    mame = tmp_path / "mame.dat"
    mame.write_text("""<?xml version="1.0"?><datafile>
<machine name="kov">
<rom name="k.bin" size="16" crc="aaaa0001"/>
</machine>
<machine name="drgw2">
<rom name="d.bin" size="16" crc="aaaa0002"/>
</machine>
<machine name="odd">
<rom name="o.bin" size="16" crc="aaaa0003"/>
</machine>
<machine name="thegladpcb">
<rom name="bios.u42" size="16" crc="b10542aa"/>
<rom name="big1" size="16" crc="12345678"/>
</machine>
<machine name="thegladpcba">
<rom name="bios.u42" size="16" crc="b10542aa"/>
</machine>
</datafile>""", encoding="utf-8")
    monkeypatch.setattr(mameset, "dat_path",
                        lambda profile="mame": fb if profile == "fbneo" else mame)
    _owned(db, ["kov", "drgw2", "odd"])
    with db:
        for s in ("thegladpcb", "thegladpcba"):
            db.execute("INSERT INTO items(id,title,system,external_id,status) "
                       "VALUES(?,?,'arcade',?,'CATALOGED')", (f"it-{s}", s, f"arcade/{s}"))
    arcadehealth.evaluate("fbneo", db)
    r = arcadehealth.repair("fbneo", db=db)
    assert r["carriers"] == ["thegladpcba"]                 # the smaller of the two carriers
    assert (r["carried_sets"], r["romset_mismatch"], r["to_arm"]) == (2, 1, 1)
    arcadehealth.repair("fbneo", apply=True, db=db)
    assert db.execute("SELECT wanted, authorized, status FROM items WHERE id='it-thegladpcba'"
                      ).fetchone()[:] == (1, 1, "CATALOGED")
