# Migration Prompts

Run these in order, one per session. Start each session by pasting the shared preamble and then the step's prompt. Do not start a step until the previous step's exit check passes.

## Shared preamble

```text
You are working on the DESYNC 3D Vault app in the DESYNC_3D_Archival repository. The old app is
in ../scrapepage. Treat it as frozen, and only read from it. First read:
- docs/Archeology/README.md (current state and project decisions)
- docs/Archeology/STACK_PROPOSAL.md (target stack and decisions)
- docs/Archeology/SCRAPEPAGE_ARCHEOLOGY.md (business rules in 2.3, findings V1 to V20)

Core rules:
1. No emojis, in code, logs, UI or docs.
2. Logging and testing are a birthright. New or changed behavior ships with tests and logs.
3. Do not remove functionality. If something looks unused, ask first.
4. Do not delete or impact production data: library.db, metadata/, previews/, scrapes/,
   cookies.json, mmf_browser_profile/, logs/, the staging directory, the NAS vault, the stash
   export directory. That data is in ../scrapepage until cutover, and in ~/DESYNC_Data after it.
   Read production data only in read-only mode. Never run a destructive command on a path you
   have not resolved. Never print secret values.
5. Update docs/Archeology/README.md at the end: what was done, the current phase,
   what is next, and any new facts.
6. Only introduce complexity where required. Say why if you add a dependency.
7. Never use em dashes, in code, logs, UI, docs or commit messages.
8. Never manually modify change logs.
9. Give little weight to development cost when choosing between options. Decide on correctness,
   safety and long-term quality.
10. Never include an agent name as a co-author, in commits or anywhere else.
11. Add open questions that are not blockers to docs/Archeology/Questions.md and keep going. Stop
    and ask only for real blockers.
12. Add enhancement ideas to docs/Archeology/Enhancements.md as you think of them. Do not build
    them unless a step or the user asks.

Work on the branch named in the step. Commit freely on that branch. Merge to main only after the
exit check passes and the user approves. Stop and ask if a step would break a rule. Report test
results honestly.
```

## Step 0A: Sanitized dev data and baselines

**Branch:** `migration/00a-baseline`

```text
Capture how the old app behaves before any code moves. Do not run anything inside ../scrapepage.
1. Add a script that builds ~/DESYNC_Data_dev from production. It opens the source read-only and
   copies library.db with SQLite's .backup. On the copy only, it must:
   - Blank PHPSESSID, cf_clearance, cookie_cf_clearance, cookie_phpsessid, cookies_json,
     mmf_access_token, mmf_refresh_token, mmf_client_id, mmf_client_secret, mmf_oauth_state and
     mmf_token_expires_at.
   - Point download_root_path, download_work_path and stash_download_path at folders in
     ~/DESYNC_Data_dev.
   - Rewrite absolute paths to the dev folders: vault_path in models and downloaded_files,
     raw_backup_path, and path in external_libraries.
   - Fail with a clear error if any listed secret is still set or any production path remains.
   Also copy previews and metadata for a small model sample, and test the script.
2. Make a throwaway copy of the old code in ~/DESYNC_Data_dev/old_app, with its library.db being
   the sanitized copy. It holds code only, with no cookies, browser profile or scrape output.
   Run it there on loopback only, with download workers paused and no network calls to MMF.
3. Record API contract snapshots for every /api route from the throwaway app. Normalize timestamps
   and other volatile fields, and redact signed URLs.
4. Add a test that fails if a snapshot contains a secret value or a signed URL.
5. Record a performance baseline from the throwaway app: startup, catalog listing, search, deep
   paging.
6. Confirm whether run_startup_analytics is ever called, and whether releases goes stale after
   ingest. Report the result. Do not fix it yet.
Before starting, record a manifest of production paths, file counts and modified times. It must
not include file contents.
Exit check: the manifest is unchanged, and the snapshot secret test passes.
```

## Step 0B: Migrate code, settings and guard

**Branch:** `migration/00b-migrate`

