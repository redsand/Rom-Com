"""PS3 discs reach the card decrypted, with keys from the local mirror."""
import csv
import struct
import zipfile
from pathlib import Path

from Crypto.Cipher import AES

from romcom import esde, ps3

KEY = "00112233445566778899AABBCCDDEEFF"
S = ps3.SECTOR


def _disc(path, serial="BLUS-30001", total=40, plain=((0, 9), (20, 29)), key=KEY):
    """A tiny PS3 disc image: header, licence sector, encrypted ranges between `plain`."""
    secs = [bytes([i % 251]) * S for i in range(total)]
    hdr = bytearray(S)
    hdr[:8] = struct.pack(">II", len(plain), 0)
    for i, (a, b) in enumerate(plain):
        hdr[8 + 8 * i:16 + 8 * i] = struct.pack(">II", a, b)
    secs[0] = bytes(hdr)
    s1 = bytearray(S)
    s1[:12] = b"PlayStation3"
    s1[16:16 + len(serial)] = serial.encode()
    secs[1] = bytes(s1)
    clear = list(secs)
    enc = ps3.encrypted_ranges(list(plain), total)
    clear[enc[0][0]] = ps3.LICENCE + bytes(S - len(ps3.LICENCE))
    out = list(clear)
    for a, b in enc:
        for n in range(a, b + 1):
            out[n] = AES.new(bytes.fromhex(key), AES.MODE_CBC, iv=n.to_bytes(16, "big")).encrypt(clear[n])
    path.write_bytes(b"".join(out))
    return b"".join(clear)


