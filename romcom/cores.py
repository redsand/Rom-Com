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
