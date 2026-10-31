# Handoff

Read this first in a new session. It explains what this project is, where everything lives, what state it is in, and what will catch you out. `README.md` in this folder holds the detailed results for each step.

**Last updated:** 2026-10-30, after the Step 6 code was finished.

## What this is

DESYNC 3D Vault is a single-user, local-first archive for a MyMiniFactory (MMF) 3D-printing library of about 4,900 models. It discovers owned models, catalogs them in SQLite, archives files to a NAS, and builds slicer-ready folders.

`DESYNC_3D_Archival` replaces the old app, `scrapepage`, which sits beside this folder. Every file here is either new or migrated, and git history started fresh. The repository has no remote, so all commits are local.

## Where things are

| Location | What it is | Rule |
|---|---|---|
| `test_stl_shelf/DESYNC_3D_Archival/` | This project | Work here |
| `test_stl_shelf/scrapepage/` | Old app and the rollback copy of production data since the 2026-10-31 cutover | Read only. Never run the old code here for tests |
| `~/DESYNC_Data` | Production data since the 2026-10-31 cutover | Never touch from dev |
| `/Volumes/STL/DESYNC_Vault` | Production NAS vault | Never touch |
| `/Volumes/STL/Loot Studios` | Production external library | Never touch |
| `~/Downloads/DESYNC_Vault_local` | Production staging, empty | Never touch |
| `~/Downloads/DESYNC_Print_Queue2` | Production stash exports, 244 entries | Never touch |
| `~/Downloads/DESYNC_Print_Queue` | Hard-coded stash fallback path | Never touch |
| `~/DESYNC_Data_dev/` | Sanitized development data | Safe to use |
| `~/DESYNC_Data_dev/library.clean.db` | Pristine baseline database | Contract and browser tests start from this |
| `~/DESYNC_Data_dev/library.db` | Live dev database, holds the user's real MMF token | Never copy into snapshots or commit |
| `~/DESYNC_Data_dev/old_app/` | Throwaway old code and test data | Reset by every contract run |
| `~/DESYNC_Data_dev/baseline/` | Production manifests and logs | Keep |
| `~/DESYNC_Backups_dev/` | Dev backups | Safe to use |
| `~/DESYNC_Data` | Production after cutover | Does not exist yet |

`.env` in the project root is gitignored and holds this machine's settings.

## Standing rules

1. No emojis, in code, logs, UI or docs.
2. Logging and testing are a birthright. New behavior ships with tests and logs.
3. Do not remove functionality. If something looks unused, ask first.
4. Do not delete or impact production data. Read it only in read-only mode, and never print secret values.
5. Track state in this folder.
6. Only introduce complexity where required, and say why when adding a dependency.
7. Never use em dashes.
8. Never manually modify change logs.
9. Give little weight to development cost when choosing between options.
10. Never include an agent name as a co-author, in commits or anywhere else. This overrides default commit attribution.
11. Put open questions that are not blockers in `Questions.md`, and keep going.
12. Put enhancement ideas in `Enhancements.md`.

Commit freely on step branches. Merge to `main` only after the exit check passes and the user approves. Do not push: there is no remote.

## Daily commands

```bash
./check.sh                                                # the full quality gate
uv run python scripts/baseline/record_contract.py --compare   # API contract, stop the dev app first
uv run python -m desync_archival.api.server               # dev app at http://localhost:8080
uv run python -m desync_archival.dbtool status            # also migrate, backup, restore
```

If your shell has `scrapepage/.venv` active, run `unset VIRTUAL_ENV` first or use `uv run --no-active`.

To confirm production is untouched, compare each manifest in `~/DESYNC_Data_dev/baseline/`:

```bash
.venv/bin/python -I scripts/production_manifest.py compare --manifest ~/DESYNC_Data_dev/baseline/production_files.json
```

`production_manifest.json` takes about 13 minutes because it walks the NAS. It always reports 244 `added` entries under `DESYNC_Print_Queue2`. Those predate this work: the folder was unreadable when the manifest was recorded, and `production_downloads.json` is the real baseline for it. Anything else is a real difference.

