"""Box art, in-game snaps and title screens for the games on the card.

ES-DE shows a blank tile for every game it has no image for, and its on-device scraper
leaves many of them blank: it matches by the name on the card, a MAME set is called
`mslug.zip`, and a scraper account's daily quota runs out long before a library this size.
So the artwork is fetched here, on the PC, once, and written into the card beside the games.

Source: libretro-thumbnails (thumbnails.libretro.com), the free set RetroArch itself uses,
one folder per system named by its RetroArch database (`playlists.DB_NAMES`). Its file names
are No-Intro / MAME description names, so a game is matched by name: the exact libretro
spelling first, then the same title with region/revision groups dropped, preferring the
region the dump is. The server publishes a directory index per folder; that list is fetched
once (and cached a month) and matched locally, instead of guessing a URL per game.

Images land in a local cache (`<ROOT>/artwork/<db>/<Named_*>/<name>.png`) shared by every
card; `export_media` copies what a card's games need into ES-DE's layout:
`<media>/<system folder>/covers|screenshots|titlescreens/<sub folders>/<rom stem>.png` —
ES-DE mirrors the ROM folder's sub folders (the score folders, a per-core arcade folder)
under each media type.
"""
import json
import re
import shutil
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import ROOT
from .db import connect

BASE = "https://thumbnails.libretro.com"
KINDS = {"boxart": "Named_Boxarts", "snap": "Named_Snaps", "title": "Named_Titles"}
ESDE = {"boxart": "covers", "snap": "screenshots", "title": "titlescreens",
        "marquee": "marquees"}
# Marquees (the cabinet's logo strip, which ES-DE themes show as the game's logo) come
# only from ArcadeDB, so only arcade sets get them.
ALL_KINDS = ("boxart", "snap", "title", "marquee")
DEFAULT_KINDS = ("boxart", "snap", "marquee")
CACHE = ROOT / "artwork"
INDEX_DAYS = 30
MISS_DAYS = 7           # a game nothing matched is not re-tried until the index may have grown
_IN_HAND = ("FOUND", "DOWNLOADED", "VERIFIED", "NORMALIZED", "INSTALLED", "TESTED")
# Card folders that hold no games.
_NOT_GAMES = {"system", "es-de", "bios", "retroarch"}
_SKIP_EXTS = {".png", ".jpg", ".txt", ".xml", ".srm", ".sav", ".state", ".cfg", ".partial"}


# Systems the RetroArch playlists never needed a database name for, but libretro has
# thumbnails for all the same.
EXTRA_DBS = {
    "ps2": "Sony - PlayStation 2", "ps3": "Sony - PlayStation 3",
    "vita": "Sony - PlayStation Vita", "3ds": "Nintendo - Nintendo 3DS",
    "amigacd32": "Commodore - CD32", "cdi": "Philips - CD-i", "atarist": "Atari - ST",
    "atari5200": "Atari - 5200", "msx": "Microsoft - MSX", "scummvm": "ScummVM",
    "neogeopocket": "SNK - Neo Geo Pocket", "neogeopocketcolor": "SNK - Neo Geo Pocket Color",
    "fmtowns": "FM Towns", "wonderswancolor": "Bandai - WonderSwan Color",
    "pcenginecd": "NEC - PC Engine CD - TurboGrafx-CD",
}


def sources(system):
    """The libretro thumbnail databases for one of our system names, best first."""
    from .playlists import DB_NAMES
    system = (system or "").lower()
    if system == "arcade":
        return ["MAME", "FBNeo - Arcade Games"]
    name = DB_NAMES.get(system) or EXTRA_DBS.get(system)
    return [name] if name else []


def libretro_name(name):
    """The file name libretro gives a title: these characters cannot be in a file name on
    every system it ships to, so each becomes an underscore."""
    return re.sub(r'[&*/:`<>?\\|"]', "_", name or "").strip()


def _norm(name):
    """A title with its (region) [flags] groups dropped and punctuation flattened —
    'Sonic the Hedgehog (USA, Europe)' and 'Sonic The Hedgehog (World) (Rev 1)' meet."""
    base = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", name or "").lower()
    base = base.replace("&", " and ")
    base = re.sub(r"[^a-z0-9]+", " ", base).strip()
    # No-Intro files a leading article at the end ('Ninja, The'); catalogs often do not.
    m = re.match(r"(the|a|an) (.+)", base)
    return f"{m.group(2)} {m.group(1)}" if m else base


def _tags(name):
    return {t.strip().lower() for g in re.findall(r"\(([^)]*)\)", name or "")
            for t in g.split(",")}


_REGION_ORDER = ["usa", "world", "europe", "japan"]


