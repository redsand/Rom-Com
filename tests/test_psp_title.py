"""A PSP disc names itself in PARAM.SFO; an adopted file goes by that name."""
from pathlib import Path

from romcom import sniff
from romcom.scanner import local_title


def _sfo(entries):
    keys = b"".join(k.encode() + b"\x00" for k, _ in entries)
    vals, index, koff = b"", b"", 0
    for k, v in entries:
        raw = v.encode() + b"\x00"
        raw += b"\x00" * (-len(raw) % 4)
        index += (koff.to_bytes(2, "little") + (0x0204).to_bytes(2, "little")
                  + len(v.encode() + b"\x00").to_bytes(4, "little")
                  + len(raw).to_bytes(4, "little") + len(vals).to_bytes(4, "little"))
        vals += raw
        koff += len(k) + 1
    head = 20 + len(index)
    keys += b"\x00" * (-len(keys) % 4)
    return (b"\x00PSF" + (0x0101).to_bytes(4, "little") + head.to_bytes(4, "little")
            + (head + len(keys)).to_bytes(4, "little") + len(entries).to_bytes(4, "little")
            + index + keys + vals)


def _iso(path, sfo):
    """A minimal ISO 9660 image: PVD at sector 16, root at 18, PSP_GAME at 19, SFO at 20."""
    def rec(name, extent, size, is_dir):
        ident = name.encode()
        n = 33 + len(ident) + (len(ident) + 1) % 2
        r = bytearray(n)
        r[0] = n
        r[2:6] = extent.to_bytes(4, "little"); r[10:14] = size.to_bytes(4, "little")
        r[25] = 2 if is_dir else 0
        r[32] = len(ident); r[33:33 + len(ident)] = ident
        return bytes(r)
    img = bytearray(21 * 2048)
    pvd = bytearray(2048); pvd[0] = 1; pvd[1:6] = b"CD001"
    pvd[156:156 + 34] = rec("\x00", 18, 2048, True)[:34]
    img[16 * 2048:17 * 2048] = pvd
    img[18 * 2048:18 * 2048 + 64] = rec("PSP_GAME", 19, 2048, True).ljust(64, b"\x00")
    img[19 * 2048:19 * 2048 + 64] = rec("PARAM.SFO;1", 20, len(sfo), False).ljust(64, b"\x00")
    img[20 * 2048:20 * 2048 + len(sfo)] = sfo
    path.write_bytes(bytes(img))


def test_psp_disc_title_comes_from_param_sfo(tmp_path):
    iso = tmp_path / "PSP - GTAvcs.iso"
    _iso(iso, _sfo([("DISC_ID", "ULES00502"), ("TITLE", "Grand Theft Auto: Vice City Stories")]))
    assert sniff.psp_info(iso) == {"title": "Grand Theft Auto: Vice City Stories",
                                   "disc_id": "ULES00502"}
    assert local_title(iso, "psp") == "Grand Theft Auto: Vice City Stories"


def test_all_caps_and_trademarks_are_tidied_and_prefixes_dropped(tmp_path):
    iso = tmp_path / "PSP - Grip shift.iso"
    _iso(iso, _sfo([("TITLE", "GRIPSHIFT\u2122")]))
    assert local_title(iso, "psp") == "Gripshift"
    assert local_title(Path("X:/PSP - Marvel ultimate Alliance.cso"), "psp") == "Marvel ultimate Alliance"
    assert local_title(Path("X:/Metal_Gear_Peace_Walker.zip"), "psp") == "Metal Gear Peace Walker"


def test_a_one_megabyte_archive_is_not_a_disc_game():
    """A .gdi plus the 1 MB Track 2 every GD-ROM shares hash-matched Blue Stinger; a PSP
    mini of a few MB is a real game."""
    assert sniff.stub_disc(970_000, "dreamcast", "Blue Stinger (Japan)")
    assert sniff.stub_disc(130_000, "wii", "Adventure Island II (USA) (SNES) (Virtual Console)")
    assert not sniff.stub_disc(4_780_000, "psp", "Pinball Fantasies (USA) (minis) (PSN)")
    assert not sniff.stub_disc(850_000_000, "dreamcast", "Dreamon Volume 10")
    assert not sniff.stub_disc(200_000, "gba", "Any cartridge")


def test_wii_disc_title_and_drive_split_suffixes(tmp_path):
    from romcom.scanner import _norm
    head = bytearray(0x260)
    head[0x200 + 0x18:0x200 + 0x1C] = bytes.fromhex("5D1C9EA3")
    head[0x200 + 0x20:0x200 + 0x36] = b"Dead Space: Extraction"
    w = tmp_path / "RZJE69-005.wbfs"
    w.write_bytes(bytes(head))
    assert sniff.nintendo_disc_title(w) == "Dead Space: Extraction"
    assert local_title(w, "wii") == "Dead Space: Extraction"
    assert _norm("Sonic Unleashed-004.wbfs") == "sonic unleashed"
    assert local_title(Path("X:/Ghost Recon Advanced Warfighter-002.iso"), "ps2") == \
        "Ghost Recon Advanced Warfighter"
