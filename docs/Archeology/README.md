# Archeology: Migration State

This directory tracks the state of the app, what has been done, and what comes next. Update this file at the end of every migration step.

## Core rules

1. No emojis.
2. Logging and testing are a birthright.
3. Do not remove functionality.
4. Do not delete or impact production data.
5. Track state, next steps and completed work in this directory using README files.
6. Only introduce complexity where required.
7. Never use em dashes.
8. Never manually modify change logs.
9. Give little weight to development cost when choosing between options.
10. Never include an agent name as a co-author.
11. Add open questions that are not blockers to `Questions.md`.
12. Add enhancement ideas to `Enhancements.md`.

## Documents

| File | Purpose |
|---|---|
| `SCRAPEPAGE_ARCHEOLOGY.md` | Findings: use cases, architecture, business rules, security register V1 to V20 |
| `STACK_PROPOSAL.md` | Accepted stack, decisions, production data safeguards, LAN consequences |
| `MIGRATION_PROMPTS.md` | Ordered prompts for Steps 0A to 9, with exit checks |
| `Questions.md` | Open questions that are not blockers |
| `Enhancements.md` | Enhancement ideas found during migration |

## Current state

- **Phase:** Step 2 code complete on branch `migration/02-security`. Its exit check still needs the user's real sign-in, on the implicit flow, and a test download.
- **Source baseline:** `scrapepage` commit `9bfa46d`, frozen and left unchanged.
- **Tests:** 84 passing on 2026-10-30, before commit `9bfa46d`. Not re-run since.
- **Production data:** Untouched. `library.db` was last modified at 15:50 on 2026-10-30, before this work began.

## Done

- [x] Application archeology
- [x] Stack proposal drafted and decisions recorded
- [x] Migration prompts written
- [x] New project created: git repository, README with setup steps, `.gitignore`, Python and Node dependency manifests, `tools/` linker, and these docs
- [x] Step 2 code: V1 to V5 and V9 closed, code grant with refresh, token rotation handling, no secrets in responses
- [x] Step 1: FastAPI and uvicorn shell, lifespan, threadpool, request logging, Playwright smoke test
- [x] Step 0C: `check.sh`, per-file lint and type ratchet, JSON log lines, request and job IDs, redaction
- [x] Step 0B: app, tests, frontend and scraper migrated; settings module and production guard added
- [x] Step 0A: production manifest, sanitized dev data, throwaway old app, 105 contract snapshots, snapshot secret test, performance baseline
- [x] Migration prompts reviewed against the code. Step 0 split into 0A, 0B and 0C, and data-safety, V6, backup and cutover gaps closed

## Next

- [ ] User: real MMF sign-in on the dev instance, then a test download to the dev vault
- [ ] Merge `migration/02-security` after that and user approval
- [ ] Run Step 3 from `MIGRATION_PROMPTS.md`

## Project decisions

- **New root.** `DESYNC_3D_Archival` is a new git repository with no history from `scrapepage`, and it sits beside `scrapepage`, not inside it. It is local only for now.
- **Old app frozen.** `scrapepage` gets no changes. It still has V1 to V3, so run it only on a trusted network until cutover.
- **Files.** Everything here is either net new or migrated from `scrapepage`.
- **Layout.** Code is organized by component under `src/desync_archival/`, `web/`, `scraper/` and `migrations/`. See the root README.
- **`tools/` holds symlinks only.** Blender, 7-Zip and unrar stay where they are installed. Adapter code lives in `src/desync_archival/external/`.
- **Production data.** It is copied to `~/DESYNC_Data` at cutover. The originals stay in `scrapepage` as the rollback copy. Development uses `~/DESYNC_Data_dev`.
- **Carried over from `Archival_DEV`.** Only `Archeology/` and `ai/`. The legacy CLI scripts and `rnd/` stay in `scrapepage`.
- **Real MMF tests.** Steps 2 and 5 use small test downloads into a dev-only vault, never the NAS.
- **Path limits.** `DESYNC_ALLOWED_ROOTS` in `.env` limits every path the API can set or browse. Settings can still change paths inside those roots.
- **Commits.** Commit freely on step branches. Merge to `main` after the exit check and user approval.
- **Backups.** A backup and restore command arrives in Step 3, a daily schedule in Step 5, and production backups in Step 9.
- **`SCRAPEPAGE_ARCHEOLOGY.md`** is a historical record. Its paths and line numbers describe `scrapepage`.

## Step 2 results

**Exit check:** Not complete. `check.sh` passes, but the user still has to confirm a real sign-in and a test download. Nothing in this step has been tried against the live MMF service. Token handling is tested with mocks built from MMF's documentation.

