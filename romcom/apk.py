"""What an Android game is, read from its own package.

An Android game is not a ROM: it is installed, not emulated, and ES-DE lists it in the
`androidgames` system by package name (a `<Title>.app` file whose contents are the
package, launched with %ANDROIDAPP%). So it lives apart from the catalog — its own table,
its own folder — and never takes part in a card's console ranking.

The facts come from the file, not its name: a filename like `Game-v1.2-full.apk` says
nothing reliable. An .apk carries a binary AndroidManifest.xml (package, version, minimum
Android) and a resources.arsc that resolves the app's label; an .xapk is a zip of the base
APK, its split APKs and its OBB expansion files, described by a manifest.json. Both are
read here with nothing but zipfile and struct, so no Android SDK is needed.
"""
import io
import json
import os
import struct
import zipfile
from pathlib import Path

EXTS = (".apk", ".xapk", ".apks")

# android: attribute resource ids — an obfuscated manifest can blank the attribute names
# in its string pool, but the resource map still says which attribute is which.
_ATTR_IDS = {0x01010001: "label", 0x0101021B: "versionCode", 0x0101021C: "versionName",
             0x0101020C: "minSdkVersion", 0x01010270: "targetSdkVersion"}

_STRING_POOL, _XML, _TABLE = 0x0001, 0x0003, 0x0002
_XML_RESOURCE_MAP, _XML_START = 0x0180, 0x0102
_TABLE_PACKAGE, _TABLE_TYPE = 0x0200, 0x0201
_T_REFERENCE, _T_STRING, _T_INT_DEC, _T_INT_HEX = 0x01, 0x03, 0x10, 0x11


def _strings(buf, off):
    """The strings of a ResStringPool chunk at `off`."""
    _, hsize, _, count, _, flags, start, _ = struct.unpack_from("<HHIIIIII", buf, off)
    utf8 = bool(flags & 0x100)
    base = off + start
    out = []
    for i in range(count):
        p = base + struct.unpack_from("<I", buf, off + hsize + 4 * i)[0]
        if utf8:
            # Two lengths: characters, then bytes; each one or two bytes long.
            for _ in range(2):
                n = buf[p]
                p += 1
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | buf[p]
                    p += 1
            out.append(buf[p:p + n].decode("utf-8", "replace"))
        else:
            n = struct.unpack_from("<H", buf, p)[0]
            p += 2
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", buf, p)[0]
                p += 2
            out.append(buf[p:p + 2 * n].decode("utf-16-le", "replace"))
    return out


def parse_manifest(buf):
    """{package, versionCode, versionName, minSdkVersion, targetSdkVersion, label, split}
    from a binary AndroidManifest.xml. `label` may be an int — a resource id the
    resources table resolves."""
    if len(buf) < 8 or struct.unpack_from("<H", buf, 0)[0] != _XML:
        raise ValueError("not a binary Android manifest")
    strings, resmap, out = [], [], {}
    off = struct.unpack_from("<H", buf, 2)[0]
    while off + 8 <= len(buf):
        ctype, _, csize = struct.unpack_from("<HHI", buf, off)
        if csize < 8:
            break
        if ctype == _STRING_POOL:
            strings = _strings(buf, off)
        elif ctype == _XML_RESOURCE_MAP:
            resmap = list(struct.unpack_from(f"<{(csize - 8) // 4}I", buf, off + 8))
        elif ctype == _XML_START:
            ext = off + 16
            _, name, astart, asize, acount = struct.unpack_from("<IIHHH", buf, ext)
            tag = strings[name] if name < len(strings) else ""
            if tag in ("manifest", "application", "uses-sdk"):
                for i in range(acount):
                    a = ext + astart + i * asize
                    _, an, raw, _, _, dtype, data = struct.unpack_from("<IIIHBBI", buf, a)
                    key = _ATTR_IDS.get(resmap[an]) if an < len(resmap) else None
                    key = key or (strings[an] if an < len(strings) else "")
                    if dtype == _T_STRING or (raw != 0xFFFFFFFF and raw < len(strings)):
                        val = strings[raw if raw != 0xFFFFFFFF else data]
                    elif dtype == _T_REFERENCE:
                        val = data          # a resource id
                    elif dtype in (_T_INT_DEC, _T_INT_HEX):
                        val = data
                    else:
                        continue
                    if tag == "manifest" and key in ("package", "versionCode", "versionName",
                                                     "split"):
                        out[key] = val
                    elif tag == "application" and key == "label":
                        out["label"] = val
                    elif tag == "uses-sdk" and key in ("minSdkVersion", "targetSdkVersion"):
                        out[key] = val
        off += csize
    return out


