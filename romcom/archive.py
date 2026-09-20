"""Internet Archive (archive.org) direct-download source — the surviving r-roms mirror.

archive.org is open (no gating, clean JSON APIs), but a plain title search is noisy: it
returns Winamp skins, hacks, and random uploads alongside real ROMs. The trick is to match
at the FILE level, not the item level — search software items for the title, list each
item's files, keep only ROM-extension files, and rank those filenames against the title.
The junk falls away (skins aren't ROM files; an unrelated item's ROMs share no tokens with
the query and the strict ranker refuses them).

It won't surface whole No-Intro/Redump SET items by title (those would need a curated
system→identifier map) — it catches individual-game items and matchable bulk uploads. It's
opt-in (`ROMCOM_ARCHIVE_ENABLED`) and, being un-gated, needs only light politeness pacing.
Downloads stream straight from the open /download/ path.
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

UA = "romcom/1.0 (+https://github.com/redsand/Rom-Com)"

# Extensions that are actual ROM/disc content (not box art, manuals, or skins).
ROM_EXTS = (".zip", ".7z", ".rar", ".iso", ".bin", ".cue", ".chd", ".rvz", ".wbfs", ".cso",
            ".nes", ".sfc", ".smc", ".fig", ".gba", ".gb", ".gbc", ".n64", ".z64", ".v64",
            ".md", ".gen", ".smd", ".sms", ".gg", ".pce", ".ngp", ".ngc", ".ws", ".wsc",
            ".a26", ".a78", ".lnx", ".col", ".int", ".vec", ".d64", ".adf", ".dsk", ".nds",
            ".3ds", ".cia", ".gcm", ".gcz", ".nsp", ".xci", ".pbp", ".cdi", ".gdi")

_PACE_LOCK = threading.Lock()
_LAST = [0.0]
_TOKEN = re.compile(r"[a-z0-9]+")

# The per-item file cap and token pre-filter keep a full-set item (which can carry tens of
# thousands of files) from blowing up memory/CPU on every search.
_MAX_FILES_PER_ITEM = 200
_SEARCH_ROWS = 8


def _pace():
    s = settings()
    pause = s["archive_delay"] + random.uniform(0, s["archive_delay"] / 2)
    with _PACE_LOCK:
        wait = _LAST[0] + pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.monotonic()


def _get(url, params=None, stream=False):
    _pace()
    r = requests.get(url, params=params, headers={"User-Agent": UA}, stream=stream,
                     timeout=settings()["archive_timeout"])
    r.raise_for_status()
    return r


def test():
    r = requests.get(settings()["archive_base"], headers={"User-Agent": UA},
                     timeout=settings()["archive_timeout"])
    r.raise_for_status()
    return True


def search(query, system=None):
    """ROM files on archive.org matching a title, ranked like every other source. Searches
    software items for the title, then keeps title-matching ROM files from each item."""
    def _pages():
        base = settings()["archive_base"]
        clean = indexer.clean_query(query)
        qtokens = set(_TOKEN.findall(clean.lower()))
        r = _get(f"{base}/advancedsearch.php", params={
            "q": f"({clean}) AND mediatype:software", "fl[]": "identifier",
            "rows": _SEARCH_ROWS, "output": "json"})
        idents = [d.get("identifier") for d in r.json().get("response", {}).get("docs", [])
                  if d.get("identifier")]
        out = {}
        for ident in idents:
            try:
                files = _get(f"{base}/metadata/{ident}").json().get("files", [])
            except Exception:
                continue
            kept = 0
            for f in files:
                name = f.get("name", "")
                low = name.lower()
                if not low.endswith(ROM_EXTS):
                    continue
                # quick pre-filter: the filename must share a token with the query, so a
                # full-set item doesn't add thousands of unrelated roms.
                if qtokens and not (qtokens & set(_TOKEN.findall(low))):
                    continue
                url = f"{base}/download/{ident}/{quote(name)}"
                out[url] = {"title": name.rsplit("/", 1)[-1], "url": url, "console": ident,
                            "size": int(f.get("size") or 0), "source": "archive"}
                kept += 1
                if kept >= _MAX_FILES_PER_ITEM:
                    break
        return list(out.values())

    from . import searchcache
    return indexer.rank(searchcache.cached("archive", f"{query}|{system or ''}", _pages), [query])


def fetch(result, dest_dir):
    """Stream a picked archive.org file straight to disk (open host, no gating)."""
    url = result["url"]
    name = unquote(url.rsplit("/", 1)[-1]) or f"{result.get('title', 'rom')}.zip"
    safe = name.replace("/", "-").replace("\\", "-").replace("..", "_").strip() or "rom.zip"
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / safe
    with _get(url, stream=True) as r, open(path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)
    return path
