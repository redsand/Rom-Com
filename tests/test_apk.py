"""Android games: read from the package itself, kept apart, and put on a card."""
import json
import struct
import zipfile

from romcom import apk
from romcom.db import connect


def _pool(strings):
    """A UTF-16 ResStringPool chunk."""
    offs, data = [], b""
    for s in strings:
        offs.append(len(data))
        data += struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0"
    while len(data) % 4:
        data += b"\0"
    hsize = 28
    start = hsize + 4 * len(strings)
    body = b"".join(struct.pack("<I", o) for o in offs) + data
    return struct.pack("<HHIIIIII", 0x0001, hsize, hsize + len(body), len(strings), 0, 0,
                       start, 0) + body


def _manifest(package, code, name, label, min_sdk=21, split=None):
    """A binary AndroidManifest.xml. `label` is a str, or an int resource id."""
    strings = ["label", "versionCode", "versionName", "minSdkVersion", "package",
               "manifest", "application", "uses-sdk", package, name]
    if isinstance(label, str):
        strings.append(label)
    if split:
        strings += ["split", split]
    S = {s: i for i, s in enumerate(strings)}
    resmap = [0x01010001, 0x0101021B, 0x0101021C, 0x0101020C]

    def attr(name, dtype, data, raw=0xFFFFFFFF):
        return struct.pack("<IIIHBBI", 0xFFFFFFFF, S[name], raw, 8, 0, dtype, data)

    def element(tag, attrs):
        ext = struct.pack("<IIHHHHHH", 0xFFFFFFFF, S[tag], 20, 20, len(attrs), 0, 0, 0)
        body = ext + b"".join(attrs)
        return struct.pack("<HHIII", 0x0102, 16, 16 + len(body), 1, 0xFFFFFFFF) + body

    m_attrs = [attr("package", 0x03, S[package], S[package]),
               attr("versionCode", 0x10, code), attr("versionName", 0x03, S[name], S[name])]
    if split:
        m_attrs.append(attr("split", 0x03, S[split], S[split]))
    lab = (attr("label", 0x03, S[label], S[label]) if isinstance(label, str)
           else attr("label", 0x01, label))
    chunks = (_pool(strings)
              + struct.pack("<HHI", 0x0180, 8, 8 + 4 * len(resmap))
              + b"".join(struct.pack("<I", r) for r in resmap)
              + element("manifest", m_attrs)
              + element("uses-sdk", [attr("minSdkVersion", 0x10, min_sdk)])
              + element("application", [lab]))
    return struct.pack("<HHI", 0x0003, 8, 8 + len(chunks)) + chunks


def _arsc(res_id, value, lang=b"\0\0"):
    """A resources.arsc holding one string resource."""
    pool = _pool([value])
    type_id, entry = (res_id >> 16) & 0xFF, res_id & 0xFFFF
    count = entry + 1
    config = struct.pack("<I", 64) + b"\0" * 4 + lang + b"\0" * 54
    hsize = 20 + len(config)
    offsets = b"".join(struct.pack("<I", 0 if i == entry else 0xFFFFFFFF) for i in range(count))
    entries = struct.pack("<HHI", 8, 0, 0) + struct.pack("<HBBI", 8, 0, 0x03, 0)
    start = hsize + len(offsets)
    tchunk = (struct.pack("<HHIBBHII", 0x0201, hsize, start + len(entries), type_id, 0, 0,
                          count, start) + config + offsets + entries)
    phsize = 288
    pkg = (struct.pack("<HHII", 0x0200, phsize, phsize + len(tchunk), res_id >> 24)
           + b"\0" * 256 + struct.pack("<IIIII", 0, 0, 0, 0, 0)) + tchunk
    body = pool + pkg
    return struct.pack("<HHII", 0x0002, 12, 12 + len(body), 1) + body