def resolve_string(buf, res_id, _depth=0):
    """The string a resource id names in resources.arsc — the default configuration's,
    else English, else any. None when the id is not a string."""
    if _depth > 4 or len(buf) < 12 or struct.unpack_from("<H", buf, 0)[0] != _TABLE:
        return None
    pkg_id, type_id, entry = res_id >> 24, (res_id >> 16) & 0xFF, res_id & 0xFFFF
    hsize = struct.unpack_from("<H", buf, 2)[0]
    off, pool, found = hsize, [], {}
    while off + 8 <= len(buf):
        ctype, chsize, csize = struct.unpack_from("<HHI", buf, off)
        if csize < 8:
            break
        if ctype == _STRING_POOL:
            pool = _strings(buf, off)
        elif ctype == _TABLE_PACKAGE and struct.unpack_from("<I", buf, off + 8)[0] == pkg_id:
            sub = off + chsize
            while sub + 8 <= off + csize:
                stype, shsize, ssize = struct.unpack_from("<HHI", buf, sub)
                if ssize < 8:
                    break
                if stype == _TABLE_TYPE and buf[sub + 8] == type_id:
                    v = _type_entry(buf, sub, shsize, entry)
                    if v is not None:
                        lang = buf[sub + 20 + 8:sub + 20 + 10]   # config.language
                        rank = 0 if lang == b"\0\0" else (1 if lang == b"en" else 2)
                        found.setdefault(rank, v)
                sub += ssize
        off += csize
    if not found:
        return None
    dtype, data = found[min(found)]
    if dtype == _T_STRING and data < len(pool):
        return pool[data]
    if dtype == _T_REFERENCE and data:
        return resolve_string(buf, data, _depth + 1)
    return None


def _type_entry(buf, off, hsize, entry):
    """(dataType, data) of entry `entry` in a ResTable_type chunk, or None."""
    flags = buf[off + 9]
    count, start = struct.unpack_from("<II", buf, off + 12)
    idx = off + hsize
    if flags & 0x01:                       # sparse: (index, offset/4) pairs, sorted
        eoff = None
        for i in range(count):
            k, o = struct.unpack_from("<HH", buf, idx + 4 * i)
            if k == entry:
                eoff = o * 4
                break
        if eoff is None:
            return None
    elif flags & 0x02:                     # 16-bit offsets, /4; 0xffff = none
        if entry >= count:
            return None
        o = struct.unpack_from("<H", buf, idx + 2 * entry)[0]
        if o == 0xFFFF:
            return None
        eoff = o * 4
    else:
        if entry >= count:
            return None
        eoff = struct.unpack_from("<I", buf, idx + 4 * entry)[0]
        if eoff == 0xFFFFFFFF:
            return None
    e = off + start + eoff
    esize, eflags = struct.unpack_from("<HH", buf, e)
    if eflags & 0x0008:                    # compact: key u16, flags u16 (type in high byte), data
        return eflags >> 8, struct.unpack_from("<I", buf, e + 4)[0]
    if eflags & 0x0001:                    # a bag (style, array…), not a value
        return None
    _, _, dtype, data = struct.unpack_from("<HBBI", buf, e + esize)
    return dtype, data


def _abis(names):
    return sorted({n.split("/")[1] for n in names
                   if n.startswith("lib/") and n.count("/") >= 2 and n.endswith(".so")})


