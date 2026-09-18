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
- Bulk and individual acquisition support.
- Downloaded vs verified state separation.
- Per-item authorization and wanted flags.
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
  and send a release to SABnzbd in two clicks
- **Activity** — download jobs with a sync button and automatic 20-second SABnzbd sync

The **Files** tab bulk-imports DAT catalogs (see below), scans downloaded ROM folders
(hash-matching files to the catalog), and exports matched files into a per-system
folder layout for an SD card. The Library tab supports bulk marking wanted/authorized
for everything matching the current filters. CSV round-trips remain CLI-only.

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
ROMCOM_SAB_CATEGORY=odin
```

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

After useful bulk opportunities are exhausted:

```bash
romcom next --limit 25
romcom search <item-id>
romcom acquire <item-id> --result 1
romcom sync
```

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