def _pick(want, candidates):
    """The closest of several same-titled thumbnails: most region/flag tags in common with
    the dump, then the commonest regions, then the plainest name."""
    have = _tags(want)

    def key(c):
        tags = _tags(c)
        region = min((_REGION_ORDER.index(t) for t in tags if t in _REGION_ORDER),
                     default=len(_REGION_ORDER))
        return (-len(have & tags), region, len(c), c)
    return min(candidates, key=key)


class Index:
    """The thumbnail names one libretro folder holds, with a normalized-name lookup."""

    def __init__(self, names):
        self.names = set(names)
        self.by_norm = {}
        for n in names:
            self.by_norm.setdefault(_norm(n), []).append(n)

    def match(self, want):
        exact = libretro_name(want)
        if exact in self.names:
            return exact
        cands = self.by_norm.get(_norm(want))
        return _pick(want, cands) if cands else None


def _session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = "romcom-artwork/1.0"
    return s


def index(db_name, kind, session=None, refresh=False):
    """The names in one libretro folder, from the cached list when it is under a month old."""
    folder = CACHE / db_name / KINDS[kind]
    cached = folder / "_index.json"
    if not refresh and cached.exists() and time.time() - cached.stat().st_mtime < INDEX_DAYS * 86400:
        return Index(json.loads(cached.read_text(encoding="utf-8")))
    url = f"{BASE}/{urllib.parse.quote(db_name)}/{KINDS[kind]}/"
    r = (session or _session()).get(url, timeout=120)
    if r.status_code == 404:
        names = []
    else:
        r.raise_for_status()
        names = sorted({urllib.parse.unquote(h)[:-4] for h in re.findall(r'href="([^"?/][^"]*\.png)"', r.text)})
    folder.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(names), encoding="utf-8")
    return Index(names)


def _download(session, db_name, kind, name):
    target = CACHE / db_name / KINDS[kind] / f"{name}.png"
    if target.exists() and target.stat().st_size:
        return target
    url = f"{BASE}/{urllib.parse.quote(db_name)}/{KINDS[kind]}/{urllib.parse.quote(name)}.png"
    r = session.get(url, timeout=60)
    if r.status_code != 200 or not r.content.startswith(b"\x89PNG"):
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".png.part")
    part.write_bytes(r.content)
    part.replace(target)
    return target


def _wanted(db, systems=None):
    """{item_id: (system, [names to try])} for every game in hand. An arcade set is known
    to libretro by its MAME description (our title), a console game by its No-Intro file
    name — every distinct stem of its files, so a renamed dump still gets a chance."""
    q = f"""SELECT i.id, lower(i.system) system, i.title, f.path, i.external_id FROM items i
            LEFT JOIN files f ON f.matched_item_id=i.id AND COALESCE(f.content,1)=1
            WHERE COALESCE(i.is_device,0)=0 AND i.system IS NOT NULL
              AND (i.status IN ({','.join('?' * len(_IN_HAND))})
                   OR (lower(i.system)='arcade' AND i.playable=1))"""
    out = {}
    for r in db.execute(q, _IN_HAND):
        if systems and r["system"] not in systems:
            continue
        setname = ((r["external_id"] or "").split("/", 1)[-1] or None
                   if r["system"] == "arcade" else None)
        sys_, names, _ = out.setdefault(r["id"], (r["system"], [], setname))
        cands = [r["title"]]
        if r["path"] and sys_ != "arcade":
            cands.insert(0, Path(r["path"]).stem)
        for c in cands:
            if c and c not in names:
                names.append(c)
    return out


