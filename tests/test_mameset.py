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
    monkeypatch.setattr(mameset, "dat_path", lambda profile="mame": dat)
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


def test_build_reports_per_set_progress(monkeypatch, tmp_path):
    """The export's job status must say where the (long, otherwise silent) set build is:
    one (i, total, setname) per set — device sets like namco54 included in the count."""
    db, tmp = _setup(monkeypatch, tmp_path)
    seen = []
    mameset.build(["galaga"], tmp / "out", db=db,
                   progress=lambda i, total, name: seen.append((i, total, name)))
    assert [s[2] for s in seen] == ["galaga", "namco54"]  # sorted; the device came along
    assert seen[0][1] == 2 and seen[1] == (1, 2, "namco54")  # i/total are the build's own scale


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


def test_fresh_sizes_the_full_rebuild_not_the_delta(monkeypatch, tmp_path):
    """A wipe clears the arcade folder before rebuilding it, so its sizing pass must
    count every rom again — fresh=True. Measuring the delta over the sets about to
    be deleted once sized a whole 70 GB rebuild as a few GB of missing chips, the
    fit check passed, and the export died at the free-space reserve mid-copy."""
    db, tmp = _setup(monkeypatch, tmp_path)
    mameset.build(["galaga"], tmp / "out", db=db)          # the old sets are all there
    delta = mameset.build(["galaga"], tmp / "out", db=db, dry_run=True)["bytes_needed"]
    fresh = mameset.build(["galaga"], tmp / "out", db=db, dry_run=True, fresh=True)["bytes_needed"]
    assert delta == 0                                        # nothing missing from the old sets
    assert fresh == 3 * 16                                   # galaga + namco54, every rom counted


def test_zipped_builds_one_zip_per_set(monkeypatch, tmp_path):
    """No emulator on a handheld scans a folder of loose chip files — every Android
    emulator and frontend reads one zip per game. The card export must therefore
    write <set>.zip with the canonical names inside, not <set>/<loose roms>."""
    import zipfile
    db, tmp = _setup(monkeypatch, tmp_path)
    r = mameset.build(["galaga"], tmp / "out", db=db, zipped=True)
    assert r["sets"]["galaga"]["complete"] is True
    assert not (tmp / "out" / "galaga").exists()             # no loose-chip folder
    z = tmp / "out" / "galaga.zip"
    assert z.exists()
    with zipfile.ZipFile(z) as f:
        assert sorted(f.namelist()) == ["gg1_1b.3p", "prom-2.5c"]   # its own chips only
        assert f.read("prom-2.5c") == b"x" * 16              # canonical name, real bytes
    # device sets come along as their own zips, the way MAME looks for them
    assert (tmp / "out" / "namco54.zip").exists()


def test_a_complete_zip_is_skipped_an_incomplete_one_is_rebuilt_whole(monkeypatch, tmp_path):
    """A zip is all-or-nothing: complete on the card means skipped (a top-up costs
    nothing), and anything short of complete is rewritten whole — a card can never
    hold a half-written set."""
    import zipfile
    db, tmp = _setup(monkeypatch, tmp_path)
    mameset.build(["galaga"], tmp / "out", db=db, zipped=True)
    again = mameset.build(["galaga"], tmp / "out", db=db, zipped=True, dry_run=True)
    assert again["bytes_needed"] == 0                          # nothing to do
    # A truncated zip — one entry removed — is not trusted and not patched:
    z = tmp / "out" / "galaga.zip"
    with zipfile.ZipFile(z) as f:
        names = [n for n in f.namelist() if n != "prom-2.5c"]
        entries = [(n, f.read(n)) for n in names]
    with zipfile.ZipFile(z, "w") as f:
        for n, data in entries:
            f.writestr(n, data)
    r = mameset.build(["galaga"], tmp / "out", db=db, zipped=True)
    assert r["copied"] == 2                                    # the whole set rewritten
    with zipfile.ZipFile(z) as f:
        assert "prom-2.5c" in f.namelist()                    # complete again


def test_a_duplicate_chip_name_lands_in_the_zip_once(monkeypatch, tmp_path):
    """A machine can list the same chip name twice — an alternate dump with a different
    hash. Both used to be written, and a core reading the first entry could load the
    wrong chip. One entry per name, the first alternate the dump has."""
    import zipfile
    db, tmp = _setup(monkeypatch, tmp_path)
    dat = tmp / "dup.dat"
    dat.write_text(DAT.replace(
        '<rom name="prom-2.5c" size="256" crc="bbbb2222" sha1="2222222222222222222222222222222222222222"/>',
        '<rom name="prom-2.5c" size="256" crc="eeee5555" sha1="5555555555555555555555555555555555555555"/>\n'
        '<rom name="prom-2.5c" size="256" crc="bbbb2222" sha1="2222222222222222222222222222222222222222"/>'),
        encoding="utf-8")
    monkeypatch.setattr(mameset, "dat_path", lambda profile="mame": dat)
    r = mameset.build(["galaga"], tmp / "out", db=db, zipped=True)
    assert r["sets"]["galaga"]["complete"] is True   # the second alternate is on disk
    with zipfile.ZipFile(tmp / "out" / "galaga.zip") as z:
        assert sorted(z.namelist()) == ["gg1_1b.3p", "prom-2.5c"]  # once, not twice
        assert z.read("prom-2.5c") == b"x" * 16


def test_no_dat_degrades_instead_of_failing(monkeypatch, tmp_path):
    db, tmp = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mameset, "dat_path", lambda profile="mame": None)
    r = mameset.build(["galaga"], tmp / "out", db=db)
    assert r["copied"] == 0 and "no romset dat" in r["error"]