def _apk(path, package="com.example.game", code=3, name="1.2", label="Example Game",
         abis=("arm64-v8a",), arsc=None, split=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", _manifest(package, code, name, label, split=split))
        if arsc:
            z.writestr("resources.arsc", arsc)
        for a in abis:
            z.writestr(f"lib/{a}/libgame.so", b"\x7fELF")
    return path


def test_the_manifest_says_what_the_game_is(tmp_path):
    info = apk.read(_apk(tmp_path / "whatever-v9-full.apk"))
    assert info["package"] == "com.example.game"
    assert info["version_code"] == 3 and info["version_name"] == "1.2"
    assert info["min_sdk"] == 21 and info["abis"] == ["arm64-v8a"]
    assert info["label"] == "Example Game"


def test_a_label_that_is_a_resource_is_resolved(tmp_path):
    """Nearly every real APK names itself through resources.arsc, not in the manifest."""
    rid = 0x7F0B0002
    p = _apk(tmp_path / "a.apk", label=rid, arsc=_arsc(rid, "Resolved Title"))
    assert apk.read(p)["label"] == "Resolved Title"


def test_an_xapk_carries_its_obb(tmp_path):
    p = tmp_path / "game.xapk"
    inner = tmp_path / "inner.apk"
    _apk(inner, package="com.big.game")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("manifest.json", json.dumps({
            "package_name": "com.big.game", "name": "Big Game", "version_code": "40",
            "version_name": "4.0", "min_sdk_version": "24"}))
        z.write(inner, "com.big.game.apk")
        z.writestr("config.arm64_v8a.apk", b"")
        z.writestr("Android/obb/com.big.game/main.40.com.big.game.obb", b"data")
    info = apk.read(p)
    assert info["kind"] == "xapk" and info["label"] == "Big Game"
    assert info["version_code"] == 40 and info["splits"] == 1
    assert info["obb"] == ["Android/obb/com.big.game/main.40.com.big.game.obb"]
    assert "arm64-v8a" in info["abis"]


def test_a_file_that_is_not_a_package_is_refused(tmp_path):
    p = tmp_path / "page.apk"
    p.write_text("<html>not found</html>")
    try:
        apk.read(p)
    except ValueError:
        return
    raise AssertionError("an HTML page was read as a game")


def _lib(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "test.db"))
    return connect()


def test_scan_keeps_the_newest_version_and_pairs_the_obb(tmp_path, monkeypatch):
    db = _lib(tmp_path, monkeypatch)
    src = tmp_path / "android"
    src.mkdir()
    _apk(src / "old.apk", code=1, name="1.0")
    _apk(src / "new.apk", code=3, name="1.2")
    (src / "com.example.game").mkdir()
    (src / "com.example.game" / "main.3.com.example.game.obb").write_bytes(b"x" * 10)
    (src / "broken.apk").write_text("nope")
    r = apk.scan(src, db=db)
    assert r["games"] == 1 and len(r["errors"]) == 1
    gs = apk.games(db)
    assert len(gs) == 1
    g = gs[0]
    assert g["version_name"] == "1.2" and g["older"] == [str(src / "old.apk")]
    assert g["obb"] == [str(src / "com.example.game" / "main.3.com.example.game.obb")]
    # A second scan opens nothing that has not changed, and forgets what is gone.
    (src / "old.apk").unlink()
    r = apk.scan(src, db=db)
    assert r["read"] == 0 and r["unchanged"] == 2
    assert apk.games(db)[0]["older"] == []


def test_android_games_are_not_catalog_items(tmp_path, monkeypatch):
    """Kept apart: a scan writes nothing the console catalog or a card ranking reads."""
    db = _lib(tmp_path, monkeypatch)
    src = tmp_path / "android"
    src.mkdir()
    _apk(src / "g.apk")
    apk.scan(src, db=db)
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_export_puts_the_package_and_an_es_de_entry_on_the_card(tmp_path, monkeypatch):
    db = _lib(tmp_path, monkeypatch)
    src = tmp_path / "android"
    src.mkdir()
    _apk(src / "g.apk")
    _apk(src / "x86only.apk", package="com.pc.only", label="PC Only", abis=("x86_64",))
    (src / "com.example.game").mkdir()
    (src / "com.example.game" / "main.3.com.example.game.obb").write_bytes(b"x" * 10)
    apk.scan(src, db=db)
    from romcom import organizer
    monkeypatch.setattr(organizer, "_reserve_bytes", lambda card: 0)
    card = tmp_path / "card"
    card.mkdir()
    dry = apk.export(card, db=db, dry_run=True)
    assert dry["games"] == 1 and not (card / apk.CARD_DIR).exists()
    r = apk.export(card, db=db)
    assert r["games"] == 1 and r["stubs"] == 1
    assert [s["title"] for s in r["skipped_abi"]] == ["PC Only"]
    game = card / apk.CARD_DIR / "Example Game"
    assert (game / "g.apk").exists()
    assert (game / "obb" / "com.example.game" / "main.3.com.example.game.obb").exists()
    stub = card / "ROMs" / "androidgames" / "Example Game.app"
    assert stub.read_text(encoding="utf-8") == "com.example.game"
    # Again: nothing to copy.
    again = apk.export(card, db=db)
    assert again["copied"] == 0 and again["stubs"] == 0
