from romcom import archive
from romcom.config import invalidate


def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ROMCOM_ARCHIVE_DELAY", "0")
    monkeypatch.setenv("ROMCOM_SEARCH_CACHE_TTL", "360")
    invalidate()


class FakeResp:
    def __init__(self, payload=None, chunks=()):
        self._p = payload
        self._c = list(chunks)
    def json(self):
        return self._p
    def iter_content(self, _):
        return iter(self._c)
    def raise_for_status(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_search_keeps_matching_rom_files_and_drops_the_junk(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)

    def fake_get(url, params=None, stream=False):
        if "advancedsearch" in url:
            return FakeResp({"response": {"docs": [{"identifier": "skins"}, {"identifier": "games"}]}})
        if "/metadata/skins" in url:                       # a Winamp-skin item — no rom files
            return FakeResp({"files": [{"name": "Chrono_Trigger_Schala.wsz", "size": "100"}]})
        if "/metadata/games" in url:
            return FakeResp({"files": [
                {"name": "Chrono Trigger (USA).zip", "size": "4000000"},   # the real rom
                {"name": "Final Fantasy VI (USA).zip", "size": "4000000"},  # rom, wrong title
                {"name": "boxart.png", "size": "50000"}]})                  # art
        raise AssertionError(url)
    monkeypatch.setattr(archive, "_get", fake_get)

    res = archive.search("Chrono Trigger", "snes")
    titles = [r["title"] for r in res]
    assert titles == ["Chrono Trigger (USA).zip"]          # skin/art excluded; FF6 pre-filtered (no shared token)
    assert res[0]["score"] >= 20 and res[0]["source"] == "archive"
    assert res[0]["url"].startswith("https://archive.org/download/games/")


def test_fetch_streams_to_disk(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    monkeypatch.setattr(archive, "_get", lambda url, params=None, stream=False: FakeResp(chunks=[b"PK\x03\x04", b"rom"]))
    dest = tmp_path / "games"
    path = archive.fetch({"title": "x", "url": "https://archive.org/download/games/Chrono%20Trigger%20(USA).zip"}, dest)
    assert path.read_bytes() == b"PK\x03\x04rom"
    assert path.name == "Chrono Trigger (USA).zip"          # url-decoded filename


def test_fetch_falls_back_to_a_title_that_writes_on_windows(monkeypatch, tmp_path):
    """An item with no file in its url names its download after the catalog title — and a
    title like 'Ever 17: The Out of Infinity' has a colon, which a Windows filename cannot
    hold: the download died with Errno 22 on every sweep until the title was sanitised."""
    env(monkeypatch, tmp_path)
    monkeypatch.setattr(archive, "_get", lambda url, params=None, stream=False: FakeResp(chunks=[b"PK\x03\x04", b"rom"]))
    dest = tmp_path / "games"
    path = archive.fetch({"title": "Ever 17: The Out of Infinity (Premium Edition)",
                          "url": "https://archive.org/download/item/"}, dest)
    assert path.exists() and path.name.endswith(".zip") and ":" not in path.name
