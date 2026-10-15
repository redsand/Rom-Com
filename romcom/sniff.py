"""Is this file really a disc for that console? A cheap look inside, for name matches.

A filename is a guess. For the disc consoles the guess goes wrong in a costly way: a
PC DVD called "Call of Duty Black Ops.zip" matched the PS3 game by name, "Ape Escape.zip"
(a PS1 bin/cue) matched the PS3 one, and an export would have carried 50 GB of them to a
card as PS3 games that no emulator can boot. Every one of these consoles stamps its discs,
so the stamp — read from the file's first 32 KB, or from an archive's member listing — can
veto the guess:

  ps3       'PlayStation3' at 0x800 (sector 1), or a PS3_GAME folder / .pkg in an archive
  ps1/ps2   ISO 9660 volume whose system id is 'PLAYSTATION'; a bin/cue for CD-era games
  psp       ISO 9660 system id 'PSP GAME'; .cso / .pbp
  wii       magic 5D1C9EA3 at 0x18; .wbfs / .rvz / .wia
  gamecube  magic C2339F3D at 0x1C; .gcz / .rvz

`consistent()` answers True (the stamp is there), False (the file is something else — a
PC installer, an Xbox 360 game folder, another console's disc) or None (can't tell
cheaply: a CHD, an unreadable archive). Only False ever overrides a name match.
"""
import re
import subprocess
import zipfile
from pathlib import Path

SEVENZIP = r"C:\Program Files\7-Zip\7z.exe"

# The consoles a name match is checked for. Cartridge systems are left alone: their
# extension check in the scanner already does this job, and they have no disc stamp.
CHECKED = ("ps3", "ps2", "ps1", "psp", "wii", "gamecube")

# Disc-image file types: never a cartridge game, whatever the file is called.
DISC_IMAGES = {".iso", ".cue", ".chd", ".gdi", ".cdi", ".rvz", ".wbfs", ".wia", ".gcz",
               ".cso", ".mdf", ".nrg"}

HEAD = 0x8030  # through the ISO 9660 primary volume descriptor's system id

_PC = (".exe", ".dll", ".cab", ".msi", ".inf")
_CD = (".cue", ".bin", ".img", ".ccd", ".sub", ".mdf", ".mds")
_GOD = re.compile(r"(^|[\\/])0000[47]000([\\/]|$)")   # Xbox 360 games-on-demand folders
_ARCHIVES = (".zip", ".7z", ".rar")


def _iso_systems(head):
    """Which checked consoles a disc image's first bytes are stamped for (may be empty)."""
    out = set()
    if len(head) >= 0x80C and head[0x800:0x80C] == b"PlayStation3":
        out.add("ps3")
    if len(head) >= 0x20:
        if head[0x18:0x1C] == bytes.fromhex("5D1C9EA3"):
            out.add("wii")
        if head[0x1C:0x20] == bytes.fromhex("C2339F3D"):
            out.add("gamecube")
    if len(head) >= 0x8028 and head[0x8001:0x8006] == b"CD001":
        sysid = head[0x8008:0x8028].decode("latin-1").strip()
        if sysid.startswith("PLAYSTATION"):
            out |= {"ps1", "ps2"}
        elif sysid.startswith("PSP GAME"):
            out.add("psp")
    return out


def _zip_names(path):
    with zipfile.ZipFile(path) as z:
        return [i.filename for i in z.infolist() if not i.is_dir()]


def _7z_names(path):
    r = subprocess.run([SEVENZIP, "l", "-slt", "-ba", str(path)], capture_output=True,
                       text=True, errors="replace", timeout=60)
    # Exit code 2 still lists a damaged archive's members, and those names are evidence.
    names = [ln[7:].strip() for ln in r.stdout.splitlines() if ln.startswith("Path = ")]
    return names or None


def _names(path):
    """Member names; 7-Zip reads what zipfile can't — a '.zip' that is really a 7z or
    rar is common in downloads."""
    if Path(path).suffix.lower() == ".zip":
        try:
            return _zip_names(path), True
        except (zipfile.BadZipFile, NotImplementedError):
            pass
    return _7z_names(path), False


