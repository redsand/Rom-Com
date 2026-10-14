"""A card ES-DE (and RetroArch, and the other front ends) can just open.

Three things decide whether a game a library holds shows up and starts on a handheld:

1. **The folder name.** ES-DE finds a system by its own short name — `psx`, not `ps1`;
   `gc`, not `gamecube`. A folder it does not know is ignored, games and all.
2. **The file type.** Each ES-DE system lists the extensions it launches; a release's
   .nfo/.txt/.jpg is not a game and only clutters the list. Cartridge systems run from
   .zip/.7z (their RetroArch cores read archives); disc emulators do not, and nothing on
   Android reads .rar — those are unpacked onto the card.
3. **One entry per game.** A disc dumped as .cue + .bin tracks (or .gdi + tracks) would list
   every track. ES-DE's "directory interpreted as file" fixes it: the files go in a folder
   named after the game's main file (`Game (USA).cue/Game (USA).cue`), which ES-DE shows and
   launches as one game.

`gamelist.xml` (ES-DE's own metadata file) carries each game's clean name, its rating
(the library's score, 0-1) and genre, merged into whatever ES-DE already wrote there.
"""
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

# Our system slug -> ES-DE's folder name, where they differ.
FOLDERS = {
    "ps1": "psx", "gamecube": "gc", "3ds": "n3ds", "vita": "psvita", "32x": "sega32x",
    "neogeopocketcolor": "ngpc", "neogeopocket": "ngp", "lynx": "atarilynx",
    "cdi": "cdimono1",
}

# Each arcade romset into the ES-DE system whose default emulator speaks it: `arcade`
# launches MAME (current), `fbneo` FinalBurn Neo, `mame` MAME 2003-Plus. An FBNeo zip in
# `arcade` is handed to current MAME, and many will not start there.
ARCADE_FOLDERS = {"mame": "arcade", "fbneo": "fbneo", "mame2003": "mame"}

_ARCH = (".zip", ".7z")
# ES-DE's launchable extensions per system (its es_systems.xml, the parts that matter for a
# library like this). Systems whose list includes .zip/.7z are cartridge systems: an
# archive goes on the card as it is.
EXTS = {
    "nes": (".nes", ".fds", ".unf", ".unif") + _ARCH,
    "snes": (".smc", ".sfc", ".swc", ".fig", ".bs", ".st") + _ARCH,
    "n64": (".n64", ".v64", ".z64", ".ndd") + _ARCH,
    "gb": (".gb", ".sgb") + _ARCH,
    "gbc": (".gbc", ".gb") + _ARCH,
    "gba": (".gba", ".agb", ".mb") + _ARCH,
    "nds": (".nds", ".dsi") + _ARCH,
    "genesis": (".md", ".gen", ".smd", ".bin", ".68k", ".sgd") + _ARCH,
    "megadrive": (".md", ".gen", ".smd", ".bin", ".68k", ".sgd") + _ARCH,
    "mastersystem": (".sms", ".bin") + _ARCH,
    "gamegear": (".gg", ".bin") + _ARCH,
    "sega32x": (".32x", ".bin") + _ARCH,
    "pcengine": (".pce", ".sgx") + _ARCH,
    "ngp": (".ngp", ".ngc") + _ARCH,
    "ngpc": (".ngc", ".ngp", ".ngpc") + _ARCH,
    "atari2600": (".a26", ".bin") + _ARCH,
    "atari5200": (".a52", ".bin") + _ARCH,
    "atari7800": (".a78", ".bin") + _ARCH,
    "atarilynx": (".lnx", ".o") + _ARCH,
    "virtualboy": (".vb", ".vboy") + _ARCH,
    "wonderswan": (".ws",) + _ARCH,
    "wonderswancolor": (".wsc",) + _ARCH,
    "c64": (".d64", ".t64", ".prg", ".crt", ".tap", ".g64") + _ARCH,
    "msx": (".rom", ".mx1", ".mx2", ".dsk", ".cas") + _ARCH,
    "amiga": (".adf", ".hdf", ".lha", ".ipf", ".dms") + _ARCH,
    "atarist": (".st", ".stx", ".msa", ".ipf") + _ARCH,
    "dos": (".dosz", ".exe", ".com", ".bat", ".conf") + _ARCH,
    "scummvm": (".scummvm", ".svm") + _ARCH,
    # Disc systems: the emulator reads the image, never an archive of it.
    "psx": (".cue", ".chd", ".pbp", ".m3u", ".iso", ".img", ".ccd", ".mdf", ".ecm", ".toc",
            ".bin"),
    "ps2": (".iso", ".chd", ".cso", ".zso", ".cue", ".m3u", ".bin"),
    "ps3": (".iso", ".ps3", ".pkg"),
    "psp": (".iso", ".cso", ".pbp", ".chd"),
    "psvita": (".vpk", ".psvita"),
    "dreamcast": (".gdi", ".cdi", ".chd", ".cue", ".iso", ".m3u"),
    "saturn": (".cue", ".chd", ".ccd", ".iso", ".m3u", ".mds"),
    "segacd": (".cue", ".chd", ".iso", ".m3u"),
    "pcenginecd": (".cue", ".chd", ".ccd", ".m3u"),
    "gc": (".iso", ".rvz", ".gcm", ".gcz", ".ciso", ".wbfs", ".dol"),
    "wii": (".iso", ".rvz", ".wbfs", ".wia", ".gcz", ".wad", ".dol"),
    "n3ds": (".3ds", ".cia", ".cxi", ".3dsx", ".app"),
    "cdimono1": (".cue", ".chd", ".iso"),
    "3do": (".iso", ".chd", ".cue"),
    "fmtowns": (".cue", ".chd", ".iso", ".m3u"),
    "amigacd32": (".cue", ".chd", ".iso"),
}

