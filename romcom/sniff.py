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