def _keys(rows):
    ps3.KEY_DIR.mkdir(parents=True, exist_ok=True)
    with open(ps3.KEY_DIR / "keys.tsv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, ps3._FIELDS, delimiter="\t")
        w.writeheader()
        for r in rows:
            w.writerow({"serial": "", "size": "", "crc32": "", "md5": "", **r})
    ps3._index.clear()


def test_the_region_table_and_the_gaps_between_plain_regions():
    assert ps3.encrypted_ranges([(0, 9), (20, 29)], 40) == [(10, 19), (30, 39)]
    assert ps3.encrypted_ranges([(0, 9279), (9312, 9599), (1460832, 1591935)], 1591936) == \
        [(9280, 9311), (9600, 1460831)]


def test_a_disc_is_decrypted_with_its_key_and_only_that_key(tmp_path):
    iso = tmp_path / "Game (USA).iso"
    clear = _disc(iso)
    _keys([{"name": "Game (USA).iso", "key": KEY}])
    info = ps3.inspect(iso)
    assert info["serial"] == "BLUS30001" and info["fits"] and not info["already_decrypted"]
    out = tmp_path / "out.iso"
    assert ps3.decrypt(iso, out, KEY)["decrypted"]
    assert out.read_bytes() == clear
    assert ps3.inspect(out)["already_decrypted"]
    ps3.decrypt(iso, iso, KEY)                       # in place, as the export does
    assert iso.read_bytes() == clear
    other = tmp_path / "other.iso"
    _disc(other)
    try:
        ps3.decrypt(other, tmp_path / "x.iso", "FF" * 16)
        assert False, "a wrong key must not write a disc"
    except OSError as e:
        assert "does not open" in str(e)


def test_a_renamed_disc_is_found_by_serial_and_size(tmp_path):
    iso = tmp_path / "my backup.iso"
    _disc(iso, serial="BLES-00772")
    _keys([{"name": "Real Name (Europe).iso", "serial": "BLES00772",
            "size": str(iso.stat().st_size), "key": KEY},
           {"name": "Real Name (Europe) (Rev 1).iso", "serial": "BLES00772", "size": "1", "key": "AB" * 16}])
    assert ps3.inspect(iso)["key"]["name"] == "Real Name (Europe).iso"


def test_the_page_table_parses():
    row = ('<TR><TD><a title="BLES-00772 v1.00" href="67942">BLES00772</a><TD>Arthur &amp; Co '
           '(Europe).iso<TD>3,260,284,928<TD>4ded5c62<TD>73bd8d425ea14dfe19a864303478f494<TD>'
           'BD7D0ECE82F8DFB953CE60FB3F705095</TR>')
    (r,) = list(ps3.parse_page(row))
    assert r == {"name": "Arthur & Co (Europe).iso", "serial": "BLES00772", "size": "3260284928",
                 "crc32": "4ded5c62", "md5": "73bd8d425ea14dfe19a864303478f494",
                 "key": "BD7D0ECE82F8DFB953CE60FB3F705095"}


def test_the_export_plan_decrypts_keyed_discs_and_drops_the_rest(tmp_path):
    loose = tmp_path / "Game (USA).iso"
    clear = _disc(loose)
    nokey = tmp_path / "Unknown (USA).iso"
    _disc(nokey, serial="BLUS-39999")
    pc = tmp_path / "PC Game.iso"
    pc.write_bytes(b"\0" * 4 * S)                    # a PC DVD matched to ps3 by its name
    zipped = tmp_path / "Zipped (USA).zip"
    inner = tmp_path / "Zipped (USA).iso"
    zclear = _disc(inner, serial="BLUS-30002")
    with zipfile.ZipFile(zipped, "w") as z:
        z.write(inner, inner.name)
    inner.unlink()
    jb = tmp_path / "Folder Game.7z"
    with zipfile.ZipFile(jb.with_suffix(".zip"), "w") as z:
        z.writestr("Folder Game (USA)/PS3_GAME/PARAM.SFO", b"\0PSF")
        z.writestr("Folder Game (USA)/PS3_GAME/USRDIR/EBOOT.BIN", b"SCE\0")
    jb = jb.with_suffix(".zip")
    _keys([{"name": "Game (USA).iso", "key": KEY}, {"name": "Zipped (USA).iso", "key": KEY}])
    files = [(str(p), p.stat().st_size) for p in (loose, nokey, pc, zipped, jb)]
    steps, entries, dropped = esde.plan("ps3", files)
    assert dropped == 2
    assert entries == ["Game (USA).iso", "Zipped (USA).iso", "Folder Game (USA).ps3"]
    card = tmp_path / "card"
    for st in steps:
        _, src, names, rel, key, size = st
        esde.make_ps3(src, names, key, card / rel)
        assert esde.ps3_done(card / rel, size)
    assert (card / "Game (USA).iso").read_bytes() == clear
    assert (card / "Zipped (USA).iso").read_bytes() == zclear
    assert (card / "Folder Game (USA).ps3" / "PS3_GAME" / "PARAM.SFO").exists()
    assert not list(card.glob("*.part")) and not list(card.glob("*.unpack"))


def test_an_export_with_ps3_games_brings_sonys_firmware(tmp_path, monkeypatch):
    from romcom.config import invalidate
    from romcom.db import connect
    from romcom.organizer import organize
    from tests.test_esde import _game
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "e.db"))
    invalidate()
    iso = tmp_path / "Game (USA).iso"
    clear = _disc(iso)
    _keys([{"name": "Game (USA).iso", "key": KEY}])
    pup = tmp_path / "PS3UPDAT.PUP"
    pup.write_bytes(ps3.PUP_MAGIC + b"\0" * 64)
    monkeypatch.setattr(ps3, "firmware", lambda **k: {"path": str(pup), "version": "4.93"})
    db = connect()
    with db:
        _game(db, "p", "Game (USA)", "ps3", iso)
    card = tmp_path / "card"
    r = organize(card)
    assert not r["errors"], r["errors"]
    assert (card / "ps3" / "Game (USA).iso").read_bytes() == clear
    assert r["decrypted_ps3"] == 1
    assert (card / "system" / "ps3" / "PS3UPDAT.PUP").read_bytes() == pup.read_bytes()
    assert r["console_bios"]["systems"]["ps3"]["copied"] == ["PS3UPDAT.PUP"]
    assert organize(card)["console_bios"]["systems"]["ps3"]["skipped"] == ["PS3UPDAT.PUP"]
