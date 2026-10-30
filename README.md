# DESYNC 3D Archival

A single-user, local-first archive and asset manager for a MyMiniFactory (MMF) 3D-printing library. It discovers the models you own, catalogs them in SQLite, archives the files to a NAS vault, and builds slicer-ready folders for printing.

This project replaces `scrapepage`, which stays frozen in the sibling directory for reference and rollback.

**Status:** Project scaffold only. The application has not been migrated yet, so there is no app to run. Follow [docs/Archeology/README.md](docs/Archeology/README.md) for the current state.

## Prerequisites

| Tool | Needed for | Install on macOS |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | Python 3.12 and dependencies | `brew install uv` |
| Node.js and npm | The library scraper | `brew install node` |
| git | Source control | `xcode-select --install` |
| Google Chrome | The scraper's visible browser | Install from google.com/chrome |
| unrar (optional) | RAR archives | `brew install --cask rar` |
| 7-Zip (optional) | 7z archives | `brew install sevenzip` |
| Blender (optional) | Mesh preview renders | `brew install --cask blender` |
| mkcert (optional) | TLS for LAN mode, from Step 7 | `brew install mkcert` |

## Setup

Run these from the project root.

```bash
# 1. Python: installs Python 3.12 if needed, then creates .venv from uv.lock
uv sync

# 2. Node: installs the scraper's dependencies from package-lock.json
npm --prefix scraper ci

# 3. Optional: a Chromium fallback for when the scraper can't launch Chrome
(cd scraper && npx playwright install chromium)

# 4. External tools: symlinks local installs into tools/, and moves nothing
./scripts/link_tools.sh

# 5. Local configuration
cp .env.example .env
```

Then edit `.env` and set `DESYNC_DATA_DIR` to an absolute path outside this repository.

## Data

Code and data are kept apart, and no data belongs in this repository.

| Location | Purpose |
|---|---|
| `~/DESYNC_Data_dev` | Development copy, safe to reset |
| `~/DESYNC_Data` | Production, created at cutover in Step 9 |
| `../scrapepage` | Current production data until cutover, then the rollback copy |

Never point a development run at `../scrapepage` or `~/DESYNC_Data`.

## Project layout

Directories are added as the migration reaches them.

```text
DESYNC_3D_Archival/
  src/desync_archival/
    api/        FastAPI routers
    services/   Domain logic
    db/         Query modules
    external/   Adapters for unrar, 7-Zip and Blender
  web/          index.html and static files
  scraper/      scrape.js and its Node dependencies
  migrations/   Numbered .sql schema files
  scripts/      Project scripts, including link_tools.sh
  tests/        Test suite
  tools/        Symlinks to local installations only
  docs/
    Archeology/ Migration state, findings, stack decisions and step prompts
    ai/         Project history
```

## Development

Migration work runs one step at a time, on the branch named in [docs/Archeology/MIGRATION_PROMPTS.md](docs/Archeology/MIGRATION_PROMPTS.md). The project rules are in [docs/Archeology/README.md](docs/Archeology/README.md).

This repository has no remote yet, and all commits are local.

Open questions go in [docs/Archeology/Questions.md](docs/Archeology/Questions.md), and enhancement ideas go in [docs/Archeology/Enhancements.md](docs/Archeology/Enhancements.md).