def fetch(systems=None, kinds=DEFAULT_KINDS, progress=None, stop=None, db=None,
          refresh=False, workers=8, items=None):
    """Find and cache artwork for every game in hand. Returns per-system coverage.

    Each (item, kind) ends as a row in `artwork`: a cached path, or NULL when nothing
    matched — a miss is not re-tried for a week, so a re-run spends its time on new games.
    `stop` (threading.Event) ends the run between games; what was fetched stays.

    An arcade set is asked of ArcadeDB by its set name first (`arcadedb`), libretro by
    title after; a libretro-only miss from before ArcadeDB was a source is tried again.
    `items` limits the run to those item ids — a romset build fetching for its own sets."""
    from . import arcadedb
    db = db or connect()
    systems = {s.lower() for s in systems} if systems else None
    kinds = [k for k in kinds if k in ALL_KINDS] or list(DEFAULT_KINDS)
    wanted = _wanted(db, systems)
    if items is not None:
        items = set(items)
        wanted = {k: v for k, v in wanted.items() if k in items}
    known = {(r["item_id"], r["kind"]): r for r in db.execute(
        "SELECT item_id, kind, path, source, checked_at FROM artwork")}
    now = time.time()

    def fresh_miss(row, arcade):
        if row["path"]:
            return Path(row["path"]).exists()
        if arcade and row["source"] != arcadedb.SOURCE:
            return False                     # never asked this ArcadeDB revision yet
        try:
            t = time.mktime(time.strptime(row["checked_at"][:19], "%Y-%m-%d %H:%M:%S"))
        except (TypeError, ValueError):
            return False
        return now - t < MISS_DAYS * 86400

    todo = [(iid, sys_, names, k, setname) for iid, (sys_, names, setname) in wanted.items()
            for k in kinds if (k in KINDS or setname)
            if refresh or not ((iid, k) in known and fresh_miss(known[(iid, k)], bool(setname)))]
    session = _session()
    adb_sets = sorted({t[4] for t in todo if t[4]})
    adb_meta = {}
    if adb_sets:
        if progress:
            progress(0, len(todo), f"artwork: asking ArcadeDB about {len(adb_sets):,} sets…")
        adb_meta = arcadedb.lookup(adb_sets, stop=stop)
    indexes, lock, results = {}, threading.Lock(), []
    no_source = sorted({t[1] for t in todo if not sources(t[1])})

    def idx(db_name, kind):
        key = (db_name, kind)
        with lock:
            if key not in indexes:
                try:
                    indexes[key] = index(db_name, kind, session, refresh=refresh)
                except Exception:
                    indexes[key] = Index([])      # unreachable folder: count as no match
            return indexes[key]

    adb_lock, adb_sessions = threading.Lock(), []

    def adb_session():
        # Opened on first use: a run whose sets ArcadeDB lists no image for asks nothing.
        with adb_lock:
            if not adb_sessions:
                adb_sessions.append(arcadedb._session())
            return adb_sessions[0]

    def one(job):
        iid, sys_, names, kind, setname = job
        if stop is not None and stop.is_set():
            return None
        if setname:
            meta = adb_meta.get(setname)
            p = (arcadedb.image(setname, kind, adb_session(), meta)
                 if arcadedb.has_image(meta, kind)
                 else arcadedb.image_path(setname, kind))
            if p:
                return (iid, kind, str(p), arcadedb.SOURCE)
            if kind not in KINDS:
                return (iid, kind, None, arcadedb.SOURCE)
        for db_name in sources(sys_):
            ix = idx(db_name, kind)
            for n in names:
                hit = ix.match(n)
                if hit:
                    try:
                        p = _download(session, db_name, kind, hit)
                    except Exception:
                        p = None
                    if p:
                        return (iid, kind, str(p), db_name)
        return (iid, kind, None, arcadedb.SOURCE if setname else None)

    todo = [t for t in todo if sources(t[1])]
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(one, todo):
            done += 1
            if res:
                results.append(res)
            if progress and (done % 25 == 0 or done == len(todo)):
                progress(done, len(todo), f"artwork: {done:,} of {len(todo):,} checked")
            if len(results) >= 500:
                _record(db, results)
                results.clear()
    _record(db, results)
    out = coverage(db, systems)
    out["no_source"] = no_source
    out["stopped"] = bool(stop is not None and stop.is_set())
    return out


def _record(db, results):
    if not results:
        return
    with db:
        db.executemany("""INSERT INTO artwork(item_id,kind,path,source,checked_at)
                          VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                          ON CONFLICT(item_id,kind) DO UPDATE SET path=excluded.path,
                          source=excluded.source, checked_at=CURRENT_TIMESTAMP""", results)


def coverage(db=None, systems=None):
    """{"systems": [{system, games, boxart, snap, title}]}: how many games in hand have each
    kind of image cached — the number the artwork card shows per system."""
    db = db or connect()
    wanted = _wanted(db, systems)
    have = {}
    for r in db.execute("SELECT item_id, kind FROM artwork WHERE path IS NOT NULL"):
        have.setdefault(r["item_id"], set()).add(r["kind"])
    agg = {}
    for iid, (sys_, _, _) in wanted.items():
        a = agg.setdefault(sys_, {"system": sys_, "games": 0, "boxart": 0, "snap": 0,
                                  "title": 0, "marquee": 0, "source": bool(sources(sys_))})
        a["games"] += 1
        for k in have.get(iid, ()):
            a[k] += 1
    return {"systems": sorted(agg.values(), key=lambda a: -a["games"])}


