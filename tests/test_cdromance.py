import time

from romcom import cdromance
from romcom.db import connect


def cdr_env(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ROMCOM_CDR_BASE", "https://cdr.example")
    monkeypatch.setenv("ROMCOM_CDR_DELAY", "0")
    monkeypatch.setenv("ROMCOM_CDR_JITTER", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    connect()
    return tmp_path


SEARCH_HTML = """
<a href="/snes-rom/super-metroid/">Super Metroid (mobile card)</a>
<a href="https://cdr.example/snes-rom/super-metroid/">Super Metroid</a>
<a href="/gba-roms/super-metroid-war/">Super Metroid War</a>
<a href="https://other.example/snes-rom/super-metroid/">off-site</a>
<a href="/category/action/">Action</a>
"""

# the post id rides next to the download-panel wrapper on the game page
GAME_PAGE = '<div id="acf-content-wrapper" data-open-default="303618" data-id="303618"></div>'

AJAX_HTML = """
<tr><td>Super Metroid.zip</td><td>2.41 MB</td>
<td><a id="dl-btn-0" class="btn" href="https://dl1.cdr.example/download.php?file=Super%20Metroid.zip&amp;id=303618&amp;platform=snes-rom&amp;key=777">Download</a></td></tr>
"""


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
    """cdromance._request stub keyed by (method, url-ish substring); records calls."""
    calls = []

    def _fake(method, url, **kw):
        calls.append({"method": method, "url": url, "t": time.monotonic(),
                      "referer": kw.get("referer"), "origin": kw.get("origin"),
                      "xhr": kw.get("xhr"), "data": kw.get("data")})
        for key, resp in responses.items():
            if method == key[0] and key[1] in url:
                return resp
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(cdromance, "_request", _fake)
    return calls


def test_search_filters_platform_and_ranks(monkeypatch, tmp_path):
    cdr_env(monkeypatch, tmp_path)
    fake_requests(monkeypatch, {("GET", "?s="): FakeResponse(SEARCH_HTML)})
    results = cdromance.search("Super Metroid", "snes")
    assert len(results) == 1  # gba filtered, other host skipped, dup link deduped
    r = results[0]
    assert r["url"] == "https://cdr.example/snes-rom/super-metroid/"
    assert r["title"] == "Super Metroid" and r["console"] == "snes-rom"
    assert r["score"] >= 100


def test_search_without_system_map_allows_any_platform(monkeypatch, tmp_path):
    cdr_env(monkeypatch, tmp_path)
    fake_requests(monkeypatch, {("GET", "?s="): FakeResponse(SEARCH_HTML)})
    results = cdromance.search("Super Metroid", "virtualboy")  # unmapped: no filter
    assert {r["console"] for r in results} == {"snes-rom", "gba-roms"}


def test_search_strips_no_intro_parentheticals(monkeypatch, tmp_path):
    """The site's search fails on "(USA)"-style suffixes — the query must be cleaned
    before it is sent, but results are ranked against the original title."""
    cdr_env(monkeypatch, tmp_path)
    calls = fake_requests(monkeypatch, {("GET", "?s="): FakeResponse(SEARCH_HTML)})
    cdromance.search("Super Metroid (USA)", "snes")
    assert "Super%20Metroid" in calls[0]["url"]
    assert "%28" not in calls[0]["url"]


def test_fetch_downloads_file(monkeypatch, tmp_path):
    """The flow mirrors the site's own cdr-dl.js: game page for the post id, then the
    Show-Links AJAX POST (which refuses plain POSTs with "error: x012"), then a
    Referer'd stream of the direct zip."""
    cdr_env(monkeypatch, tmp_path)
    dest = tmp_path / "games"
    calls = fake_requests(monkeypatch, {
        ("GET", "/snes-rom/super-metroid"): FakeResponse(GAME_PAGE),
        ("POST", "ajax.php"): FakeResponse(AJAX_HTML),
        ("GET", "download.php"): FakeResponse(chunks=[b"PK\x03\x04", b"data"]),
    })
    pick = {"title": "Super Metroid", "score": 100,
            "url": "https://cdr.example/snes-rom/super-metroid/"}
    path = cdromance.fetch(pick, dest)
    assert path.read_bytes() == b"PK\x03\x04data"
    assert path.name == "Super Metroid.zip"  # named from the download link's file param
    post = next(c for c in calls if c["method"] == "POST")
    assert "ajax.php" in post["url"]
    assert post["data"] == {"post_id": "303618"} and post["xhr"] is True
    assert post["referer"] == pick["url"] and post["origin"] == "https://cdr.example"
    fileget = next(c for c in calls if c["method"] == "GET" and "download.php" in c["url"])
    assert fileget["referer"] == pick["url"]


def test_fetch_falls_back_to_data_post_id(monkeypatch, tmp_path):
    """Older pages may lack the acf wrapper; data-post-id carries the same id."""
    cdr_env(monkeypatch, tmp_path)
    calls = fake_requests(monkeypatch, {
        ("GET", "/snes-rom/x"): FakeResponse('<body data-post-id="99"></body>'),
        ("POST", "ajax.php"): FakeResponse(AJAX_HTML),
        ("GET", "download.php"): FakeResponse(chunks=[b"x"]),
    })
    cdromance.fetch({"title": "t", "url": "https://cdr.example/snes-rom/x/"}, tmp_path)
    post = next(c for c in calls if c["method"] == "POST")
    assert post["data"] == {"post_id": "99"}


def test_fetch_raises_when_the_ajax_refuses(monkeypatch, tmp_path):
    """A plain POST without X-Requested-With gets "error: x012" — if the site ever
    answers that way to ours, the failure must say so, not "no link"."""
    cdr_env(monkeypatch, tmp_path)
    fake_requests(monkeypatch, {
        ("GET", "/snes-rom/x"): FakeResponse(GAME_PAGE),
        ("POST", "ajax.php"): FakeResponse("error: x012"),
    })
    try:
        cdromance.fetch({"title": "t", "url": "https://cdr.example/snes-rom/x/"}, tmp_path)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as ex:
        assert "x012" in str(ex)


def test_fetch_sanitizes_filenames(monkeypatch, tmp_path):
    cdr_env(monkeypatch, tmp_path)
    dest = tmp_path / "games"
    ajax = AJAX_HTML.replace("Super%20Metroid.zip", "..%2Fevil%2Fname.zip")
    fake_requests(monkeypatch, {
        ("GET", "/snes-rom/x"): FakeResponse(GAME_PAGE),
        ("POST", "ajax.php"): FakeResponse(ajax),
        ("GET", "download.php"): FakeResponse(chunks=[b"x"]),
    })
    path = cdromance.fetch({"title": "t", "url": "https://cdr.example/snes-rom/x/"}, dest)
    assert path.parent == dest  # stayed inside dest despite ../ in the remote name
    assert "/" not in path.name and "\\" not in path.name and ".." not in path.name


def test_pacing_delays_requests(monkeypatch, tmp_path):
    cdr_env(monkeypatch, tmp_path, ROMCOM_CDR_DELAY="0.15", ROMCOM_CDR_JITTER="0")
    # real _pace + real requests.request replaced — pacing is what's under test
    monkeypatch.setattr(cdromance.requests, "request",
                        lambda *a, **k: FakeResponse())
    calls = []
    real_pace = cdromance._pace

    def spy_pace():
        calls.append(time.monotonic())
        real_pace()

    monkeypatch.setattr(cdromance, "_pace", spy_pace)
    cdromance._LAST[0] = 0.0
    for _ in range(4):
        cdromance._request("GET", "https://cdr.example/x")
    # request 1 goes out immediately (nothing sent before it); every later one
    # waits out the full delay since the previous request
    assert calls[1] - calls[0] < 0.05
    for a, b in zip(calls[1:], calls[2:]):
        assert b - a >= 0.14
    cdromance._LAST[0] = 0.0  # don't slow later tests in this process