```text
Move the app into the new repository. Do not change app behavior.
1. Migrate the app from ../scrapepage into the layout in the root README: services/ to
   src/desync_archival/services/, ui/server.py and ui/routes/ to src/desync_archival/api/,
   ui/index.html and ui/static/ to web/, scrape.js to scraper/, tests/ to tests/, and
   scripts/blender_render_mesh.py to scripts/. Change imports mechanically. Do not copy data,
   secrets or scrape output.
2. Leave test logic alone. The exception is path constants, such as tests that refer to
   ROOT_DIR/previews or ROOT_DIR/library.db. Point those at a sandbox and list each change.
3. Add one settings module reading a required data directory and APP_ENV. Replace every ROOT_DIR
   path anchor with it, and remove the hard-coded library.db defaults from
   services/stash_service.py:19, services/stash_exporter.py:70, ui/routes/settings.py:328 and
   ui/server.py:32 (line numbers from scrapepage). Replace VAULT_DB_PATH. Pass scrape.js an
   explicit cookies path instead of using its working directory. Production use must still work
   through the setting.
4. Resolve relative stored paths against the data directory, not the working directory. This
   covers raw_backup_path and metadata_backup_path.
5. Add a production guard: outside APP_ENV=production, the app and the test suite refuse to start
   if a path resolves to a production location. Check environment settings, and also the paths
   stored in app_settings and external_libraries at startup and whenever a path setting changes.
   Protected locations are ../scrapepage, ~/DESYNC_Data, and every path in
   DESYNC_PROTECTED_PATHS, which must list the NAS vault, the staging directory and the stash
   export directory. Compare whole path components, so ~/DESYNC_Data_dev does not match
   ~/DESYNC_Data. Test it, including symlinks and relative paths.
Exit check: 84 or more tests pass, the migrated app matches the snapshots on the dev copy, and
the production manifest from Step 0A is unchanged.
```

## Step 0C: Checks and logging

**Branch:** `migration/00c-tooling`

```text
Add the quality gate and logging baseline. Do not change app behavior.
1. Add one check.sh that runs pytest, ruff, pyright, pip-audit, npm audit --audit-level=moderate
   and the snapshot secret test. Report existing lint and type errors, but do not mass-fix them.
2. Extend the stdlib logger to write JSON lines with request_id and job_id via contextvars, keep
   the layers and UI log viewer, and add a tested redaction filter for tokens, cookies and signed
   URLs.
Exit check: check.sh passes, and the production manifest from Step 0A is unchanged.
```

## Step 1: FastAPI shell

**Branch:** `migration/01-fastapi-shell`

```text
Run the app on FastAPI and uvicorn with no logic changes.
1. Create the FastAPI app and one catch-all /api route that calls the existing dispatch_route,
   including 2-tuple, 3-tuple, string and bytes responses.
2. Serve index.html, static files and /previews, including the SVG placeholder and cache headers.
3. Start and stop TaskManager and DownloadManager in the app lifespan, including clean shutdown.
4. Run blocking handlers in the threadpool so one slow route cannot stall others.
5. Add request logging middleware that sets request_id.
6. Add @playwright/test in tests/e2e/ with its own package.json, separate from the scraper's
   dependencies. Add it to check.sh and the root README setup.
Exit check: all contract snapshots match, and the frontend works unchanged in a Playwright
smoke test on a dev copy.
```

## Step 2: Security fixes on the FastAPI shell

**Branch:** `migration/02-security`

```text
Fix V1 to V5 and V9 on the FastAPI shell. Keep each change small.
1. Bind to 127.0.0.1 by default, with host and port from settings.
2. Stop returning secrets from GET /api/settings and GET /api/auth/status. Return "configured"
   flags. Update the frontend so Settings still lets the user see that a value is set and
   replace it, and POST /api/settings must not overwrite a secret with a blank.
3. Restrict /api/scrapes/download to scrapes/*/model_ids.txt and models.json. It must never serve
   cookies.json.
4. Serve only web/index.html and web/static/. Disable directory listings.
5. Remove wildcard CORS. In middleware, validate Host and Origin and require application/json on
   POST and DELETE.
6. In middleware, add a request body size limit, and return generic errors while logging details
   server side.
7. Move MMF sign-in to the authorization code grant documented in the MyMiniFactory
   api-documentation repo: server-side code exchange at
   https://auth.myminifactory.com/v1/oauth/tokens, verified state, stored refresh token, and
   automatic refresh before expiry and on 401, including in download workers. Make the redirect
   URI a setting, defaulting to the current value. Keep the current implicit flow working until
   the new flow has been tested by the user with a real login, then ask before removing it.
Existing tests that assert unsafe behavior may change. List each one and the reason.
Add a test for every finding proving it is closed.
Keep a list of every intended contract snapshot change, and update only those snapshots.
For real MMF tests, the user enters MMF credentials on the dev instance only, and they stay in
~/DESYNC_Data_dev. Downloads are small test downloads into the dev vault, never the NAS. Before
the first real login, report whether MMF's documentation says a new token revokes older ones.
Exit check: check.sh passes, and the user confirms sign-in and a test download to the dev vault
in a real browser.
```