def read_apk(z):
    """Facts from an open APK zip."""
    names = z.namelist()
    info = parse_manifest(z.read("AndroidManifest.xml"))
    if isinstance(info.get("label"), int):
        label = None
        if "resources.arsc" in names:
            try:
                label = resolve_string(z.read("resources.arsc"), info["label"])
            except (struct.error, IndexError, ValueError):
                label = None
        info["label"] = label
    info["abis"] = _abis(names)
    return info


def read(path):
    """What an .apk or .xapk is: {package, label, version_name, version_code, min_sdk,
    abis, bytes, obb: [names], splits: n, kind}. Raises ValueError for a file that is
    not an Android package."""
    path = Path(path)
    size = path.stat().st_size
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a zip: {e}") from None
    with z:
        names = z.namelist()
        if "AndroidManifest.xml" in names:
            m = read_apk(z)
            return {"package": m.get("package"), "label": m.get("label"),
                    "version_name": _s(m.get("versionName")),
                    "version_code": _i(m.get("versionCode")),
                    "min_sdk": _i(m.get("minSdkVersion")), "abis": m.get("abis") or [],
                    "split": m.get("split"), "bytes": size, "obb": [], "splits": 0,
                    "kind": "apk"}
        if "manifest.json" in names:
            # XAPK: manifest.json describes the bundle; the base APK inside is the truth
            # for anything it leaves out.
            j = json.loads(z.read("manifest.json").decode("utf-8", "replace"))
            apks = [n for n in names if n.lower().endswith(".apk")]
            base = next((n for n in apks if Path(n).stem == j.get("package_name")),
                        apks[0] if apks else None)
            m = {}
            if base:
                with zipfile.ZipFile(io.BytesIO(z.read(base))) as inner:
                    m = read_apk(inner)
            abis = set(m.get("abis") or [])
            for n in apks:                 # config.arm64_v8a.apk carries the native libs
                stem = Path(n).stem
                if stem.startswith("config.") and "_" in stem and "dpi" not in stem:
                    abis.add(stem[len("config."):].replace("_", "-"))
            return {"package": j.get("package_name") or m.get("package"),
                    "label": j.get("name") or m.get("label"),
                    "version_name": _s(j.get("version_name") or m.get("versionName")),
                    "version_code": _i(j.get("version_code") or m.get("versionCode")),
                    "min_sdk": _i(j.get("min_sdk_version") or m.get("minSdkVersion")),
                    "abis": sorted(abis), "split": None, "bytes": size,
                    "obb": [n for n in names if n.lower().endswith(".obb")],
                    "splits": max(0, len(apks) - 1), "kind": "xapk"}
        apks = [n for n in names if n.lower().endswith(".apk")]
        base = next((n for n in apks if Path(n).name.lower() == "base.apk"), None)
        if base:
            # .apks (SAI, bundletool): base.apk and its splits, with no manifest of the
            # bundle's own — an 8 GB one was 'not an Android package'. The base is read
            # in place: it can be over a gigabyte, too much to hold in memory.
            with z.open(base) as f, zipfile.ZipFile(f) as inner:
                m = read_apk(inner)
            abis = set(m.get("abis") or [])
            for n in apks:
                stem = Path(n).stem
                for pre in ("split_config.", "config."):
                    if stem.startswith(pre) and "_" in stem and "dpi" not in stem:
                        abis.add(stem[len(pre):].replace("_", "-"))
            return {"package": m.get("package"), "label": m.get("label"),
                    "version_name": _s(m.get("versionName")),
                    "version_code": _i(m.get("versionCode")),
                    "min_sdk": _i(m.get("minSdkVersion")), "abis": sorted(abis),
                    "split": None, "bytes": size,
                    "obb": [n for n in names if n.lower().endswith(".obb")],
                    "splits": max(0, len(apks) - 1), "kind": "apks"}
    raise ValueError("no AndroidManifest.xml, manifest.json or base.apk — not an Android "
                     "package")


