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
| `HANDOFF.md` | Start here in a new session: context, locations, status and pitfalls |
| `SCRAPEPAGE_ARCHEOLOGY.md` | Findings: use cases, architecture, business rules, security register V1 to V20 |
| `STACK_PROPOSAL.md` | Accepted stack, decisions, production data safeguards, LAN consequences |
| `MIGRATION_PROMPTS.md` | Ordered prompts for Steps 0A to 9, with exit checks |
| `Questions.md` | Open questions that are not blockers |
| `Enhancements.md` | Enhancement ideas found during migration |

## Current state

- **Phase:** Step 8 is merged. Key decoupling (Step 8B) is complete on `migration/08b-keys` and needs approval to merge.
- **Source baseline:** `scrapepage` commit `9bfa46d`, frozen and left unchanged.
- **Tests:** `check.sh` passes at the latest commit: 384 Python tests, 1 skipped, 22 browser tests and 110 contract cases.
- **Production data:** Untouched. Dev work never reaches it, and `library.db` still has its 15:50 modified time from 2026-10-30. The last full NAS comparison, after Step 4, was clean.

## Done

- [x] Application archeology
- [x] Stack proposal drafted and decisions recorded
- [x] Migration prompts written
- [x] New project created: git repository, README with setup steps, `.gitignore`, Python and Node dependency manifests, `tools/` linker, and these docs
- [x] Step 6 code: lit-html views, strict CSP, API key setting removed
- [x] Step 5: job recovery, external tools adapter, scraper events, download resume, scheduled backups, pause before token expiry
- [x] Step 4: domain routers with Pydantic bodies, query modules, catch-all removed, dry-run and audit on destructive routes, V6 closed
- [x] Step 3: numbered migrations, runner, one schema source, verified backup and restore, WAL
- [x] Step 2 code: V1 to V5 and V9 closed, code grant with refresh, token rotation handling, no secrets in responses
- [x] Step 1: FastAPI and uvicorn shell, lifespan, threadpool, request logging, Playwright smoke test
- [x] Step 0C: `check.sh`, per-file lint and type ratchet, JSON log lines, request and job IDs, redaction
- [x] Step 0B: app, tests, frontend and scraper migrated; settings module and production guard added
- [x] Step 0A: production manifest, sanitized dev data, throwaway old app, 105 contract snapshots, snapshot secret test, performance baseline
- [x] Migration prompts reviewed against the code. Step 0 split into 0A, 0B and 0C, and data-safety, V6, backup and cutover gaps closed

## Next

- [ ] User: look over the UI on the dev app, then approve the merge of `migration/06-frontend`
- [x] Step 7 LAN mode: login, TLS, startup refusal
- [x] Step 8 code: FTS5 catalog search
- [x] Merge Step 8
- [x] Step 8B code: key decoupling, duplicate report, cutover tool
- [ ] Merge Step 8B after approval

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

## Step 8 results

Measured with `record_contract.py --measure` on the sanitized copy of 4,893 models, the same size as production, with 15 repeats on this Mac. "Before" is the Step 7 code measured the same night, because the Step 0A numbers came from the old app and aren't comparable.

| Measure | Before median ms | After median ms | Before max ms | After max ms |
|---|---:|---:|---:|---:|
| Search, no match (`zzzqqq`) | 8.71 | 1.12 | 13.14 | 1.44 |
| Search, typical (`dragon`) | 8.69 | 6.35 | 9.82 | 7.07 |
| Search, common (`the`) | 9.25 | 8.01 | 12.43 | 8.51 |
| Search, two letters (`dr`) | 8.68 | 9.90 | 12.24 | 10.30 |
| Catalog first page | 2.79 | 2.63 | 44.06 | 40.28 |
| Catalog deep page, offset 4,800 | 3.94 | 3.82 | 6.55 | 4.08 |
| Metrics | 17.86 | 13.28 | 25.57 | 13.70 |
| Creators | 3.53 | 3.34 | 6.88 | 3.64 |
| Releases | 2.08 | 2.01 | 2.55 | 2.27 |
| Startup, including migrating a fresh copy | 151.6 | 211.9 | 230.7 | 213.8 |

