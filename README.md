# Rom-Com

Rom-Com is a catalog-driven retro game library manager aimed at an Odin 2 Portal or similar emulation handheld.

It separates four concerns:

1. **Catalog** — what titles/releases exist.
2. **Collection state** — what you own, want, downloaded, verified, installed, tested, or excluded.
3. **Acquisition planning** — bulk-first planning with explicit coverage and authorization gates.
4. **Verification** — local scanning and hash reconciliation.

> Rom-Com is for content you are authorized to obtain and use. It does not ship ROMs, BIOS files, game data, credentials, or API keys.

## Current capabilities

- SQLite state database with automatic schema migration.
- Generic Logiqx-style DAT/XML importer (plain, gzip, or zip).
- ScummVM compatibility importer.
- Nancy Drew 1–34 series seed.
- Stable source/external IDs and aliases.
- CRC32/MD5/SHA1 catalog matching.
- Exact normalized filename fallback matching.
- Bulk archive definitions and coverage scoring.
- Newznab-compatible index search with ranking and size filters.
- SABnzbd queue/history integration.
- romsgames.net direct-download fallback with paced, jittered requests.
- Bulk and individual acquisition support.
- Automatic acquire-download-import pipeline for approved & wanted items.
- Downloaded vs verified state separation.
- Per-item authorization and wanted flags, with ownership kept honest automatically
  (`mark-owned`: everything on disk is wanted & authorized).
- YAML overrides for durable local preferences.
- Health/doctor command.
- Text and JSON reports.
- Pytest coverage and GitHub Actions CI.

## Install

```bash
git clone https://github.com/redsand/Rom-Com.git
cd Rom-Com

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env

python -m romcom init
python -m romcom seed
python -m romcom doctor
```

You can also install the CLI:

```bash
pip install -e .
romcom status
```

## Web UI

```bash
romcom web
```

Starts a local web interface at http://127.0.0.1:8927/ and opens it in your browser
(`--port`, `--host`, `--no-browser` to override). It covers the day-to-day workflow
without the CLI:

- **Dashboard** — collection progress per system, health checks, catalog coverage
- **Library** — browse/filter every item; toggle authorized/wanted and edit status inline
- **Acquire** — recommended bulk volumes and next individual picks; search the indexer
  and send a release to SABnzbd in two clicks, or sweep everything approved & wanted
  in one background run (see [Automatic acquiring](#automatic-acquiring))
- **Activity** — download jobs with a sync button and automatic 20-second SABnzbd sync

The **Files** tab bulk-imports DAT catalogs (see below), scans downloaded ROM folders
(hash-matching files to the catalog), and exports matched files into a per-system
folder layout for an SD card. The Library tab supports bulk marking wanted/authorized
for everything matching the current filters, and a *Mark owned as wanted + authorized*
button for everything already on disk (see
[Ownership](#anything-you-already-have-is-wanted--authorized)). CSV round-trips remain
CLI-only.

The **Settings** tab edits the indexer/SABnzbd connection details and the automatic
acquiring behavior (download folder, category, poll interval, caps) without touching
`.env` by hand. The form is populated with the values currently in effect — including
anything read from `.env` — and each field carries a badge showing where its value
comes from: *saved in UI*, *from .env*, or *default*. Saved values live in the
database and take precedence over `.env`; clearing a field hands control back to
`.env`/defaults.

```bash
romcom scan "H:\downloads\roms"          # hash files, match & verify against the catalog
romcom adopt                             # catalog unmatched scanned files as 'local' entries
romcom organize "E:\" --system nes       # copy matched files to E:\nes\… (blank = all systems)
```

Scanning matches loose files by hash and also looks **inside zip archives** (zip directories
carry each member's CRC32, so this is cheap; candidates are confirmed via md5/sha1).
Anything still unmatched can be *adopted*: `adopt` creates `local` catalog entries with the
system detected from the folder name or file extension, registers the file's hashes, and marks
them FOUND — so downloaded content always appears in the Library and the SD-card export.

## Bulk DAT import

```bash
romcom import-dats "H:\path\to\dats"          # catalog only (wanted=0)
romcom import-dats "H:\path\to\dats" --wanted # also mark everything wanted
romcom import-dats file.zip --system snes     # force a system instead of auto-detect
```

Walks a folder (or single file) of `.dat` / `.xml` / `.zip` / `.gz` catalogs and imports
every recognized DAT. Handles Logiqx XML, clrmamepro, and DOSCenter formats. The system
and source are auto-detected from each DAT's header, filename, and folder; DATs for
unsupported systems (arcade sets, artwork packs, BIOS images…) are skipped and reported.
ScummVM engine DATs don't create new items — they attach verification hashes and aliases
to your existing ScummVM entries so `romcom scan` can hash-verify them.

Imported items default to `wanted=0` so reference catalogs don't flood the missing list;
flag the ones you care about in the Library tab (or `set-series` / CSV).

## Secrets

`.env` is gitignored.

```env
NZB_API_URL=https://api.nzbplanet.net/api
NZB_API_KEY=
SAB_URL=http://127.0.0.1:8080/api
SAB_API_KEY=
SAB_VERIFY_SSL=true
ROMCOM_DB=romcom.db
ROMCOM_SAB_CATEGORY=Games
ROMCOM_DOWNLOAD_DIR=H:\downloads\complete
ROMCOM_ACQUIRE_POLL=30
ROMCOM_ACQUIRE_MAX_WAIT_MIN=240
ROMCOM_ACQUIRE_BATCH_MAX=0
ROMCOM_ACQUIRE_PARALLEL=3
ROMCOM_ACQUIRE_WATCH=false
ROMCOM_ACQUIRE_INTERVAL=300
ROMCOM_ACQUIRE_WATCH_BATCH=50
ROMCOM_ACQUIRE_SWEEP_PAUSE=15
ROMCOM_WEBDL_BASE=https://www.romsgames.net
ROMCOM_WEBDL_DELAY=30
ROMCOM_WEBDL_JITTER=15
ROMCOM_WEBDL_TIMEOUT=60
```

`ROMCOM_SAB_CATEGORY` is the SABnzbd category every queued download is tagged with —
SABnzbd files completed downloads into that category's folder, so point
`ROMCOM_DOWNLOAD_DIR` at it (or at the complete folder root; the scan is recursive).

## Catalog completeness

```bash
romcom catalog-status
romcom catalog-status --strict
```

This audits every enabled source/system in `catalogs.yaml`. A whole platform that has never been imported is reported as **MISSING**, preventing a partially-loaded database from appearing complete.

## Catalogs

### DAT/XML

Import a No-Intro/Redump-style DAT you have obtained from the catalog provider:

```bash
romcom import-dat Nintendo-SNES.dat --system snes --source nointro
romcom import-dat Sony-PlayStation.dat.zip --system ps1 --source redump
```

Use `--catalog-only` if you want entries recorded without automatically marking them wanted.

### ScummVM

```bash
romcom import-scummvm
```

This records ScummVM ID, title and support level in the local catalog.

## Nancy Drew

```bash
romcom seed
romcom series "Nancy Drew"
```

The seed tracks all 34 mainline titles independently from their current runtime/emulator support.

## Ownership and authorization

Acquisition commands are blocked unless the entry is explicitly authorized.

### Anything you already have is wanted & authorized

An item in hand — `FOUND`, `DOWNLOADED`, `VERIFIED`, `NORMALIZED`, `INSTALLED`, `TESTED`,
or a file locally cataloged by `adopt` — is part of the intended collection, so Rom-Com
flags it **wanted + authorized** rather than leaving it half-flagged. Scanning a folder
does this as it matches each file, and one sweep covers what is already in the database:

```bash
romcom mark-owned
```

The Library tab has the same thing as a *Mark owned as wanted + authorized* button. Two
properties make this safe to run at any time, including while the watcher is running:

- **It never downloads anything again.** The pipeline only ever searches items whose
  status is `CATALOGED`/`MISSING` — i.e. with nothing on disk. Owned statuses are not
  picks, and a `wanted` item is only *missing* while it has nothing on disk.
- **It only raises the two flags.** Status, notes, play state and everything else are
  untouched, and an explicitly `EXCLUDED` item is left alone — exclusion is the
  deliberate "I don't want this back" switch.

What changes is the bookkeeping: coverage, the per-system progress on the dashboard, the
missing list (`romcom missing`, the Library's *Missing* view) and the volume planner now
all measure against the collection you actually have instead of the handful of items that
happened to be flagged.

For a one-off change:

```bash
romcom set nancy-01 authorized true
romcom set nancy-01 wanted true
```

For an entire series:

```bash
romcom set-series "Nancy Drew" authorized true
romcom set-series "Nancy Drew" wanted true
```

For spreadsheet-style review/editing:

```bash
romcom export-csv library.csv
# edit authorized/wanted/status/runtime/notes/etc.
romcom import-csv library.csv
```

For durable settings, edit `overrides.yaml` and rerun:

```bash
romcom seed
```

Example:

```yaml
items:
  nancy-01:
    authorized: true
    wanted: true
    preferred_runtime: scummvm
    notes: Owned on original media
```

## Bulk-first workflow

Define authorized bundles in `volumes.yaml`.

```yaml
volumes:
  - id: freeware-adventure-volume
    title: Authorized Adventure Freeware Archive
    authorized: true
    estimated_bytes: 21474836480
    search:
      - "exact authorized archive release"
    covers:
      - some-game-id
      - another-game-id
```

Then:

```bash
romcom seed
romcom bulk-plan
romcom search freeware-adventure-volume
romcom acquire freeware-adventure-volume --result 1
romcom sync
```

Bulk downloads only move covered titles to **FOUND**. They are not considered verified until the completed files are scanned and hash-matched.

## Individual gap filling

Both `bulk-plan` and `next` measure against items with **nothing on disk yet** — a title
you already have is not a gap to fill and does not make a bundle worth downloading.

After useful bulk opportunities are exhausted:

```bash
romcom next --limit 25
romcom search <item-id>
romcom acquire <item-id> --result 1
romcom sync
```

## Automatic acquiring

Marking an item **both authorized and wanted** is the trigger: the web UI immediately
starts a background run that loops through every armed item, searches the indexer,
queues the best result in SABnzbd, waits for the downloads to finish, and then scans
`ROMCOM_DOWNLOAD_DIR` to match the completed files into the library.

- The **Acquire** tab's *Download approved & wanted* button sweeps everything armed in
  one run — useful for items armed outside the UI (CSV import, `set-series`, overrides).
  The CLI equivalent is `romcom auto-acquire` (same pipeline, live progress on stdout).
- Only **CATALOGED/MISSING** items are attempted. FAILED downloads are not retried
  automatically (a checkbox toggle elsewhere would otherwise re-queue them forever) —
  retry those from the search drawer; FOUND items are already waiting in the download
  directory and get picked up by the scan phase instead.
- A release is queued only if its title actually resembles the item (rank score ≥ 20);
  otherwise the item is skipped with a reason and stays CATALOGED for a manual look.
- The wait phase polls SABnzbd every `ROMCOM_ACQUIRE_POLL` seconds and gives up after
  `ROMCOM_ACQUIRE_MAX_WAIT_MIN` minutes (or 10 consecutive failed syncs). Interrupted
  runs resume when the server restarts, and a fresh run skips anything already queued.
- `ROMCOM_ACQUIRE_BATCH_MAX` caps how many items one run will attempt — searched,
  whether the search leads to a queue, a skip, or a failure (0 = unlimited). With
  thousands of armed items, set it so a single toggle floods neither SABnzbd nor the
  indexer's API; the result reports how many armed items were left for the next run.
- `ROMCOM_ACQUIRE_PARALLEL` (default 3) caps how many downloads are kept in flight
  at once (0 = unlimited). The run is a rolling pipeline: as soon as a download
  finishes, the next armed item is searched and queued, and the completed file is
  scanned into the library right away — there's no "download everything, then
  import" tail.
- **Continuous watching** — the *keep downloading continuously* toggle on the
  Acquire tab (or `ROMCOM_ACQUIRE_WATCH=true`, or `romcom auto-acquire --watch`)
  turns the pipeline into an always-on downloader in the Sonarr/Radarr sense: it
  sweeps, fills every free download slot, imports files as they land, rests, and
  sweeps again — forever. The toggle survives server restarts.
  - **Every sweep is bounded.** A watching sweep attempts at most
    `ROMCOM_ACQUIRE_WATCH_BATCH` items (default 50, 0 = unlimited), so it always
    ends and reports instead of grinding through a whole library in one cycle. A
    sweep that stopped at the cap rests only `ROMCOM_ACQUIRE_SWEEP_PAUSE` seconds
    (default 15) because there is a queue of untried items behind it; a sweep with
    nothing left to do rests the full `ROMCOM_ACQUIRE_INTERVAL` (default 300).
  - **Search cooldowns make the re-sweep cheap.** An item that turned up nothing is
    dated in the `events` table and skipped until its window expires — one hour for a
    transient failure (indexer hiccup, SABnzbd down, no download folder), six hours
    for "no usable result anywhere", which is a property of the sources rather than a
    passing problem. That is what lets each new sweep spend its budget on titles it
    has not tried yet instead of re-searching the same dead ends; the Acquire tab
    shows the held-back count next to the eligible one, and the trail is pruned after
    two days. `romcom auto-acquire` reports the same numbers (`armed_remaining`,
    `cooling`) in its JSON result.
- With `ROMCOM_DOWNLOAD_DIR` unset the run still queues and tracks downloads; it just
  reports that the import scan was skipped.
- When the indexer has no usable result, the item falls back to **romsgames.net**:
  the pipeline searches the site, picks the best-matching page for the item's
  console, and downloads the file straight into `ROMCOM_DOWNLOAD_DIR` (item goes
  DOWNLOADED; the scan phase matches it like any other file). Every request to the
  site is spaced `ROMCOM_WEBDL_DELAY` seconds plus up to `ROMCOM_WEBDL_JITTER` of
  random offset — a deliberately human pace so their rate limiting never trips.
  Single-item equivalent: `romcom webdl <item-id>` (add `--result N` to override
  the automatic pick).

## Verification

```bash
romcom scan /path/to/completed/library
```

A hash match promotes an item to **VERIFIED**. A normalized exact filename match promotes it only to **FOUND**.

## Reports

```bash
romcom status
romcom report
romcom report --json
romcom missing
romcom missing --system scummvm
```

## State model

```text
CATALOGED -> FOUND -> QUEUED -> DOWNLOADING -> DOWNLOADED
                                          \-> FAILED

FOUND/DOWNLOADED -> VERIFIED -> NORMALIZED -> INSTALLED -> TESTED
EXCLUDED and MANUAL are explicit side states.
```

A completed SAB job is deliberately **not** equivalent to a verified game.

## Files

- `catalogs.yaml` — intended catalog-source scope.
- `policy.yaml` — region/language/revision preferences.
- `series.yaml` — curated series definitions.
- `volumes.yaml` — authorized bulk archive definitions.
- `overrides.yaml` — persistent local ownership/preferences.
- `romcom.db` — generated state database, gitignored.
- `.env` — local secrets, gitignored.

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q
```

GitHub Actions runs the same test suite on pushes and pull requests.
