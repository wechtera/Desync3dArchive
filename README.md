# DESYNC 3D Archival

A single-user, local-first archive and asset manager for a MyMiniFactory (MMF) 3D-printing library. It discovers the models you own, catalogs them in SQLite, archives the files to a NAS vault, and builds slicer-ready folders for printing.

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
- **Review** by clicking a release or model. It opens on **What to check**, which says what's wrong with each flagged card and what to do. Clicking one jumps to it, with its rows in blue and the row causing the concern in red. The tree shows which card owns each folder or file. Tick rows, then use **New Card** or **Move to Card**, and rename or skip cards. Your cards are saved separately, so a new preview keeps them, and only something new reopens the release.
- **Approve and Add Reviewed** adds releases you marked reviewed, plus unflagged models from the rest. Flagged models and unticked releases stay proposed for later, and added models leave the review screen.
- **Approve and Add All** adds everything still proposed. An unticked release is skipped for good.
- **Full Rescan** checks known models against storage. It never adds new folders.

Images and archive peeks then run in the background, with pause and resume. **Settings > Inactive** lists missing folders, unlinked models, unavailable models and failed renders. **Settings > Libraries** shows disk figures only.

After upgrading, fill the model levels from stored peeks once. It backs up first and can run again safely:

```bash
scripts/production.sh python -m desync_archival.dbtool build-models --confirm-production
```

**Archive Review**, beside **Linked Libraries**, works the same way for peeked archives. **View Library > Models** browses models, with search across models, model groups and artifacts.

### Importing files

Import File, under Manage / Retrieve / Reconcile Assets > External Vault, takes any mix of archives, loose files and folders. Everything is copied to `ad-hoc/` at the vault root, a fixed location apart from the creator folders. A folder becomes one collection and a file its own, and repeats are skipped by hash. Archives are peeked and wait in Archive Review. The picker can only open folders listed in `DESYNC_ALLOWED_ROOTS`.

### Duplicates

Find Duplicates, under Vault Reconcile, compares whole files. Results stay on the card until each pair is acted on. Keep Both and Ignore are remembered, so that pair isn't raised again. Delete moves one file to `.Pending-Deletion` at the vault root, a hidden, fixed folder, and leaves a tombstone in the catalog. Linked library files are read-only and can't be deleted. Settings > Disk Usage can purge that folder, listing each file first and asking twice, and only for files moved over 14 days ago.

### PDFs

View Library > PDFs lists every PDF the catalog knows about, from MMF, linked libraries, imports and inside peeked archives. The list reads SQLite only, so it works with the NAS offline. **Update PDFs**, under Manage / Retrieve / Reconcile Assets > Vault Reconcile, records PDFs in linked libraries and makes first page previews with Ghostscript, kept locally. A PDF inside an archive can only be named once that archive is downloaded and peeked, so until then it shows as an archive not read yet.

## Backups

The app backs up monthly while it's running, and catches up if it was off. **Settings > Backups** shows what exists, runs a backup, checks one, and sets the folders. A full backup holds the database, `metadata/` and a local copy of your secrets, and the last 14 are kept. It also goes to the second folder if one is set, without the secrets, so a restore from there means reloading cookies. If that folder isn't available, the local backup still counts. Migrations take a quick database copy first.

```bash
scripts/production.sh python -m desync_archival.dbtool backup
uv run python -m desync_archival.dbtool restore BACKUP_FILE NEW_PATH
```

## Moving to another machine

Files on the NAS and in linked libraries stay where they are and aren't in the bundle. This copies the catalog, metadata and previews:

```bash
scripts/production.sh python -m desync_archival.dbtool export /Volumes/NAS/desync-bundle
```

Add `--with-secrets` to include your secrets, and keep that folder private. On the new machine, install the app, set `DESYNC_DATA_DIR` to an empty folder, then:

```bash
uv run python -m desync_archival.dbtool import /Volumes/NAS/desync-bundle --map /Volumes/STL=/mnt/nas
```

`--map OLD=NEW` rewrites stored paths, and can be repeated. `MANIFEST.json` in the bundle lists the paths in use.

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

By default the app listens on this machine only, with no login. LAN mode lets other devices on your network reach it over HTTPS with a password. The app refuses to start in LAN mode without the password and both certificate files.

**1. Set a password**, at least 8 characters:

```bash
scripts/production.sh python -m desync_archival.dbtool set-password
```

**2. Make a certificate** for this machine's name and address. Find them with `scutil --get LocalHostName` and `ipconfig getifaddr en0`:

```bash
brew install mkcert
mkcert -install
mkdir -p ~/.config/desync-archival/tls && cd ~/.config/desync-archival/tls
mkcert -cert-file vault.pem -key-file vault-key.pem MY-MAC.local 192.168.1.20 localhost 127.0.0.1
```

**3. Set these** in `~/.config/desync-archival/production.env`, using full paths:

```bash
DESYNC_HOST=0.0.0.0
DESYNC_ALLOWED_HOSTS=MY-MAC.local,192.168.1.20
DESYNC_TLS_CERT=/Users/you/.config/desync-archival/tls/vault.pem
DESYNC_TLS_KEY=/Users/you/.config/desync-archival/tls/vault-key.pem
DESYNC_REQUIRE_LOGIN=1
```

**4. Restart** with `scripts/server.sh restart`, and allow incoming connections if macOS asks.

**5. Open it** at `https://MY-MAC.local:8080` from any device on the network. `http://` no longer works in this mode, on this machine either.

**6. Trust the certificate on other devices.** They'll show a warning until they trust mkcert's root certificate, which is `rootCA.pem` in the folder shown by `mkcert -CAROOT`. Copy only that file, never `rootCA-key.pem`, and install it on the device. On an iPhone, also turn it on under Settings > General > About > Certificate Trust Settings.

If the address changes, reserve it in your router, or use the `.local` name. A new address needs a new certificate and an updated `DESYNC_ALLOWED_HOSTS`.

To turn LAN mode off, set `DESYNC_HOST=127.0.0.1`, clear the other four values and restart.

There is one password and no user accounts. It is stored as a salted scrypt hash in `~/.config/desync-archival/secrets.json`, mode 0600, outside the repository and data folder. Sessions last 30 days and live in memory, so restarting the app signs everyone out. Five wrong passwords from one address lock that address out for 15 minutes. Changing the password signs everyone out.

**MMF sign-in:** the redirect URI in Settings must exactly match one registered with MMF, and it has to work from the browser doing the sign-in, because MMF sends that browser there. `http://localhost:8080/api/auth/callback` only works on the server itself. From another computer, register and save something like `https://vault.local:8080/api/auth/callback`, or sign in on the server.

**Scraper on a headless server:** a scrape runs without a window by default, but a Cloudflare check needs a visible window, so a server with no display can't pass one. `cf_clearance` is tied to the IP address and browser that earned it, so a cookie copied from another computer may fail. If scraping fails there, scrape on a computer with a display or paste model IDs into the download queue.

## Configuration

Settings live in two places. Anything in the Settings tab is stored by the app. Server settings, which must be known before the app starts, live in `~/.config/desync-archival/production.env` for production and `.env` for development. Changes apply on restart. The file is shell syntax: `NAME=value` with no spaces around the equals sign, and quotes around a path containing spaces. Passwords and keys don't go in it.

| Setting | What it does | |
|---|---|---|
| `APP_ENV` | `production`. Any other value refuses production paths | Required |
| `DESYNC_DATA_DIR` | Folder for the database, previews, metadata and logs. Keep it off the NAS | Required |
| `DESYNC_ALLOWED_ROOTS` | Colon-separated folders the file browsers and Import File can reach. Defaults to the data folder | Usually set |
| `DESYNC_PROTECTED_PATHS` | Colon-separated folders a development run must never touch | Development |
| `DESYNC_BACKUP_DIR` | Backup folder. Settings > Backups wins over this | Optional |
| `DESYNC_BACKUP_MIRROR_DIR` | Second backup copy. Settings > Backups wins over this | Optional |
| `DESYNC_HOST`, `DESYNC_PORT` | Address and port. Defaults are `127.0.0.1` and `8080` | Optional |
| `DESYNC_ALLOWED_HOSTS` | Comma-separated names devices may use to reach the app | LAN mode |
| `DESYNC_REQUIRE_LOGIN` | `1` requires the password. Always on in LAN mode | LAN mode |
| `DESYNC_TLS_CERT`, `DESYNC_TLS_KEY` | HTTPS certificate and key | LAN mode |
| `DESYNC_SECRETS_FILE` | Moves the secrets file | Rare |
| `DESYNC_BLENDER_BIN`, `DESYNC_UNRAR_BIN`, `DESYNC_7ZIP_BIN`, `DESYNC_GHOSTSCRIPT_BIN` | Tool locations, if not found automatically | Rare |
| `DESYNC_PRODUCTION_ENV` | Moves the settings file. Set it in your shell | Rare |

The Help page in the app has the same reference with examples. Settings > Directories lists every location in use on this machine, with its purpose and how to change it.

## Data

Code and data are kept apart, and no data belongs in this repository.

| Location | Purpose |
|---|---|
| `~/DESYNC_Data_dev` | Development copy, safe to reset |
| `~/DESYNC_Data` | Production |
| `../scrapepage_deprecated` | Deprecated. The old app's data, kept for reference. Its secrets are wiped, so reload them before running it |

Never point a development run at `../scrapepage_deprecated` or `~/DESYNC_Data`.

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
```

## Development

This repository has no remote yet, and all commits are local.

Run `./check.sh` before committing. It covers tests, lint, types and dependency audits.
