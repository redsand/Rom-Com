"""Which libretro core each system needs, why that one, and fetching it.

Choosing a core is the part that sends people hunting through forums, because the tradeoff
is rarely stated plainly: the most accurate core for a system is usually the slowest, and on
a desktop that difference is invisible. So every recommendation here is the accurate one
unless it is genuinely too heavy, and the alternative is named with the reason you would
switch.

`confidence` is about the recommendation, not the core's quality:
  certain  — one obvious answer, no real debate
  strong   — a clear favourite, alternatives are niche
  depends  — a real tradeoff; read the note before accepting the default
"""
import re
from pathlib import Path

CORES = {
    # system: (file, display, why, confidence, alternative)
    "nes":        ("mesen_libretro", "Mesen",
                   "the most accurate NES emulator there is, and still effortless on a PC",
                   "certain", "nestopia_libretro (lighter, fine for most carts)"),
    "snes":       ("snes9x_libretro", "Snes9x",
                   "accurate enough that you will not notice, and runs anywhere",
                   "strong", "bsnes_hd_beta_libretro (cycle-accurate + widescreen hacks, much heavier)"),
    "n64":        ("mupen64plus_next_libretro", "Mupen64Plus-Next",
                   "the maintained N64 core; ParaLLEl is more accurate but needs Vulkan",
                   "depends", "parallel_n64_libretro (accurate RDP, Vulkan only)"),
    "gb":         ("gambatte_libretro", "Gambatte",
                   "the reference Game Boy core",
                   "certain", "sameboy_libretro (even more accurate, also excellent)"),
    "gbc":        ("gambatte_libretro", "Gambatte",
                   "same core covers Color",
                   "certain", "sameboy_libretro"),
    "gba":        ("mgba_libretro", "mGBA",
                   "accurate, fast, and handles the awkward carts (solar, rumble, tilt)",
                   "certain", "vbam_libretro (older, fewer quirks handled)"),
    # The buildbot name is `melondsds_libretro`, not `melonds_ds_libretro` — the obvious
    # spelling 404s, which is exactly the kind of hunt this module exists to remove.
    "nds":        ("melondsds_libretro", "melonDS DS",
                   "the actively developed DS core; DeSmuME is the fallback if it misbehaves",
                   "strong", "desmume_libretro (older, slower, handles odd dumps)"),
    "virtualboy": ("mednafen_vb_libretro", "Beetle VB",
                   "the only serious Virtual Boy option",
                   "certain", None),
    "mastersystem": ("genesis_plus_gx_libretro", "Genesis Plus GX",
                     "one core covers Master System, Game Gear, Genesis and Sega CD",
                     "certain", None),
    "gamegear":   ("genesis_plus_gx_libretro", "Genesis Plus GX",
                   "same core as Master System",
                   "certain", None),
    "genesis":    ("genesis_plus_gx_libretro", "Genesis Plus GX",
                   "the accurate Genesis core, and it covers the whole 8/16-bit Sega line",
                   "certain", "picodrive_libretro (needed for 32X)"),
    "segacd":     ("genesis_plus_gx_libretro", "Genesis Plus GX",
                   "needs Sega CD BIOS files in RetroArch's system folder",
                   "strong", None),
    "32x":        ("picodrive_libretro", "PicoDrive",
                   "Genesis Plus GX does NOT do 32X — this is the core for it",
                   "certain", None),
    "saturn":     ("mednafen_saturn_libretro", "Beetle Saturn",
                   "accurate but demanding; needs Saturn BIOS",
                   "depends", "yabasanshiro_libretro (much faster, less accurate)"),
    "dreamcast":  ("flycast_libretro", "Flycast",
                   "the Dreamcast core, no real competition",
                   "certain", None),
    "ps1":        ("swanstation_libretro", "SwanStation",
                   "the maintained DuckStation fork: fast, accurate, upscales well",
                   "strong", "mednafen_psx_hw_libretro (Beetle PSX HW, more accurate, heavier)"),
    "psp":        ("ppsspp_libretro", "PPSSPP",
                   "the only PSP core",
                   "certain", None),
    "pcengine":   ("mednafen_pce_libretro", "Beetle PCE",
                   "covers TurboGrafx-16 and PC Engine CD",
                   "certain", None),
    "lynx":       ("handy_libretro", "Handy",
                   "the standard Lynx core",
                   "strong", "mednafen_lynx_libretro"),
    "wonderswan": ("mednafen_wswan_libretro", "Beetle WonderSwan",
                   "the only maintained option",
                   "certain", None),
    "atari2600":  ("stella_libretro", "Stella",
                   "the reference 2600 emulator",
                   "certain", None),
    "atari7800":  ("prosystem_libretro", "ProSystem",
                   "the standard 7800 core",
                   "certain", None),
    "c64":        ("vice_x64_libretro", "VICE x64",
                   "use vice_x64sc_libretro instead if a demo or cracktro misbehaves",
                   "depends", "vice_x64sc_libretro (cycle-exact, slower)"),
    "amiga":      ("puae_libretro", "PUAE",
                   "needs Kickstart ROMs in RetroArch's system folder or nothing boots",
                   "strong", "uae4arm_libretro"),
    "3do":        ("opera_libretro", "Opera",
                   "the 3DO core; needs a 3DO BIOS",
                   "certain", None),
    "dos":        ("dosbox_pure_libretro", "DOSBox Pure",
                   "mounts zips and folders directly, no manual DOSBox config",
                   "certain", "dosbox_svn_libretro (classic, needs conf files)"),
    "gamecube":   ("dolphin_libretro", "Dolphin",
                   "standalone Dolphin is considerably better than the libretro core here",
                   "depends", "standalone Dolphin (recommended for GameCube/Wii)"),
    "wii":        ("dolphin_libretro", "Dolphin",
                   "standalone Dolphin is considerably better than the libretro core here",
                   "depends", "standalone Dolphin (recommended for GameCube/Wii)"),
}

