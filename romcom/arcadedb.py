"""Arcade artwork and facts by set name, from ArcadeDB (adb.arcadeitalia.net).

libretro-thumbnails names arcade art after a MAME description, so a set is found only when
our title is spelled the way that folder spells it, version suffix and all. 515 of 882 MAME
sets on a card had a flyer that way. ArcadeDB answers by the set name itself — `gticlub`,
`mk4` — so a set it knows always gets its own flyer, in-game snap, title screen and marquee,
and the facts a front end shows beside them: year, maker, players, genre and the history
text.

Its scraper service takes many set names per request (`;`-separated) and returns, per set,
the image links and those facts. Both are cached under `<ROOT>/artwork/ArcadeDB/`: facts as
`meta/<set>.json` (a set it does not know as `{}`, re-asked after MISS_DAYS), images as
`<kind>/<set>.png|jpg`. A full-size flyer is a 1.2 MB scan; it is scaled to FLYER_HEIGHT
pixels tall and kept as a JPEG of ~100 KB, so two thousand of them fit on a card without
crowding out games.
"""
import io
import json
import time
from pathlib import Path

from .config import ROOT

BASE = "https://adb.arcadeitalia.net"
CACHE = ROOT / "artwork" / "ArcadeDB"
# Our artwork kinds -> ArcadeDB image types.
TYPES = {"boxart": "flyer", "snap": "ingame", "title": "title", "marquee": "marquee"}
BATCH = 40
MISS_DAYS = 30
FLYER_HEIGHT = 720
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) romcom-artwork/1.0"


def _session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def _meta_path(setname):
    return CACHE / "meta" / f"{setname}.json"


def cached(setname):
    """The facts cached for one set ({} when ArcadeDB does not know it), or None."""
    p = _meta_path(setname)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fresh(setname):
    p = _meta_path(setname)
    if not p.exists():
        return False
    meta = cached(setname)
    if meta:
        return True
    return time.time() - p.stat().st_mtime < MISS_DAYS * 86400


def lookup(setnames, session=None, stop=None):
    """Ask ArcadeDB about every set not already cached; returns {set: facts} for all of
    them (facts {} for a set it does not know). Network trouble leaves a batch uncached,
    to be asked again next run."""
    session = session or _session()
    todo = [s for s in dict.fromkeys(setnames) if not _fresh(s)]
    (CACHE / "meta").mkdir(parents=True, exist_ok=True)
    for i in range(0, len(todo), BATCH):
        if stop is not None and stop.is_set():
            break
        chunk = todo[i:i + BATCH]
        try:
            r = session.get(f"{BASE}/service_scraper.php",
                            params={"ajax": "query_mame", "game_name": ";".join(chunk)},
                            timeout=90)
            r.raise_for_status()
            results = r.json().get("result") or []
        except Exception:
            continue
        found = {x.get("game_name"): x for x in results if x.get("game_name")}
        for s in chunk:
            x = found.get(s) or {}
            _meta_path(s).write_text(json.dumps(x), encoding="utf-8")
    return {s: cached(s) or {} for s in setnames}


def _shrink_flyer(data):
    """A flyer scan scaled to FLYER_HEIGHT tall, as JPEG bytes; the original when Pillow
    is missing or cannot read it."""
    try:
        from PIL import Image
    except ImportError:
        return data, ".png"
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.height > FLYER_HEIGHT:
            im = im.resize((max(1, round(im.width * FLYER_HEIGHT / im.height)), FLYER_HEIGHT),
                           Image.LANCZOS)
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (0, 0, 0))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue(), ".jpg"
    except Exception:
        return data, ".png"


def image_path(setname, kind):
    """The cached image of one kind for a set, or None."""
    for ext in (".jpg", ".png"):
        p = CACHE / kind / f"{setname}{ext}"
        if p.exists() and p.stat().st_size:
            return p
    return None


def image(setname, kind, session=None, meta=None):
    """Download (once) and cache one image of a set; returns its path or None. Only kinds
    ArcadeDB's facts list a link for are asked for — a set without a marquee costs no
    request."""
    if kind not in TYPES:
        return None
    have = image_path(setname, kind)
    if have:
        return have
    meta = cached(setname) if meta is None else meta
    url = (meta or {}).get(f"url_image_{TYPES[kind]}")
    if not url:
        return None
    session = session or _session()
    try:
        r = session.get(url, timeout=90)
    except Exception:
        return None
    data = r.content
    if r.status_code != 200 or not (data.startswith(b"\x89PNG") or data[:3] == b"\xff\xd8\xff"):
        return None
    ext = ".jpg" if data[:3] == b"\xff\xd8\xff" else ".png"
    if kind == "boxart":
        data, ext = _shrink_flyer(data)
    target = CACHE / kind / f"{setname}{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    part.replace(target)
    return target


def gamelist_fields(meta):
    """ES-DE gamelist fields from ArcadeDB facts: desc, developer, releasedate, players."""
    if not meta:
        return {}
    out = {}
    hist = (meta.get("history") or "").replace("\r\n", "\n").strip()
    if hist:
        # The history text opens with a line of boilerplate ("Arcade Video game published
        # 30 years ago:") and ends with sources and trivia; the story is the first part.
        parts = [p.strip() for p in hist.split("\n\n") if p.strip()]
        if parts and parts[0].lower().startswith("arcade video game published"):
            parts = parts[1:]
        body = []
        for p in parts:
            if p.startswith("- ") and p.endswith(" -"):
                break                                  # '- TECHNICAL -' and what follows
            body.append(p)
        if body:
            out["desc"] = "\n\n".join(body)[:2000]
    if meta.get("manufacturer"):
        out["developer"] = meta["manufacturer"]
        out["publisher"] = meta["manufacturer"]
    year = str(meta.get("year") or "")[:4]
    if year.isdigit():
        out["releasedate"] = f"{year}0101T000000"
    if meta.get("players"):
        out["players"] = str(meta["players"])
    return out
