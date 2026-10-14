"""Will this arcade set actually run? One verdict per set, per core.

`playable` answers a narrower question — are all the chips on disk — and a set can pass it
and still sit on the card doing nothing:

1. The emulator never finished the machine. Every romset dat says so in its own words,
   `<driver status="good|imperfect|preliminary">` (MAME 2003-Plus adds color/sound/graphic),
   and a preliminary driver boots to a black screen however perfect the zip is.
2. The set needs more than chips. A CHD (the hard disk / CD of Killer Instinct, area51mx,
   every Naomi GD-ROM game) is a `<disk>` entry no chip hash can satisfy; a device set
   (namco54) or a romof bios parent (neogeo) is a zip of its own beside the game's.

Each core is judged against its own dat — a set fine on FBNeo can be preliminary on MAME
2003-Plus — and stored in `arcade_health`. The owner's verdict after actually playing it
(`arcade_verdicts`) beats the computed one: the dat is a guess about emulation, the owner
has seen the screen. A verdict for core '*' covers every core.

    works    complete, driver good
    partial  complete, but the dat says imperfect emulation (sound, colours, graphics)
    broken   something is missing, or the driver is preliminary / not runnable
    unknown  complete, but the dat carries no driver status at all (the 0.78 fallbacks)
"""
import json
import re
from pathlib import Path

from .db import connect
from . import memtrim

STATUSES = ("works", "partial", "broken", "unknown")

# Export gates: which statuses reach a card. 'no_broken' is the default — a broken set is
# disk space spent on a game that will not start.
GATES = {"all": None,
         "no_broken": {"works", "partial", "unknown"},
         "works_only": {"works"}}
DEFAULT_GATE = "no_broken"

_MACHINE = re.compile(r'<(?:game|machine)\s+name="([^"]+)"')


def _attr(line, name):
    m = re.search(r'\b' + name + r'="([^"]*)"', line)
    return m.group(1) if m else None


_parsed = {}


def parse(path):
    """{setname: machine} for every machine in a dat, in one line-by-line pass — the same
    shape of reader `mameset` uses, because these dats run to 85 MB and every attribute this
    needs sits on one line. Cached per file and mtime: an export judges a core and then
    asks the same dat for its parents."""
    memtrim.use("arcadehealth.dats", _parsed)
    key = (str(path), Path(path).stat().st_mtime)
    if key in _parsed:
        return _parsed[key]
    out = _parse(path)
    if len(_parsed) >= 3:
        _parsed.clear()
    _parsed[key] = out
    return out


def _parse(path):
    out, cur = {}, None
    for line in Path(path).open(encoding="utf-8", errors="replace"):
        m = _MACHINE.search(line)
        if m:
            cur = {"name": m.group(1), "cloneof": _attr(line, "cloneof"),
                   "romof": _attr(line, "romof"), "sourcefile": _attr(line, "sourcefile"),
                   # MAME 2003-Plus marks its boards (neogeo) runnable="no" instead
                   "isbios": _attr(line, "isbios") == "yes" or (
                       _attr(line, "runnable") == "no" and _attr(line, "isdevice") != "yes"),
                   "isdevice": _attr(line, "isdevice") == "yes",
                   "runnable": _attr(line, "runnable") != "no",
                   "driver": None, "roms": [], "nodump": 0, "disks": [], "devices": [],
                   "merged": False}
            out[cur["name"]] = cur
            continue
        if cur is None:
            continue
        if "<rom " in line:
            if 'merge="' in line:
                cur["merged"] = True
            if _attr(line, "status") == "nodump":
                cur["nodump"] += 1      # never dumped: MAME runs it as "best available"
                continue
            sha, crc = _attr(line, "sha1"), _attr(line, "crc")
            if sha or crc:
                cur["roms"].append(((sha or "").lower(), (crc or "").lower(),
                                    _attr(line, "name") or ""))
        elif "<disk " in line:
            if _attr(line, "status") == "nodump" or _attr(line, "optional") == "yes":
                continue
            cur["disks"].append({"name": _disk_name(_attr(line, "name")),
                                 "sha1": (_attr(line, "sha1") or "").lower(),
                                 "merge": _attr(line, "merge")})
        elif "<device_ref " in line:
            cur["devices"].append(_attr(line, "name"))
        elif "<driver " in line:
            cur["driver"] = {k: _attr(line, k) for k in
                             ("status", "emulation", "color", "sound", "graphic")
                             if _attr(line, k)}
    return out


