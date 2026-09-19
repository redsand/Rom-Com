"""romsgames.net direct-download source — the fallback when the NZB indexer has nothing.

The site's own flow is three steps: search page → ROM page → a one-time download
URL (the captcha in its page JS is a client-side gate only). We replay exactly
that flow. To stay a welcome guest, every request — search, page, or file — is
spaced by ROMCOM_WEBDL_DELAY seconds plus up to ROMCOM_WEBDL_JITTER of random
offset, so a sweep of many items trickles out at a human pace instead of
tripping their rate limiting.
"""
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote

import requests

from .config import settings
from . import indexer

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# romcom system -> romsgames console slugs (the prefix of each ROM page URI,
# e.g. "gameboy-advance-rom-pokemon-firered-version"). Systems with no entry are
# searched without a console filter and rely on the title score alone.
SYSTEM_SLUGS = {
    "32x": ("sega-32x",), "3ds": ("nintendo-3ds",), "amiga": ("amiga-500",),
    "amstradcpc": ("amstrad-cpc",), "arcade": ("mame-037b11",),
    "atari2600": ("atari-2600",), "atari5200": ("atari-5200-supersystem",),
    "atari7800": ("atari-7800-prosystem",), "atarist": ("atari-st",),
    "c64": ("commodore-64",), "dreamcast": ("dreamcast",), "gamecube": ("gamecube",),
    "gamegear": ("game-gear",), "gb": ("gameboy",), "gba": ("gameboy-advance",),
    "gbc": ("gameboy-color",), "genesis": ("sega-genesis",), "lynx": ("atari-lynx",),
    "mastersystem": ("sega-master-system",), "msx": ("msx-2",), "msx2": ("msx-2",),
    "n64": ("nintendo-64",), "nds": ("nintendo-ds",),
    "neogeopocket": ("neo-geo-pocket",), "neogeopocketcolor": ("neo-geo-pocket-color",),
    "nes": ("nintendo",), "ps1": ("playstation",), "ps2": ("playstation-2",),
    "psp": ("playstation-portable",), "saturn": ("sega-saturn",),
    "snes": ("super-nintendo",), "virtualboy": ("nintendo-virtual-boy",),
    "wii": ("nintendo-wii",), "wonderswan": ("wonderswan",), "x68000": ("sharp-x68000",),
}

_PAGE_LINK = re.compile(r'href="/([a-z0-9-]+?)-rom-([a-z0-9-]+)/"')
_MEDIA_ID = re.compile(r'data-media-id="(\d+)"')

_LOCK = threading.Lock()
_LAST = [0.0]  # monotonic timestamp of the last request — shared across threads


def _pace():
    """Block until it's polite to send the next request (delay + random offset)."""
    s = settings()
    pause = s["webdl_delay"] + random.uniform(0, s["webdl_jitter"])
    with _LOCK:
        wait = _LAST[0] + pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.monotonic()


def _request(method, url, referer=None, json_accept=False, stream=False, data=None):
    _pace()
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    if json_accept:
        headers["Accept"] = "application/json"
    r = requests.request(method, url, headers=headers, data=data, stream=stream,
                         timeout=settings()["webdl_timeout"])
    r.raise_for_status()
    return r


def test():
    """One-off connectivity check for the Settings tab — a single request to the
    site's front page, outside the scraper flow, so it doesn't wait out the
    pacing delay (a human clicking "test" is its own rate limit)."""
    r = requests.get(settings()["webdl_base"], headers={"User-Agent": UA},
                     timeout=settings()["webdl_timeout"])
    r.raise_for_status()
    return True


def search(query, system=None):
    """ROM pages matching a title, ranked against it the same way NZB results are.

    Returns indexer-style results (title/url/score) so the acquirer's pick logic
    applies unchanged; the url is the ROM page, not the file. No-Intro/Redump
    titles carry "(USA)"-style suffixes the site's search chokes on — those are
    cleaned off before querying, while identity-bearing groups are kept as bare
    words (see indexer.clean_query; stripping them wholesale searches for a
    generic prefix that many unrelated pages also match).

    The results are ranked against the ORIGINAL query, not the cleaned one, so the
    region/revision words still count against a candidate: a page that lacks them
    is a worse match than one that has them.
    """
    clean = indexer.clean_query(query)
    base = settings()["webdl_base"]
    html = _request("GET", f"{base}/search/?q={quote(clean)}").text
    allowed = SYSTEM_SLUGS.get((system or "").lower())
    pages = {}
    for console, slug in _PAGE_LINK.findall(html):
        if allowed and console not in allowed:
            continue
        url = f"{base}/{console}-rom-{slug}/"  # search cards repeat each link; keep one
        pages[url] = {"title": slug.replace("-", " ").strip(), "url": url, "console": console}
    return indexer.rank(list(pages.values()), [query])


def fetch(result, dest_dir):
    """Resolve a picked search result to the one-time file URL and save it.

    Mirrors the site's own download.js: POST the page URL with its mediaId and an
    Accept: application/json header, then GET the returned downloadUrl with the
    page as Referer (the static host rejects referer-less requests).
    """
    page = result["url"]
    media_id = _MEDIA_ID.search(_request("GET", page).text)
    if not media_id:
        raise RuntimeError("no mediaId on ROM page")
    r = _request("POST", page + "?download", referer=page, json_accept=True,
                 data={"mediaId": media_id.group(1)})
    info = r.json()
    dl_url, name = info.get("downloadUrl"), unquote(info.get("downloadName") or "rom.zip")
    if not dl_url:
        raise RuntimeError(info.get("asset", {}).get("maintenanceMessage") or "no downloadUrl returned")

    safe = name.replace("/", "-").replace("\\", "-").replace("..", "_").strip() or "rom.zip"
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / safe
    with _request("GET", dl_url, referer=page, stream=True) as r, open(path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)
    return path