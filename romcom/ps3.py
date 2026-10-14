"""PS3 discs a handheld can play: decrypted ISOs, with keys mirrored on this machine.

A Redump PS3 ISO is the disc as pressed, and most of it is still encrypted. Emulators on
Android (aPS3e, which ES-DE launches for `ps3`) and RPCS3's ISO loader need it decrypted.
Each disc has its own 16-byte key ("disc key", .dkey); Redump publishes them and
https://ps3.aldostools.org/dkey.html lists every one with the ISO's name, size, CRC32 and MD5.

**The mirror** (`DAT/PS3 dkeys/`, refreshed by `romcom ps3 refresh`):
- `REDUMP_DKEY.7z` — aldostools' bulk archive of `<Redump name>.dkey` files (GitHub),
  unpacked beside it in `REDKEY/`;
- `dkey.html` — the site's table, kept as fetched;
- `keys.tsv` — both merged: name, serial, size, crc32, md5, key. This is what lookups read.

**The disc layout** (sector = 2048 bytes): sector 0 starts with a big-endian u32 count `n`
of plain (unencrypted) regions, a zero u32, then `2n` u32 sector numbers — each plain
region's first and last sector, both inclusive. Everything between two plain regions is
encrypted: each sector on its own with AES-128-CBC, the disc key as key and the sector
number (16 bytes, big-endian) as IV. Sector 1 holds "PlayStation3" and the serial.

**Proof of a key**: the first encrypted sector decrypts to the disc licence area, which
begins `PS3LICDA`. A key is tried on that one sector before anything is written, so a
wrong match (another region's release) is caught, never burned onto a card.
"""
import csv
import html
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_DIR = ROOT / "DAT" / "PS3 dkeys"
DKEY_7Z_URL = "https://github.com/aldostools/Resources/raw/main/REDUMP_DKEY.7z"
DKEY_PAGE_URL = "https://ps3.aldostools.org/dkey.html"
SECTOR = 2048
LICENCE = b"PS3LICDA"
_FIELDS = ("name", "serial", "size", "crc32", "md5", "key")


# ---------------------------------------------------------------- the key mirror

def refresh(session=None):
    """Download the key archive and table again and rebuild `keys.tsv`."""
    import requests
    s = session or requests.Session()
    # The site's mod_security answers 406 to a scripted User-Agent or a bare Accept.
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/"
                                    "537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
                      "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    got = {}
    r = s.get(DKEY_7Z_URL, timeout=120)
    r.raise_for_status()
    _write(KEY_DIR / "REDUMP_DKEY.7z", r.content)
    got["archive_bytes"] = len(r.content)
    try:
        r = s.get(DKEY_PAGE_URL, timeout=120)
    except requests.exceptions.SSLError:
        # The site's certificate has lapsed before (2026-10); the table is public data,
        # checked against each disc's licence sector before use, so read it anyway.
        r = s.get(DKEY_PAGE_URL, timeout=120, verify=False)
    if r.ok and "<TR><TD>" in r.text:
        _write(KEY_DIR / "dkey.html", r.content)
        got["page_bytes"] = len(r.content)
    else:
        got["page_error"] = f"HTTP {r.status_code}; kept the copy already here"
    got.update(rebuild())
    return got


def _write(path, data):
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)


def rebuild():
    """keys.tsv from the mirrored archive and page (no network)."""
    rows = {}
    page = KEY_DIR / "dkey.html"
    if page.exists():
        for r in parse_page(page.read_text(encoding="utf-8", errors="replace")):
            rows[r["name"].lower()] = r
    arc = KEY_DIR / "REDUMP_DKEY.7z"
    from_archive = 0
    if arc.exists():
        out = KEY_DIR / "REDKEY"
        _unpack(arc, KEY_DIR)
        for f in sorted(out.glob("*.dkey")):
            key = f.read_text(encoding="ascii", errors="replace").strip()
            if not re.fullmatch(r"[0-9A-Fa-f]{32}", key):
                continue
            name = f.stem + ".iso"
            r = rows.setdefault(name.lower(), {"name": name, "serial": "", "size": "",
                                               "crc32": "", "md5": "", "key": key.upper()})
            r["key"] = r["key"] or key.upper()
            from_archive += 1
    tsv = KEY_DIR / "keys.tsv"
    part = tsv.with_name("keys.tsv.part")
    with open(part, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, _FIELDS, delimiter="\t")
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])
    os.replace(part, tsv)
    _index.clear()
    return {"keys": len(rows), "from_archive": from_archive, "file": str(tsv)}


_ROW = re.compile(r"<TR><TD><a[^>]*>(?P<serial>[^<]*)</a><TD>(?P<name>[^<]*)<TD>(?P<size>[\d,]*)"
                  r"<TD>(?P<crc>[0-9a-fA-F]*)<TD>(?P<md5>[0-9a-fA-F]*)<TD>(?P<key>[0-9a-fA-F]{32})",
                  re.I)


