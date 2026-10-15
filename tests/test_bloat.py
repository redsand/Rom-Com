"""Downloads that can never apply are found by what is inside them."""
import zipfile

from romcom import bloat


def _zip(path, names):
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, b"x")
    return path


def test_pc_games_and_x360_packages_are_bloat_but_console_games_are_not(tmp_path):
    assert bloat.classify(_zip(tmp_path / "CoD.zip", ["setup.exe", "data1.cab"])) == "pc"
    assert bloat.classify(_zip(tmp_path / "SC.zip", ["Soul Calibur.cdi", "selfboot.exe"])) is None
    assert bloat.classify(_zip(tmp_path / "MW.zip", ["415607E6/00007000/ABC", "x/Data0000"])) == "x360"
    assert bloat.classify(_zip(tmp_path / "Mario.zip", ["Mario (USA).nes"])) is None


def test_an_unidentified_dvd_goes_only_once_romcom_rejected_it(tmp_path):
    iso = tmp_path / "Bee Movie Game.iso"
    head = bytearray(0x8100)
    head[0x8001:0x8006] = b"CD001"                         # ISO 9660, no console system id
    iso.write_bytes(bytes(head))
    assert bloat.classify(iso, rejected=False) is None
    assert bloat.classify(iso, rejected=True) == "dvd"
    xbox = tmp_path / "Halo 2.iso"
    data = bytearray(0x10100)
    data[0x10000:0x10014] = b"MICROSOFT*XBOX*MEDIA"
    xbox.write_bytes(bytes(data))
    assert bloat.classify(xbox, rejected=True) is None      # the owner keeps Xbox discs
