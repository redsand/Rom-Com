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