def parse_page(text):
    """The rows of aldostools' dkey table: serial, Redump ISO name, size, CRC32, MD5, key."""
    for m in _ROW.finditer(text):
        yield {"name": html.unescape(m["name"]).strip(), "serial": m["serial"].strip().upper(),
               "size": m["size"].replace(",", ""), "crc32": m["crc"].lower(),
               "md5": m["md5"].lower(), "key": m["key"].upper()}


def _unpack(archive, into):
    from . import esde
    exe = esde.seven_zip()
    if not exe:
        raise OSError("7-Zip is needed to unpack the PS3 key archive, and was not found")
    p = subprocess.run([exe, "x", "-y", f"-o{into}", str(archive)], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=600, errors="replace")
    if p.returncode != 0:
        raise OSError(f"7-Zip could not unpack {Path(archive).name}: {p.stderr.strip()[-200:]}")


_index = {}


def index():
    """{"md5": {...}, "name": {...}, "serial": {serial: [rows]}} from keys.tsv, cached by mtime."""
    tsv = KEY_DIR / "keys.tsv"
    if not tsv.exists():
        if (KEY_DIR / "REDUMP_DKEY.7z").exists() or (KEY_DIR / "dkey.html").exists():
            rebuild()
        else:
            return {"md5": {}, "name": {}, "serial": {}, "count": 0}
    stamp = tsv.stat().st_mtime
    if _index.get("stamp") != stamp:
        md5s, names, serials = {}, {}, {}
        with open(tsv, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
        for r in rows:
            if r["md5"]:
                md5s[r["md5"]] = r
            names[_norm(r["name"])] = r
            if r["serial"]:
                serials.setdefault(r["serial"], []).append(r)
        _index.clear()
        _index.update(stamp=stamp, md5=md5s, name=names, serial=serials, count=len(rows))
    return _index


def _norm(name):
    stem = Path(str(name)).name
    if stem.lower().endswith((".iso", ".dkey")):
        stem = stem.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()


def status():
    idx = index()
    return {"keys": idx.get("count", 0), "dir": str(KEY_DIR),
            "mirrored": [p.name for p in sorted(KEY_DIR.glob("*")) if p.is_file()]
            if KEY_DIR.exists() else []}


# ---------------------------------------------------------------- system firmware

FIRMWARE_DIR = ROOT / "DAT" / "PS3 firmware"
# Sony's own update list for US consoles: the current full image (PS3UPDAT.PUP) is the
# line without IncrementalUpdateVersion. Every PS3 emulator installs from this file.
UPDATE_LIST_URL = "http://fus01.ps3.update.playstation.net/update/ps3/list/us/ps3-updatelist.txt"
PUP_MAGIC = b"SCEUF"


def firmware_latest(session=None):
    """(version, url) of Sony's current full PS3 system software image."""
    import requests
    r = (session or requests).get(UPDATE_LIST_URL, timeout=60)
    r.raise_for_status()
    for line in r.text.splitlines():
        if "PS3UPDAT.PUP" in line and "IncrementalUpdateVersion" not in line:
            f = dict(kv.split("=", 1) for kv in line.strip().strip(";").split(";") if "=" in kv)
            v = f.get("SystemSoftwareVersion", "")
            try:
                v = f"{float(v):.2f}"                # "4.9300" -> "4.93"
            except ValueError:
                v = v or "?"
            return v, f["CDN"]
    raise OSError("Sony's PS3 update list names no PS3UPDAT.PUP")


def firmware(refresh=False, session=None):
    """The mirrored PS3UPDAT.PUP: {path, version, bytes}, downloaded from Sony when absent
    (or with `refresh`, when Sony's version is newer)."""
    import json
    FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    pup, meta = FIRMWARE_DIR / "PS3UPDAT.PUP", FIRMWARE_DIR / "version.json"
    have = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
    if pup.exists() and not refresh:
        return {"path": str(pup), "version": have.get("version", "?"), "bytes": pup.stat().st_size}
    import requests
    s = session or requests.Session()
    version, url = firmware_latest(s)
    if pup.exists() and have.get("version") == version:
        return {"path": str(pup), "version": version, "bytes": pup.stat().st_size}
    part = pup.with_name("PS3UPDAT.PUP.part")
    with s.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(part, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    with open(part, "rb") as fh:
        if fh.read(5) != PUP_MAGIC:
            part.unlink()
            raise OSError("the download from Sony is not a PS3 update image (no SCEUF header)")
    _replace_soon(part, pup)
    meta.write_text(json.dumps({"version": version, "url": url}), encoding="utf-8")
    return {"path": str(pup), "version": version, "bytes": pup.stat().st_size}


def _replace_soon(src, dst, tries=20):
    """os.replace, retried briefly: a virus scanner holds a fresh 200 MB file for a moment."""
    import time
    for i in range(tries):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.5)


def firmware_cached():
    pup = FIRMWARE_DIR / "PS3UPDAT.PUP"
    return pup if pup.exists() else None


# ---------------------------------------------------------------- one disc

def read_header(head):
    """(serial, [(first, last)] plain regions) from a disc's first two sectors, or None
    when the bytes are not a PS3 disc."""
    if len(head) < 2 * SECTOR or head[SECTOR:SECTOR + 12] != b"PlayStation3":
        return None
    serial = head[SECTOR + 16:SECTOR + 48].split(b"\0")[0].decode("ascii", "replace").strip()
    n = struct.unpack(">I", head[:4])[0]
    if not 0 < n < 128:
        return None
    vals = struct.unpack(f">{2 * n}I", head[8:8 + 8 * n])
    return serial.replace("-", "").upper(), list(zip(vals[0::2], vals[1::2]))


def encrypted_ranges(plain, total_sectors):
    """[(first, last)] inclusive sector ranges between the plain regions."""
    out = []
    for (_, end), (start, _) in zip(plain, plain[1:]):
        if start > end + 1:
            out.append((end + 1, start - 1))
    if plain and plain[-1][1] + 1 < total_sectors:
        out.append((plain[-1][1] + 1, total_sectors - 1))
    return out


def find_key(names=(), size=None, md5=None, serial=None):
    """The key row for a disc: by MD5, then by Redump name (any of `names` — the file's,
    the archive's, the catalog title), then by serial with the same size. None if absent."""
    idx = index()
    if md5 and md5.lower() in idx["md5"]:
        return idx["md5"][md5.lower()]
    for n in names:
        r = idx["name"].get(_norm(n)) if n else None
        if r and (not size or not r["size"] or int(r["size"]) == size):
            return r
    if serial:
        hits = idx["serial"].get(serial.replace("-", "").upper(), [])
        same = [r for r in hits if size and r["size"] and int(r["size"]) == size]
        if len(same) == 1 or (not size and len(hits) == 1):
            return (same or hits)[0]
    return None


def _cipher(key, sector):
    from Crypto.Cipher import AES
    return AES.new(key, AES.MODE_CBC, iv=sector.to_bytes(16, "big"))


def key_fits(fh, key, plain, total):
    """Does `key` decrypt this disc? Tried on the first encrypted sector (the licence)."""
    enc = encrypted_ranges(plain, total)
    if not enc:
        return True
    first = enc[0][0]
    fh.seek(first * SECTOR)
    return _cipher(key, first).decrypt(fh.read(SECTOR)).startswith(LICENCE)


def already_decrypted(fh, plain, total):
    enc = encrypted_ranges(plain, total)
    if not enc:
        return True
    fh.seek(enc[0][0] * SECTOR)
    return fh.read(SECTOR).startswith(LICENCE)


def decrypt(src, dst, key, progress=None, chunk_sectors=4096):
    """Write `src` (an encrypted PS3 ISO) to `dst` decrypted. `src == dst` decrypts in
    place. Raises OSError when the key does not open this disc."""
    from Crypto.Cipher import AES
    key = bytes.fromhex(key) if isinstance(key, str) else key
    src, dst = Path(src), Path(dst)
    size = src.stat().st_size
    total = size // SECTOR
    in_place = src.resolve() == dst.resolve()
    with open(src, "r+b" if in_place else "rb") as fi:
        hdr = read_header(fi.read(2 * SECTOR))
        if not hdr:
            raise OSError(f"{src.name} is not a PS3 disc image")
        _, plain = hdr
        if already_decrypted(fi, plain, total):
            if not in_place:
                shutil.copyfile(src, dst)
            return {"decrypted": False, "already": True}
        if not key_fits(fi, key, plain, total):
            raise OSError(f"the disc key does not open {src.name} (a different release's key?)")
        enc = encrypted_ranges(plain, total)
        todo = sum(b - a + 1 for a, b in enc)
        fo = fi if in_place else open(dst, "wb")
        try:
            done, s = 0, 0
            while s < total or (not in_place and s * SECTOR < size):
                n = min(chunk_sectors, max(total - s, 1))
                fi.seek(s * SECTOR)
                buf = bytearray(fi.read(n * SECTOR))
                if not buf:
                    break
                hit = False
                for first, last in enc:
                    lo, hi = max(first, s), min(last, s + n - 1)
                    for sec in range(lo, hi + 1):
                        o = (sec - s) * SECTOR
                        buf[o:o + SECTOR] = AES.new(key, AES.MODE_CBC,
                                                    iv=sec.to_bytes(16, "big")
                                                    ).decrypt(bytes(buf[o:o + SECTOR]))
                        hit = True
                        done += 1
                if hit or not in_place:
                    fo.seek(s * SECTOR)
                    fo.write(buf)
                s += n
                if progress and hit:
                    progress(done, todo)
        finally:
            if not in_place:
                fo.close()
    return {"decrypted": True, "already": False}


def inspect(path, names=(), md5=None):
    """What a loose ISO is: {serial, size, key row or None, already_decrypted, fits}."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as fh:
        hdr = read_header(fh.read(2 * SECTOR))
        if not hdr:
            return {"ps3": False}
        serial, plain = hdr
        total = size // SECTOR
        done = already_decrypted(fh, plain, total)
        row = find_key((path.name, *names), size=size, md5=md5, serial=serial)
        fits = bool(row) and not done and key_fits(fh, bytes.fromhex(row["key"]), plain, total)
    return {"ps3": True, "serial": serial, "size": size, "key": row,
            "already_decrypted": done, "fits": fits}
