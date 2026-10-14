"""Arcade parts fetched by setname: only the chips a set lacks, each checked by the dat."""
import hashlib
import io
import zipfile
import zlib

from romcom import arcadefetch as af


def _rom(data, name):
    return (hashlib.sha1(data).hexdigest(), f"{zlib.crc32(data):08x}", name)


def _m(roms=(), devices=(), romof=None, cloneof=None, disks=(), isbios=False):
    return {"roms": list(roms), "devices": list(devices), "romof": romof, "cloneof": cloneof,
            "disks": list(disks), "isbios": isbios, "isdevice": False}


def _zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


class Resp:
    def __init__(self, content=b"", status=200, json_=None, text=""):
        self.content, self.status_code, self._json, self.text = content, status, json_, text
        self.ok = status < 400
        self.raw = io.BytesIO(content)

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise OSError(self.status_code)

    def iter_content(self, n):
        yield self.content

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeIA:
    """archive.org with a few items: metadata, merged-zip listings and downloads."""
    def __init__(self, items, members=None):
        self.items, self.members, self.got = items, members or {}, []
        self.headers = {}

    def get(self, url, timeout=None, headers=None, stream=False):
        self.got.append(url)
        if "/metadata/" in url:
            item = url.rsplit("/", 1)[1]
            files = self.items.get(item)
            return Resp(json_={"files": [{"name": n, "size": len(d)} for n, d in files.items()]}
                        if files else {})
        tail = url.split("/download/", 1)[1]
        from urllib.parse import unquote
        item, _, path = unquote(tail).partition("/")
        if path.endswith("/"):
            names = self.members.get((item, path[:-1]), {})
            from urllib.parse import quote
            return Resp(text="".join(f'<a href="//archive.org/download/{item}/{quote(path[:-1])}/{n}">'
                                     for n in names))
        if (item, path.split("/")[0]) in self.members and "/" in path:
            c, inner = path.split("/", 1)
            return Resp(self.members[(item, c)][inner])
        data = self.items.get(item, {}).get(path)
        if data is None:
            return Resp(status=404)
        if headers and "Range" in headers:
            return Resp(data[:124], status=206)
        return Resp(data)


def _ctx(tmp_path, machines, have=(), chds=None):
    return {"machines": machines, "sha": {h[0] for h in have}, "crc": {h[1] for h in have},
            "chds": chds or {}, "dest": tmp_path / "patches", "zips": {}}


def test_only_the_missing_chips_come_and_each_is_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(af, "CACHE", tmp_path / "cache")
    owned, new, dev = b"old chip", b"the u64 chip", b"device chip"
    r_owned, r_new, r_dev = _rom(owned, "a.u1"), _rom(new, "u64.u64"), _rom(dev, "d.ic1")
    machines = {"game": _m([r_owned, r_new], devices=["snd"]), "snd": _m([r_dev])}
    # The per-machine item has a stale zip (no u64); the current release has it, and the
    # device's own zip carries the device chip. A same-CRC impostor is refused by SHA1.
    ia = FakeIA({
        "arcade_game": {"game.zip": _zip({"a.u1": owned})},
        af.CURRENT_ITEM: {"game.zip": _zip({"a.u1": owned, "u64.u64": new}),
                          "snd.zip": _zip({"d.ic1": dev})},
    })
    monkeypatch.setattr(af, "MERGED", [])
    monkeypatch.setattr(af, "CHD_COLLECTION", ("none", "CHDs"))
    ctx = _ctx(tmp_path, machines, have=[r_owned])
    rep = af.fetch_set("game", ctx, ia)
    assert sorted(rep["chips"]) == ["game/u64.u64", "snd/d.ic1"]
    assert rep["missing"] == [] and rep["errors"] == []
    assert (ctx["dest"] / "game" / "u64.u64").read_bytes() == new
    assert not (ctx["dest"] / "game" / "a.u1").exists()          # owned already: not written
    # A second set needing the same device finds it counted, and downloads nothing.
    machines["game2"] = _m([r_owned], devices=["snd"])
    n = len(ia.got)
    assert af.fetch_set("game2", ctx, ia)["chips"] == []
    assert len(ia.got) == n


def test_a_clone_chip_is_found_in_its_parents_merged_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(af, "CACHE", tmp_path / "cache")
    chip = b"clone-only chip"
    r = _rom(chip, "c.u2")
    machines = {"par": _m([]), "clo": _m([r], romof="par", cloneof="par")}
    merged = ("merged-item", "MAME (merged).zip")
    monkeypatch.setattr(af, "MERGED", [merged])
    monkeypatch.setattr(af, "CHD_COLLECTION", ("none", "CHDs"))
    ia = FakeIA({}, members={merged: {"par.zip": _zip({"c.u2": chip})}})
    rep = af.fetch_set("clo", _ctx(tmp_path, machines), ia)
    assert rep["chips"] == ["clo/c.u2"] and rep["sources"] == ["merged-item/par.zip"]


