"""vimm.net (Vimm's Lair) direct-download source — an opt-in second direct fallback.

Two things make Vimm different from the romsgames source (webdl.py):

1. **One download at a time.** Vimm rejects concurrent downloads from the same IP, so
   every fetch is serialized through a single-slot semaphore (`_SLOT`). While a Vimm
   download holds the slot, SABnzbd and romsgames keep running in parallel — only Vimm
   is one-in-flight. This mirrors the per-source download lock in AutoDownloadPool.

2. **It's soft-gated.** Direct hits to a `/vault/<id>` page can 403/404 without a
   browser-shaped request. We sidestep that the lightweight way (no headless browser):
   a persistent `requests.Session` primed by first loading the vault front page, so we
   carry its cookies, plus a full browser User-Agent and a Referer chain. If Vimm ever
   hardens to a JS challenge this won't be enough and a real browser (Playwright) would
   be the fallback — hence this source is opt-in (`ROMCOM_VIMM_ENABLED`), off by default.

The flow mirrors the site's own: search list → /vault/<id> page (carries a hidden
`mediaId`) → GET the download host with that mediaId and the page as Referer.
"""
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .config import settings
from . import indexer

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# romcom system -> the labels Vimm shows in the "System" column of its results table.
# A row whose system isn't in the mapped set is dropped; unmapped systems keep every row
# and lean on the title score alone.
VIMM_SYSTEMS = {
    "nes": {"nes"}, "snes": {"snes"}, "n64": {"nintendo 64", "n64"},
    "gb": {"game boy", "gameboy"}, "gbc": {"game boy color", "gbc"},
    "gba": {"game boy advance", "gba"}, "genesis": {"genesis", "mega drive"},
    "gamegear": {"game gear"}, "mastersystem": {"master system"},
    "32x": {"32x", "sega 32x"}, "segacd": {"sega cd"}, "saturn": {"saturn"},
    "dreamcast": {"dreamcast"}, "ps1": {"ps1", "psx", "playstation"},
    "ps2": {"ps2", "playstation 2"}, "psp": {"psp"}, "gamecube": {"gamecube"},
    "wii": {"wii"}, "wiiu": {"wii u"}, "nds": {"ds", "nintendo ds"}, "3ds": {"3ds"},
    "virtualboy": {"virtual boy"}, "atari2600": {"atari 2600"},
    "atari5200": {"atari 5200"}, "atari7800": {"atari 7800"}, "lynx": {"lynx"},
    "pcengine": {"turbografx-16", "tg16", "turbografx"}, "wonderswan": {"wonderswan"},
}

_VAULT_ID = re.compile(r"^/vault/(\d+)$")
_MEDIA_ID = re.compile(r'name="mediaId"\s+value="(\d+)"')
_CD_NAME = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.I)

_SLOT = threading.Semaphore(1)   # Vimm allows exactly one download at a time
_PACE_LOCK = threading.Lock()
_LAST = [0.0]                     # monotonic timestamp of the last request
_SESSION = [None]                # lazily-built, cookie-carrying session
_SESSION_LOCK = threading.Lock()


def _session():
    """A persistent session primed with Vimm's cookies — the lightweight stand-in for a
    real browser profile that gets past the site's soft gating."""
    with _SESSION_LOCK:
        if _SESSION[0] is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            try:  # prime cookies; if it fails, the first real request still tries
                s.get(settings()["vimm_base"], timeout=settings()["vimm_timeout"])
            except Exception:
                pass
            _SESSION[0] = s
        return _SESSION[0]


def _pace():
    """Block until it's polite to send the next request (delay + random offset)."""
    s = settings()
    pause = s["vimm_delay"] + random.uniform(0, s["vimm_jitter"])
    with _PACE_LOCK:
        wait = _LAST[0] + pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.monotonic()


def _get(url, referer=None, stream=False):
    _pace()
    headers = {"Referer": referer} if referer else {}
    r = _session().get(url, headers=headers, stream=stream, timeout=settings()["vimm_timeout"])
    r.raise_for_status()
    return r


def test():
    """Connectivity check for the Settings tab — one plain request to the front page."""
    r = requests.get(settings()["vimm_base"], headers={"User-Agent": UA},
                     timeout=settings()["vimm_timeout"])
    r.raise_for_status()
    return True


def search(query, system=None):
    """Vimm vault pages matching a title, ranked the same way NZB/romsgames results are.

    Returns indexer-style results (title/url/score, plus source='vimm'); the url is the
    /vault/<id> page, not the file. Region/revision suffixes like "(USA)" trip Vimm's
    search, so they're cleaned off before querying (indexer.clean_query also keeps
    identity-bearing groups, which the old strip-everything regex deleted). Ranking
    still uses the original query, so region words count against a candidate.
    """
    def _pages():
        clean = indexer.clean_query(query)
        base = settings()["vimm_base"]
        html = _get(f"{base}/vault/?p=list&mode=search&q={quote(clean)}",
                    referer=f"{base}/vault/").text
        allowed = VIMM_SYSTEMS.get((system or "").lower())
        pages = {}
        for a in BeautifulSoup(html, "html.parser").select('a[href^="/vault/"]'):
            m = _VAULT_ID.match(a.get("href", ""))
            if not m:
                continue
            title = a.get_text(strip=True)
            # Rows carry decoration links (a "new" marker at /vault/999999, a /manual/… icon):
            # a real game link has a non-numeric title that isn't the manual.
            if not title or title.isdigit() or "manual" in title.lower():
                continue
            row = a.find_parent("tr")
            sys_cell = row.find("td").get_text(strip=True).lower() if row and row.find("td") else ""
            if allowed and not any(sys_cell == lbl or sys_cell.startswith(lbl) for lbl in allowed):
                continue
            url = f"{base}/vault/{m.group(1)}"
            pages[url] = {"title": title, "url": url, "console": sys_cell, "source": "vimm"}
        return list(pages.values())
    from . import searchcache
    return indexer.rank(searchcache.cached("vimm", f"{query}|{system or ''}", _pages), [query])


