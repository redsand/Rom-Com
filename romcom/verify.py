"""Does what we downloaded actually look like a game for the system we asked about?

A direct source answers a title search with whatever it has under that name. Searching for a
Nintendo DS title returned a music album, and the pipeline accepted it: the item went
DOWNLOADED, the tracks were adopted as games, and the first anyone knew was melonDS
answering "ROM isn't valid, did you select the right file?".

Nothing here inspects a rom's internals. The question is only whether the shape of what
arrived is consistent with the request, which is enough to catch an album, a PDF scan or an
installer, and cheap enough to run on every download.
"""
import zipfile
from pathlib import Path

from .scanner import ARCADE_SET_EXTS, EXT_SYSTEM, NOT_GAME_EXTS

# Words that identify a release as something other than a game, wherever they appear in the
# name. Checked before downloading, because the cheapest bad download is the one skipped.
RELEASE_SMELLS = ("flac", "320kbps", "192kbps", "discography", "vinyl", "soundtrack",
                  "ost ", " ost", "audiobook", "ebook", "epub", "bluray", "bdrip",
                  "dvdrip", "x264", "x265", "hdtv", "webrip", "xxx", "porn", "crack only",
                  "keygen", "patch only")

ARCHIVES = {".zip", ".7z", ".rar", ".tar", ".gz"}


# Ancillary files that ship beside content and say nothing about it either way.
JUNK_EXTS = set(NOT_GAME_EXTS) | {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".txt", ".ini",
                                  ".cfg", ".dat", ".db", ".sav"}


def classify(name, system):
    """"game", "junk", or "unknown" for one filename.

    Three outcomes rather than a boolean, because the middle one matters. Roms turn up under
    extensions nothing has heard of — every MAME chip is .ic27 or .u5 or nothing at all — so
    an unrecognised name cannot be treated as evidence against a download. Only a bundle that
    is entirely ancillary is evidence.
    """
    p = Path(name)
    ext = p.suffix.lower()
    inner = Path(p.stem).suffix.lower()
    if ext in ARCHIVES or ext in ARCADE_SET_EXTS:
        return "game"
    # A rom extension for this system wins, including one hiding under a decoy suffix:
    # `Stargate.smc.ttf` is a real 2 MB cartridge wearing a font extension.
    for candidate in (ext, inner):
        if EXT_SYSTEM.get(candidate) == (system or "").lower():
            return "game"
    # An extension belonging to a *different* system is evidence against — that is how nes
    # roms came to be filed under arcade.
    if EXT_SYSTEM.get(ext):
        return "junk"
    if ext in JUNK_EXTS and inner not in EXT_SYSTEM:
        return "junk"
    return "unknown"


def _plausible_name(name, system):
    return classify(name, system) != "junk"


def result_smells_wrong(result):
    """A reason to skip a search result before downloading it, or None.

    Deliberately conservative: a false positive here means a game silently never downloads,
    which is worse than the occasional wasted fetch.
    """
    text = " ".join(str(result.get(k) or "") for k in ("title", "name", "url")).lower()
    for smell in RELEASE_SMELLS:
        if smell in text:
            return f"result looks like a non-game release ({smell.strip()!r} in its name)"
    return None


def check_download(path, system, sample=40):
    """Inspect what landed. Returns (ok, reason).

    Accepts a file or a directory. For an archive the member names are read — the extension
    of a zip says nothing, and its contents say everything.
    """
    p = Path(path)
    if not p.exists():
        return False, f"{p} does not exist"

    names = []
    if p.is_dir():
        names = [f.name for f in p.rglob("*") if f.is_file()][:400]
        if not names:
            return False, "downloaded directory is empty"
    else:
        names = [p.name]
        if p.suffix.lower() in ARCHIVES and zipfile.is_zipfile(p):
            try:
                with zipfile.ZipFile(p) as z:
                    members = [n for n in z.namelist() if not n.endswith("/")][:sample]
                if members:
                    names = members
            except (OSError, zipfile.BadZipFile):
                pass                     # unreadable archive: judge it by its own name

    kinds = [classify(n, system) for n in names]
    if "game" in kinds:
        return True, None
    if "unknown" in kinds:
        # Unrecognised is not wrong. Arcade sets are chip images with extensions like .ic27,
        # and rejecting those would refuse most of what this library actually holds.
        return True, None
    seen = sorted({Path(n).suffix.lower() or "(none)" for n in names})[:5]
    return False, (f"nothing in the download looks like a {system} game "
                   f"(found {', '.join(seen)})")