def _disk_name(name):
    """A dat's disk name as MAME looks for it on disk, without the .chd MAME 2003-Plus's
    dat writes into the name itself."""
    name = name or ""
    return name[:-4] if name.lower().endswith(".chd") else name


def needed_parents(machines, name):
    """The romof ancestors a set cannot boot without, nearest first. Every dat here lists a
    clone's shared roms itself (marked merge=), so a zip built from the clone's own list
    carries them and the plain parent is not needed — but a bios is (FBNeo's mslug lists no
    neogeo chips), and so is a set the dat never defines (the 0.78 bios MAME 2003-Plus
    leans on). A clone that lists nothing merged shares no chip with its parent in any of
    the three dats (CPS3's no-CD sfiii3n): it stands alone, and pulling its parent in put
    a CHD-less sfiii3 on the card beside it."""
    out, cur, seen = [], machines.get(name), {name}
    while cur and cur["romof"] and cur["romof"] not in seen:
        parent = cur["romof"]
        seen.add(parent)
        pm = machines.get(parent)
        if pm is None:
            out.append(parent)
            break
        if pm["isbios"]:
            out.append(parent)
        cur = pm
    return out


def alt_dats(core, dat):
    """Where a core's undefined bios parents come from: the current-MAME dat, then the
    extra dats beside the core's own (`organizer.romset`'s order)."""
    from . import mameset
    return [p for p in [mameset.dat_path("mame"), *mameset.dat_paths(core)[1:]]
            if p and p != dat]


def chd_sha1(path):
    """The sha1 a dat's `<disk>` names, read from the CHD's own header — a CHD is matched by
    this, never by a hash of the file (the file sha1 the scanner records is a different
    number). v3/v4/v5 headers keep it at different offsets; anything else returns None."""
    try:
        with open(path, "rb") as f:
            head = f.read(124)
    except OSError:
        return None
    if len(head) < 100 or head[:8] != b"MComprHD":
        return None
    version = int.from_bytes(head[12:16], "big")
    at = {3: 80, 4: 48, 5: 84}.get(version)
    return head[at:at + 20].hex() if at else None


def chd_index(db):
    """{lower-cased disk name: [paths]} for every .chd on disk. A dat names a disk by its
    file stem (`area51mx` -> area51mx.chd), which is how MAME itself looks for it."""
    out = {}
    for r in db.execute("SELECT path FROM files WHERE lower(path) LIKE '%.chd'"):
        out.setdefault(Path(r["path"]).stem.lower(), []).append(r["path"])
    return out


def find_chd(disk, index):
    """A path on disk holding this CHD, or None. The header sha1 decides when it can be
    read; a CHD whose header cannot be read is trusted by name, because refusing it would
    call a playable game broken over a header version this reader does not know."""
    for p in index.get(disk["name"].lower(), []):
        if not Path(p).exists():
            continue
        got = chd_sha1(p)
        if got is None or not disk["sha1"] or got == disk["sha1"]:
            return p
    return None


def _have(db):
    have_sha = {(r[0] or "").lower() for r in db.execute(
        "SELECT sha1 FROM files WHERE sha1 IS NOT NULL")}
    have_crc = {(r[0] or "").lower() for r in db.execute(
        "SELECT crc32 FROM files WHERE crc32 IS NOT NULL")}
    return have_sha, have_crc