## Step 3: Migrations

**Branch:** `migration/03-migrations`

```text
Give the schema one source of truth.
1. Add numbered .sql files in migrations/ and a small runner using PRAGMA user_version.
2. Write 0001 as the exact current schema, from a dev copy. A DB with user_version 0 and the
   expected tables is adopted as baseline with no structural change.
3. Replace CREATE TABLE IF NOT EXISTS blocks and "self-healing" ALTER shims across modules with
   the runner. Keep tests passing, and make their databases come from migrations.
4. Add a backup command. It writes a timestamped library.db made with SQLite's .backup to
   DESYNC_BACKUP_DIR, which must be outside the data directory, then verifies it with
   PRAGMA integrity_check and row counts. Backup files are 0600, because they contain secrets
   until Step 9 removes them. It keeps a set retention and logs every run. Add a restore command
   that restores to a new path only, and test backup and restore together.
5. The runner uses the backup command before any change, and it must never run automatically
   against production. Applying to production is an explicit command, used only at cutover.
6. Add 0002 for WAL support and foreign-key or index corrections if needed, tested on a dev copy.
Exit check: on a dev copy, row counts for every table are identical before and after, and a
backup restores with matching row counts.
```

## Step 4: Routers and query modules

**Branch:** `migration/04-routers`

```text
Replace the catch-all one domain per commit, in this order: catalog, archives, downloads, stashes,
settings and storage, tasks, reconcile, auth.
For each domain:
1. Add a FastAPI router with Pydantic request models. Keep every URL and alias unchanged.
2. Move SQL from handlers into a query module. Keep it raw SQL.
3. Keep the contract snapshots green. Any difference must be intentional and listed.
4. Add validation tests.
Also add dry-run and an audit log entry to destructive routes (V12): delete-orphans, staging purge,
reconcile reset, tasks clear, stashes purge. Keep current behavior reachable from the UI through
a confirm step.
Close V6: add a DESYNC_ALLOWED_ROOTS setting read from .env, which cannot be changed over the
network. /api/system/browse, /api/storage/config, path fields in POST /api/settings and
/api/settings/libraries must all reject paths outside it. Settings can still change paths inside
the allowed roots. Test each route with paths outside the roots, relative paths and symlinks.
Business rules in archeology section 2.3 must stay covered by tests.
Remove the catch-all only when it has no traffic in the contract suite.
Exit check: all contract tests pass with the catch-all removed.
```

## Step 5: Jobs and external tools

**Branch:** `migration/05-jobs-tools`

```text
1. Add one DB-backed job runner with explicit states, using threads and no broker. On restart,
   mark or resume orphaned running jobs. Port scrape, ingest, reconcile, archive peek, stash
   export and external indexing onto it. Keep the one-active-pipeline rule unless asked, and keep
   download workers, pacing and the circuit breaker intact.
2. Fix the releases rebuild if Step 0A confirmed it goes stale.
3. Fix the `parsed` versus `raw` bug in the cookie-file fallback in _async_ingest.
4. Create one external tools adapter for unrar, 7z and Blender in
   src/desync_archival/external/: lookup through tools/ symlinks, then PATH, then settings,
   timeouts everywhere, `--` separators where supported, path normalization, and limits on
   extracted size and file count. Test with hostile archives: path traversal, option-like member
   names, oversized contents. Remove duplicated discovery code and sanitize_segment copies.
5. Change scrape.js to emit JSON lines, parse those instead of console text, make the 4,868 target
   and 4,000 early-exit values dynamic, remove --no-sandbox if the Turnstile flow still works, and
   log the URL path of intercepted JSON without query strings.
   Stop stale cookies accumulating in scrapes/ for new runs. Do not delete existing ones.
6. Add HTTP Range resume to downloads if MMF's file hosts support it. Fall back to the current
   behavior if they do not.
7. Run the Step 3 backup on a daily schedule through the job runner, and show the last backup
   time and result in Settings.
Exit check: killing the app mid-job on a dev copy leaves recoverable state, and a real small
scrape and a test download to the dev vault succeed with user approval.
```