# A disc's main file, best first; the tracks it names ride along beside it.
_PRIMARY = (".m3u", ".cue", ".gdi", ".ccd", ".mds", ".toc")
_TRACKS = {".bin", ".img", ".raw", ".sub", ".mdf", ".ecm", ".iso", ".wav"}
UNPACK = (".zip", ".7z", ".rar")


def folder(system):
    s = (system or "unknown").lower()
    return FOLDERS.get(s, s)


def single_file(system):
    """A cartridge system: one game is one file (its list takes .zip as a whole game)."""
    return ".zip" in EXTS.get(folder(system), ()) and folder(system) not in ("dos", "scummvm")


def playable(system, name):
    """Can the system's ES-DE entry launch a file called `name`? None when the system has
    no list here (an unknown system: copied as before)."""
    exts = EXTS.get(folder(system))
    if exts is None:
        return None
    return Path(name).suffix.lower() in exts


def seven_zip():
    for c in (shutil.which("7z"), r"C:\Program Files\7-Zip\7z.exe",
              r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if c and Path(c).exists():
            return c
    return None


_listing = {}


def members(archive):
    """[(inner path, size)] of an archive's files, from its headers (no extraction)."""
    archive = Path(archive)
    key = (str(archive), archive.stat().st_mtime)
    if key in _listing:
        return _listing[key]
    out = []
    if archive.suffix.lower() == ".zip":
        import zipfile
        try:
            with zipfile.ZipFile(archive) as z:
                out = [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]
        except (zipfile.BadZipFile, OSError, NotImplementedError):
            out = []                     # 7-Zip below reads what zipfile will not
    if not out:
        exe = seven_zip()
        if exe:
            try:
                p = subprocess.run([exe, "l", "-slt", "-ba", str(archive)], capture_output=True,
                                   stdin=subprocess.DEVNULL,
                                   text=True, timeout=300, errors="replace")
                cur = {}
                for line in (p.stdout or "").splitlines() + [""]:
                    if not line.strip():
                        if cur.get("Path") and cur.get("Folder", "-") != "+":
                            out.append((cur["Path"], int(cur.get("Size") or 0)))
                        cur = {}
                    elif " = " in line:
                        k, v = line.split(" = ", 1)
                        cur[k.strip()] = v.strip()
            except (OSError, subprocess.TimeoutExpired):
                out = []
    _listing[key] = out or []
    return _listing[key]


def extract(archive, names, target_dir):
    """Unpack `names` (inner paths) from `archive` flat into `target_dir`."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    if Path(archive).suffix.lower() == ".zip":
        import zipfile
        try:
            with zipfile.ZipFile(archive) as z:
                for n in names:
                    with z.open(n) as src, open(target_dir / Path(n).name, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
            return
        except (zipfile.BadZipFile, NotImplementedError):
            pass                         # deflate64 and friends: 7-Zip reads them
    exe = seven_zip()
    if not exe:
        raise OSError("7-Zip is needed to unpack .7z/.rar for the card, and was not found")
    p = subprocess.run([exe, "e", "-y", f"-o{target_dir}", str(archive), *names],
                       stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=7200, errors="replace")
    if p.returncode != 0:
        raise OSError(f"7-Zip could not unpack {Path(archive).name}: "
                      f"{(p.stderr or p.stdout or '').strip()[-200:]}")


def _main_file(names):
    """The file ES-DE should list for a set of disc files, or None for 'each is a game'."""
    by_ext = {}
    for n in names:
        by_ext.setdefault(Path(n).suffix.lower(), []).append(n)
    for ext in _PRIMARY:
        if by_ext.get(ext):
            return sorted(by_ext[ext], key=lambda n: (len(n), n))[0]
    return None


def plan(system, files, chd=False):
    """How one game's files reach the card, as ES-DE wants them.

    `files`: [(source path, size)] for one catalog item. Returns (steps, entries, dropped):
    steps are ("copy", src, rel, size), ("extract", archive, [inner names], rel_dir,
    size) or ("chd", src, [inner names] | None, main, rel, est_size); entries are the
    paths ES-DE lists for the game (relative to the system folder); dropped counts files
    that are no game for this system (.nfo, .txt, a .rar of nothing launchable).

    With `chd` (and chdman at hand), a disc image a CHD can hold — a .cue/.gdi/.toc and
    its tracks, a PS2 .iso — becomes one compressed .chd: a single file per game, about
    the size of the archive it came in."""
    sys_ = folder(system)
    exts = EXTS.get(sys_)
    if exts is None:                                  # unknown system: as before
        steps = [("copy", src, Path(src).name, size) for src, size in files]
        return steps, [Path(src).name for src, _ in files], 0
    keep, unpack, dropped = [], [], 0
    for src, size in files:
        ext = Path(src).suffix.lower()
        if ext == ".cue" and not _cue_tracks_present(src):
            dropped += 1                   # nothing can play a .cue without its tracks
            continue
        if ext in exts and ext != ".rar":
            keep.append((src, size))
        elif ext in UNPACK:
            inner = [(n, s) for n, s in members(src)
                     if Path(n).suffix.lower() in exts or Path(n).suffix.lower() in _TRACKS]
            inner = [(n, s) for n, s in inner if not Path(n).name.startswith(".")]
            if any(Path(n).suffix.lower() in exts for n, _ in inner):
                unpack.append((src, inner))
            else:
                dropped += 1
        else:
            dropped += 1
    steps, entries = [], []
    if chd and sys_ in CHD_SYSTEMS and chdman():
        # Loose: the main file converts in place, its tracks beside it. Archived: each
        # archive that holds a convertible disc is unpacked to a temp dir and converted.
        names = [Path(s).name for s, _ in keep]
        main = _chd_main(sys_, names)
        if (not main and len(keep) == 1 and sys_ in _RAW_BIN
                and names[0].lower().endswith(".bin") and keep[0][1] % 2352 == 0):
            main = names[0]                # a lone track: make_chd writes it a .cue
        if main:
            src = next(s for s, _ in keep if Path(s).name == main)
            rel = f"{Path(main).stem}.chd"
            steps.append(("chd", src, None, main, rel, int(sum(z for _, z in keep) * 0.6)))
            entries.append(rel)
            keep = []
        rest = []
        for src, inner in unpack:
            main = _chd_main(sys_, [Path(n).name for n, _ in inner])
            if main:
                rel = f"{Path(main).stem}.chd"
                steps.append(("chd", src, [n for n, _ in inner], main, rel,
                              Path(src).stat().st_size))
                entries.append(rel)
            else:
                rest.append((src, inner))
        unpack = rest
    # Loose files of one disc (a .cue and its .bin) stay together like an unpacked one.
    loose_names = [Path(s).name for s, _ in keep]
    main = _main_file(loose_names) if len(keep) > 1 else None
    if main:
        d = main                                       # "Game.cue/" holding Game.cue + tracks
        steps += [("copy", s, f"{d}/{Path(s).name}", z) for s, z in keep]
        entries.append(d)
    else:
        steps += [("copy", s, Path(s).name, z) for s, z in keep]
        entries += [Path(s).name for s, _ in keep]
    for src, inner in unpack:
        names = [n for n, _ in inner]
        size = sum(s for _, s in inner)
        flat = [Path(n).name for n in names]
        main = _main_file(flat)
        if main and len(flat) > 1:
            steps.append(("extract", src, names, main, size))
            entries.append(main)
        else:
            steps.append(("extract", src, names, "", size))
            entries += [f for f in flat if Path(f).suffix.lower() in exts]
    return steps, entries, dropped


# Disc systems whose ES-DE emulators read CHD (RetroArch's Beetle/SwanStation, Flycast,
# Beetle Saturn, Genesis Plus GX, PCE CD, Opera, SAME CDi; NetherSX2 for PS2).
CHD_SYSTEMS = {"psx", "ps2", "dreamcast", "saturn", "segacd", "pcenginecd", "3do",
               "cdimono1", "fmtowns", "amigacd32"}


def _chd_main(sys_, names):
    """The file chdman converts for this disc, or None: a .cue/.gdi/.toc, or a PS2 .iso
    (a DVD). A DiscJuggler .cdi or a lone .bin is not something chdman reads."""
    for ext in (".gdi", ".cue", ".toc"):
        hit = sorted(n for n in names if n.lower().endswith(ext))
        if hit:
            return hit[0]
    if sys_ == "ps2":
        hit = sorted(n for n in names if n.lower().endswith(".iso"))
        if len(hit) == 1:
            return hit[0]
    return None


# A lone .bin is a single raw track: its mode, for the .cue chdman needs to read it.
_RAW_BIN = {"psx": "MODE2/2352", "saturn": "MODE1/2352", "segacd": "MODE1/2352",
            "pcenginecd": "MODE1/2352"}


def _cue_tracks_present(cue):
    """Every FILE a loose .cue names is beside it on disk (a .cue naming none is given
    the benefit of the doubt)."""
    try:
        text = Path(cue).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    names = re.findall(r'^\s*FILE\s+"([^"]+)"', text, re.M | re.I) or \
        re.findall(r"^\s*FILE\s+(\S+)", text, re.M | re.I)
    return all((Path(cue).parent / n).exists() for n in names)


def chdman():
    """chdman.exe: on PATH, or beside the MAME the arcade launcher is set up with."""
    c = shutil.which("chdman")
    if c:
        return c
    try:
        from .player import emulator_options
        for _name, template in emulator_options("arcade"):
            for token in template.replace('"', " ").split():
                if token.lower().endswith("mame.exe"):
                    cand = Path(token).with_name("chdman.exe")
                    if cand.exists():
                        return str(cand)
    except Exception:
        pass
    return None


def make_chd(src, names, main, target, system):
    """Convert one disc to `target` (.chd). `names` set: `src` is an archive, unpacked to a
    temp dir first; None: `src` is the loose main file, its tracks beside it. Written to a
    .part file and renamed, so a .chd on the card is always a finished one."""
    import tempfile
    exe = chdman()
    if not exe:
        raise OSError("chdman (it ships with MAME) is needed to make CHDs, and was not found")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    with tempfile.TemporaryDirectory(prefix="romcom-chd-") as tmp:
        if names is not None:
            extract(src, names, tmp)
            inp = Path(tmp) / main
        else:
            inp = Path(src)
        if inp.suffix.lower() == ".bin":
            # A lone track: describe it to chdman as the single-track disc it is.
            # chdman reads a FILE line relative to the .cue, whatever it says, so the
            # track sits beside it: linked where the volume allows, else copied.
            if inp.parent != Path(tmp):
                local = Path(tmp) / inp.name
                try:
                    os.link(inp, local)
                except OSError:
                    shutil.copyfile(inp, local)
                inp = local
            cue = Path(tmp) / (inp.stem + ".cue")
            cue.write_text(f'FILE "{inp.name}" BINARY\n  TRACK 01 {_RAW_BIN.get(folder(system), "MODE2/2352")}\n'
                           f"    INDEX 01 00:00:00\n", encoding="utf-8")
            inp = cue
        cmd = [exe, "createdvd" if folder(system) == "ps2" and main.lower().endswith(".iso")
               else "createcd", "-i", str(inp), "-o", str(part), "-f"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=4 * 3600,
                           stdin=subprocess.DEVNULL,
                           errors="replace")
        if p.returncode != 0 or not part.exists():
            try:
                part.unlink()
            except OSError:
                pass
            raise OSError(f"chdman could not convert {Path(main).name}: "
                          f"{(p.stderr or p.stdout or '').strip()[-300:]}")
    os.replace(part, target)


_TAGS = re.compile(r"\s*[(\[][^)\]]*[)\]]")


_ARTICLE = re.compile(r"^(?P<head>[^-]+?), (?P<art>The|A|An)(?P<tail>\s+-\s+.*)?$")


def display_name(title):
    """'Super Mario World (USA) (Rev 1)' -> 'Super Mario World'; No-Intro's 'Legend of
    Zelda, The - Four Swords' -> 'The Legend of Zelda - Four Swords', as the box says.
    (ES-DE can ignore a leading article when it sorts.)"""
    name = _TAGS.sub("", title or "").strip()
    m = _ARTICLE.match(name)
    if m:
        name = f"{m['art']} {m['head']}{m['tail'] or ''}"
    return name or (title or "").strip()


def write_gamelists(dest, games, media_dir=None):
    """Merge each game's name, rating and genre into <dest>/ES-DE/gamelists/<folder>/
    gamelist.xml. `games`: {folder: [{"path": rel, "name", "rating" (0-1 or None),
    "genre"}]}. ES-DE's own fields (play count, favorite, last played) are kept; only
    entries this export wrote are touched. Returns {folder: entries written}."""
    out = {}
    root_dir = Path(dest) / "ES-DE" / "gamelists"
    for fold, entries in games.items():
        path = root_dir / fold / "gamelist.xml"
        try:
            tree = ET.parse(path)
            root = tree.getroot()
        except (OSError, ET.ParseError):
            root = ET.Element("gameList")
            tree = ET.ElementTree(root)
        index = {}
        for g in root.findall("game"):
            p = (g.findtext("path") or "").strip()
            index[p] = g
        for e in entries:
            rel = "./" + e["path"].replace("\\", "/")
            g = index.get(rel)
            if g is None:
                g = ET.SubElement(root, "game")
                ET.SubElement(g, "path").text = rel
                index[rel] = g
            for tag, val in (("name", e.get("name")), ("genre", e.get("genre")),
                             ("rating", None if e.get("rating") is None
                              else f"{e['rating']:.2f}")):
                if not val:
                    continue
                el = g.find(tag)
                if el is None:
                    el = ET.SubElement(g, tag)
                el.text = val
        path.parent.mkdir(parents=True, exist_ok=True)
        ET.indent(tree)
        tmp = path.with_suffix(".xml.tmp")
        tree.write(tmp, encoding="utf-8", xml_declaration=True)
        os.replace(tmp, path)
        out[fold] = len(entries)
    return out


def rating(score):
    """The library's 0-100 score as ES-DE's 0-1 rating, in its half-star steps."""
    if not score:
        return None
    return round(min(100, max(0, score)) / 10) / 10