def judge(machines, have_sha, have_crc, chds, outside=frozenset()):
    """{setname: {status, reason, driver, family, missing, is_bios}} for every machine that
    is not itself a device. `outside` names the bios parents another dat can supply (the
    0.78 fallbacks MAME 2003-Plus needs) — a romof parent this dat never defines is fine
    when it is in there."""
    chips_ok = {}

    def chips(name, seen=()):
        """The set's own chips and every device it pulls in, recursively — a device can
        reference devices of its own. Returns the list of what is missing."""
        if name in chips_ok:
            return chips_ok[name]
        m = machines.get(name)
        if m is None:
            return [("device", name)]
        miss = [("rom", r[2]) for r in m["roms"]
                if not ((r[0] and r[0] in have_sha) or (r[1] and r[1] in have_crc))]
        for d in m["devices"]:
            if d and d != name and d not in seen:
                if chips(d, seen + (name,)):
                    miss.append(("device", d))
        chips_ok[name] = miss
        return miss

    out = {}
    for name, m in machines.items():
        if m["isdevice"]:
            continue
        missing = list(chips(name))
        for parent in needed_parents(machines, name):
            if parent in machines:
                if chips(parent):
                    missing.append(("bios" if machines[parent]["isbios"] else "parent", parent))
            elif parent not in outside:
                missing.append(("bios", parent))
        for d in m["disks"]:
            if not find_chd(d, chds):
                missing.append(("chd", d["name"]))
        drv = m["driver"] or {}
        status, reason = _verdict(m, drv, missing)
        out[name] = {"status": status, "reason": reason,
                     "driver": drv.get("status"), "family": m["sourcefile"],
                     "missing": missing, "is_bios": m["isbios"]}
    return out


def _verdict(m, drv, missing):
    """The status and the one-line reason the badge shows. Missing parts outrank the driver:
    a fix for those is a download, a fix for a preliminary driver is a different core."""
    if missing:
        kinds = {}
        for kind, name in missing:
            kinds.setdefault(kind, []).append(name)
        parts = []
        for kind in ("chd", "bios", "parent", "device", "rom"):
            names = kinds.get(kind)
            if not names:
                continue
            if kind == "rom":
                parts.append(f"missing {len(names)} rom{'s' if len(names) > 1 else ''}")
            else:
                parts.append(f"needs {kind} {', '.join(names[:3])}"
                             + (f" +{len(names) - 3}" if len(names) > 3 else ""))
        return "broken", "; ".join(parts)
    if not m["runnable"]:
        return "broken", "not runnable on its own"
    status = drv.get("status") or drv.get("emulation")
    if status == "preliminary":
        return "broken", "driver preliminary — the core cannot really run it"
    if status == "protection":
        return "broken", "unemulated protection"
    rough = [k for k in ("color", "sound", "graphic")
             if drv.get(k) in ("imperfect", "preliminary")]
    if status == "imperfect" or rough:
        what = ", ".join(rough) if rough else "emulation"
        return "partial", f"{what} imperfect"
    if status == "good":
        return "works", ("best available — some chips were never dumped"
                         if m["nodump"] else "")
    return "unknown", "dat has no driver status"


def evaluate(core, db=None, store=True):
    """Judge every set of one core's romset against what is on disk, and store it.
    Returns {setname: verdict}, or {} when the core has no dat installed."""
    from . import mameset
    db = db or connect()
    dat = mameset.dat_path(core)
    if not dat:
        return {}
    machines = parse(dat)
    have_sha, have_crc = _have(db)
    chds = chd_index(db)
    # Bios parents a core's dat references but never defines come from the other dats
    # `organizer.romset` builds them from — the same fallbacks, so the verdict and the
    # build agree on what can be supplied.
    gaps = {m["romof"] for m in machines.values()
            if m["romof"] and m["romof"] not in machines}
    outside = set()
    if gaps:
        for alt in alt_dats(core, dat):
            outside |= gaps & mameset.assemblable(db, alt)
    res = judge(machines, have_sha, have_crc, chds, outside)
    if store:
        _store(db, core, res)
    return res