# --- Playwright: Vimm's game pages sit behind a Cloudflare challenge that serves a decoy
# (a 404 "system vault" with no mediaId) to plain HTTP and even to headless automation. The
# only reliable way in is a real browser whose persistent profile carries a cf_clearance
# cookie a human solved once (see capture()). Downloads then run headless, reusing it. ---

def _playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise RuntimeError("Vimm needs Playwright — run: pip install playwright playwright-stealth "
                           "&& playwright install chrome, then `romcom vimm capture` once")


def _with_page(headless, fn):
    """Run fn(page) in a persistent, stealthed real-Chrome context. The profile dir is where
    the captured Cloudflare clearance lives, so headless reuse looks like the same returning
    user. Everything (including the file save) must happen before the context closes."""
    sp = _playwright()
    try:                        # the stealth wrapper applies the full evasion suite per page
        from playwright_stealth import Stealth
        cm = Stealth().use_sync(sp())
    except ImportError:
        cm = sp()
    with cm as pw:
        prof = settings()["vimm_profile_dir"]
        Path(prof).mkdir(parents=True, exist_ok=True)
        launch = dict(user_agent=UA, accept_downloads=True,
                      args=["--disable-blink-features=AutomationControlled"])
        try:                    # real Chrome is far less detectable than bundled Chromium
            ctx = pw.chromium.launch_persistent_context(prof, channel="chrome", headless=headless, **launch)
        except Exception:
            ctx = pw.chromium.launch_persistent_context(prof, headless=headless, **launch)
        try:
            return fn(ctx.pages[0] if ctx.pages else ctx.new_page())
        finally:
            ctx.close()


def _wait_cf(page, timeout):
    """Wait out a Cloudflare 'Just a moment…' interstitial (a captured profile passes it
    automatically). Returns when the real page has loaded or the timeout elapses."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if "just a moment" not in (page.title() or "").lower():
            return
        time.sleep(1)


def capture(timeout=240):
    """Open Vimm in a VISIBLE browser so a human can solve the Cloudflare challenge once.
    The persistent profile keeps the clearance cookie; fetch() then reuses it headlessly.
    Run via `romcom vimm capture`. Returns True if a real Vimm page loaded before timeout."""
    base = settings()["vimm_base"]

    def _fn(page):
        page.goto(f"{base}/vault/", wait_until="domcontentloaded", timeout=60000)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            t = (page.title() or "").lower()
            if "vimm" in t and "just a moment" not in t:
                time.sleep(2)   # let cookies settle
                return True
            time.sleep(2)
        return False
    return _with_page(headless=False, fn=_fn)


def _click_download(page):
    """Click whatever the game page uses to start the download. Vimm's exact control has
    varied; try the most specific selectors first."""
    for sel in ('form[action*="download"] button[type="submit"]',
                'form[action*="download"] input[type="submit"]',
                'button:has-text("Download")', 'a:has-text("Download")',
                'form[method="post"] button[type="submit"]', 'button[type="submit"]'):
        el = page.query_selector(sel)
        if el:
            el.click()
            return
    raise RuntimeError("no download control found on the Vimm game page")


def fetch(result, dest_dir):
    """Download a Vimm game via a headless real-Chrome session reusing the captured profile —
    one at a time. If Vimm serves the decoy (no captured/expired session), raises so the
    caller records a failure and the item is retried later; run `romcom vimm capture` to fix."""
    page_url = result["url"]
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    def _fn(page):
        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        _wait_cf(page, settings()["vimm_timeout"])
        # The real game page carries a mediaId; the decoy (system vault) does not.
        if not (page.query_selector('input[name="mediaId"]') or "mediaid" in (page.content() or "").lower()):
            raise RuntimeError("Vimm served a decoy page — no captured session (run `romcom vimm capture`) or it expired")
        with page.expect_download(timeout=int(settings()["vimm_timeout"]) * 1000) as di:
            _click_download(page)
        dl = di.value
        name = dl.suggested_filename or f"{result.get('title', 'rom')}.zip"
        safe = name.replace("/", "-").replace("\\", "-").replace("..", "_").strip() or "rom.zip"
        out = dest / safe
        dl.save_as(str(out))
        return out

    with _SLOT:                 # Vimm: exactly one browser download at a time
        return _with_page(headless=True, fn=_fn)