## Migration status

| Step | Status | Notes |
|---|---|---|
| 0A to 0C | Done | Sanitized data, baselines, migration, guard, `check.sh`, JSON logging |
| 1 | Done | FastAPI shell |
| 2 | Done | User confirmed real sign-in and a test download |
| 3 | Done | Migrations, backups, WAL |
| 4 | Done | Routers, query modules, dry-run, audit log, V6 |
| 5 | Done | Job recovery, tools adapter, scraper events, backups. User confirmed a real scrape and download |
| 6 | Done | Merged to `main` after the user's review |
| 7 | Done | User logged in from another computer. Merged to `main` |
| 8 | Done | Merged to `main` |
| 8B | Done | Key decoupling merged to `main`, see `KEY_DECOUPLING_PLAN.md`. Dev database is at schema 7 |
| 9 | **In progress** | On `migration/09-cutover`. Stages 1 to 3 of the prompt are done on dev data. Production stages wait for approval, one at a time |

`MIGRATION_PROMPTS.md` holds each step's prompt. Use the file, not an older pasted copy: the prompts have been rewritten since they were first written.

## Decisions that are easy to get wrong

- **Secrets live in the file, with the database as a fallback.** `services/secret_store.py` holds the rule: a key in the file wins, and writes always go to the file. Read secrets through `auth_service.get_setting` or `overlay`, never straight from `app_settings`. Dev and production must not share a secrets file.
- **Every test gets a fresh secrets file,** and tests pin host, TLS, backup folder and secrets away from `.env`. Keep that when adding a setting.
- **Internal keys are not MMF IDs.** Old rows happen to share the number, new rows do not. Reach MMF, metadata files and vault folders through `external_key`, using `services/identity.py`. An ID above 2,000,000,000 no longer means external, so use `source`.
- **Uniqueness lives in triggers, not constraints.** An increment column is the only key, and `tests/test_increment_keys.py` enforces it. `ON CONFLICT` no longer works on business values, so update first and insert if nothing changed. The `sqlite3` command-line tool cannot write to `models` or `model_tags`.
- **A missing mount limits features, it never invalidates data.** If the NAS or an external library is offline, browsing, metadata and adding to stashes still work, because they read the catalog. Only work that needs the files, such as export, render or download, stops. Reconcile skips offline libraries so their files are not reported as missing. Unlinking is different: it is a deliberate choice and flags contents inactive.
- **Sign-in is the OAuth implicit flow.** The user chooses the token duration on MMF's login page, and 168 hours has been confirmed. There are no refresh tokens. The value stored as `mmf_client_secret` is an API key, not an OAuth secret. The user decided not to use it, so Step 6 removed the field and the code grant. Step 9 blanks the stored value.
- **Frontend rules:** render with `template.js`, never `innerHTML`. Bind events with `@click` in templates and `data-click` in `index.html`, because the CSP blocks inline handlers. Pass API URLs through `safeUrl`. A local variable must never be named `html`.
- **Only the Client ID is needed to sign in,** because the login page requires it. Session cookies are still required for `scrape.js`, which has no token path.
- **Do not disguise requests to MMF.** A probe that varied user agents was blocked as bot-check circumvention. If MMF ever refuses the app, surface that to the user.
- **Never copy from the NAS into dev.** External models show their cached renders from `previews/`. `build_dev_data.py` copies those for every external model, and mesh files stay on the NAS.
- **Do not run `rar` or bypass Gatekeeper.** macOS blocks that unverified binary and prompts the user. The app only needs `unrar`.
- **The scraper stays.** MMF's API has no library or purchases endpoint.
- **The app never migrates its own database.** It refuses to start and names the command. On production, `dbtool migrate` also needs `--confirm-production`.
- **Destructive routes dry-run unless the body has `"confirm": true`.** All of them write to `audit_log`.
- **Allowed paths come from `DESYNC_ALLOWED_ROOTS`,** which can't be changed over the network. At cutover it must list the data directory, NAS vault, staging, stash folder and every external library.
- **`npm_audit_allowed.json` lists reviewed advisories** under the stealth plugin. The only fix is a breaking downgrade, which can't be tested without a real scrape.
- **`quality_baseline.json` is a ratchet.** Existing lint and type findings are recorded per file, and a new one fails `check.sh`. Lower it with `scripts/quality_ratchet.py --update`, and don't raise it to hide a finding of your own.