BUILDBOT = "https://buildbot.libretro.com/nightly/windows/x86_64/latest/{core}.dll.zip"

# Console BIOS the cores cannot boot without — a missing one here means "some games
# cannot be played", not "fewer options". Each slot lists one or more alternative
# filenames for the same job (regions, board revisions); any one file in a slot
# satisfies it, and RetroArch reads every file by these names straight from its System
# Directory. Filenames are matched case-insensitively; some cores also take a zip twin
# of the same name, which this match accepts by ignoring nothing — the plain name is
# what the core's docs promise. Deliberately absent:
#   n64     — Mupen64Plus-Next needs no BIOS (the obvious suspect, and innocent)
#   ps1     — SwanStation boots without a BIOS; only Beetle PSX HW demands one
#   pcengine — only CD games need a System Card, and cart-only libraries would be
#              flagged needlessly
BIOS = {
    "dreamcast": {"label": "Dreamcast",
                  "slots": [["dc_boot.bin"], ["dc_flash.bin"]],
                  "dir": "dc",               # Flycast reads system/dc/, not system/
                  # Flycast has carried an HLE BIOS for years: with no dc_boot.bin it boots
                  # games on its own (libretro's docs list both files as optional). The real
                  # BIOS still helps the odd game and shows the boot animation.
                  "optional": True,
                  "why": "optional: Flycast boots games with its built-in HLE BIOS; the real "
                         "dc_boot.bin + dc_flash.bin help a few games and add the boot screen"},
    "saturn":    {"label": "Saturn",
                  "slots": [["mpr-17933.bin", "sega_101.bin", "sega_102.bin"]],
                  "also": ["sega_101.bin"],
                  "why": "Beetle Saturn boots nothing without the console BIOS "
                         "(mpr-17933 = US/EU, sega_101 = Japan)"},
    "segacd":    {"label": "Sega CD",
                  "slots": [["bios_CD_E.bin", "bios_CD_U.bin", "bios_CD_J.bin"]],
                  "also": ["bios_CD_E.bin", "bios_CD_U.bin", "bios_CD_J.bin"],
                  "why": "the disc's boot code comes from the region BIOS — E = Europe, "
                         "U = US, J = Japan"},
    "3do":       {"label": "3DO",
                  "slots": [["panafz10.bin", "panafz10-norsa.bin", "panafz1.bin",
                             "opera_bios.rom"]],
                  "why": "the Opera core will not boot without one"},
    # PS2 BIOS dumps carry the console model in a dozen spellings (SCPH-70012_BIOS_V12_USA_200.BIN,
    # scph39001.bin, ps2-0230a-20080220.bin), so they are found by shape — the name pattern and
    # the 4 MiB a PS2 BIOS always is — not by one filename. They go to system/pcsx2/bios/:
    # RetroArch's PCSX2 core reads them there, and NetherSX2's first-run "Import BIOS" picker
    # is pointed at the same folder. Sony never published one; it comes from the owner's own
    # console, dropped anywhere in the library.
    "ps2":       {"label": "PlayStation 2",
                  "slots": [],
                  "pattern": r"^(scph|ps2[-_ ]|ps2bios|bios[-_ ]?scph).*\.bin$",
                  "size": 4194304,
                  "dir": "pcsx2/bios",
                  "why": "PCSX2 and NetherSX2 boot nothing without a PS2 BIOS dumped from a "
                         "real console (any region; a SCPH-*.bin of 4 MB)"},
    "amiga":     {"label": "Amiga",
                  "slots": [["kick13.rom", "kick33180.A500", "kick34005.A500",
                             "kick40068.A1200", "kick40063.A600"]],
                  # 1.3 boots most A500 games; the A1200's 3.1 is for AGA games, and
                  # PUAE picks the model per game, so every Kickstart there goes along.
                  "also": ["kick34005.A500", "kick33180.A500", "kick40068.A1200",
                           "kick40063.A600"],
                  "why": "PUAE needs a Kickstart ROM in the system folder or nothing boots"},
    "amigacd32": {"label": "Amiga CD32",
                  "slots": [["kick40060.CD32"], ["kick40060.CD32.ext"]],
                  "why": "PUAE boots a CD32 disc only with the CD32 Kickstart and its "
                         "extended ROM"},
    "ps1":       {"label": "PlayStation",
                  "slots": [["scph5501.bin", "scph5500.bin", "scph5502.bin", "scph1001.bin"]],
                  "also": ["scph5500.bin", "scph5501.bin", "scph5502.bin"],
                  "why": "Beetle PSX and SwanStation need the console BIOS (scph5501 = US, "
                         "5500 = Japan, 5502 = Europe); PCSX ReARMed's built-in one runs "
                         "fewer games"},
    "pcenginecd": {"label": "PC Engine CD",
                   "slots": [["syscard3.pce"]],
                   "why": "Beetle PCE boots a CD-ROM game only with the System Card 3.0"},
    "gba":       {"label": "Game Boy Advance",
                  "slots": [["gba_bios.bin"]],
                  "optional": True,
                  "why": "optional: mGBA and gpSP run games without it; the real BIOS "
                         "fixes a few and shows the boot logo"},
    "nds":       {"label": "Nintendo DS",
                  "slots": [["bios7.bin"], ["bios9.bin"], ["firmware.bin"]],
                  "dir": "melonDS DS",       # melonDS DS reads system/melonDS DS/
                  "optional": True,
                  "why": "optional: melonDS DS and DraStic carry their own BIOS; the real "
                         "files help the odd game and the DS menu"},
    "atari5200": {"label": "Atari 5200",
                  "slots": [["5200.rom"]],
                  "why": "a8800 and Atari800 boot 5200 games only with its BIOS"},
    "atarist":   {"label": "Atari ST",
                  "slots": [["tos.img"]],
                  "why": "Hatari needs a TOS ROM to boot"},
}