Only the four search rows changed in code, so differences in the other rows are noise. Search query time alone, without page assembly, averaged 4.0 ms before and 0.48 ms after across the term set below. Startup rises because the harness migrates a fresh copy on every start, and the backfill takes 45 ms. That is a one-time cost, not a cost on every start of a migrated database, though that was reasoned from the harness rather than timed separately.

- **FTS5:** migration 0004 adds `catalog_search`, a trigram index with triggers on `models` and `model_tags`. It adds 3.3 MB. Search still uses `LIKE`: FTS5 picks candidates and SQLite re-checks each one, so substrings, wildcards and case behavior are unchanged. Plain word matching would have lost `ragon` finding `dragon`. Terms under three letters can't use trigrams and take about 1 ms longer.
- **Same results:** 17,664 terms from the real catalog, including 1 to 9 letter fragments in mixed case, gave 0 differences from the old query. Contract: 110 of 110.
- **Keyset pagination: not added.** A deep page costs about 1 ms more than the first page, so the baseline shows no real problem, and the prompt says not to optimize without one.
- **Lazy image loading: no change needed.** Grid, release, stash and submodel images already use `loading="lazy"`. The detail page's main image stays eager because it is the first thing on screen.
- **Migration, backup and restore** ran on the dev database, and the restored copy passed integrity checks.
- **Bug fixed along the way:** tests and the harness inherited `DESYNC_HOST` from `.env`, so a LAN setup would have changed what they ran against. They now pin loopback with login off.

## Step 7 results

- One password: salted scrypt hash in a 0600 file outside the repository and data folder, set with `dbtool set-password`. No users table.
- Session cookie is HttpOnly, Secure and SameSite=Strict. Sessions are held in memory as hashes, last 30 days and end on restart, logout or a password change.
- Five failures from one address lock it out for 15 minutes.
- `/api/auth/callback` stays public because Strict cookies are not sent on the redirect back from MMF. The page holds no data, and its `token_callback` POST is same-site, so it still needs a session.
- Tested: every registered route refuses anonymous requests, and the real server command runs over TLS and refuses a LAN bind when unprotected.
- Exit check: passed on 2026-10-30. The user logged in from another computer.
- Not tested: MMF sign-in through a LAN redirect URI.

## Step 6 results

**Exit check:** Automated parts passed. No view uses `innerHTML`, `insertAdjacentHTML` or `escapeHtml`, and no inline handler remains. Browser tests render hostile names through every view, and none run. Only `auth_service.py`, to hide it from the API, and `logger.py`, to redact it, still name `mmf_client_secret`.

**Baselines proved the risk:** tests written against the old views failed before conversion, because a hostile creator name ran as code on the creators page, quote characters broke release and stash buttons, and a `javascript:` URL from the API became a clickable link.

**Templates:** `lit-html` 3.3.1 is vendored in `web/static/vendor/`, with its npm checksum verified and no build step. `web/static/js/template.js` wraps it. Its `render` clears placeholder markup first, because `lit-html` adds to static content where `innerHTML` replaced it. `safeUrl` allows only http and https links from API data.

**CSP:** every response carries a policy with `script-src 'self'`, so inline scripts and handlers can't run. A browser test proves an injected `onerror` is blocked. Inline `style` attributes are allowed with `style-src 'unsafe-inline'`, because the views use them widely. `img-src` allows `https:` for MMF cover images.

**Handlers:** the 110 inline handlers in `index.html` became `data-click`, `data-change` and `data-input`, dispatched by `web/static/js/actions.js`. The 96 in view templates became `@click` bindings.

**Sign-in callback:** the page's inline script moved to `web/static/js/auth_callback.js`.

**API key setting removed:** the Settings field, `client_secret` in `/api/auth/config`, the code grant, token refresh and `mmf_auth_flow` are gone. Sign-in is the implicit flow only. The value stored in `mmf_client_secret` stays hidden until Step 9 blanks it, and `SECRET_SETTING_KEYS` still lists it for that reason.

**Tests removed:** the token refresh and download worker refresh tests went with the code they covered. Token refresh is no longer a feature.