def cached_sizes(kinds=DEFAULT_KINDS, db=None):
    """{item_id: bytes} of the cached images `export_media` would copy for each game — what
    a card export sets aside so the covers fit after the games."""
    db = db or connect()
    out = {}
    for r in db.execute("SELECT item_id, kind, path FROM artwork WHERE path IS NOT NULL"):
        if r["kind"] not in kinds:
            continue
        try:
            out[r["item_id"]] = out.get(r["item_id"], 0) + Path(r["path"]).stat().st_size
        except OSError:
            pass
    return out


def export_media(dest, kinds=DEFAULT_KINDS, media_dir=None, systems=None, room_for=None,
                 progress=None, db=None, dry_run=False, entries=None):
    """Copy cached artwork for the games on a card into ES-DE's media layout. Only games
    actually on the card get images, named after the file ES-DE lists. Never downloads —
    `fetch` does that — so a card export stays an offline copy.

    `entries` ({folder: [(path ES-DE lists, item_id)]}) is the export's own record of what
    it wrote — needed once it unpacks archives, since a card file's name is then the
    archive's inner name, not a file the library knows. Without it the card is walked."""
    db = db or connect()
    dest = Path(dest)
    media = Path(media_dir) if media_dir else dest / "ES-DE" / "downloaded_media"
    kinds = [k for k in kinds if k in ALL_KINDS]
    art = {}
    for r in db.execute("SELECT item_id, kind, path FROM artwork WHERE path IS NOT NULL"):
        art.setdefault(r["item_id"], {})[r["kind"]] = r["path"]
    if not art or not dest.exists():
        return {"written": 0, "skipped": 0, "bytes": 0, "games": 0, "without_art": 0,
                "media_dir": str(media)}
    # A card file is known by its name: arcade zips by set name, the rest by the name the
    # export copied them under (the source file's own).
    by_set = {r["s"]: r["id"] for r in db.execute(
        "SELECT id, substr(external_id, instr(external_id,'/')+1) s FROM items "
        "WHERE lower(system)='arcade'")}
    by_file = {}
    for r in db.execute("SELECT path, matched_item_id FROM files "
                        "WHERE matched_item_id IS NOT NULL AND COALESCE(content,1)=1"):
        if r["matched_item_id"] in art:
            by_file.setdefault(Path(r["path"]).name.lower(), r["matched_item_id"])
    wanted = {s.lower() for s in systems} if systems else None
    written = skipped = nbytes = games = without = 0
    stopped = False
    if entries is not None:
        listed = [(fold, Path(e), iid) for fold, lst in sorted(entries.items())
                  for e, iid in lst]
    else:
        listed = _walk_card(dest, wanted, by_set, by_file)
    for fold, entry, iid in listed:
        games += 1
        imgs = art.get(iid, {})
        if not any(k in imgs for k in kinds):
            without += 1
            continue
        # ES-DE names media after the entry without its extension — for a directory
        # interpreted as a file ("Game.cue/") too.
        for k in kinds:
            src = imgs.get(k)
            if not src or not Path(src).exists():
                continue
            ext = Path(src).suffix.lower() or ".png"
            target = media / fold / ESDE[k] / entry.parent / f"{entry.stem}{ext}"
            size = Path(src).stat().st_size
            if target.exists() and target.stat().st_size == size:
                skipped += 1
                continue
            for other in (".png", ".jpg"):
                # One image per game and kind: a front end picks whichever it finds first.
                if other != ext and not dry_run:
                    try:
                        target.with_suffix(other).unlink()
                    except OSError:
                        pass
            if room_for is not None and not room_for(size):
                stopped = True
                break
            nbytes += size
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)
            written += 1
        if stopped:
            break
        if progress and games % 200 == 0:
            progress(games, 0, f"artwork: {fold}/{entry}")
    return {"written": written, "skipped": skipped, "bytes": nbytes, "games": games,
            "without_art": without, "media_dir": str(media), "stopped": stopped}


def _walk_card(dest, wanted, by_set, by_file):
    """(folder, entry, item_id) for the games found on a card by walking it — the
    standalone `artwork export`, when no export just said what it wrote."""
    out = []
    for top in sorted(p for p in dest.iterdir() if p.is_dir()):
        sys_ = top.name.lower()
        if sys_ in _NOT_GAMES or sys_.startswith(".") or (wanted and sys_ not in wanted):
            continue
        for f in sorted(top.rglob("*")):
            if not f.is_file() or f.suffix.lower() in _SKIP_EXTS:
                continue
            if sys_ == "arcade":
                if f.suffix.lower() != ".zip":
                    continue                  # a CHD beside its zip is not a game
                iid = by_set.get(f.stem)
            else:
                iid = by_file.get(f.name.lower())
            if iid:
                out.append((top.name, f.relative_to(top), iid))
    return out