def _s(v):
    return None if v is None else str(v)


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- the library
def source_dir():
    from .config import settings
    return (settings().get("android_games_dir") or "").strip().strip('"')


def scan(folder=None, db=None, progress=None):
    """Read every Android package under `folder` (the setting android_games_dir) into the
    android_games table. A file already read at the same size and time is not opened
    again; a row whose file is gone is dropped. OBB files beside a game (`<package>/`
    or `main.<code>.<package>.obb`) are found by package and recorded with it.

    Returns {"folder", "games", "read", "unchanged", "errors": [{path, error}]}."""
    from .db import connect
    db = db or connect()
    folder = Path(folder or source_dir())
    if not str(folder) or not folder.is_dir():
        raise ValueError(f"no Android games folder at {folder!s} — set android_games_dir "
                         f"or pass the folder")
    files = sorted(p for p in folder.rglob("*")
                   if p.is_file() and p.suffix.lower() in EXTS)
    obbs = [p for p in folder.rglob("*.obb") if p.is_file()]
    known = {r["path"]: r for r in db.execute("SELECT * FROM android_games")}
    seen, read_n, same, errors = set(), 0, 0, []
    for i, p in enumerate(files):
        if progress:
            progress(i, len(files), p.name)
        st = p.stat()
        key = str(p)
        seen.add(key)
        row = known.get(key)
        if row and row["bytes"] == st.st_size and row["mtime"] == st.st_mtime:
            same += 1
            continue
        try:
            info = read(p)
            err = None
        except Exception as e:      # one damaged package must not end the shelf's scan
            info, err = {"bytes": st.st_size}, str(e) or type(e).__name__
            errors.append({"path": key, "error": err})
        with db:
            db.execute(
                "INSERT INTO android_games(path,package,label,version_name,version_code,"
                "min_sdk,abis,split,kind,bytes,mtime,obb,error,scanned_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(path) DO UPDATE SET package=excluded.package,"
                "label=excluded.label,version_name=excluded.version_name,"
                "version_code=excluded.version_code,min_sdk=excluded.min_sdk,"
                "abis=excluded.abis,split=excluded.split,kind=excluded.kind,"
                "bytes=excluded.bytes,mtime=excluded.mtime,obb=excluded.obb,"
                "error=excluded.error,scanned_at=CURRENT_TIMESTAMP",
                (key, info.get("package"), info.get("label"), info.get("version_name"),
                 info.get("version_code"), info.get("min_sdk"),
                 ",".join(info.get("abis") or []), info.get("split"), info.get("kind"),
                 st.st_size, st.st_mtime, json.dumps(info.get("obb") or []), err))
        read_n += 1
    # OBBs that sit beside the packages rather than inside an XAPK.
    by_pkg = {}
    for o in obbs:
        parts = o.name.split(".")
        pkg = (".".join(parts[2:-1]) if len(parts) > 3 and parts[0] in ("main", "patch")
               else o.parent.name)
        by_pkg.setdefault(pkg, []).append(str(o))
    with db:
        for r in db.execute("SELECT path, package, kind FROM android_games").fetchall():
            if r["path"] not in seen and r["path"].startswith(str(folder)):
                db.execute("DELETE FROM android_games WHERE path=?", (r["path"],))
            elif r["kind"] == "apk" and r["package"] in by_pkg:
                db.execute("UPDATE android_games SET obb=? WHERE path=?",
                           (json.dumps(sorted(by_pkg[r["package"]])), r["path"]))
    games = db.execute("SELECT COUNT(DISTINCT package) FROM android_games "
                       "WHERE package IS NOT NULL AND split IS NULL").fetchone()[0]
    return {"folder": str(folder), "games": games, "read": read_n, "unchanged": same,
            "errors": errors}