**Contract snapshots changed, all intended:** `auth_status` lost `secret_configured`, `refresh_configured` and `flow`, and `auth_callback` now loads a script file.

**Q12:** `route.fetch` mocks could run past the end of a test because of the app's polling, with a `Response has been disposed` error. All are static now, which is the likely cause of the rare failure, though that isn't proven.

## Step 5 results

**Exit check:** Passed. A killed app recovers, which `tests/test_job_recovery.py` proves with SIGKILL. The user ran a real headless scrape: 4,869 plain numeric IDs, all 4,868 catalog models plus 1 new. Model 729914 downloaded to the dev vault: 2 RAR files, 177 MB, sizes and SHA-256 matching, archive contents indexed, and nothing on the NAS.

**Not yet known:** whether MMF's hosts honor `Range`, because no download has needed to resume. With `--no-sandbox` removed, the scrape passed with no Cloudflare check appearing, so the check itself is untested in that mode.

- **Job runner (item 1):** every background job starts through `TaskManager._spawn`, with a time limit and a crash guard. At startup, `jobs.recover_interrupted` turns tasks left `running` into `interrupted`, and returns `downloading` jobs to `pending` and `offloading` jobs to `staging_complete`. Before this, one crash blocked every future task. `tests/test_job_recovery.py` kills a real server with SIGKILL and restarts it.
- **Releases (item 2):** ingest now rebuilds `releases`. A failed rebuild logs and keeps the ingest result.
- **Cookie fallback (item 3):** fixed, through one `parse_cookies` helper.
- **External tools (item 4):** all unrar, 7-Zip and Blender calls go through `src/desync_archival/external/`, closing V7. Lookup order is `tools/` symlinks, PATH, a setting, then common folders. Calls have timeouts, stdin closed, and lenient decoding. Bulk extraction checks paths, file count and size before unpacking, then checks again afterward and removes symlinks. `sanitize_segment` has one home in `services/paths.py`.

- **Scraper (item 5):** `scrape.js` emits JSON lines, and `TaskManager._handle_scrape_line` reads them. Other text, such as a Node error, stays in the task log. The 4,000 early exit is now 80% of the catalog size, passed with `--expected`. The 4,868 target is gone on purpose: stopping at the catalog size could miss new purchases, so scrolling stops when no new models appear. `--no-sandbox` is removed, intercepted URLs are logged as paths only, and each run's cookie copy is 0600 and deleted when the scraper exits. Existing copies in old `scrapes/` folders were left alone.
- **Download resume (item 6):** a saved `.part` file is resumed with a `Range` request, only when the host answers 206 from exactly that byte. Otherwise the file restarts. The checksum covers the whole file. Whether MMF's hosts honor `Range` is not known yet.
- **Backups (item 7):** a daily `database_backup` task runs through the job runner, checked hourly, and waits if another task is running. Settings shows the last result.
- **Token expiry (item 8):** workers stop claiming new jobs 10 minutes before a token that can't refresh expires, and a banner asks to reconnect. Signing in reloads the workers, so the queue resumes alone. A file already downloading finishes if it can.

- **Scrape fixes after the first real run:** session cookies were dropped by Chrome, because they reached it as `SameSite=None` without `secure`, so the page showed a login. Cookies are now always secure, and Cookie-Editor's `lax` and `no_restriction` values map correctly. The scrape is headless by default through `scraper_headless`, with a Settings switch for a visible window. A login page, a Cloudflare check in headless mode, or a closed window now stops with a plain message. Headless can't pass a Cloudflare check. The app reports that and does not try to get around it.

- **Cookie fields:** Settings has a field for each of `cf_clearance`, `PHPSESSID`, `REMEMBERME`, `__cf_bm`, `_cfuvid` and `__cflb`. Each is a hidden secret and merges into `cookies_json` under its own domain, so nobody has to maintain the JSON by hand.