def _store(db, core, res):
    with db:
        db.execute("DELETE FROM arcade_health WHERE core=?", (core,))
        db.executemany(
            """INSERT INTO arcade_health(core,setname,status,reason,driver,family,missing,
                                         is_bios,checked_at)
               VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            [(core, s, v["status"], v["reason"], v["driver"], v["family"],
              json.dumps(v["missing"][:40]) if v["missing"] else None, int(v["is_bios"]))
             for s, v in res.items()])


def evaluate_all(db=None, progress=None):
    """Every installed core's verdicts. {core: {status: count}}."""
    from . import mameset
    db = db or connect()
    out = {}
    for i, core in enumerate(mameset.PROFILES):
        if progress:
            progress(i, len(mameset.PROFILES), f"judging {core} sets…")
        res = evaluate(core, db)
        if res:
            counts = {s: 0 for s in STATUSES}
            for v in res.values():
                counts[v["status"]] += 1
            out[core] = counts
    return out


def set_verdict(setname, verdict, core="*", note=None, db=None):
    """The owner's word on a set after playing it. `verdict` None clears it, putting the
    computed status back in charge."""
    if verdict is not None and verdict not in ("works", "partial", "broken"):
        raise ValueError("verdict must be works, partial or broken")
    db = db or connect()
    with db:
        if verdict is None:
            db.execute("DELETE FROM arcade_verdicts WHERE core=? AND setname=?",
                       (core, setname))
        else:
            db.execute("""INSERT INTO arcade_verdicts(core,setname,verdict,note)
                          VALUES(?,?,?,?)
                          ON CONFLICT(core,setname) DO UPDATE SET verdict=excluded.verdict,
                          note=excluded.note, updated_at=CURRENT_TIMESTAMP""",
                       (core, setname, verdict, note))
    return {"setname": setname, "core": core, "verdict": verdict}


def statuses(core, db=None):
    """{setname: (status, reason)} as it stands for a core — the owner's verdict where one
    exists (this core's first, then '*'), the computed status otherwise."""
    db = db or connect()
    out = {r["setname"]: (r["status"], r["reason"] or "") for r in db.execute(
        "SELECT setname,status,reason FROM arcade_health WHERE core=?", (core,))}
    for r in db.execute("""SELECT setname,verdict,note,core FROM arcade_verdicts
                           WHERE core IN (?,'*') ORDER BY core='*' DESC""", (core,)):
        # '*' rows first, the core's own rows after — so the specific one wins.
        out[r["setname"]] = (r["verdict"], r["note"] or "your verdict")
    return out


def gate(core, sets, level=DEFAULT_GATE, db=None, known=None):
    """Split `sets` into what may reach a card under `level` and what is held back,
    ({kept...}, {set: reason}). A set never judged passes: no verdict is not a reason to
    keep a game off the card. `known` is a `statuses()` result, when the caller has one."""
    allowed = GATES[level if level in GATES else DEFAULT_GATE]
    if allowed is None:
        return list(sets), {}
    known = statuses(core, db) if known is None else known
    kept, held = [], {}
    for s in sets:
        st = known.get(s)
        if st is None or st[0] in allowed:
            kept.append(s)
        else:
            held[s] = f"{st[0]}: {st[1]}" if st[1] else st[0]
    return kept, held


def blocked(cores, level=DEFAULT_GATE, db=None):
    """Sets the gate keeps off a card for every one of `cores` — what the export planner
    subtracts from the arcade figure, from stored verdicts only (no dat parse), so the plan
    stays fast. A set any chosen core can run is not blocked."""
    allowed = GATES[level if level in GATES else DEFAULT_GATE]
    if allowed is None:
        return set()
    out = None
    for core in cores:
        bad = {s for s, (st, _) in statuses(core, db).items() if st not in allowed}
        out = bad if out is None else out & bad
    return out or set()


def _reason_kind(why):
    """'needs chd area51mx; missing 3 roms' -> 'needs chd': the first, biggest problem,
    without the names — so reasons group into the fixes they call for."""
    first = why.split(";")[0].strip()
    if first.startswith("needs "):
        return " ".join(first.split(" ")[:2])
    if first.startswith("missing "):
        return "missing roms"
    return first or "broken"


def summary(db=None):
    """Counts per core and status, plus the biggest reasons sets are broken — the start of
    the metrics the UI grows into."""
    db = db or connect()
    out = {}
    for core in [r["core"] for r in db.execute(
            "SELECT DISTINCT core FROM arcade_health ORDER BY core")]:
        counts = {s: 0 for s in STATUSES}
        reasons = {}
        for s, (st, why) in statuses(core, db).items():
            counts[st] = counts.get(st, 0) + 1
            if st == "broken":
                key = _reason_kind(why)
                reasons[key] = reasons.get(key, 0) + 1
        out[core] = {"counts": counts,
                     "top_broken_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:8]}
    return out


# ------------------------------------------------------------------------------ metrics
_IN_HAND = ("FOUND", "DOWNLOADED", "VERIFIED", "NORMALIZED", "INSTALLED", "TESTED")
_SET = "substr(external_id, instr(external_id,'/')+1)"


def _in_hand(db):
    """{setname: (item_id, status)} for the arcade games the library holds some of. The
    catalog lists every machine MAME knows; metrics about sets nobody downloaded would
    bury the ones worth fixing."""
    return {r["s"]: (r["id"], r["status"]) for r in db.execute(
        f"""SELECT {_SET} s, id, status FROM items
            WHERE system='arcade' AND COALESCE(is_device,0)=0
              AND status IN ({','.join('?' * len(_IN_HAND))})""", _IN_HAND)}


def board(family):
    """'capcom/cps2.cpp', 'capcom/d_cps2.cpp', 'cps2.c' -> 'cps2': the driver file is the
    board family, spelled three ways by three dats."""
    if not family:
        return "unknown"
    stem = family.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return stem[2:] if stem.startswith("d_") else stem


def _rows(db, core):
    return {r["setname"]: r for r in db.execute(
        "SELECT setname,status,reason,family,missing FROM arcade_health WHERE core=?", (core,))}


def metrics(core="mame", db=None):
    """Where the library's arcade sets stand on one core, and what to fix first.

    `boards` groups by driver family (cps2, neogeo, naomi…) so a whole board's trouble shows
    as one line; `fixes` ranks single downloads by how many owned games each would unlock
    (one neogeo bios can bring back a hundred sets); `elsewhere` counts the sets broken
    here that another core judges playable — a preliminary driver is fixed by switching
    cores, not by downloading anything."""
    db = db or connect()
    hand = _in_hand(db)
    rows = _rows(db, core)
    known = statuses(core, db)
    counts = {s: 0 for s in STATUSES} | {"unjudged": 0}
    boards, fixes, roms_missing, chd_sets = {}, {}, [], []
    driver_broken, short = [], {}
    for s in hand:
        st = known.get(s, (None, ""))[0]
        counts[st or "unjudged"] = counts.get(st or "unjudged", 0) + 1
        row = rows.get(s)
        b = boards.setdefault(board(row["family"] if row else None),
                              {s_: 0 for s_ in STATUSES} | {"unjudged": 0, "total": 0})
        b[st or "unjudged"] += 1
        b["total"] += 1
        if st != "broken" or not row:
            continue
        missing = json.loads(row["missing"]) if row["missing"] else []
        deps = {(k, n) for k, n in missing if k in ("bios", "device", "parent", "chd")}
        if any(k == "rom" for k, _ in missing):
            roms_missing.append(s)
            short[s] = (frozenset(n for k, n in missing if k == "rom"), deps)
        elif deps:
            for k, n in deps:
                fixes.setdefault((k, n), []).append(s)
            if any(k == "chd" for k, _ in deps):
                chd_sets.append(s)
        elif not missing:
            driver_broken.append(s)
    # Chips many owned sets lack at once are a board's bios in another romset version
    # (MAME 2003-Plus's neogeo wants sfix.sfx, mame.sm1, mamelo.lo): one download that
    # brings back every set whose only gap is that chip group.
    seen = {}
    for chips_, _ in short.values():
        for c in chips_:
            seen[c] = seen.get(c, 0) + 1
    for s, (chips_, deps) in short.items():
        shared = frozenset(c for c in chips_ if seen[c] >= 3)
        if shared and shared == chips_ and not deps:
            fixes.setdefault(("chips", ", ".join(sorted(shared))), []).append(s)
    elsewhere = {}
    for other in [c for c in _cores_judged(db) if c != core]:
        ok = statuses(other, db)
        n = sum(1 for s in driver_broken if ok.get(s, (None,))[0] in ("works", "partial"))
        if n:
            elsewhere[other] = n
    fix_list = sorted(({"kind": k, "name": n, "unlocks": len(v), "sets": sorted(v)[:12]}
                       for (k, n), v in fixes.items()), key=lambda f: (-f["unlocks"], f["name"]))
    _name_chip_sources(core, fix_list)
    return {"core": core, "owned": len(hand), "counts": counts,
            "checked_at": db.execute("SELECT MAX(checked_at) FROM arcade_health WHERE core=?",
                                     (core,)).fetchone()[0],
            "boards": sorted(({"board": k, **v} for k, v in boards.items()),
                             key=lambda b: (-b["broken"], -b["total"], b["board"])),
            "fixes": fix_list[:40], "fix_count": len(fix_list),
            "roms_missing": len(roms_missing), "chd_sets": len(chd_sets),
            "driver_broken": len(driver_broken), "elsewhere": elsewhere}


def _name_chip_sources(core, fixes):
    """Which board set carries a chip group — 'get neogeo (MAME 2003-Plus's)' is a fix an
    owner can act on, three chip names are not. The affected games' own romof root says it
    first (2003-Plus's neogeo is runnable="no", FBNeo's pgm is not flagged at all); failing
    that, any bios in the core's dats that holds every chip."""
    from . import mameset
    chip_fixes = [f for f in fixes if f["kind"] == "chips"]
    if not chip_fixes:
        return
    for dat in mameset.dat_paths(core):
        machines = parse(dat)
        for f in chip_fixes:
            if f.get("from"):
                continue
            want = set(f["name"].split(", "))
            # Up each affected game's romof chain, top first: the board that holds the
            # whole chip group wins, else the top of the chain — a parent the dat never
            # defines (FBNeo's pgm) is still the name to fetch.
            votes = {}
            for s_ in f["sets"]:
                chain, m = [], machines.get(s_)
                while m and m["romof"] and m["romof"] not in chain and m["romof"] != s_:
                    chain.append(m["romof"])
                    m = machines.get(m["romof"])
                holder = next((n for n in reversed(chain) if n in machines
                               and want <= {r[2] for r in machines[n]["roms"]}), None)
                top = holder or (chain[-1] if chain else None)
                if top:
                    votes[top] = votes.get(top, 0) + 1
            pick = max(votes, key=votes.get) if votes else None
            if not pick:
                pick = next((n for n, m in sorted(machines.items())
                             if m["isbios"] and want <= {r[2] for r in m["roms"]}), None)
            if pick:
                f["from"], f["from_dat"] = pick, dat.name


def _cores_judged(db):
    return [r[0] for r in db.execute("SELECT DISTINCT core FROM arcade_health ORDER BY core")]


def _chip_carriers(core, short, hand, db):
    """MAME sets not in hand that hold the chips `short` ({set: [missing rom names]}) lack
    on `core`, matched by CRC — the catalog only fetches MAME sets, so a chip another
    romset version wants is reachable when MAME files it under some other machine.

    Returns (carrier setnames, sets every one of whose chips a carrier covers). Greedy: the
    carrier covering the most still-uncovered chips first, the smaller set on a tie, so one
    PCB set stands in for a whole board's games rather than a download per game."""
    from . import mameset
    own, ref = mameset.dat_path(core), mameset.dat_path("mame")
    if not own or not ref or own == ref:
        return set(), set()
    machines, mame = parse(own), parse(ref)
    need = {}
    for s, names in short.items():
        crcs = {n: crc for _, crc, n in (machines.get(s) or {}).get("roms", [])}
        got = {crcs.get(n) for n in names}
        if got and None not in got and "" not in got:
            need[s] = got
    wanted = set().union(*need.values()) if need else set()
    catalog = {r["s"] for r in db.execute(
        f"SELECT {_SET} s FROM items WHERE system='arcade'")}
    holds = {}
    for name, m in mame.items():
        if name in hand or name not in catalog:
            continue
        have = {crc for _, crc, _ in m["roms"]} & wanted
        if have:
            holds[name] = (have, len(m["roms"]))
    reachable = set().union(*(h for h, _ in holds.values())) if holds else set()
    fixable = {s: c for s, c in need.items() if c <= reachable}
    todo = set().union(*fixable.values()) if fixable else set()
    picked = set()
    while todo:
        best = max(holds, key=lambda n: (len(holds[n][0] & todo), -holds[n][1], n))
        picked.add(best)
        todo -= holds[best][0]
    return picked, set(fixable)


def repair(core="mame", apply=False, db=None):
    """Arm the downloads that fix owned broken sets, for the acquire pipeline to fetch.

    A missing bios/device/parent is a catalog item of its own (neogeo, namco54): it is
    marked wanted + authorized, and set back to MISSING when the library holds it only in
    part, because the acquirer attempts CATALOGED/MISSING items only. A game missing its
    own chips or CHD is re-armed the same way — on the mame core only: the catalog is the
    MAME romset, so a set short of FBNeo's or 2003-Plus's chip versions is a romset
    mismatch another download of the MAME set will not cure. Unless MAME files the same
    chip under another set: FBNeo's pgm wants bios.u42, which MAME keeps in thegladpcb —
    that carrier is armed instead (`_chip_carriers`), and the set counts as a mismatch
    only when some chip has no carrier at all.
    A preliminary driver is not a download at all. `apply=False` is the preview."""
    db = db or connect()
    hand = _in_hand(db)
    rows = _rows(db, core)
    known = statuses(core, db)
    want_deps, want_sets, mismatch, short = set(), set(), 0, {}
    for s in hand:
        if known.get(s, (None,))[0] != "broken" or s not in rows:
            continue
        missing = json.loads(rows[s]["missing"]) if rows[s]["missing"] else []
        for k, n in missing:
            if k in ("bios", "device", "parent"):
                want_deps.add(n)
        if any(k in ("rom", "chd") for k, _ in missing):
            if core == "mame":
                want_sets.add(s)
            elif any(k == "chd" for k, _ in missing):
                mismatch += 1
            else:
                short[s] = [n for k, n in missing if k == "rom"]
    carriers, carried = (_chip_carriers(core, short, hand, db) if short
                         else (set(), set()))
    mismatch += len(short) - len(carried)
    names = sorted(want_deps | want_sets | carriers)
    items = {}
    for i in range(0, len(names), 800):
        chunk = names[i:i + 800]
        for r in db.execute(
                f"""SELECT id, {_SET} s, status, wanted, authorized FROM items
                    WHERE system='arcade' AND {_SET} IN ({','.join('?' * len(chunk))})""",
                chunk):
            items[r["s"]] = r
    # Only what would change: an item already armed and CATALOGED/MISSING is in the
    # acquirer's queue already.
    todo = [r for r in items.values()
            if not (r["wanted"] and r["authorized"] and r["status"] in ("CATALOGED", "MISSING"))]
    if core != "mame":
        # A dependency the MAME catalog already holds whole is this core's chip-version
        # mismatch too: fetching the MAME set again changes nothing.
        held = [r for r in todo if r["status"] in ("VERIFIED", "NORMALIZED", "INSTALLED",
                                                   "TESTED")]
        mismatch += len(held)
        todo = [r for r in todo if r not in held]
    out = {"core": core, "deps": len(want_deps), "sets": len(want_sets),
           "not_in_catalog": sorted(set(names) - set(items))[:40],
           "already_queued": len(items) - len(todo), "to_arm": len(todo),
           "romset_mismatch": mismatch,
           "carriers": sorted(carriers), "carried_sets": len(carried),
           "sample": sorted(r["s"] for r in todo)[:30], "applied": False}
    if apply and todo:
        with db:
            for r in todo:
                db.execute("""UPDATE items SET wanted=1, authorized=1,
                              status=CASE WHEN status IN ('CATALOGED','MISSING') THEN status
                                          ELSE 'MISSING' END,
                              updated_at=CURRENT_TIMESTAMP WHERE id=?""", (r["id"],))
                db.execute("INSERT INTO events(item_id,event,detail) VALUES(?,?,?)",
                           (r["id"], "arcade-repair",
                            f"armed to fix broken {core} sets ({r['status']} -> "
                            f"{r['status'] if r['status'] in ('CATALOGED', 'MISSING') else 'MISSING'})"))
        out["applied"] = True
    return out