def _member_head(path, member, is_zip=False):
    """The first HEAD bytes of one archive member, without unpacking the rest: zip
    streams it, 7-Zip is stopped once enough has arrived (its decoder runs front to back)."""
    if is_zip:
        try:
            with zipfile.ZipFile(path) as z, z.open(member) as fh:
                return fh.read(HEAD)
        except NotImplementedError:   # deflate64 and friends: 7-Zip has them
            pass
    p = subprocess.Popen([SEVENZIP, "e", "-so", "-bd", str(path), member],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        buf = b""
        while len(buf) < HEAD:
            b = p.stdout.read(HEAD - len(buf))
            if not b:
                break
            buf += b
        return buf
    finally:
        p.kill()
        p.wait()


def evidence(path):
    """What a file says about its console: ('iso', systems) from a disc stamp, ('names',
    member list) from an archive, or None when it can't be read cheaply."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext == ".iso":
            with open(p, "rb") as f:
                return ("iso", _iso_systems(f.read(HEAD)))
        if ext in _ARCHIVES:
            names, is_zip = _names(p)
            if names is None:
                return None
            isos = [n for n in names if n.lower().endswith(".iso")]
            if len(isos) == 1 and len(names) <= 3:
                return ("iso", _iso_systems(_member_head(p, isos[0], is_zip)))
            return ("names", names)
        return ("names", [p.name])
    except (OSError, zipfile.BadZipFile, subprocess.SubprocessError, KeyError, ValueError,
            NotImplementedError):
        return None


# Consoles a vetoed match may move to: the checked ones, plus Dreamcast, whose GD-ROM
# and DiscJuggler images were the commonest thing misnamed as a PSP or Wii game.
MOVABLE = CHECKED + ("dreamcast",)


def systems(ev):
    """The consoles this evidence positively points at, most likely first."""
    out = [s for s in CHECKED if consistent(ev, s)]
    if ev and ev[0] == "names" and {Path(n.lower()).suffix for n in ev[1]} & {".gdi", ".cdi"}:
        out.append("dreamcast")
    return out


def consistent(ev, system):
    """True / False / None: does this evidence fit a disc for `system`?"""
    if system not in CHECKED or ev is None:
        return None
    kind, data = ev
    if kind == "iso":
        return system in data
    low = [n.lower().replace("\\", "/") for n in data]
    exts = {Path(n).suffix for n in low}
    if any("ps3_game/" in n or n.endswith("/param.sfo") for n in low) or ".pkg" in exts:
        return system == "ps3"
    if exts & {".gdi", ".cdi"}:
        return False                    # Dreamcast GD-ROM / DiscJuggler images
    if ".pbp" in exts:
        return system in ("psp", "ps1")  # a PSP eboot is as often a PS1 game, and PS1 cores run it
    if ".cso" in exts:
        return system in ("psp", "ps2")  # PCSX2/NetherSX2 read CSO too
    if exts & {".wbfs", ".wia"}:
        return system == "wii"
    if ".gcz" in exts:
        return system == "gamecube"
    if ".rvz" in exts:
        return system in ("wii", "gamecube")
    if any(_GOD.search(n) for n in low):
        return False                    # an Xbox 360 game folder
    if ".app" in exts and exts & {".h3", ".tmd"}:
        return False                    # a Wii U install package — no emulator here runs it
    if exts & set(_PC):
        return False                    # an installer or a PC game folder
    if exts & set(_CD) and not exts & {".iso", ".chd"}:
        return system in ("ps1", "ps2")  # a CD image: never a DVD/Blu-ray console's disc
    return None


# ---------------------------------------------------------------------- disc titles
def _iso_find(f, extent, size, name):
    """The (extent, size) of `name` in an ISO 9660 directory, or None."""
    f.seek(extent * 2048)
    data = f.read(size)
    i = 0
    want = name.upper()
    while i < len(data):
        n = data[i]
        if n == 0:                                  # padding to the next sector
            i = (i // 2048 + 1) * 2048
            continue
        rec = data[i:i + n]
        ident = rec[33:33 + rec[32]].decode("latin-1").split(";")[0].upper()
        if ident == want:
            return int.from_bytes(rec[2:6], "little"), int.from_bytes(rec[10:14], "little")
        i += n
    return None


def _sfo(data):
    """{key: value} from a PlayStation PARAM.SFO."""
    if data[:4] != b"\x00PSF":
        return {}
    keys, vals, count = (int.from_bytes(data[o:o + 4], "little") for o in (8, 12, 16))
    out = {}
    for e in range(count):
        b = 20 + e * 16
        k_off = int.from_bytes(data[b:b + 2], "little")
        fmt = int.from_bytes(data[b + 2:b + 4], "little")
        length = int.from_bytes(data[b + 4:b + 8], "little")
        d_off = int.from_bytes(data[b + 12:b + 16], "little")
        key = data[keys + k_off:data.index(b"\x00", keys + k_off)].decode("latin-1")
        raw = data[vals + d_off:vals + d_off + length]
        out[key] = (int.from_bytes(raw[:4], "little") if fmt == 0x0404
                    else raw.split(b"\x00")[0].decode("utf-8", "replace").strip())
    return out


def psp_info(path):
    """{"title", "disc_id"} read from a PSP .iso's PSP_GAME/PARAM.SFO — the name on the
    box, where a file is often called 'PSP - GTAvcs'. {} for anything else."""
    p = Path(path)
    if p.suffix.lower() != ".iso":
        return {}
    try:
        with open(p, "rb") as f:
            f.seek(16 * 2048)
            pvd = f.read(2048)
            if pvd[1:6] != b"CD001":
                return {}
            root = pvd[156:156 + 34]
            hit = _iso_find(f, int.from_bytes(root[2:6], "little"),
                            int.from_bytes(root[10:14], "little"), "PSP_GAME")
            hit = hit and _iso_find(f, *hit, "PARAM.SFO")
            if not hit:
                return {}
            f.seek(hit[0] * 2048)
            sfo = _sfo(f.read(min(hit[1], 64 * 1024)))
    except (OSError, ValueError, IndexError):
        return {}
    title = re.sub(r"\s+", " ", str(sfo.get("TITLE") or "")).strip()
    return {"title": title, "disc_id": str(sfo.get("DISC_ID") or "")} if title else {}


# ---------------------------------------------------------------------- stub discs
# An archive this small cannot hold a disc game for these consoles. What turns up this
# size is a download that stopped after the first file: a .gdi and the 1 MB audio Track 2
# every GD-ROM shares byte for byte, which hash-matched whichever catalog disc carried that
# track ('Love Hina - Smile Again.zip' was filed, verified, as Blue Stinger). PSP minis and
# PSN titles are real games of a few MB, and are let through by name.
DISC_SYSTEMS = ("dreamcast", "ps1", "ps2", "ps3", "saturn", "segacd", "pcenginecd", "3do",
                "gamecube", "wii", "psp")
STUB_BYTES = 5_000_000
_SMALL_OK = re.compile(r"\((?:minis|psn)\)|\bminis\b|\[np[a-z]{2}\d{5}\]", re.I)


def stub_disc(size, system, title=""):
    """True when `size` bytes is too small to be a disc game for `system`."""
    return ((system or "").lower() in DISC_SYSTEMS and 0 <= (size or 0) < STUB_BYTES
            and not _SMALL_OK.search(title or ""))
