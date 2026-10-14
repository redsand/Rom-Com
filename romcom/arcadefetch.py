"""Arcade sets, bios boards, device chips and CHDs fetched by setname, checked chip by chip.

The title-driven acquirer cannot fetch arcade: an indexer search for "NBA Hang Time" finds
nothing that is a MAME 0.289 `nbahangt` zip, and a whole set is the wrong unit anyway —
a broken owned set usually lacks one chip MAME's current dump added (`nbahangt` wants
`459_nba_hang_time_u64.u64`), a device's chips (`m50dass`), a bios, or a CHD.

So this works from the dat. For a set it lists every chip the set needs — its own, its
bios parents', and every device it pulls in, recursively — and every CHD, keeps the ones
the library has no file for (by SHA1 or CRC, as `arcadehealth.judge` counts them), and
looks for exactly those on archive.org, in this order:

1. `arcade_<set>` — the per-machine items (one per MAME set: its zip, its devices' zips,
   its CHDs), tried for the set and then each machine that owns a missing chip;
2. `mame-0.289` — the current release as one zip per set;
3. the 0.279 and 0.278 merged full sets, one member zip at a time (archive.org serves a
   single member out of a zip; a clone's chips live in its parent's zip there).

CHDs come from the `arcade_<set>` items, or the 0.278 working-only CHD collection. A zip is
downloaded whole (they are small) and only the chips whose CRC is wanted are written, each
checked against the dat's SHA1. A CHD is chosen by the SHA1 in its header, read with a
16-byte-aligned range request before the gigabytes are fetched. Everything lands in
`<download dir>/arcade-patches/<owner set>/` under the dat's own chip and disk names, and
that folder is scanned, so the health check and every romset build see it at once.
"""
import json
import os
import re
import tempfile
import threading
import time
import zipfile
import hashlib
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "DAT" / "arcade-sources"
IA = "https://archive.org"
UA = "romcom/1.0 (+https://github.com/redsand/Rom-Com)"
CURRENT_ITEM = "mame-0.289"
MERGED = [("mame-roms-0.279-merged", "MAME ROMs 0.279 (merged).zip"),
          ("mame-0.278-roms-merged", "MAME 0.278 ROMs (merged).zip")]
# Full non-merged sets kept as one real file per set (fast, whole downloads; a clone's zip
# carries every chip it needs).
NON_MERGED = [("mame-0.264-roms-non-merged", "MAME 0.264 ROMs (non-merged)")]
CHD_COLLECTION = ("mame-0.278-chds-roms-working-only", "CHDs")
# Planet Emulation's MAME merged sets (0.285 in 2026-10): one page per set listing every
# chip's CRC, so a zip is downloaded only when it holds a chip that is wanted.
PLANETEMU = "https://www.planetemu.net"
PLANETEMU_SECTION = "mame-roms-merged"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
LISTING_DAYS = 7
_lock = threading.Lock()
_mem = {}


# ---------------------------------------------------------------- archive.org access

