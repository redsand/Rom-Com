import threading
import time

from romcom import vimm


def vimm_env(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("ROMCOM_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ROMCOM_VIMM_BASE", "https://vimm.example")
    monkeypatch.setenv("ROMCOM_VIMM_DL_BASE", "https://dl.vimm.example")
    monkeypatch.setenv("ROMCOM_VIMM_DELAY", "0")
    monkeypatch.setenv("ROMCOM_VIMM_JITTER", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from romcom.config import invalidate
    invalidate()


class FakeResp:
    def __init__(self, text="", chunks=(), headers=None):
        self.text = text
        self._chunks = list(chunks)
        self.headers = headers or {}

    def iter_content(self, _):
        return iter(self._chunks)

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SEARCH_HTML = """
<table>
  <tr><td>SNES</td><td>
      <a href="/vault/999999">9</a>
      <a href="/vault/1652">Super Mario World</a>
      <a href="/manual/4770">Read manual</a></td><td>USA</td></tr>
  <tr><td>NES</td><td><a href="/vault/587">Super Mario Bros.</a></td><td>USA</td></tr>
</table>
"""


def test_search_filters_by_system_and_tags_source(monkeypatch, tmp_path):
    vimm_env(monkeypatch, tmp_path)
    monkeypatch.setattr(vimm, "_get", lambda url, referer=None, stream=False: FakeResp(text=SEARCH_HTML))
    results = vimm.search("Super Mario World", "snes")
    assert len(results) == 1                       # the NES row is filtered out
    r = results[0]
    assert r["url"] == "https://vimm.example/vault/1652"   # decoration/manual links skipped
    assert r["title"] == "Super Mario World" and r["source"] == "vimm"
    assert r["console"] == "snes" and r["score"] >= 100


def test_search_without_system_map_keeps_all(monkeypatch, tmp_path):
    vimm_env(monkeypatch, tmp_path)
    monkeypatch.setattr(vimm, "_get", lambda url, referer=None, stream=False: FakeResp(text=SEARCH_HTML))
    results = vimm.search("Super Mario", "dos")     # unmapped system: no console filter
    assert {r["url"].rsplit("/", 1)[-1] for r in results} == {"1652", "587"}


def test_fetch_runs_the_browser_flow_and_returns_saved_path(monkeypatch, tmp_path):
    """fetch() drives the download through the (Playwright) browser layer and returns the
    saved path, holding and releasing the single Vimm slot. The browser flow itself is
    integration-tested on a real machine after `romcom vimm capture`."""
    vimm_env(monkeypatch, tmp_path)
    dest = tmp_path / "games"; saved = dest / "Super Mario World (USA).zip"

    def fake_with_page(headless, fn):
        dest.mkdir(parents=True, exist_ok=True); saved.write_bytes(b"PK\x03\x04data")
        return saved
    monkeypatch.setattr(vimm, "_with_page", fake_with_page)

    path = vimm.fetch({"title": "Super Mario World", "url": "https://vimm.example/vault/1652"}, dest)
    assert path == saved and path.read_bytes() == b"PK\x03\x04data"
    assert vimm._SLOT._value == 1                    # the single slot was released


def test_fetch_serializes_one_at_a_time(monkeypatch, tmp_path):
    """Vimm's defining constraint: while one fetch holds the slot, a second blocks until
    the first finishes — even though it's a different thread."""
    vimm_env(monkeypatch, tmp_path)
    monkeypatch.setattr(vimm, "_with_page", lambda headless, fn: tmp_path / "x.zip")

    vimm._SLOT.acquire()          # pretend a download is already in flight
    done = threading.Event()

    def run():
        vimm.fetch({"title": "t", "url": "https://vimm.example/vault/1652"}, tmp_path / "g")
        done.set()
    threading.Thread(target=run, daemon=True).start()
    try:
        assert not done.wait(0.3)  # blocked on the busy slot
        vimm._SLOT.release()       # the in-flight download finishes
        assert done.wait(2)        # now the second one runs
    finally:
        if vimm._SLOT._value == 0:
            vimm._SLOT.release()


# ------------------------------------------------- Cloudflare challenge reporting

class FakePage:
    """Enough of a Playwright page for the challenge probes."""
    def __init__(self, title="Vimm's Lair: Nintendo DS Vault", body="", turnstile=None, media=False):
        self._title, self._body, self._turnstile, self._media = title, body, turnstile, media

    def goto(self, _url, **_kw): return None
    def title(self): return self._title
    def inner_text(self, _sel): return self._body
    def content(self): return "<html>mediaId</html>" if self._media else "<html></html>"

    def query_selector(self, sel):
        if sel == vimm._TURNSTILE and self._turnstile is not None:
            value = self._turnstile
            return type("El", (), {"get_attribute": staticmethod(lambda _n: value)})()
        if sel == 'input[name="mediaId"]' and self._media:
            return object()
        return None


def test_an_unsolved_turnstile_widget_is_recognised_as_a_challenge():
    """The widget leaves the title untouched, which is exactly why checking the title alone
    missed it and the failure got reported as an expired session instead."""
    page = FakePage(body="Checking if you are human. This should only take a moment.", turnstile="")
    assert vimm._challenge(page) == "a Cloudflare Turnstile challenge"


def test_a_solved_turnstile_is_not_a_challenge():
    assert vimm._challenge(FakePage(turnstile="0.abcdef-token")) is None


def test_the_classic_interstitial_is_still_recognised():
    assert vimm._challenge(FakePage(title="Just a moment...")) == "a Cloudflare challenge"


def test_a_clean_page_reports_no_challenge():
    assert vimm._challenge(FakePage(body="Download  Region  USA")) is None


def test_wait_cf_returns_the_challenge_it_could_not_clear(monkeypatch):
    monkeypatch.setattr(vimm.time, "sleep", lambda _s: None)
    page = FakePage(body="checking if you are human", turnstile="")
    assert vimm._wait_cf(page, 0) == "a Cloudflare Turnstile challenge"
    assert vimm._wait_cf(FakePage(body="all good"), 0) is None


def test_fetch_blames_the_challenge_not_the_session(monkeypatch, tmp_path):
    """The whole point: a blocked page must not be reported as 'no captured session', which
    sends people off to re-run `vimm capture` against a wall that capture cannot clear."""
    vimm_env(monkeypatch, tmp_path)
    # Without this the wait spins against the real 120s vimm_timeout: sleep is stubbed
    # out, so the loop burns two minutes of wall clock doing nothing.
    monkeypatch.setenv("ROMCOM_VIMM_TIMEOUT", "0")
    from romcom.config import invalidate; invalidate()
    monkeypatch.setattr(vimm.time, "sleep", lambda _s: None)
    page = FakePage(body="Checking if you are human", turnstile="")
    monkeypatch.setattr(vimm, "_with_page", lambda headless, fn: fn(page))
    try:
        vimm.fetch({"title": "X", "url": "https://vimm.example/vault/1"}, tmp_path)
        assert False, "expected a RuntimeError"
    except RuntimeError as e:
        assert "Turnstile" in str(e) and "captured session" not in str(e)


def test_fetch_still_reports_a_decoy_when_nothing_is_blocking(monkeypatch, tmp_path):
    """The other direction must survive: an unchallenged page with no mediaId really is the
    stale-session case, and must keep saying so."""
    vimm_env(monkeypatch, tmp_path)
    monkeypatch.setattr(vimm, "_with_page", lambda headless, fn: fn(FakePage(body="ordinary vault")))
    try:
        vimm.fetch({"title": "X", "url": "https://vimm.example/vault/1"}, tmp_path)
        assert False, "expected a RuntimeError"
    except RuntimeError as e:
        assert "decoy" in str(e)