**Findings closed, each with tests in `tests/test_security.py`**
- **V1:** host and port are settings, defaulting to `127.0.0.1:8080`. The app refuses to bind beyond loopback until LAN mode exists. There is still no login, so V1 fully closes in Step 7.
- **V2:** `GET /api/settings` and `GET /api/auth/status` return no secrets, only configured flags. `/api/scrapes/download` serves only `scrapes/<run>/model_ids.txt` and `models.json`.
- **V3:** wildcard CORS is gone. Host and Origin are validated on every request, and a POST or DELETE with a body must be JSON.
- **V4:** only `web/index.html` and `web/static/` are served, with no directory listings.
- **V5:** `state` is single-use and verified on the implicit callback, so a caller can't inject a token. The authorization code grant is built, but this account can't use it.
- **V9:** login is a real 302 with an encoded URL.
- **V11, in part:** bodies are limited to 2 MiB, and unhandled exceptions return a generic error and log the detail.

**MMF token findings**
- MMF's documentation does not say a new login revokes older tokens.
- It does say a refresh MAY issue a new refresh token and MAY revoke the old one. So refresh tokens are treated as single-use: one refresh at a time under a lock, and the replacement is saved at once.
- The documented example shows access tokens lasting 3,600 seconds under the code grant, compared with 21,600 under the implicit grant. Tokens refresh 5 minutes before expiry and on a 401.
- A 401 on the download stream, on metadata requests and during ingest now refreshes once and retries before the old failure path runs. 403 still trips the circuit breaker at once.

**Sign-in flow:** the implicit grant stays, and is the default. The value stored in `mmf_client_secret` is an MMF API key, and MMF has no OAuth client secret for this application, so the code grant fails with HTTP 401, "Invalid client ID or secret". The user confirmed this on 2026-10-30. The old app never sent that value, so this never surfaced. The code grant and refresh handling stay in the code behind `mmf_auth_flow`, and a real login has never tested them.

**Limitation:** with the implicit grant there are no refresh tokens. Access tokens last about 6 hours, and a 401 during a long download run still trips the circuit breaker until the user reconnects. V5 is closed for `state` checking, but the implicit grant itself remains in use.

**Bugs found and fixed**
- The Connect button could never work: it called `fetch` on a cross-site redirect and read a `login_url` field the API has never returned.
- Replacing one cookie in Settings used to rebuild `cookies_json` from that one value, dropping the rest. The server now merges it into the stored list.

**Decision:** a POST or DELETE with an empty body needs no Content-Type. It has nothing to parse, and Origin checks still cover it. The frontend sends several of these.

**Contract snapshots changed, all intended, 3 of 105**
- `settings`: secret keys removed, `secrets_configured` added.
- `auth_status`: `client_secret` removed, and `secret_configured`, `refresh_configured`, `flow` and `redirect_uri` added.
- `auth_token_callback`: now 403 without a matching `state`.

**Existing tests changed because they asserted unsafe behavior**
- `test_server_api.py::test_auth_config_persistence`: expected the client secret back from the status route.
- `test_server_api.py::test_auth_login_url_generation`: expected the implicit grant even with a secret set.
- `test_server_api.py::test_auth_token_callback_success`: sent no `state`.
- `test_app_lifespan.py`: wildcard CORS, OPTIONS preflight and exception text in 500 responses are gone, and test clients now use an allowed host.

**Still open:** 20 route error paths return exception text, for example `Failed reading artifact: {e}`. Step 4 replaces those handlers, and the work is logged as E12.

## Step 1 results

**Exit check:** Passed. All 105 contract snapshots match on FastAPI. The Playwright smoke test passes 12 of 12 against a sanitized dev copy, with every request outside the local app blocked. `check.sh` passes.

**Real start:** the app started through `main()` on dev data with live workers, bound to loopback, served the UI and API, and logged a clean shutdown on SIGTERM.

**Dependencies added:** `fastapi` and `uvicorn`, from the accepted stack, and `@playwright/test` in `tests/e2e/`, kept apart from the scraper's dependencies.

**Intended behavior changes**
- The server binds to `127.0.0.1:8080`. Step 2 still has to make host and port settings.
- Directory listings under `/static/` are gone, and `/static/` paths that are not files return 404. HEAD on `/api` routes is not served.
- Static files use ETag caching from Starlette, not the `http.server` `Last-Modified` handling.
- Every request is logged at INFO with method, path, status and duration. Query strings are left out, because they can carry credentials, and uvicorn's own access log is off.
- Unknown GET paths return JSON 404 instead of an HTML 404.

