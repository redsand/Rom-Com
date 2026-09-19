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


def fetch(result, dest_dir):
    """Resolve a picked /vault page to its file and save it — one download at a time.

    Holds the single Vimm slot for the whole page→download exchange, so no second Vimm
    download can start until this one finishes. GET the vault page (carrying session
    cookies), read its hidden mediaId, then GET the download host with the page as
    Referer; the filename comes from the response's Content-Disposition.
    """
    page = result["url"]
    base = settings()["vimm_base"]
    with _SLOT:
        media = _MEDIA_ID.search(_get(page, referer=f"{base}/vault/").text)
        if not media:
            raise RuntimeError("no mediaId on Vimm vault page (gated or markup changed)")
        dl = f"{settings()['vimm_dl_base']}/?mediaId={media.group(1)}"
        with _get(dl, referer=page, stream=True) as r:
            name = None
            cd = r.headers.get("Content-Disposition", "")
            m = _CD_NAME.search(cd)
            if m:
                name = m.group(1).strip()
            name = (name or f"{result.get('title', 'rom')}.zip")
            safe = name.replace("/", "-").replace("\\", "-").replace("..", "_").strip() or "rom.zip"
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            path = dest / safe
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
    return path
