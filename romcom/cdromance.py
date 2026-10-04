"""cdromance.org direct-download source — the opt-in fallback after romsgames/Vimm.

The site is plain WordPress over plain HTTP: no Cloudflare gate, no captcha, no
one-at-a-time download slot. Its own download button runs a small AJAX POST
(`post_id` to the cdr-main plugin, flagged X-Requested-With) that answers with a
table of direct zip links, so we replay exactly that — search page, game page,
AJAX, file — with every request spaced `ROMCOM_CDR_DELAY` seconds plus
`ROMCOM_CDR_JITTER` random, the same welcome-guest pacing the other scrapes use.
"""
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from .config import settings
from . import indexer

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# romcom system -> cdromance platform slugs (the first path segment of each game
# page, e.g. https://cdromance.org/snes-rom/super-metroid/). Systems with no entry
# are searched without a platform filter and rely on the title score alone.
CDR_SYSTEMS = {
    "3do": ("3do-iso",), "dreamcast": ("dc-iso",), "gamegear": ("game-gear",),
    "gamecube": ("gamecube",), "gb": ("gameboy-roms",), "gba": ("gba-roms",),
    "gbc": ("gameboy-color-roms",), "genesis": ("sega_genesis_roms",),
    "msdos": ("msdos",), "msx": ("msx-roms",), "n64": ("n64-roms",),
    "nds": ("nds-roms",), "neogeocd": ("neo-geo-cd",), "neogeopocket": ("neo-geo-pocket",),
    "nes": ("nes-roms",), "pc98": ("pc-98",), "pcengine": ("turbografx-16",),
    "pcfx": ("pc-fx",), "ps1": ("psx-iso",), "ps2": ("ps2-iso",), "psp": ("psp",),
    "psvita": ("vita",), "scummvm": ("scummvm",), "segacd": ("sega_cd_isos",),
    "snes": ("snes-rom",), "turbografx16": ("turbografx-16",), "turbografxcd": ("turbografx-cd",),
    "windows": ("windows",), "wii": ("wii-iso",), "wonderswan": ("wonderswan",),
}

# Two-segment paths that are WordPress chrome, not game pages.
_NON_GAME = {"category", "tag", "author", "page", "feed", "wp-content", "wp-json", "wp-admin"}

# game-page anchors: <a href="[host]/<platform>/<game>/">title text</a>. The host is
# optional so relative hrefs count too; anything on another host is skipped.
_LINK = re.compile(r'<a\s[^>]*href="(?:https?://([^/"\s]+))?(/[a-z0-9_-]+/[a-z0-9_-]+)/?"'
                   r'[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WRAPPER = re.compile(r'id="acf-content-wrapper"')
_POST_ID = re.compile(r'data-id="(\d+)"')
_POST_ID2 = re.compile(r'data-post-id="(\d+)"')
# The AJAX table's download buttons, whatever attribute order they come in — the
# invariant is download.php; the href is the direct file.
_DL_URL = re.compile(r'href="([^"]*download\.php[^"]*)"')
_AJAX_ERR = re.compile(r"(error[^<\n]{0,40})")

_LOCK = threading.Lock()
_LAST = [0.0]  # monotonic timestamp of the last request — shared across threads


def _pace():
    """Block until it's polite to send the next request (delay + random offset)."""
    s = settings()
    pause = s["cdromance_delay"] + random.uniform(0, s["cdromance_jitter"])
    with _LOCK:
        wait = _LAST[0] + pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.monotonic()


def _request(method, url, referer=None, origin=None, xhr=False, stream=False, data=None):
    _pace()
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    if origin:
        headers["Origin"] = origin
    if xhr:
        # The AJAX endpoint refuses a plain POST with "error: x012" — this header
        # is what the site's own cdr-dl.js sends, so we send it too.
        headers["X-Requested-With"] = "XMLHttpRequest"
    r = requests.request(method, url, headers=headers, data=data, stream=stream,
                         timeout=settings()["cdromance_timeout"])
    r.raise_for_status()
    return r


def test():
    """One-off connectivity check for the Settings tab — a single request to the
    site's front page, outside the scraper flow, so it doesn't wait out the
    pacing delay (a human clicking "test" is its own rate limit)."""
    r = requests.get(settings()["cdromance_base"], headers={"User-Agent": UA},
                     timeout=settings()["cdromance_timeout"])
    r.raise_for_status()
    return True


def search(query, system=None):
    """Game pages matching a title, ranked against it the same way NZB results are.

    Returns indexer-style results (title/url/score) so the acquirer's pick logic
    applies unchanged; the url is the game page, not the file. No-Intro/Redump
    title suffixes are cleaned off before querying (see indexer.clean_query), but
    results are ranked against the ORIGINAL query so the region/revision words
    still count against a candidate that lacks them.
    """
    def _pages():
        base = settings()["cdromance_base"]
        host = urlparse(base).netloc.lower().removeprefix("www.")
        clean = indexer.clean_query(query)
        html = _request("GET", f"{base}/?s={quote(clean)}").text
        allowed = CDR_SYSTEMS.get((system or "").lower())
        pages = {}
        for h, path, text in _LINK.findall(html):
            if h and h.lower().removeprefix("www.") != host:
                continue  # a stray absolute link to some other site
            plat, game = path.strip("/").split("/", 1)
            if plat in _NON_GAME:
                continue
            if allowed and plat not in allowed:
                continue
            url = f"{base}{path}/"
            title = " ".join(_TAG.sub("", text).split())
            if not title:  # anchor with no text — fall back to the URL slug
                title = game.replace("-", " ").strip()
            pages[url] = {"title": title, "url": url, "console": plat}
        return list(pages.values())
    from . import searchcache
    return indexer.rank(searchcache.cached("cdromance", f"{query}|{system or ''}", _pages), [query])


def fetch(result, dest_dir):
    """Resolve a picked search result to the direct zip and save it.

    Mirrors the site's own cdr-dl.js: the game page carries the post id, the
    "Show Links" AJAX answers with download.php rows, and the picked row is a
    plain streamed GET with the game page as Referer.
    """
    base = settings()["cdromance_base"]
    page = result["url"]
    html = _request("GET", page).text
    m = _WRAPPER.search(html)
    pid = _POST_ID.search(html, m.end(), m.end() + 600) if m else None
    pid = pid or _POST_ID.search(html) or _POST_ID2.search(html)
    if not pid:
        raise RuntimeError("no post id on game page")
    r = _request("POST", f"{base}/wp-content/plugins/cdr-main/public/ajax.php",
                 referer=page, origin=base, xhr=True, data={"post_id": pid.group(1)})
    links = _DL_URL.findall(r.text)
    if not links:
        err = _AJAX_ERR.search(r.text)
        raise RuntimeError((err.group(1) if err else "no download link on page").strip())
    href = links[0].replace("&amp;", "&")  # HTML-escaped ampersands break query parsing
    name = (parse_qs(urlparse(href).query).get("file") or [""])[0]
    name = unquote(name)
    safe = name.replace("/", "-").replace("\\", "-").replace("..", "_").strip() or "rom.zip"
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / safe
    with _request("GET", href, referer=page, stream=True) as r, open(path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)
    return path