def test_a_chd_is_chosen_by_the_sha1_in_its_header(tmp_path, monkeypatch):
    monkeypatch.setattr(af, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(af, "MERGED", [])
    monkeypatch.setattr(af, "CHD_COLLECTION", ("none", "CHDs"))

    def chd(sha):
        h = bytearray(124)
        h[:8] = b"MComprHD"
        h[12:16] = (5).to_bytes(4, "big")
        h[84:104] = bytes.fromhex(sha)
        return bytes(h) + b"payload"
    want, other = "11" * 20, "22" * 20
    machines = {"big": _m([], disks=[{"name": "big-hdd", "sha1": want, "merge": None}])}
    ia = FakeIA({"arcade_big": {"wrong.chd": chd(other), "renamed.chd": chd(want)}})
    rep = af.fetch_set("big", _ctx(tmp_path, machines), ia)
    assert rep["disks"] == ["big/big-hdd"] and rep["missing"] == []
    assert (tmp_path / "patches" / "big" / "big-hdd.chd").read_bytes() == chd(want)


def test_what_no_source_has_is_reported_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(af, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(af, "MERGED", [])
    monkeypatch.setattr(af, "CHD_COLLECTION", ("none", "CHDs"))
    machines = {"rare": _m([_rom(b"x", "x.bin")])}
    rep = af.fetch_set("rare", _ctx(tmp_path, machines), FakeIA({}))
    assert rep["chips"] == [] and rep["missing"] == ["rare/x.bin"]


def test_planet_emulation_is_asked_only_when_its_page_lists_a_wanted_chip(tmp_path, monkeypatch):
    monkeypatch.setattr(af, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(af, "MERGED", [])
    monkeypatch.setattr(af, "NON_MERGED", [])
    monkeypatch.setattr(af, "CHD_COLLECTION", ("none", "CHDs"))
    sin = b"sine table" * 10
    r = _rom(sin, "sin-table.bin")
    machines = {"kbm": _m([], devices=["swp30"]), "swp30": _m([r]), "other": _m([])}
    pages = {"swp30": {"id": "1", "filename": "swp30.zip", "crcs": {r[1]}, "url": "u"},
             "kbm": {"id": "2", "filename": "kbm.zip", "crcs": {"deadbeef"}, "url": "u"}}
    monkeypatch.setattr(af, "planetemu_page", lambda name: pages.get(name))
    asked = []
    monkeypatch.setattr(af, "planetemu_download",
                        lambda page: asked.append(page["filename"]) or _zip({"sin-table.bin": sin}))
    rep = af.fetch_set("kbm", _ctx(tmp_path, machines), FakeIA({}))
    assert rep["chips"] == ["swp30/sin-table.bin"] and rep["sources"] == ["planetemu/swp30.zip"]
    assert asked == ["swp30.zip"]                 # kbm.zip lists no wanted chip: not fetched


def test_a_core_tries_its_own_romset_before_mames(monkeypatch):
    """2003-Plus and FBNeo want their own dumps: their romset's zip for the set comes
    first, MAME's after (a shared CRC is still a hit, and every chip is checked)."""
    files = {"MAME_2003-Plus_Reference": {"roms/1942.zip": {}},
             "mame-2003-plus-reference-set": {},
             af.CURRENT_ITEM: {"1942.zip": {}}}
    monkeypatch.setattr(af, "item_files", lambda s, item: files.get(item, {}))
    monkeypatch.setattr(af, "zip_members", lambda s, item, c: ["1942.zip"])
    monkeypatch.setattr(af, "MERGED", [])
    monkeypatch.setattr(af, "NON_MERGED", [])
    labels = [l for l, _ in af._zip_sources(None, ["1942"], {}, None, "mame2003")]
    assert labels == ["MAME_2003-Plus_Reference/1942.zip", f"{af.CURRENT_ITEM}/1942.zip"]
    fb = [l for l, _ in af._zip_sources(None, ["1942"], {}, None, "fbneo")]
    assert fb[0] == "retroarch-fbneo-romset-complete/1942.zip"
    assert [l for l, _ in af._zip_sources(None, ["1942"], {}, None)] == [f"{af.CURRENT_ITEM}/1942.zip"]