- **First real scrape:** headless, 8 seconds, 4,869 models: all 4,868 in the catalog plus 1 new purchase, Master Wendigo (276218). The fixed 4,868 target would have stopped short of it. The JSON came from `/api/data-library/objectPreviews`, one API batch of 12,485 items. No secrets reached the logs.
- **Scraped IDs:** every ID arrived as `object-99104`, breaking the numeric ID rule in archeology 2.3. Ingest from a scrape folder was already safe, because it strips non-digits, but the files were wrong. `normalizeId` in `scrape.js` now stores plain digits and drops anything else.
- **Download claim race (V16):** two workers could claim one job, because the claim read a row and then updated it. In a real run, one worker moved a finished file while the other was validating it, and the job failed. The old app has the same race. Download and offload claims are now atomic. `JobClaimTests` races 8 threads over 30 jobs, and fails on the old code.

**Contract:** `downloads_status` and `downloads_queue_get` gain `reconnect_required`, and `settings` gains flags for the four new cookie fields.

**Tool syntax verified against real binaries:** 7-Zip treated `-oevil.stl` as an option without `--`, and extracted it as a file with `--`. unrar also uses `--`: a bare `-` fails with "Unknown option", which `test_real_unrar_accepts_the_stop_marker` guards. A real RAR archive passed listing, bulk extraction and single-member extraction through the adapter.

**RAR test is opt-in:** `rar` is unverified by macOS Gatekeeper and prompts the user, so the real RAR fixture test runs only with `DESYNC_TEST_RAR=1`. `unrar` is unaffected. Do not bypass Gatekeeper.

## Step 4 results

**Exit check:** Passed. The catch-all and `dispatch_route` are gone, and all 110 contract cases pass through the routers. Each of the 8 domains moved in its own commit, with tests and the contract green after each one.

**Routers:** every URL and alias is declared in `src/desync_archival/api/routers/`, with Pydantic bodies in `api/schemas.py`. Bad bodies return 400. Unknown routes and wrong methods keep the old JSON 404. Shared plumbing is in `api/routing.py`.

**Query modules:** handler SQL moved unchanged into `src/desync_archival/db/queries/`, 57 statements in all. It was lifted by script, not retyped. Query names follow the handler and a number, and they are worth renaming when each area is next changed. Services still hold their own SQL: this step covered route handlers only.

**V12:** delete-orphans, staging purge, reconcile reset, tasks clear and stashes purge now dry-run by default and report what they would do. Only `"confirm": true` acts. Every call, dry or real, writes a row to the new `audit_log` table with the request ID. The UI sends `confirm` only after its dialog is accepted, and the task clear and stash purge dialogs now show counts from a dry run.

**V6 closed:** `DESYNC_ALLOWED_ROOTS` in `.env` limits `/api/system/browse`, `/api/storage/config`, path fields in `POST /api/settings` and `/api/settings/libraries`. It can't be changed over the network, and it defaults to the data directory. Symlinks and relative paths are resolved before the check.

**V11 and E12:** route error responses no longer return exception text. Detail goes to the log.

**Contract snapshots changed, 15 of 110, all intended**
- New dry-run cases: `reconcile_delete_orphans_dry_run`, `reconcile_staging_purge_dry_run`, `reconcile_reset_dry_run`, `stashes_purge_dry_run`, `tasks_clear_dry_run`.
- Request gained `confirm`, with identical responses: `reconcile_delete_orphans`, `reconcile_orphan_audit`, `reconcile_staging_purge`, `reconcile_staging_purge_alias`, `reconcile_staging_sweep`, `reconcile_reset`, `stashes_purge`, `settings_stashes_purge`, `tasks_clear`.
- `render_stl`: the error is now generic.

**Tests removed:** two tests in `test_stash_routes.py` called the removed dispatcher directly. The routers and contract cover that path now. Tests that patched `dispatch_route` now add a small test route.

**Bugs found**
- `create_stash` returned raw exception text as a 400 for any failure. Only `ValueError` does now. A new test exposed it.
- Running `python -m desync_archival.api.server` crashed on a circular import after the routers were added, and would have left routes with no managers. In-process tests could not see it, so `tests/test_entry_point.py` now starts the real command.

**npm audit:** a new advisory put 5 moderate findings under `puppeteer-extra-plugin-stealth`: a denial of service in `minimatch`, reached only by `rimraf` on a local temp folder. The only fix is a breaking stealth plugin downgrade, which can't be tested without a real scrape. `npm_audit_allowed.json` lists those packages with reasons, and any other moderate or higher finding still fails `check.sh`.