**Kept on purpose until Step 2:** wildcard CORS, Python exception text in 500 responses, and no Host or Origin checks.

**Test wiring changes:** `tests/test_server_api.py` and `tests/test_migration_wiring.py` now start the app through `tests/live_server.py`. Test logic is unchanged. The logs folder's size is now marked volatile in the `settings_directories` snapshot, because it depends on how much has been logged.

## Step 0C results

**Exit check:** Passed. The directory manifest shows 244 differences, all "added" under `DESYNC_Print_Queue2` and none anywhere else. Full Disk Access took effect during the run, so those files were readable for the first time against a baseline that held nothing for that folder. The newest was modified at 10:29 on 2026-10-30, before this work began, so nothing changed there. `DESYNC_Vault_local` is empty. Baselines for both folders are now in `~/DESYNC_Data_dev/baseline/production_downloads.json`, so later steps can compare them. That file and `production_files.json` are compared separately from the main manifest, which reports these 244 entries as added until it is re-recorded.

**`check.sh`:** runs pytest, the snapshot secret test, ruff, pyright, pip-audit and `npm audit --audit-level=moderate`. All pass. The contract still matches all 105 snapshots after the logging change.

**Ratchet:** `quality_baseline.json` holds existing findings per file: ruff 752 and pyright 29, across `src`, `tests` and `scripts`. A new finding fails `check.sh`. The 2 extra pyright findings compared with Step 0B are `bpy` imports in the Blender script, which came from adding `scripts` to the scope.

**Logging**
- `logs/vault.log` now holds one JSON object per line, so old text lines and new JSON lines will sit in one file after cutover if `logs/` is copied.
- The UI log viewer and console keep the readable text format.
- `request_id` is set per API request. `job_id` is set in the task workers and in download and offload processing.
- Redaction runs on the logger, so the file, UI buffer and console all get redacted text, including exception text.
- `pipeline_tasks.log_output` is a separate store and is not redacted by this filter.
- The audit reports 3 low-severity issues in the `deepmerge` chain under the stealth plugin. They are below the moderate threshold.

## Step 0B results

**Contract:** the migrated app matched all 105 snapshots from the old app, run from an unrelated working directory against the same sanitized data. The comparison has a test proving it detects changes.

**Tests:** 142 pass, which includes the 84 migrated tests with their logic unchanged.

**Intended behavior changes**
- Python source is no longer served as static files, because the static root moved to `web/`. This closes part of V4 early.
- Saving a production path outside `APP_ENV=production` now returns 400 from `POST /api/settings`, `POST /api/storage/config` and `POST /api/settings/libraries`.
- Relative `raw_backup_path` and `metadata_backup_path` values now resolve against the data directory.
- `scrape.js` requires `--cookies <path>`. The task manager passes the run folder's cookie file, which is the same file it used through its working directory.

**Test changes, path constants only**
- `tests/test_server_api.py`: the database and vault now come from the settings module, and `VAULT_DB_PATH` is gone.
- `tests/test_mesh_renderer.py`: `previews_dir` now comes from the settings module.
- All migrated tests: imports changed mechanically to `desync_archival`.

**Not verified:** a real scrape, because it needs a visible browser and MMF. The `--cookies` flag is tested only up to the point where the browser would launch.

**Existing findings, reported and not fixed:** ruff reports 758 and pyright reports 27 in the migrated code. New files are clean.

**Still bound to `0.0.0.0:8080`:** behavior is unchanged until Step 2, so the migrated app has the same exposure as the old one when run directly. The baseline harness binds to loopback.

## Step 0A results

**Rebuild the baseline**

```bash
./scripts/build_dev_data.py --protect /Volumes/STL --protect ~/Downloads/DESYNC_Vault_local --protect ~/Downloads/DESYNC_Print_Queue2
.venv/bin/python scripts/baseline/record_contract.py
```

**Contract snapshots:** 105 cases in `tests/contract/snapshots/`, covering every route and alias in the old dispatcher. Two full runs matched exactly. No snapshot held a production secret or a signed URL. The status mix is 84 of 200, 3 of 201, 14 of 400, 3 of 404 and 1 of 500.

**Known limits of the snapshots**
- `stash_export` records its failure path only, because the dev vault holds placeholders for one model.
- `render_stl` records a 500 caused by a placeholder that is not a real zip. That is the old behavior for an invalid archive.
- Scrape and ingest record failure paths, because the harness blocks `node` and outside network use.
- `/previews` and static files are not `/api` routes, so snapshots do not cover them. `tests/test_app_lifespan.py` and the Playwright test do.

