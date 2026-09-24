"""Write RetroArch playlists from this catalog instead of from RetroArch's scanner.

RetroArch's scan only adds a file whose CRC appears in its own database, and silently skips
everything else. Measured against this library that is most of it: 1,866 of 29,181 GBA roms,
328 of 3,272 NES, 244 of 707 SNES. Nothing tells you the rest were passed over — the
playlist simply looks complete.

This catalog already knows what is on disk, what its real title is, and which of it you chose
to keep, so it can write the playlist directly: every game, correctly named, pointed at the
core configured for that system. Written under separate names by default, because an archive
tool has no business overwriting the work of another program without being asked.
"""
import json
from pathlib import Path

from .db import connect
from .player import emulator_options, rompath

# RetroArch identifies a playlist by its database name, which must match its thumbnail packs
# exactly or artwork silently stops working.
DB_NAMES = {
    "nes": "Nintendo - Nintendo Entertainment System",
    "snes": "Nintendo - Super Nintendo Entertainment System",
    "n64": "Nintendo - Nintendo 64",
    "gb": "Nintendo - Game Boy",
    "gbc": "Nintendo - Game Boy Color",
    "gba": "Nintendo - Game Boy Advance",
    "nds": "Nintendo - Nintendo DS",
    "virtualboy": "Nintendo - Virtual Boy",
    "gamecube": "Nintendo - GameCube",
    "wii": "Nintendo - Wii",
    "mastersystem": "Sega - Master System - Mark III",
    "gamegear": "Sega - Game Gear",
    "genesis": "Sega - Mega Drive - Genesis",
    "segacd": "Sega - Mega-CD - Sega CD",
    "32x": "Sega - 32X",
    "saturn": "Sega - Saturn",
    "dreamcast": "Sega - Dreamcast",
    "ps1": "Sony - PlayStation",
    "psp": "Sony - PlayStation Portable",
    "pcengine": "NEC - PC Engine - TurboGrafx 16",
    "lynx": "Atari - Lynx",
    "wonderswan": "Bandai - WonderSwan",
    "atari2600": "Atari - 2600",
    "atari7800": "Atari - 7800",
    "c64": "Commodore - 64",
    "amiga": "Commodore - Amiga",
    "3do": "The 3DO Company - 3DO",
    "dos": "DOS",
    "arcade": "MAME",
}


def playlists_dir():
    """RetroArch's playlists folder, derived from wherever its cores were configured."""
    for _name, template in emulator_options("gba") or emulator_options("nes"):
        for token in template.replace('"', " ").split():
            if token.lower().endswith(".dll"):
                return Path(token).parent.parent / "playlists"
    return None


def _core_for(system):
    """(core_path, core_name) so RetroArch launches without asking which core to use."""
    for _name, template in emulator_options(system):
        toks = template.replace('"', " ").split()
        for t in toks:
            if t.lower().endswith(".dll"):
                return str(Path(t)), Path(t).stem.replace("_libretro", "")
    return "DETECT", "DETECT"


def build(systems=None, keep_only=False, dest=None, replace=False, db=None):
    """Write one playlist per system. Returns {system: count}."""
    db = db or connect()
    dest = Path(dest) if dest else playlists_dir()
    if not dest:
        return {"error": "RetroArch playlists folder not found — is a core configured?"}
    dest.mkdir(parents=True, exist_ok=True)

    wanted = {s.lower() for s in systems} if systems else None
    written, skipped, unloadable = {}, {}, {}
    for system, dbname in sorted(DB_NAMES.items()):
        if wanted and system not in wanted:
            continue
        core_path, core_name = _core_for(system)
        if system == "arcade":
            # MAME is driven by set name out of a rompath, not by a file path, so a playlist
            # of chip files would be meaningless. Point at the staged set directories.
            root = rompath()
            if not root or not Path(root).exists():
                skipped[system] = "no staged rompath yet — run: romcom organize <dir> --system arcade"
                continue
            rows = [{"path": str(Path(root) / r["external_id"].split("/", 1)[-1]),
                     "label": r["title"], "crc": ""}
                    for r in db.execute(
                        "SELECT title, external_id FROM items WHERE system='arcade'"
                        " AND playable=1 AND COALESCE(is_device,0)=0"
                        + (" AND keep=1" if keep_only else "") + " ORDER BY title")
                    if (Path(root) / r["external_id"].split("/", 1)[-1]).exists()]
        else:
            # One entry per game, not per file. The same rom sits in several folders here
            # (a set, its -processed copy, and a per-system tree), and a playlist listing
            # each one shows the same game three times with nothing to tell them apart.
            from .player import rom_for
            best = {}
            for r in db.execute(
                    """SELECT i.id, i.title, f.path, f.crc32, f.bytes, i.system FROM items i
                       JOIN files f ON f.matched_item_id = i.id
                       WHERE i.system = ? AND COALESCE(f.content,1)=1 """
                    + ("AND i.keep=1 " if keep_only else "")
                    + "ORDER BY i.title", (system,)):
                best.setdefault(r["id"], []).append(dict(r))
            rows = []
            for iid, files in best.items():
                pick = rom_for(db, {"id": iid, "system": system}) if len(files) > 1 else files[0]["path"]
                chosen = next((x for x in files if x["path"] == pick), files[0])
                rows.append({"path": chosen["path"], "label": chosen["title"],
                             "crc": (chosen["crc32"] or "").upper()})
            rows.sort(key=lambda x: x["label"].lower())
        if not rows:
            continue
        name = (dbname if replace else f"Rom-Com - {dbname}") + ".lpl"
        (dest / name).write_text(json.dumps({
            "version": "1.5",
            "default_core_path": core_path if core_path != "DETECT" else "",
            "default_core_name": core_name if core_name != "DETECT" else "",
            "label_display_mode": 0, "right_thumbnail_mode": 0, "left_thumbnail_mode": 0,
            "thumbnail_match_mode": 0, "sort_mode": 0,
            "items": [{"path": r["path"], "label": r["label"],
                       "core_path": core_path, "core_name": core_name,
                       "crc32": (r["crc"] + "|crc") if r["crc"] else "00000000|crc",
                       # db_name stays the canonical one even when the file is renamed, or
                       # RetroArch stops finding the matching thumbnail pack.
                       "db_name": dbname + ".lpl"}
                      for r in rows],
        }, indent=2), encoding="utf-8")
        written[system] = len(rows)
        # Entries RetroArch will list but refuse to load: the only copy we hold wears a
        # decoy extension (`Stargate.smc.wmf`). Reported rather than hidden — we do have the
        # game, and pretending the playlist is fully playable would be the dishonest part.
        from .scanner import EXT_SYSTEM
        odd = sum(1 for r in rows
                  if system != "arcade"
                  and EXT_SYSTEM.get(Path(r["path"]).suffix.lower()) != system
                  and Path(r["path"]).suffix.lower() not in (".zip", ".7z"))
        if odd:
            unloadable[system] = odd
    return {"dest": str(dest), "written": written, "skipped": skipped,
            "unloadable": unloadable, "total": sum(written.values()),
            "replaced_retroarch_own": bool(replace)}
