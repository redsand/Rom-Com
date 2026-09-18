import time

from romcom import webdl
from romcom.db import connect


def webdl_env(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ROMCOM_WEBDL_BASE", "https://roms.example")
    monkeypatch.setenv("ROMCOM_WEBDL_DELAY", "0")
    monkeypatch.setenv("ROMCOM_WEBDL_JITTER", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    connect()
    return tmp_path


SEARCH_HTML = """
<a href="/gameboy-rom-super-mario-land/">Super Mario Land</a>
<a href="/gameboy-rom-super-mario-land/">Super Mario Land</a>
<a href="/gameboy-advance-rom-super-mario-advance/">Super Mario Advance</a>
<a href="/sega-genesis-rom-super-mario-land-snes/">unrelated console</a>
"""

ROM_PAGE = '<button data-media-id="67006" type="submit">Save Game</button>'


class FakeResponse:
    def __init__(self, text="", chunks=(), json=None):
        self.text = text
        self._chunks = list(chunks)
        self._json = json

    def json(self):
        return self._json

    def iter_content(self, _):
        return iter(self._chunks)

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_requests(monkeypatch, responses):
    """webdl._request stub keyed by (method, url-ish substring); records call times."""
    calls = []

    def _fake(method, url, **kw):
        calls.append({"method": method, "url": url, "t": time.monotonic(),
                      "referer": kw.get("referer"), "json_accept": kw.get("json_accept"),
                      "data": kw.get("data")})
        for key, resp in responses.items():
            if method == key[0] and key[1] in url:
                return resp
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(webdl, "_request", _fake)
    return calls


def test_search_filters_console_and_ranks(monkeypatch, tmp_path):
    webdl_env(monkeypatch, tmp_path)
    fake_requests(monkeypatch, {("GET", "/search/"): FakeResponse(SEARCH_HTML)})
    results = webdl.search("Super Mario Land", "gb")
    assert all(r["console"] == "gameboy" for r in results)  # gba/genesis filtered out
    assert len(results) == 1  # duplicate mobile/desktop card links deduped
    assert results[0]["title"] == "super mario land" and results[0]["score"] >= 100
    assert results[0]["url"] == "https://roms.example/gameboy-rom-super-mario-land/"


def test_search_without_system_map_allows_any_console(monkeypatch, tmp_path):
    webdl_env(monkeypatch, tmp_path)
    fake_requests(monkeypatch, {("GET", "/search/"): FakeResponse(SEARCH_HTML)})
    results = webdl.search("Super Mario Land", "pcengine")  # unmapped system: no console filter
    assert {r["console"] for r in results} >= {"gameboy", "sega-genesis"}


def test_fetch_downloads_file(monkeypatch, tmp_path):
    webdl_env(monkeypatch, tmp_path)
    dest = tmp_path / "games"
    calls = fake_requests(monkeypatch, {
        ("GET", "gameboy-rom-super-mario-land"): FakeResponse(ROM_PAGE),
        ("POST", "?download"): FakeResponse(json={
            "downloadUrl": "https://static.example/abc/output.bin",
            "downloadName": "Super%20Mario%20Land%20(World).zip"}),
        ("GET", "static.example"): FakeResponse(chunks=[b"PK\x03\x04", b"data"]),
    })
    pick = {"title": "super mario land", "score": 100,
            "url": "https://roms.example/gameboy-rom-super-mario-land/"}
    path = webdl.fetch(pick, dest)
    assert path.read_bytes() == b"PK\x03\x04data"
    assert path.name == "Super Mario Land (World).zip"
    # the flow mirrors the site's own download.js: json POST carrying the page's
    # mediaId, then Referer'd file GET
    post = next(c for c in calls if c["method"] == "POST")
    assert post["json_accept"] and post["data"] == {"mediaId": "67006"}
    assert post["referer"].endswith("/gameboy-rom-super-mario-land/")
    fileget = next(c for c in calls if c["method"] == "GET" and "static.example" in c["url"])
    assert fileget["referer"] == pick["url"]


def test_fetch_sanitizes_filenames(monkeypatch, tmp_path):
    webdl_env(monkeypatch, tmp_path)
    dest = tmp_path / "games"
    fake_requests(monkeypatch, {
        ("GET", "rom-"): FakeResponse(ROM_PAGE),
        ("POST", "?download"): FakeResponse(json={
            "downloadUrl": "https://static.example/x.bin",
            "downloadName": "..%2Fevil%2Fname.zip"}),
        ("GET", "static.example"): FakeResponse(chunks=[b"x"]),
    })
    pick = {"title": "t", "score": 100, "url": "https://roms.example/gameboy-rom-t/"}
    path = webdl.fetch(pick, dest)
    assert path.parent == dest  # stayed inside dest despite ../ in the remote name
    assert "/" not in path.name and "\\" not in path.name and ".." not in path.name


def test_fetch_without_download_url_raises(monkeypatch, tmp_path):
    webdl_env(monkeypatch, tmp_path)
    fake_requests(monkeypatch, {
        ("GET", "rom-"): FakeResponse(ROM_PAGE),
        ("POST", "?download"): FakeResponse(json={"asset": {}}),
    })
    pick = {"title": "t", "score": 100, "url": "https://roms.example/gameboy-rom-t/"}
    try:
        webdl.fetch(pick, tmp_path)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as ex:
        assert "downloadUrl" in str(ex)


def test_pacing_delays_requests(monkeypatch, tmp_path):
    webdl_env(monkeypatch, tmp_path, ROMCOM_WEBDL_DELAY="0.15", ROMCOM_WEBDL_JITTER="0")
    # real _pace + real requests.request replaced — pacing is what's under test
    monkeypatch.setattr(webdl.requests, "request",
                        lambda *a, **k: FakeResponse())
    calls = []
    real_pace = webdl._pace

    def spy_pace():
        calls.append(time.monotonic())
        real_pace()

    monkeypatch.setattr(webdl, "_pace", spy_pace)
    webdl._LAST[0] = 0.0
    for _ in range(4):
        webdl._request("GET", "https://roms.example/x")
    # request 1 goes out immediately (nothing sent before it); every later one
    # waits out the full delay since the previous request
    assert calls[1] - calls[0] < 0.05
    for a, b in zip(calls[1:], calls[2:]):
        assert b - a >= 0.14
    webdl._LAST[0] = 0.0  # don't slow later tests in this process

def test_search_strips_no_intro_parentheticals(monkeypatch, tmp_path):
    """The site's search fails on "(USA, Europe)" suffixes and silently falls
    back to featured games — the query must be cleaned before it is sent."""
    webdl_env(monkeypatch, tmp_path)
    calls = fake_requests(monkeypatch, {("GET", "/search/"): FakeResponse(SEARCH_HTML)})
    webdl.search("10-Yard Fight (USA, Europe)", "nes")
    assert "10-Yard%20Fight" in calls[0]["url"]  # parens never sent
    assert "%28USA%29" not in calls[0]["url"] and "%28" not in calls[0]["url"]
