# DESYNC 3D Archival

A single-user, local-first archive and asset manager for a MyMiniFactory (MMF) 3D-printing library. It discovers the models you own, catalogs them in SQLite, archives the files to a NAS vault, and builds slicer-ready folders for printing.

This project replaces `scrapepage`, which stays frozen in the sibling directory for reference and rollback.

**Status:** Production cut over on 2026-10-31 and runs from `~/DESYNC_Data` on code from `migration/09-cutover`, which is not merged yet. Run production commands through `scripts/production.sh`. Remaining steps are in [docs/Archeology/CUTOVER.md](docs/Archeology/CUTOVER.md). Start with [docs/Archeology/HANDOFF.md](docs/Archeology/HANDOFF.md).

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

# 4. Browser tests: installs Playwright and its Chromium build
npm --prefix tests/e2e ci
(cd tests/e2e && npx playwright install chromium)

# 5. External tools: symlinks local installs into tools/, and moves nothing
./scripts/link_tools.sh

# 6. Local configuration
cp .env.example .env
```

Then edit `.env` and set `DESYNC_DATA_DIR` to an absolute path outside this repository. Set `DESYNC_PROTECTED_PATHS` to your NAS, staging and stash folders. The app refuses to start in development without it.

To build a sanitized development copy from production:

```bash
./scripts/build_dev_data.py --protect /path/to/nas --protect /path/to/staging --protect /path/to/stash
```

## Running production

Production runs in the background from `~/DESYNC_Data`, using the settings in `~/.config/desync-archival/production.env`. Run these from this folder:

```bash
scripts/server.sh status    # is it running, and where
scripts/server.sh start     # start it, and wait until it is listening
scripts/server.sh stop      # stop it, and give workers time to finish cleanly
scripts/server.sh restart   # stop, then start
```

The server does not start by itself after a reboot, so run `start`. Stop the dev app first if it is on the same port. The script never stops another program on that port and never forces a stop. Startup output goes to `~/DESYNC_Data/logs/server.out`, and the app log is `~/DESYNC_Data/logs/vault.log`.

For any other production command, use `scripts/production.sh`, for example `scripts/production.sh python -m desync_archival.dbtool status`.

### Linked libraries

Linked libraries such as Loot Studios are read in place and never changed. In **Manage / Retrieve / Reconcile Assets > Linked Libraries**:

- **Preview New Folders** shows what would be added, and adds nothing. Run it after adding a folder to the library.
- **Review** by clicking a release or model. The tree shows which card owns each folder or file. Tick rows, then use **New Card** or **Move to Card**, and rename or skip cards. Your cards are saved separately, so a new preview keeps them, and only something new reopens the release.
- **Approve and Add** brings in the approved folders. It can stop and resume, and untick a release to leave it out. An approved release can no longer be edited here.
- **Full Rescan** checks known models against storage. It never adds new folders.

Images and archive peeks then run in the background, with pause and resume. **Settings > Inactive** lists missing folders, unlinked models, unavailable models and failed renders. **Settings > Libraries** shows disk figures only.

## Running

```bash
uv run python -m desync_archival.api.server   # web UI at http://localhost:8080
./check.sh                                    # tests, lint, types and dependency audits
uv run python -m desync_archival.dbtool status   # schema version
uv run python -m desync_archival.dbtool migrate  # apply migrations, after a verified backup
uv run python -m desync_archival.dbtool backup   # write a verified backup to DESYNC_BACKUP_DIR
uv run python -m desync_archival.dbtool restore BACKUP_FILE NEW_PATH   # restore to a new path
uv run python -m desync_archival.dbtool export-secrets   # copy secrets from the database to the secrets file
uv run python scripts/cutover.py --help   # staged migration rehearsal and rollback
uv run python scripts/baseline/record_contract.py --compare   # compare the API to the snapshots
```

The app never migrates a database by itself. If the schema is behind, it refuses to start and names the command to run. On production, `migrate` also needs `--confirm-production`.

Sign in from Settings with **Connect**, which opens MMF's login page, where you also choose how long the sign-in lasts. Only the Client ID has to be saved first. The library scrape needs session cookies, which go in their own Settings fields, and it runs without a browser window by default. If Cloudflare asks for a check, turn that switch off and scrape in a visible window.

The app and the old app both use port 8080, so stop one before starting the other. `check.sh` runs the browser tests when `~/DESYNC_Data_dev` exists, and they use port 8099.

## LAN mode

By default the app listens on loopback and has no login. To reach it from another computer:

```bash
uv run python -m desync_archival.dbtool set-password   # at least 8 characters

brew install mkcert
mkcert -install
mkcert vault.local 192.168.1.20   # use this server's hostname and address
```

Then set these in `.env` and start the app as usual:

```bash
DESYNC_HOST=0.0.0.0
DESYNC_ALLOWED_HOSTS=vault.local,192.168.1.20
DESYNC_TLS_CERT=/full/path/to/vault.local+1.pem
DESYNC_TLS_KEY=/full/path/to/vault.local+1-key.pem
```

The app refuses to start beyond loopback without the password and both certificate files. `DESYNC_ALLOWED_HOSTS` replaces the defaults, so list every name people will type, and include `localhost` if you still use it on the server. To trust the certificate on another computer, copy `rootCA.pem` from the folder `mkcert -CAROOT` prints and add it to that computer's trust store. Never copy `rootCA-key.pem`.

There is one password and no user accounts. It is stored as a salted scrypt hash in `~/.config/desync-archival/secrets.json`, mode 0600, outside the repository and data folder. Sessions last 30 days and live in memory, so restarting the app signs everyone out. Five wrong passwords from one address lock that address out for 15 minutes. Changing the password signs everyone out.

**MMF sign-in:** the redirect URI in Settings must exactly match one registered with MMF, and it has to work from the browser doing the sign-in, because MMF sends that browser there. `http://localhost:8080/api/auth/callback` only works on the server itself. From another computer, register and save something like `https://vault.local:8080/api/auth/callback`, or sign in on the server.

**Scraper on a headless server:** a scrape runs without a window by default, but a Cloudflare check needs a visible window, so a server with no display can't pass one. `cf_clearance` is tied to the IP address and browser that earned it, so a cookie copied from another computer may fail. If scraping fails there, scrape on a computer with a display or paste model IDs into the download queue.

## Data

Code and data are kept apart, and no data belongs in this repository.

| Location | Purpose |
|---|---|
| `~/DESYNC_Data_dev` | Development copy, safe to reset |
| `~/DESYNC_Data` | Production |
| `../scrapepage` | The rollback copy of production, unchanged since cutover |

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
  tests/        Test suite, with browser tests in tests/e2e/
  tools/        Symlinks to local installations only
  docs/
    Archeology/ Migration state, findings, stack decisions and step prompts
    ai/         Project history
```

## Development

Migration work runs one step at a time, on the branch named in [docs/Archeology/MIGRATION_PROMPTS.md](docs/Archeology/MIGRATION_PROMPTS.md). The project rules are in [docs/Archeology/README.md](docs/Archeology/README.md).

This repository has no remote yet, and all commits are local.

Open questions go in [docs/Archeology/Questions.md](docs/Archeology/Questions.md), and enhancement ideas go in [docs/Archeology/Enhancements.md](docs/Archeology/Enhancements.md).
