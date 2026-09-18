# Rom-Com

A catalog-driven ROM/game-library manager for an Odin 2 Portal or similar emulation handheld.

Rom-Com keeps four concerns separate:

1. **Catalog** — what titles/releases exist.
2. **Collection state** — what you own, have acquired, verified, installed, tested, or completed.
3. **Acquisition planning** — bulk-first planning with explicit coverage and authorization gates.
4. **Library verification** — local scanning, hashing, reconciliation, and reporting.

> Rom-Com is designed for material you are authorized to obtain and use. It does not ship game content, ROMs, BIOS files, API keys, or credentials.

## Goals

- Maintain an exhaustive catalog without forcing every release onto the handheld.
- Import or reconcile catalog data from preservation-oriented sources.
- Track series such as **Nancy Drew** independently of runtime/emulator support.
- Prefer high-coverage bulk acquisitions before individual gap-filling.
- Use NZBPlanet/Newznab-compatible search and SABnzbd as optional acquisition backends for authorized material.
- Keep state in SQLite rather than a giant mutable YAML file.
- Verify local files using hashes instead of filenames whenever possible.
- Generate a clear completeness report by platform and series.

## Quick start

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
python -m romcom status
```

## Example commands

```bash
# Initialize the SQLite database
python -m romcom init

# Seed catalog/series records from YAML
python -m romcom seed

# Show overall status
python -m romcom status

# Show missing entries
python -m romcom missing

# Limit to a system or series
python -m romcom missing --system scummvm
python -m romcom series "Nancy Drew"

# Build a bulk-first plan
python -m romcom bulk-plan

# Search the configured indexer for one authorized entry
python -m romcom search <item-id>

# Queue a selected authorized result in SABnzbd
python -m romcom acquire <item-id> --result 1

# Reconcile SABnzbd queue/history
python -m romcom sync

# Scan an existing library and hash files
python -m romcom scan /path/to/library
```

## Data model

Rom-Com intentionally distinguishes:

```text
CATALOGED
WANTED
OWNED
MISSING
FOUND
QUEUED
DOWNLOADING
DOWNLOADED
EXTRACTED
VERIFIED
NORMALIZED
INSTALLED
TESTED
COMPLETE
FAILED
MANUAL
EXCLUDED
```

A title can therefore be cataloged without being owned, owned without being installed, and installed without being verified.

## Configuration

- `catalogs.yaml` — catalog sources and platform scope.
- `policy.yaml` — region/language/revision preferences.
- `series.yaml` — manually curated series definitions and aliases.
- `overrides.yaml` — local corrections and exclusions.
- `.env` — secrets and service endpoints; never commit this file.

## Bulk-first acquisition

Bulk entries explicitly state what they cover. Rom-Com scores them by useful coverage of currently-missing wanted items, then falls back to individual searches only after bulk opportunities are exhausted.

The planner does **not** assume a large archive is good merely because it is large.

Conceptually:

```text
coverage score = newly covered wanted items / download size
```

After a completed SABnzbd job, Rom-Com can reconcile the job and later verify actual extracted content with the scanner.

## Project status

This repository starts with the first working foundation:

- SQLite schema and migrations-on-init
- YAML seed/config loader
- Newznab-compatible search client
- SABnzbd queue/history client
- bulk-first planner
- local hash scanner
- CLI
- Nancy Drew series seed
- safe credential handling

Next planned work: catalog importers (No-Intro/Redump/ScummVM), archive-content matching, CHD-oriented post-processing hooks, ES-DE export, and richer reports.