## Step 3 results

**Exit check:** Passed on the dev database through `dbtool migrate`: all 16 tables and 107,444 rows matched before and after, with integrity `ok`, 0 foreign key violations, WAL mode and schema version 2. A backup restored to a new path with identical row counts, and restoring over an existing file was refused. All 105 contract snapshots still match, without changes.

**One schema source:** about 570 lines of table creation and `ALTER` shims are gone from 11 modules. `migrations/0001_baseline.sql` is the exact schema captured from a dev copy. A live database at `user_version` 0 is adopted as version 1 with no structural change, after its tables and columns are checked. `0002_wal.sql` turns on WAL. No foreign key or index corrections were needed.

**Production safety:** the app never migrates by itself anywhere. It refuses to start if the schema is behind, and it names the command to run. On production, `dbtool migrate` also needs `--confirm-production`. Migration takes a verified backup first, stops if the backup fails, and checks row counts afterward. Production has not been touched.

**Backups:** `dbtool backup` writes a verified 0600 file to `DESYNC_BACKUP_DIR` and keeps 14. Each backup is one self-contained file. `dbtool restore` only writes to a new path. The daily schedule arrives in Step 5.

**Bugs found and fixed**
- External library scans could never finish. The indexer's last step wrote to `releases` using a constraint the real table has never had, so it failed on the real schema. All 3 scans in history show as failed, and Loot Studios still shows 0 models and no scan date. The test fixture had its own `releases` table, which hid this. Rescanning at cutover should correct those library totals.
- `run_startup_analytics` dropped and recreated `releases`. It now clears the rows, so the migrations stay the only schema source. It is still never called, and Step 5 addresses that.
- Default settings read `cookies.json` from the working directory. They now read it from the data directory.
- Stash service tests never ran the "downloaded first" guardrail, because their minimal tables lacked the vault columns. They run with it now.

**Test changes:** databases in test fixtures now come from migrations through `tests/db_helper.py`, apart from `tests/test_analytics.py`, which has its own in-memory schema and logic. Inserts gained the columns the real schema requires. Assertions did not change, except that the stash service schema test now calls the helper, and `test_stash_service.py` gives its models a vault.

**Dev tooling:** contract runs now set `nas/vault`, `stash` and `staging` aside and restore them. Baselines start from `library.clean.db`, so real dev sign-ins can't reach a snapshot.

## Step 2 results

**Exit check:** `check.sh` passes. On 2026-10-31 the user signed in through the implicit flow in a fresh browser and downloaded model 638590 to the dev vault. The file's size and SHA-256 match the record, and it stayed off the NAS. The code grant and refresh paths were never exercised against MMF, and Step 6 removes them.

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

**API key:** not used. The user chose OAuth only, so the app sends the key nowhere.

**Token duration:** the user chooses it on MMF's login page, and that choice sets `expires_in`. A real sign-in on 2026-10-31 returned a token lasting 168 hours, far more than the 21,600 seconds in MMF's documentation. Only the Client ID in Settings matters for this flow.

**Limitation:** the implicit grant has no refresh tokens, so a 401 after the chosen duration still trips the circuit breaker until the user reconnects. V5 is closed for `state` checking, but the implicit grant itself remains in use.

**What each credential does**
- Client ID: needed only to open the MMF login page. `handle_get_auth_login` returns 400 without it. Requests made with a token don't use it.
- API Key / Secret: used only by the code grant and refresh paths, which stay off. Step 6 removes it.
- OAuth token: used for metadata, ingest and downloads.
- Session cookies: required by `scrape.js`, which has no token path. Ingest and downloads send them too, as a fallback.

**Bugs found and fixed**
- Times from SQLite are UTC but were shown unconverted, so evening activity in New York showed tomorrow's date. The UI now converts them in the downloads, tasks and library views. Stored data and API responses are unchanged.
- Running download workers kept the token from startup, so a sign-in didn't reach them until a restart. They now reload on sign-in and disconnect.
- The profile lookup can fail just after sign-in, which left the username blank. The status route now fills it in.
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