def _session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def _get(s, url, **kw):
    """GET with a few retries: archive.org answers 503 when a datanode is busy."""
    last, timeout = None, kw.pop("timeout", 120)
    for i in range(4):
        try:
            r = s.get(url, timeout=timeout, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                last = OSError(f"HTTP {r.status_code} from {url}")
                time.sleep(2 + 3 * i)
                continue
            return r
        except Exception as e:        # connection reset mid-way: try again
            last = e
            time.sleep(2 + 3 * i)
    raise OSError(f"archive.org did not answer for {url}: {last}")


def _cached(key, loader):
    """A listing from memory, then from CACHE (under LISTING_DAYS old), then the network."""
    with _lock:
        if key in _mem:
            return _mem[key]
    f = CACHE / (re.sub(r"[^A-Za-z0-9._-]+", "_", key) + ".json")
    if f.exists() and time.time() - f.stat().st_mtime < LISTING_DAYS * 86400:
        data = json.loads(f.read_text(encoding="utf-8"))
    else:
        data = loader()
        CACHE.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")
    with _lock:
        _mem[key] = data
    return data


def item_files(s, item):
    """{file name: {size, sha1}} of an archive.org item; {} when there is no such item."""
    def load():
        r = _get(s, f"{IA}/metadata/{item}")
        d = r.json() if r.ok else {}
        if d.get("is_dark") or not d.get("files"):
            return {}
        return {f["name"]: {"size": int(f.get("size") or 0), "sha1": f.get("sha1", "")}
                for f in d["files"]}
    return _cached(f"files_{item}", load)


def zip_members(s, item, container):
    """Member names of a zip on archive.org (its browse listing), as a list."""
    def load():
        r = _get(s, f"{IA}/download/{item}/{quote(container)}/", timeout=300)
        if not r.ok:
            return []
        names = re.findall(r'href="[^"]*/' + re.escape(quote(container)).replace("\\%", "%")
                           + r'/([^"/]+)"', r.text)
        from urllib.parse import unquote
        return sorted({unquote(n) for n in names})
    return _cached(f"zip_{item}_{container}", load)


def _url(item, *parts):
    return f"{IA}/download/{item}/" + "/".join(quote(p) for p in parts)


# ---------------------------------------------------------------- what a set lacks

def needs(setname, machines, have_sha, have_crc, chds):
    """{"chips": {crc: (chip name, sha1, owner machine)}, "disks": [{name, sha1, owner}]}
    the library lacks for `setname` to run: its own chips, its bios parents', and the
    chips of every device it references (recursively)."""
    from .arcadehealth import needed_parents, find_chd
    chips, disks, seen = {}, [], set()

    def walk(name, with_devices=True):
        if name in seen or name not in machines:
            return
        seen.add(name)
        m = machines[name]
        for sha, crc, chip in m["roms"]:
            if (sha and sha in have_sha) or (crc and crc in have_crc):
                continue
            if crc and crc not in chips:
                chips[crc] = (chip, sha, name)
        for d in m["disks"]:
            if not find_chd(d, chds):
                disks.append({"name": d["name"], "sha1": d["sha1"], "owner": name})
        if with_devices:
            for dev in m["devices"]:
                walk(dev)

    walk(setname)
    for parent in needed_parents(machines, setname):
        walk(parent)
    return {"chips": chips, "disks": disks}


def _owners(setname, machines, want):
    """Machines whose zips may hold the wanted chips, best first: the set, the machines
    that own a missing chip, then the set's parent chain (merged sets keep a clone's chips
    in its parent's zip)."""
    out = [owner for _, _, owner in want["chips"].values()]
    out += [d["owner"] for d in want["disks"]]
    out.append(setname)
    m = machines.get(setname)
    while m and (m.get("cloneof") or m.get("romof")):
        nxt = m.get("cloneof") or m.get("romof")
        if nxt in out and nxt != setname:
            out.append(nxt)
            break
        out.append(nxt)
        m = machines.get(nxt)
    seen, uniq = set(), []
    for n in out:
        if n and n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


_pe = {"session": None, "last": 0.0, "pages": {}}
_pe_lock = threading.Lock()


def _pe_session():
    import requests
    if _pe["session"] is None:
        s = requests.Session()
        s.headers.update({"User-Agent": BROWSER_UA,
                          "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        _pe["session"] = s
    return _pe["session"]


def _pe_pace(gap=3.0):
    wait = _pe["last"] + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _pe["last"] = time.monotonic()


def planetemu_page(setname):
    """{"id", "filename", "crcs"} of Planet Emulation's page for a MAME set, or None."""
    with _pe_lock:
        if setname in _pe["pages"]:
            return _pe["pages"][setname]
        _pe_pace()
        try:
            r = _pe_session().get(f"{PLANETEMU}/en/rom/{PLANETEMU_SECTION}/{quote(setname)}",
                                  timeout=60)
        except Exception:
            return None
        page = None
        m = re.search(r'class="downloadForm"[^>]*data-filename="([^"]+)"[^>]*>\s*'
                      r'<input type="hidden" name="id" value="(\d+)"', r.text) if r.ok else None
        if m:
            crcs = set(re.findall(r'rom-detail__files-hash">([0-9a-fA-F]{8})<', r.text))
            page = {"id": m.group(2), "filename": m.group(1),
                    "crcs": {c.lower() for c in crcs},
                    "url": f"{PLANETEMU}/en/rom/{PLANETEMU_SECTION}/{quote(setname)}"}
        _pe["pages"][setname] = page
        return page


def planetemu_download(page):
    """The zip a Planet Emulation page offers, as bytes. Their download quota is asked
    first, as their own page does; a refusal waits out its retryAfter (up to 2 minutes)."""
    with _pe_lock:
        s = _pe_session()
        for _ in range(3):
            _pe_pace()
            q = s.get(f"{PLANETEMU}/download/quota", timeout=60,
                      headers={"Referer": page["url"]})
            info = q.json() if q.ok else {}
            if info.get("blocked"):
                time.sleep(min(int(info.get("retryAfter") or 30), 120))
                continue
            data = {"id": page["id"], "download": f"Download {page['filename']}"}
            if info.get("pass"):
                data["pass"] = info["pass"]
            r = s.post(f"{PLANETEMU}/download/rom", data=data, timeout=900,
                       headers={"Referer": page["url"]})
            if r.ok and r.content[:2] == b"PK":
                return r.content
            if r.status_code == 429:
                time.sleep(60)
                continue
            raise OSError(f"Planet Emulation answered {r.status_code} for {page['filename']}")
    raise OSError(f"Planet Emulation's download quota stayed closed for {page['filename']}")


def _zip_sources(s, owners, machines, wanted=None):
    """(label, url) of every zip worth opening for these owners, in source order. A url
    "planetemu:<set>" is fetched through Planet Emulation's download form."""
    out = []
    for o in owners:
        files = item_files(s, f"arcade_{o}")
        for name in sorted(files, key=lambda n: (n != f"{o}.zip", n)):
            if name.lower().endswith(".zip") and "/" not in name:
                out.append((f"arcade_{o}/{name}", _url(f"arcade_{o}", name)))
    current = item_files(s, CURRENT_ITEM)
    for o in owners:
        if f"{o}.zip" in current:
            out.append((f"{CURRENT_ITEM}/{o}.zip", _url(CURRENT_ITEM, f"{o}.zip")))
    for o in owners:
        root = (machines.get(o) or {}).get("cloneof") or o
        page = planetemu_page(root) if wanted else None
        if page and page["crcs"] & set(wanted):
            out.append((f"planetemu/{page['filename']}", f"planetemu:{root}"))
    for item, folder in NON_MERGED:
        files = item_files(s, item)
        for o in owners:
            name = f"{folder}/{o}.zip"
            if name in files:
                out.append((f"{item}/{o}.zip", _url(item, folder, f"{o}.zip")))
    for item, container in MERGED:
        members = set(zip_members(s, item, container))
        for o in owners:
            root = o
            m = machines.get(o)
            if m and m.get("cloneof"):
                root = m["cloneof"]
            for z in dict.fromkeys((f"{o}.zip", f"{root}.zip")):
                if z in members:
                    out.append((f"{item}/{z}", _url(item, container, z)))
    seen, uniq = set(), []
    for lab, u in out:
        if u not in seen:
            seen.add(u)
            uniq.append((lab, u))
    return uniq


def _chd_sources(s, owners):
    """(label, url) of CHD files that may be a wanted disk."""
    out = []
    for o in owners:
        for name in item_files(s, f"arcade_{o}"):
            if name.lower().endswith(".chd") and "/" not in name:
                out.append((f"arcade_{o}/{name}", _url(f"arcade_{o}", name)))
    item, folder = CHD_COLLECTION
    files = item_files(s, item)
    for o in owners:
        z = f"{folder}/{o}.zip"
        if z in files:
            for inner in zip_members(s, item, z):
                if inner.lower().endswith(".chd"):
                    out.append((f"{item}/{z}/{inner}", _url(item, folder, f"{o}.zip", inner)))
    return out


def _chd_header_sha1(s, url):
    r = _get(s, url, headers={"Range": "bytes=0-123"}, stream=True)
    if r.status_code not in (200, 206):
        return None
    head = r.raw.read(124)
    r.close()
    if len(head) < 100 or head[:8] != b"MComprHD":
        return None
    at = {3: 80, 4: 48, 5: 84}.get(int.from_bytes(head[12:16], "big"))
    return head[at:at + 20].hex() if at else None


# ---------------------------------------------------------------- fetching

def dest_root():
    from .config import settings
    base = (settings().get("download_dir") or "").strip()
    return Path(base) / "arcade-patches" if base else ROOT / "arcade-patches"


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)


def fetch_set(setname, ctx, s=None, progress=None):
    """Fetch what `setname` lacks. `ctx` is a `context()` (shared across sets, so a chip
    one set brought is not fetched again for the next). Returns a report."""
    s = s or _session()
    machines, have_sha, have_crc, chds = ctx["machines"], ctx["sha"], ctx["crc"], ctx["chds"]
    rep = {"set": setname, "chips": [], "disks": [], "sources": [], "missing": [], "errors": []}
    if setname not in machines:
        rep["errors"].append("not in the MAME dat")
        return rep
    want = needs(setname, machines, have_sha, have_crc, chds)
    if not want["chips"] and not want["disks"]:
        return rep
    owners = _owners(setname, machines, want)
    root = ctx["dest"]
    chips = dict(want["chips"])
    if chips:
        for label, url in _zip_sources(s, owners, machines, chips):
            if not chips:
                break
            data = ctx["zips"].get(url)
            if data is None:
                if progress:
                    progress(f"{setname}: {label}")
                try:
                    if url.startswith("planetemu:"):
                        data = planetemu_download(planetemu_page(url.split(":", 1)[1]))
                    else:
                        r = _get(s, url, timeout=600)
                        data = r.content if r.ok else b""
                except OSError as e:
                    rep["errors"].append(str(e))
                    continue
                ctx["zips"][url] = data
            if not data:
                continue
            try:
                z = zipfile.ZipFile(__import__("io").BytesIO(data))
                infos = z.infolist()
            except zipfile.BadZipFile:
                rep["errors"].append(f"{label}: not a whole zip")
                continue
            used = False
            for info in infos:
                crc = f"{info.CRC:08x}"
                if crc not in chips:
                    continue
                name, sha, owner = chips[crc]
                blob = z.read(info)
                if sha and hashlib.sha1(blob).hexdigest() != sha:
                    continue                        # same CRC, different chip: keep looking
                _write(root / owner / name, blob)
                have_crc.add(crc)
                if sha:
                    have_sha.add(sha)
                rep["chips"].append(f"{owner}/{name}")
                del chips[crc]
                used = True
            if used:
                rep["sources"].append(label)
    for d in want["disks"]:
        target = root / d["owner"] / f"{d['name']}.chd"
        if target.exists():
            continue
        for label, url in _chd_sources(s, list(dict.fromkeys([d["owner"], *owners]))):
            try:
                got = _chd_header_sha1(s, url)
            except OSError as e:
                rep["errors"].append(str(e))
                continue
            if got != d["sha1"]:
                continue
            if progress:
                progress(f"{setname}: {label} (CHD)")
            part = target.with_name(target.name + ".part")
            part.parent.mkdir(parents=True, exist_ok=True)
            try:
                with _get(s, url, stream=True, timeout=600) as r:
                    r.raise_for_status()
                    with open(part, "wb") as fh:
                        for chunk in r.iter_content(1 << 20):
                            fh.write(chunk)
                from .arcadehealth import chd_sha1
                if chd_sha1(part) != d["sha1"]:
                    raise OSError(f"{label}: the CHD that arrived is not {d['name']}")
                os.replace(part, target)
                ctx["chds"].setdefault(d["name"].lower(), []).append(str(target))
                rep["disks"].append(f"{d['owner']}/{d['name']}")
                rep["sources"].append(label)
                break
            except OSError as e:
                rep["errors"].append(str(e))
                try:
                    part.unlink()
                except OSError:
                    pass
        else:
            pass
    left = needs(setname, machines, have_sha, have_crc, ctx["chds"])
    rep["missing"] = ([f"{o}/{n}" for n, _, o in left["chips"].values()] +
                      [f"{d['owner']}/{d['name']}.chd" for d in left["disks"]])
    return rep


def context(db=None, dest=None):
    from . import mameset
    from .arcadehealth import parse, _have, chd_index
    from .db import connect
    db = db or connect()
    sha, crc = _have(db)
    return {"machines": parse(mameset.dat_path("mame")), "sha": sha, "crc": crc,
            "chds": chd_index(db), "dest": Path(dest) if dest else dest_root(), "zips": {}}


def finish(ctx, db=None, progress=None):
    """Scan what landed so the library (and the health check) know it, then re-judge."""
    from .scanner import scan
    from . import arcadehealth
    from .db import connect
    if not ctx["dest"].exists():
        return {}
    r = scan(str(ctx["dest"]), name_match=True, adopt=True)
    db = db or connect()
    return {"scan": {k: r.get(k) for k in ("files", "matched", "adopted") if k in r},
            "health": arcadehealth.evaluate_all(db=db)}


def fetch_sets(setnames, db=None, progress=None, scan_after=True):
    """Fetch for several sets with one shared context, then scan and re-judge once."""
    ctx = context(db)
    s = _session()
    reports = []
    for i, name in enumerate(setnames):
        if progress:
            progress(f"[{i + 1}/{len(setnames)}] {name}")
        try:
            reports.append(fetch_set(name, ctx, s, progress))
        except Exception as e:                       # one set's trouble is not the run's
            reports.append({"set": name, "chips": [], "disks": [], "missing": [],
                            "sources": [], "errors": [f"{type(e).__name__}: {e}"]})
    out = {"sets": reports, "chips": sum(len(r["chips"]) for r in reports),
           "disks": sum(len(r["disks"]) for r in reports), "dest": str(ctx["dest"])}
    if scan_after and (out["chips"] or out["disks"]):
        out.update(finish(ctx, db))
    return out


def broken_fixable(core="mame", db=None):
    """Owned sets the health check calls broken for want of chips, devices, a bios or a
    CHD (not a preliminary driver): the sets a fetch can bring back."""
    from .arcadehealth import _in_hand, _rows, statuses
    from .db import connect
    db = db or connect()
    known, rows = statuses(core, db), _rows(db, core)
    out = []
    for s_ in _in_hand(db):
        if known.get(s_, (None,))[0] != "broken" or s_ not in rows:
            continue
        missing = json.loads(rows[s_]["missing"]) if rows[s_]["missing"] else []
        if any(k in ("rom", "device", "bios", "parent", "chd") for k, _ in missing):
            out.append(s_)
    return sorted(out)