**Performance baseline** (sanitized copy of 4,893 models, 15 repeats, on this Mac, in `tests/contract/performance_baseline.json`)

| Measure | Median ms | Max ms |
|---|---|---|
| Startup to ready, from import | 30.4 | 39.2 |
| Catalog first page | 2.13 | 3.36 |
| Catalog search | 7.61 | 7.83 |
| Catalog deep page, offset 4,800 | 3.06 | 3.35 |
| Metrics | 11.63 | 11.95 |
| Creators | 2.55 | 2.76 |
| Releases | 1.38 | 1.49 |

These are single-client timings on an idle app, so they say nothing about UI stalls during downloads.

**`run_startup_analytics` and `releases`:** Confirmed. `run_startup_analytics` is exported but never called. The server's "Running database startup analytics" message only runs `init_persistent_tables`. The only `releases` writers are that function and the external indexer, so ingest does not update `releases`. It has not drifted yet: all 587 rows were computed on 2026-10-25, 7 MMF models were updated after that, and no designer-month is missing or has a wrong model count. A future ingest of new models would go stale.

**Exit check:** Passed. The production manifest shows 0 differences across `library.db`, `cookies.json`, `metadata/`, `previews/`, `scrapes/`, `logs/`, `mmf_browser_profile/`, the NAS vault and `Loot Studios`. The staging and stash folders in `~/Downloads` could not be listed, because macOS privacy controls block this process, so the manifest does not cover them. The sanitizer and harness never receive those paths except as rewrite targets.

## Facts found along the way

- SQLite runs in `delete` journal mode, not WAL. `user_version` is 0.
- `run_startup_analytics` is exported but not called from `services/` or `ui/`, although the server logs that it is running startup analytics. The `releases` table may therefore go stale after an ingest. Confirm this and keep the rebuild behavior during migration.
- Hard-coded `library.db` defaults remain at `services/stash_service.py:19`, `services/stash_exporter.py:70`, `ui/routes/settings.py:328` and `ui/server.py:32`.
- The MMF API docs site returns HTTP 403 to automated fetches. The same spec is at `github.com/MyMiniFactory/api-documentation`.
- That spec has no library or purchases endpoint, so the scraper stays.
- MMF documents the authorization code grant with refresh tokens. The app uses the implicit grant today.
- Past task logs do not record the URL of the JSON that `scrape.js` intercepts.
- `requests`, `tqdm` and the Python `playwright` package are imported only by `Archival_DEV/scripts/main.py`, `metadata_scrape.py` and `seed_db.py`. Those scripts did not move, so the new project does not declare these packages.
- `scrape.js` reads and rewrites `cookies.json` relative to its working directory. It has to get an explicit path so a dev run can't touch production cookies.
- `test_stl_shelf/scrap/library.db` is a stray 36 KB database holding only the stash tables. It is not production data and was not migrated.
- Blender is at `/Applications/Blender.app`, not on `PATH`. 7-Zip is `/opt/homebrew/bin/7zz`, and `7z` is not installed.
- `app_settings` stores the real NAS, staging and stash paths alongside the live secrets, so a plain `.backup` copy is not safe for development.
- Old `ui/server.py` starts download workers at startup, and `VAULT_DB_PATH` only redirects the database. Logs, previews, scrapes and metadata still resolve under `ROOT_DIR`.
- 4,886 `raw_backup_path` rows and the `metadata_backup_path` setting are relative. The other 7 rows are absolute paths under `test_stl_shelf`.
- `/Volumes/STL/Loot Studios` is a registered external library, so it is production storage as well as the vault.
- The old app was running from `scrapepage` on `0.0.0.0:8080` throughout Step 0A, started at 14:57 on 2026-10-30.
- Blanked SQLite values survive in free pages until `VACUUM`, so the dev copy uses `secure_delete` and `VACUUM`, and its raw bytes are checked for secrets.
- `DEFAULT_STASH_EXPORT_PATH` falls back to `~/Downloads/DESYNC_Print_Queue`, a folder separate from the configured `DESYNC_Print_Queue2`. Both are in the local protected paths.
- MMF's OAuth documentation is `oauth2-instructions.md` in `MyMiniFactory/api-documentation`.
- MMF documents a refreshable implicit path at `/v1/oauth/mobile/login` and `/v1/oauth/mobile/refresh`, needing no client secret, but marks it "Only for mobile clients" with device details. This app is not a mobile client, so it is not used.
- At last check, `downloaded_files` was all `nas` tier and `download_jobs` had no queued jobs, only 103 completed.