## Known limits

- **V1** is closed by Step 7. Beyond loopback, the app needs login, a password and TLS, or it refuses to start. `LoginMiddleware.PUBLIC_PATHS` is an exact-match allow list, so a new route is protected by default.
- **V5:** token injection is closed, but the deprecated implicit grant is still in use because it is the only option.
- **Untested against real services:** whether MMF's hosts honor `Range`, a Cloudflare check with `--no-sandbox` removed, and the `releases` fix against the real Loot Studios folder. 
- **Snapshot gaps:** stash export, scrape and ingest record failure paths only.
- **The old app is exposed** on `0.0.0.0:8080` with secrets readable when it runs. The user stopped it during Step 2. Keep it stopped.
- **Secrets from V2 need rotating** in Step 9. They were exposed by the old app.
- **Service-layer SQL** is still in the services. Step 4 covered route handlers only.

## How to work here

- Start the contract comparison and `check.sh` only with the dev app stopped, and restart it afterward. The user stays signed in, because the token is in the dev database.
- Contract runs set `nas/vault`, `stash` and `staging` aside and restore them. If `baseline/kept_storage` exists, a run was interrupted, so restore those folders by hand.
- Copying a database in WAL mode: remove `-wal` and `-shm` beside the target first, or the copy can be corrupted.
- Test fixtures build databases through `tests/db_helper.py`. Never rewind `user_version` on a shared test database without restoring it.
- When a contract snapshot changes on purpose, accept only that case with `record_contract.py --accept NAME`, and list it in `README.md`.
- Run the real server command after changing startup. In-process tests can't see entry point bugs, and `tests/test_entry_point.py` exists because of one.
- Gate commits on real results. Scripts here commit only when `check.sh` and the contract both pass.

## Next action

**Production cut over on 2026-10-31 and is running from `~/DESYNC_Data` at `http://127.0.0.1:8080`,** on code from `migration/09-cutover`. Run production commands through `scripts/production.sh`, which reads `~/.config/desync-archival/production.env`. The pre-migration backup is `DESYNC_Backups/library-20261031-131839-482898.db`. `../scrapepage` is unchanged and remains the rollback copy.

1. The user signs in to MMF again. The stored token expired on 2026-10-30.
2. To reach production from other machines: run `scripts/production.sh python -m desync_archival.dbtool set-password`, set `DESYNC_HOST=0.0.0.0`, `DESYNC_ALLOWED_HOSTS` and the TLS paths in `production.env`, and restart.
3. Finish stage 6 of `CUTOVER.md`: Loot Studios rescan, scheduled backup and a test restore, then a real scrape, ingest, download and stash export.
4. Merge `migration/09-cutover` when the user approves. Production runs from this branch, so do not switch this checkout to `main` first.
5. After the user accepts: stage 7, blank the secrets and rotate them. Ask separately before removing anything in `../scrapepage`.

Decisions already made: production stays on this Mac (E21 covers moving later), and production backups go to `test_stl_shelf/DESYNC_Backups` on the same disk for now. The user knows that does not protect against losing the drive.

**State of dev:** the user is signed in on the dev app with a 7-day token issued 2026-10-30, and session cookies are saved in dev. Cloudflare cookies expire quickly, so expect to paste fresh ones before the next scrape. The dev app was left running on port 8080 and may need restarting. The dev catalog does not have Master Wendigo (276218) yet, because no ingest was run. Job 112 for model 148200 shows `failed` in dev and left files in dev staging, which is harmless.