## Step 6: Frontend

**Branch:** `migration/06-frontend`

```text
Vendor one pinned lit-html release with no build step. For each view: write Playwright tests for
current behavior first, then replace innerHTML with lit-html templates. Order: creators, releases,
tasks, stash_modal, stashes, settings, downloads, models. Keep vault.css and all behavior.
Also fix the OAuth callback page's innerHTML use, and add a strict Content-Security-Policy
with no inline scripts.
Remove the API Key / Secret setting, because the app signs in through the MMF login page and uses
only the OAuth token. Remove the field from Settings, client_secret from /api/auth/config, the
authorization code grant, token refresh and mmf_auth_flow, which all depend on it. Keep the
Client ID, because the login page needs it, and keep the session cookie fields, because the
scraper needs them. Do not change mmf_client_secret in production: Step 9 blanks it.
Exit check: no innerHTML remains, an XSS test using hostile model names passes, every view's tests
pass, and no route or view reads mmf_client_secret.
```

## Step 7: LAN mode

**Branch:** `migration/07-lan`

```text
Add opt-in LAN mode.
1. Single password stored as a salted scrypt hash, set from a CLI command. Session cookie with
   HttpOnly, Secure, SameSite=Strict, login rate limiting, and a logout route. Nothing else: no
   users table.
2. If bound beyond loopback, refuse to start without a password and a TLS certificate.
3. Serve TLS through uvicorn, and document creating a certificate with mkcert.
4. Add an allowed-hosts setting for the Host check.
5. Document scraper limits on a headless server and the redirect URI requirement.
Exit check: another computer can log in and use the app, and tests prove anonymous requests fail.
```

## Step 8: Performance

**Branch:** `migration/08-performance`

```text
Compare against the Step 0A baseline on a dev copy. Add an FTS5 migration for name, designer and
tags, with the same search results as today plus better speed. Add keyset pagination where the
baseline shows a real problem, and keep the API contract. Add lazy image loading.
Do not optimize anything without a measured need.
Exit check: a before and after table is recorded in the Archeology README.
```

## Step 9: Secrets and cutover

**Branch:** `migration/09-cutover`. **This is the only step that changes production. Get approval at each numbered stage.**

```text
Prepare, but do not run against production without explicit approval at each stage.
1. Write a cutover runbook in docs/Archeology/CUTOVER.md with commands, checks and rollback.
   Rollback must cover the storage that cutover does not copy: the NAS vault and the stash export
   directory stay shared. Files the new app adds are unknown to the old database, so after a
   rollback, old delete-orphans must not run until a reconcile has reviewed them.
2. Add a command that copies secrets from app_settings to a 0600 secrets file. Make the app read
   from that file, falling back to app_settings during transition.
3. Rehearse the full cutover on a fresh dev copy: backup, migrations, WAL, FTS5, secrets, stored
   path rewrites, and a restore from backup. The 7 absolute raw_backup_path rows must end up
   pointing at the new data directory. Compare
   row counts (expected at last count: 4,893 models, 23,572 model_files, 496 downloaded_files,
   but use the live numbers at the time).
4. With approval: stop the old app, take a verified backup, copy production data from
   ../scrapepage to ~/DESYNC_Data, run the same steps on that copy using
   `dbtool migrate --confirm-production`, verify, and start the new app with APP_ENV=production.
   DESYNC_ALLOWED_ROOTS must list the data directory, the NAS vault, staging, the stash export
   folder and every external library, or Settings will refuse them. Rescan Loot Studios, because
   its totals are still blank from the indexer bug fixed in Step 3. Do not change the originals
   in ../scrapepage. Set DESYNC_BACKUP_DIR
   to a different disk from the data directory, confirm the scheduled backup runs, and restore
   the first one to a new path to prove it works. Add the secrets file to the backup.
5. Keep the old app, its data and the backup available until the user accepts the result.
6. Only after acceptance, and with separate approval, blank secrets in app_settings in
   ~/DESYNC_Data/library.db and rotate the MMF client secret and cookies, since they were exposed
   by V2. Ask separately before removing any data left in ../scrapepage.
7. Update the root README, and record final state in the Archeology README.
Exit check: the user signs off after a real scrape, ingest, download and stash export.
```
