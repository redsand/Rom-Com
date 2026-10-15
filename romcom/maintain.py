"""Library upkeep that RomCom does by itself.

Each step below was once a problem found by hand and fixed with a one-off command: games
left in a .rar in the incoming folder, a download the scan never connected to its game
(the site spelled it 'Millionnaire'), a 1 MB 'disc' matched by a shared audio track, a
Wii ISO kept as a DS game, covers never fetched for new games, the same dump kept three
times. The service runs them on a schedule (web.py: the incoming step every few minutes,
the whole list daily) and the Tidy-up button runs them on demand.

Every step reports what it did and none can stop the others. Steps that delete or move
files away from a game wait while a card export or romset build is running, since that
job may be reading those very files.
"""
import json
import time

from .config import ROOT

STATE = ROOT / "logs" / "upkeep.json"

# (name, what it is for) in the order they run. 'incoming' first: what it brings in is
# what the later steps tidy.
STEPS = (
    ("incoming", "unpack and import games dropped in the incoming folder"),
    ("claim", "attach downloaded files the scan never connected to their game"),
    ("stubs", "let go of 'disc' archives too small to hold the disc, and fetch them again"),
    ("wrong", "let go of downloads that are another game or another platform"),
    ("labels", "re-label the library (fruit machines, demos, e-books…)"),
    ("artwork", "fetch covers and screenshots for games that have none"),
    ("dupes", "delete exact duplicate copies"),
    ("prune", "keep one copy per game: drop extra dumps of the same game"),
)
# Steps that remove files: never while an export or romset build may be reading them.
DESTRUCTIVE = {"dupes", "prune"}
# Systems the per-game prune runs on: the ones whose extra copies cost real space.
PRUNE_SYSTEMS = ("gba", "psp", "dreamcast", "n64", "nds", "nes", "snes", "ps1", "ps2",
                 "gamecube", "saturn", "genesis", "gb", "gbc")


def _step(name, progress, stop):
    from . import reconcile
    if name == "incoming":
        from . import incoming
        return incoming.run(progress=progress, stop=stop)
    if name == "claim":
        r = reconcile.unclaimed_downloads(apply=True)
        return {"claimed": r["count"]}
    if name == "stubs":
        r = reconcile.stub_discs(apply=True)
        return {"let_go": r["count"]}
    if name == "wrong":
        r = reconcile.wrong_downloads(apply=True)
        return {"let_go": r["count"], "orphan_bytes": r["bytes"],
                "orphans": [f for b in r["items"] for f in b["orphans"]][:200]}
    if name == "labels":
        from . import labels
        return {"labels": sum(labels.refresh().values())}
    if name == "artwork":
        from . import artwork
        r = artwork.fetch(stop=stop, progress=progress)
        return {"stopped": r.get("stopped"),
                "boxart": sum(s["boxart"] for s in r["systems"]),
                "games": sum(s["games"] for s in r["systems"])}
    if name == "dupes":
        from . import dupes
        r = dupes.cleanup(apply=True)
        return {"deleted": r.get("deleted"), "bytes_freed": r.get("bytes_freed")}
    if name == "prune":
        from . import prune
        r = prune.cleanup(apply=True, systems=list(PRUNE_SYSTEMS))
        return {"deleted": r.get("deleted"), "files_deleted": r.get("files_deleted"),
                "bytes_freed": r.get("bytes_freed")}
    raise ValueError(f"no upkeep step {name!r}")


def run(steps=None, progress=None, stop=None, busy=None):
    """Run the upkeep steps (all by default). `busy()` answers whether a card export or
    romset build is running; destructive steps are skipped while it does. Returns
    {"steps": {name: report or {"error"} or {"skipped"}}, "took": seconds}."""
    names = [n for n, _ in STEPS if steps is None or n in steps]
    out, t0 = {}, time.time()
    for i, name in enumerate(names):
        if stop is not None and stop.is_set():
            out[name] = {"skipped": "stopped"}
            continue
        if name in DESTRUCTIVE and busy is not None and busy():
            out[name] = {"skipped": "a card export or romset build is running — next time"}
            continue
        if progress:
            progress(i, len(names), f"upkeep: {dict(STEPS)[name]}")
        try:
            out[name] = _step(name, progress, stop)
        except Exception as e:
            out[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
    if progress:
        progress(len(names), len(names), "upkeep done")
    result = {"steps": out, "took": round(time.time() - t0, 1), "full": steps is None,
              "finished": time.strftime("%Y-%m-%d %H:%M:%S")}
    _remember(result)
    return result


def _state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember(result):
    st = _state()
    st["last"] = result
    if result.get("full"):
        st["last_full"] = time.time()
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
    except OSError:
        pass


def last_full():
    """When the whole upkeep list last ran (epoch seconds; 0 when never)."""
    return float(_state().get("last_full") or 0)


def last():
    """The last upkeep run's report, or None."""
    return _state().get("last")