def test_dat_path_finds_each_core_profile(monkeypatch, tmp_path):
    """Each core speaks its own romset version, so each profile resolves its own dat:
    mame filters its folder for the arcade dat, the others take the one dat their
    project ships."""
    import shutil
    monkeypatch.setattr(mameset, "ROOT", tmp_path)
    (tmp_path / "DAT/MAME").mkdir(parents=True)
    (tmp_path / "DAT/MAME/full.dat").write_text("x", encoding="utf-8")
    (tmp_path / "DAT/MAME/MAME 0.289 (arcade).dat").write_text("x", encoding="utf-8")
    (tmp_path / "DAT/FBNeo").mkdir()
    (tmp_path / "DAT/FBNeo/FinalBurn Neo.dat").write_text("x", encoding="utf-8")
    (tmp_path / "DAT/MAME2003-Plus").mkdir()
    (tmp_path / "DAT/MAME2003-Plus/mame2003-plus.xml").write_text("x", encoding="utf-8")
    assert mameset.dat_path("mame").name.endswith("(arcade).dat")   # not the full dat
    assert mameset.dat_path("fbneo").name == "FinalBurn Neo.dat"
    assert mameset.dat_path("mame2003").name == "mame2003-plus.xml"
    assert mameset.dat_path("nope") is None                         # unknown core
    shutil.rmtree(tmp_path / "DAT/FBNeo")
    assert mameset.dat_path("fbneo") is None                        # dat not installed


FBNEO_DAT = """<?xml version="1.0"?><datafile>
<game name="mslug" romof="neogeo">
<rom name="m1.bin" size="16" crc="aaaa1111"/>
</game>
</datafile>"""


def test_a_crc_only_logiqx_dat_builds(monkeypatch, tmp_path):
    """The FBNeo dat carries crc but no sha1 — the hash index must find roms by crc alone."""
    db, tmp = _setup(monkeypatch, tmp_path)
    fdat = tmp / "fbneo.dat"
    fdat.write_text(FBNEO_DAT, encoding="utf-8")
    monkeypatch.setattr(mameset, "dat_path", lambda profile="mame": fdat)
    r = mameset.build(["mslug"], tmp / "out", db=db, zipped=True, profile="fbneo")
    assert r["sets"]["mslug"]["complete"] is True
    import zipfile
    with zipfile.ZipFile(tmp / "out" / "mslug.zip") as z:
        assert z.namelist() == ["m1.bin"]


def test_romof_gaps_names_the_bios_parents_a_dat_never_defines(monkeypatch, tmp_path):
    """A core dat's games say romof="neogeo" without defining neogeo — the core still
    looks for neogeo.zip, so a romset build has to know that parent must come from
    somewhere else. A romof target the dat itself defines is not a gap."""
    db, tmp = _setup(monkeypatch, tmp_path)
    fdat = tmp / "fbneo.dat"
    fdat.write_text(FBNEO_DAT, encoding="utf-8")
    assert mameset.romof_gaps(fdat) == {"neogeo": 1}
    defined = tmp / "defined.dat"
    defined.write_text(FBNEO_DAT + '<game name="neogeo">\n'
                       '<rom name="bios.bin" size="16" crc="bbbb2222"/>\n</game>\n',
                       encoding="utf-8")
    assert mameset.romof_gaps(defined) == {}


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


def test_best_available_counts_as_runnable(monkeypatch, tmp_path):
    """MAME says "best available" when a set is as complete as anyone's copy can be — the
    only roms absent were never dumped — and the game runs. Filing those with the genuinely
    broken sets understated this library by 787 playable games."""
    db, tmp = _setup(monkeypatch, tmp_path)
    import subprocess
    monkeypatch.setattr(mameset, "dat_path", lambda: tmp / "arcade.dat")
    monkeypatch.setattr(mameset, "build", lambda *a, **k: {"sets": {}, "copied": 0})
    import romcom.player as pl
    monkeypatch.setattr(pl, "rompath", lambda: str(tmp))
    exe = tmp / "mame.exe"; exe.write_bytes(b"x")
    monkeypatch.setattr(pl, "emulator_options", lambda s: [("mame", f'"{exe}" {{set}}')])

    class R:
        stdout = ("romset galaga is good\n"
                  "romset 005 is best available\n"
                  "romset wrecked is bad\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    r = mameset.prove(setnames=["galaga", "005", "wrecked"], db=db)
    assert r["good"] == ["galaga"]
    assert r["best_available"] == ["005"]
    assert list(r["bad"]) == ["wrecked"]
    assert r["runnable"] == ["005", "galaga"]


def test_the_emulators_verdict_replaces_our_arithmetic(monkeypatch, tmp_path):
    """assemblable() is a calculation over the dat; prove() is the emulator's answer. They
    disagreed on 230 of 6,041 sets, and where they disagree the emulator is right — it is the
    thing that has to load the game."""
    db, tmp = _setup(monkeypatch, tmp_path)
    with db:
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id,playable)"
                   " VALUES('a','Galaga','arcade','VERIFIED','antopisa','arcade/galaga',0)")
        db.execute("INSERT INTO items(id,title,system,status,catalog_source,external_id,playable)"
                   " VALUES('b','Wrecked','arcade','VERIFIED','antopisa','arcade/wrecked',1)")
    mameset.record_proof({"runnable": ["galaga"], "bad": {"wrecked": "romset wrecked is bad"}}, db=db)
    got = {r["id"]: r["playable"] for r in db.execute("SELECT id,playable FROM items")}
    assert got == {"a": 1, "b": 0}