def games(db=None):
    """One row per game: the newest version of each package, its split APKs and OBBs.
    Older versions are listed under `older` — kept on disk, never sent to a card."""
    from .db import connect
    db = db or connect()
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM android_games WHERE package IS NOT NULL AND error IS NULL "
        "ORDER BY package, COALESCE(version_code,0) DESC, bytes DESC")]
    out = {}
    for r in rows:
        if r["split"]:
            continue
        g = out.get(r["package"])
        if g is None:
            r["obb"] = json.loads(r["obb"] or "[]")
            r["splits_files"] = [x["path"] for x in rows if x["package"] == r["package"]
                                 and x["split"] and x["version_code"] == r["version_code"]]
            r["older"] = []
            r["title"] = title(r)
            out[r["package"]] = r
        else:
            g["older"].append(r["path"])
    return sorted(out.values(), key=lambda g: g["title"].lower())


def title(row):
    """The name a game goes on the card under: its label, else the package's last word."""
    t = (row.get("label") or "").strip()
    if not t:
        t = (row.get("package") or Path(row["path"]).stem).rsplit(".", 1)[-1]
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad or ord(c) < 32 else c for c in t).strip(" .") or "game"


# ------------------------------------------------------------------------------- the card
CARD_DIR = "Android Games"


def export(card, db=None, dry_run=False, abi="arm64-v8a", progress=None):
    """Put the library's Android games on a card, apart from its ROMs.

    `<card>/Android Games/<Title>/` gets the package (and its split APKs and OBBs) to
    install from on the handheld; `<card>/ROMs/androidgames/<Title>.app` holds the package
    name, which is how ES-DE lists an installed game. A game built only for another CPU
    (no `abi` among its native libraries) is left off: it would not install. A package
    with no native code runs anywhere.

    Files already on the card at the same size are left alone. The copy stops at the
    card's free-space reserve, like every other export. Returns a report."""
    import shutil
    from . import esde
    from .organizer import _free_bytes, _reserve_bytes
    root = esde.card_root(card)
    roms = esde.roms_dir(root) / "androidgames"
    dest = root / CARD_DIR
    reserve = _reserve_bytes(root)
    report = {"card": str(root), "games": 0, "copied": 0, "bytes": 0, "skipped_abi": [],
              "no_room": [], "stubs": 0, "dry_run": dry_run, "errors": []}
    todo = []
    for g in games(db):
        if g["abis"] and abi not in g["abis"].split(","):
            report["skipped_abi"].append({"title": g["title"], "abis": g["abis"]})
            continue
        files = [g["path"], *g["splits_files"]]
        folder = dest / g["title"]
        plan = [(Path(f), folder / Path(f).name) for f in files]
        # An OBB goes in obb/<package>/ — where it is copied to on the handheld.
        plan += [(Path(o), folder / "obb" / g["package"] / Path(o).name)
                 for o in g["obb"] if not str(o).lower().endswith(".xapk")
                 and Path(o).is_file()]
        todo.append((g, plan))
    for i, (g, plan) in enumerate(todo):
        if progress:
            progress(i, len(todo), g["title"])
        need = [(s, t) for s, t in plan
                if not (t.exists() and t.stat().st_size == s.stat().st_size)]
        size = sum(s.stat().st_size for s, _ in need)
        free = _free_bytes(root)
        if not dry_run and need and (free is None or free - size < reserve):
            report["no_room"].append({"title": g["title"], "bytes": size})
            continue
        report["games"] += 1
        report["bytes"] += size
        if dry_run:
            report["copied"] += len(need)
            continue
        try:
            for s, t in need:
                t.parent.mkdir(parents=True, exist_ok=True)
                tmp = t.with_name(t.name + ".part")
                shutil.copyfile(s, tmp)
                os.replace(tmp, t)
                report["copied"] += 1
            roms.mkdir(parents=True, exist_ok=True)
            stub = roms / f"{g['title']}.app"
            if not stub.exists() or stub.read_text(encoding="utf-8").strip() != g["package"]:
                stub.write_text(g["package"], encoding="utf-8")
                report["stubs"] += 1
        except OSError as e:
            report["errors"].append({"title": g["title"], "error": str(e)})
    return report