def bios_files(db):
    """The library's BIOS-shaped files, indexed case-insensitively by filename.

    BIOS data files are not catalog items — import deliberately skips console-bios DATs and
    the dumps land in `files` unmatched — so none of the item-driven queries can ever see
    them. This walks the `files` table and keeps only entries whose basename is a name some
    slot wants.
    """
    want = {n.lower() for spec in BIOS.values()
            for n in [x for slot in spec["slots"] for x in slot] + spec.get("also", [])}
    out = {}
    for r in db.execute("SELECT path, bytes FROM files"):
        name = str(r["path"] or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name in want:
            out.setdefault(name, []).append((r["path"], r["bytes"] or 0))
    return out


def _by_pattern(db, spec):
    """{basename: [(path, bytes)]} of library files matching a BIOS spec's name pattern
    and exact size."""
    pat = re.compile(spec["pattern"], re.I)
    out = {}
    for r in db.execute("SELECT path, bytes FROM files WHERE bytes=?", (spec["size"],)):
        name = str(r["path"] or "").replace("\\", "/").rsplit("/", 1)[-1]
        if pat.match(name):
            out.setdefault(name.lower(), []).append((r["path"], r["bytes"] or 0))
    return out


def bios_report(db, systems=None):
    """Which BIOS slots the library fills, per system — the export's shopping list.

    `systems` (a set of system names) limits the report to the systems being exported.
    For each system: `fills` maps the BIOS filename that will be copied to
    (source path, bytes), taking the first candidate found for a slot; `missing` lists
    the slots nothing in the library fills, each as its alternative filenames.
    """
    found = bios_files(db)
    wanted = set(BIOS) if systems is None else set(BIOS) & set(systems)
    picked, missing = {}, {}
    for system in sorted(wanted):
        spec = BIOS[system]
        if spec.get("pattern"):
            # Found by shape; every distinct dump goes along (the emulator lets you pick
            # a region), keyed by its place under system/ so the copy needs no special case.
            fills = {f"{spec['dir']}/{name}": hits[0]
                     for name, hits in sorted(_by_pattern(db, spec).items())[:8]}
            if fills:
                picked[system] = fills
            else:
                missing[system] = [["SCPH-*.bin (4 MB, from your own PS2)"]]
            continue
        # Keyed by the file's place under system/: a core with its own folder there
        # (Flycast's dc/) gets it, the rest sit at the top.
        at = (lambda n: f"{spec['dir']}/{n}") if spec.get("dir") else (lambda n: n)
        fills, miss = {}, []
        for slot in spec["slots"]:
            for name in slot:
                if name.lower() in found:
                    fills[at(name)] = found[name.lower()][0]
                    break
            else:
                miss.append(slot)
        # The other regions and models go along when the library has them: one slot is
        # enough to boot, but a Japanese disc wants the Japanese BIOS.
        for name in spec.get("also", []):
            if name.lower() in found:
                fills.setdefault(at(name), found[name.lower()][0])
        if fills:
            picked[system] = fills
        if miss:
            missing[system] = miss
    return {"picked": picked, "missing": missing}


def bios_status(db=None):
    """BIOS needs per system with games in the library, plus what's missing and blocked.

    Like `status()`, this is for answering "why can't I play it" — with a console game
    count on each row, because that is the size of what a missing file locks out.
    """
    from .db import connect
    db = db or connect()
    have = {}
    for r in db.execute("""SELECT i.system, COUNT(DISTINCT i.id) n FROM items i
                           JOIN files f ON f.matched_item_id = i.id GROUP BY i.system"""):
        have[(r["system"] or "").lower()] = r["n"]
    report = bios_report(db)
    rows = []
    for system in sorted(BIOS):
        # Only consoles that own games: a missing 3DO BIOS is noise while the library
        # holds no 3DO games, and the doctor check would cry about phantom gaps.
        games = have.get(system, 0)
        if not games:
            continue
        rows.append({"system": system, "label": BIOS[system]["label"],
                     "why": BIOS[system]["why"], "games": games,
                     "optional": bool(BIOS[system].get("optional")),
                     "have": sorted(report["picked"].get(system, {})),
                     "missing": list(report["missing"].get(system, []))})
    missing = [r for r in rows if r["missing"] and not r["optional"]]
    rows.sort(key=lambda r: (not r["missing"], -r["games"]))
    return {"rows": rows, "missing": missing, "missing_count": len(missing),
            "games_blocked": sum(r["games"] for r in missing)}


def cores_dir():
    """RetroArch's cores folder, taken from emulators.yaml so there is one source of truth."""
    from .config import load_yaml
    cfg = load_yaml("emulators.yaml") or {}
    explicit = cfg.get("retroarch_cores")
    if explicit:
        return Path(str(explicit))
    # Otherwise infer it from whatever a -L argument points at.
    for template in (cfg.get("emulators") or {}).values():
        for token in str(template).replace('"', " ").replace("'", " ").split():
            if token.lower().endswith(".dll"):
                return Path(token).parent
    return None


def status(db=None):
    """What each system needs, whether it is installed, and how much it unlocks.

    Sorted by games unlocked, because that is the order worth downloading in — the point is
    to stop hunting, not to produce another list to work through.
    """
    from .db import connect
    db = db or connect()
    have = {}
    for r in db.execute("""SELECT i.system, COUNT(DISTINCT i.id) n FROM items i
                           JOIN files f ON f.matched_item_id = i.id
                           GROUP BY i.system"""):
        have[(r["system"] or "").lower()] = r["n"]
    d = cores_dir()
    rows = []
    for system, games in sorted(have.items(), key=lambda kv: -kv[1]):
        if system == "arcade":
            rows.append({"system": system, "games": games, "core": None, "name": "MAME",
                         "installed": True, "why": "not a libretro core — MAME runs it directly",
                         "confidence": "certain", "alternative": None})
            continue
        spec = CORES.get(system)
        if not spec:
            rows.append({"system": system, "games": games, "core": None, "name": None,
                         "installed": False, "why": "no core mapped yet",
                         "confidence": None, "alternative": None})
            continue
        core, name, why, confidence, alt = spec
        installed = bool(d and (d / f"{core}.dll").exists())
        rows.append({"system": system, "games": games, "core": core, "name": name,
                     "installed": installed, "why": why, "confidence": confidence,
                     "alternative": alt})
    missing = [r for r in rows if r["core"] and not r["installed"]]
    return {"cores_dir": str(d) if d else None, "rows": rows,
            "missing": missing, "missing_count": len(missing),
            "games_blocked": sum(r["games"] for r in missing)}


def install(systems=None, db=None, dest=None, progress=None, all_systems=False):
    """Download the cores this library needs straight from the libretro buildbot.

    RetroArch's own Core Downloader works, but it means knowing which of several cores per
    system to pick and finding each by name in a long list. The library already knows which
    systems have files, and CORES already holds the choice, so neither question needs asking.
    """
    import io
    import urllib.request
    import zipfile
    dest = Path(dest) if dest else cores_dir()
    if not dest:
        raise RuntimeError("RetroArch cores folder is unknown — set `retroarch_cores:` in "
                           "emulators.yaml")
    dest.mkdir(parents=True, exist_ok=True)
    wanted = status(db)["missing"]
    if all_systems:
        # Every system this build knows about, not just those with files today. Acquiring a
        # ps1 game should not then require a second trip to fetch a core.
        wanted = [{"system": sysname, "core": spec[0]} for sysname, spec in sorted(CORES.items())
                  if not (dest / f"{spec[0]}.dll").exists()]
    if systems:
        keep = {s.lower() for s in systems}
        wanted = [r for r in wanted if r["system"] in keep]
    # One core can serve several systems (Genesis Plus GX covers four); download it once.
    seen, out = set(), []
    for r in wanted:
        if r["core"] in seen:
            continue
        seen.add(r["core"])
        url = BUILDBOT.format(core=r["core"])
        if progress:
            progress(r["core"], r["system"])
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                blob = resp.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for member in z.namelist():
                    if member.lower().endswith(".dll"):
                        (dest / Path(member).name).write_bytes(z.read(member))
            out.append({"core": r["core"], "system": r["system"], "ok": True,
                        "bytes": len(blob)})
        except Exception as e:
            out.append({"core": r["core"], "system": r["system"], "ok": False,
                        "error": f"{type(e).__name__}: {e}"})
    return {"dest": str(dest), "results": out,
            "installed": sum(1 for o in out if o["ok"]),
            "failed": [o for o in out if not o["ok"]]}
