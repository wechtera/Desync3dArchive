# Stack Proposal

**Status:** Accepted, decisions recorded in section 8. The server OS for LAN use is still to be confirmed.
**Date:** 2026-10-30
**Replaces:** section 10 of `SCRAPEPAGE_ARCHEOLOGY.md`, which predates the rule "only introduce complexity where required".

## 1. Recommendation

Keep the architecture: one Python process, SQLite, and local disk plus the NAS. Replace the unsafe or hand-rolled layers around it.

The problems found in the archeology are in the HTTP layer, secrets handling, schema management, job recovery and UI rendering. None of them come from Python or SQLite. A full rewrite in a new language would put the tested domain logic at risk without fixing anything those layers don't already fix.

## 2. Stack

| Layer | Today | Proposed | Why |
|---|---|---|---|
| Language and tooling | Python 3.12, uv | **Keep** | Works, and 84 tests depend on it |
| HTTP | stdlib `http.server`, regex if-chain | **FastAPI on uvicorn**, bound to `127.0.0.1` by default, with LAN bind opt-in | Request validation, size limits, middleware for Host/Origin checks, generated API docs |
| App login | None | **One password, stored as a salted hash (stdlib `scrypt`), with a session cookie.** Required when bound beyond loopback | Enough for one user on a LAN. No user table or roles |
| Transport | HTTP only | **HTTP on loopback. For LAN, uvicorn serves TLS from a local certificate made with `mkcert`** | The password and session cookie should not cross the network in clear text. No reverse proxy needed |
| MMF sign-in | Implicit grant, no refresh | **Authorization code grant with refresh tokens** | Documented by MMF. Stops the 6-hour token expiry from halting long download runs |
| Database | SQLite, rollback-journal mode | **SQLite in WAL mode** | Readers stop blocking on the download workers' writes |
| Data access | Raw SQL | **Keep raw SQL**, moved into per-domain query modules | An ORM adds a large layer and no capability this app needs |
| Migrations | `CREATE IF NOT EXISTS` and `ALTER` in 7 modules | **Numbered `.sql` files plus `PRAGMA user_version`**, with a runner of about 50 lines | One schema source of truth. `user_version` is 0 today, so the live DB becomes the baseline |
| Search | `LIKE '%term%'` | **SQLite FTS5** on name, designer and tags | Built in, and no new service |
| Background jobs | `TaskManager` and `DownloadManager` singletons with threads | **One small DB-backed job runner**, in-process, using threads | Adds restart recovery and timeouts. No Redis or queue library |
| Scraper | Node, `scrape.js`, progress parsed from stdout | **Keep Node**, and have it emit JSON lines | Required: the documented API has no library or purchases endpoint. The stealth plugin is Node-only and the scraper works |
| External tools | `unrar`, `7z`, Blender, with copy-pasted discovery | **Keep the tools**, behind one adapter module, reached through symlinks in `tools/` | One place for argument safety, timeouts and extraction quotas. Installations stay where they are |
| Frontend | Vanilla ES modules, `innerHTML` strings | **Keep modules and CSS. Replace `innerHTML` with vendored `lit-html`** | Escapes by default and needs no build step. Views can be ported one at a time, so functionality isn't rewritten from scratch |
| Secrets | Plaintext in `app_settings` | **One `0600` secrets file outside the repo and the data directory** | Secrets leave the DB. The OS keychain was dropped because it is unreliable on a headless server, and one mechanism is simpler than two |
| Logging | stdlib with topic layers | **Keep stdlib and layers**, add JSON lines output (section 4) | Extends what exists |
| Tests | `unittest` | **`pytest`**, which runs the existing tests unchanged, plus Playwright UI tests | Keeps all 84 tests and adds coverage for the untested UI |
| Static checks | None | **`ruff`, `pyright`, `pip-audit`, `npm audit`** behind one `check.sh` | One command, and GitHub Actions can call it later |
| Dependencies | `requests`, `tqdm`, Python `playwright` declared | **Not declared in the new project** | Imported only by legacy scripts that stay in `scrapepage` |

## 3. Considered and rejected

| Option | Why not |
|---|---|
| SQLAlchemy and Alembic (earlier suggestion) | Rewrites all SQL for no functional gain |
| Postgres | Single user. It would add a server to run, back up and migrate |
| Redis, Dramatiq, RQ | One user and one active pipeline task do not justify a broker |
| Docker (earlier suggestion) | The scraper needs a headed Chrome for the Turnstile click, and the app needs the Mac's NAS mount and Homebrew tools. Containers make all of that harder |
| React, Svelte, Vite (earlier suggestion) | A build pipeline plus a rewrite of about 6,000 lines of JS, with the highest risk of losing behavior |
| Go or Rust backend | Downloads are limited by MMF pacing and the NAS, not by language speed |

## 4. Logging and testing as a baseline

These are in place before any feature moves, and they apply to every later change.

**Logging**
- One JSON line per event in `logs/`, keeping the `http`, `sql`, `storage` and `archive` layers.
- Every request gets a `request_id`, and every job event a `job_id`, through `contextvars`.
- Every request, job state change, external command, and destructive action is logged with its outcome.
- A redaction filter strips tokens, cookies and signed URLs, and has its own tests.
- The Settings log viewer keeps working.

**Testing**
- A change is not done until it has tests and `check.sh` passes.
- API contract snapshots are recorded from the current app before routes move.
- Logging is tested too: key events have assertions.
- Each UI view gets a Playwright test before it is ported.

## 5. Protecting production data

**Production means:** `library.db`, `metadata/`, `previews/`, `scrapes/`, `cookies.json`, `mmf_browser_profile/`, `logs/`, the staging directory, the NAS vault, and the stash export directory.

- **Copies for development.** Work uses a copy made with SQLite's `.backup` in `~/DESYNC_Data_dev`, outside the repo. Production is never used as a test target.
- **Explicit data path.** The app gets one required setting for its data directory. Every hard-coded `ROOT_DIR / "library.db"` default goes, in `stash_service.py:19`, `stash_exporter.py:70`, `settings.py:328` and `server.py:32`.
- **Production guard.** Outside `APP_ENV=production`, the app and the test suite refuse to start if any configured path resolves to a production location. That covers both `scrapepage` and `~/DESYNC_Data`.
- **Destructive actions.** They get a dry-run step and an audit log entry, and are disabled outside production.
- **Cutover is a separate, approved step.** WAL mode, migrations and moving secrets all change the real database. Each runs only at cutover, after a verified backup, with row counts compared before and after. Nothing gets deleted from `app_settings` until the secrets file has been confirmed working.
- **Rollback.** Cutover copies production data to `~/DESYNC_Data`, and the new app changes only that copy. The originals in `scrapepage` stay as they are, with the old app runnable against them, until the new one has been accepted.

## 6. Performance

Nothing has been profiled yet, so these are expected gains. Phase 0 records a baseline to measure them against.

| Target | Change |
|---|---|
| UI stalls during downloads | WAL mode |
| Catalog search | FTS5 |
| Deep paging | Keyset pagination instead of `OFFSET` |
| Interrupted large downloads | HTTP `Range` resume, if MMF's file hosts support it |
| 1.6 GB `previews/` | Lazy loading and correct cache headers |

## 7. Migration shape

The work happens in `DESYNC_3D_Archival`, a new repository beside `scrapepage`. Each file is either net new or migrated, and `scrapepage` stays frozen. The new app stays usable after every phase from Phase 0. URLs under `/api` don't change, so the frontend keeps working throughout.

| Phase | Work |
|---|---|
| 0 | Foundations: migrate the code and tests, production guard, dev data copy, `check.sh`, logging baseline, contract snapshots, performance baseline |
| 1 | FastAPI wraps the existing `dispatch_route` through one catch-all route, so every endpoint moves at once with no logic changes |
| 2 | Security fixes on the FastAPI shell (loopback bind, secret exposure, CORS, static files, OAuth `state`). This follows Phase 1 so nothing is built twice for `http.server` |
| 3 | Migration runner, with the live schema captured as the baseline |
| 4 | Routers replace the catch-all one domain at a time, read-only first: catalog, downloads, stashes, settings, tasks, reconcile |
| 5 | Job runner and external tool adapters, with extraction hardening |
| 6 | Frontend ported view by view to `lit-html` |
| 7 | LAN mode (login and TLS) |
| 8 | Measured performance work, including FTS5 |
| 9 | Secrets, then rehearsed cutover on a copy, then production cutover to `~/DESYNC_Data` |

## 8. Decisions

1. **Network reach: local, with optional access from another computer on the LAN.** Loopback stays the default. LAN mode is opt-in and refuses to start without a password and a TLS certificate.
2. **Scraper: stays.** The documented API (`MyMiniFactory/api-documentation`, read 2026-10-30) has no library or purchases endpoint. `/users/{username}/objects` is documented only as "a user's objects", with nothing tying it to purchases. `scrape.js` also catches JSON from an undocumented endpoint on the library page, but past task logs do not record its URL. Phase 5 logs that URL, for information only.
3. **Frontend: `lit-html`.**
4. **New project.** See "Project decisions" in `README.md` for the new root, layout, `tools/` and data location decisions.

## 9. LAN consequences

- **The scraper needs a display.** It runs a visible Chrome for the Turnstile click. On a headless server it won't work. The existing "paste IDs" ingest stays as the way in: scrape on a desktop, then submit the IDs.
- **`cf_clearance` is tied to IP and browser fingerprint.** Cookies captured on one machine may not work on another.
- **The MMF redirect URI is hard-coded** to `http://localhost:8080/api/auth/callback` in `ui/routes/auth.py:13`. It becomes a setting, and it must match what is registered on your MMF API client. Connecting MMF from a second computer needs a redirect URI that works from that computer's browser.
- **`/api/system/browse` lists the server's folders.** It stays, restricted to configured roots and behind login.
- **macOS tool paths** (`/opt/homebrew/bin/*`) become lookups on `PATH` plus settings.

## 10. Still to confirm

- **Server OS for LAN use.** It affects only path defaults and how the app is run as a service, so it does not block Phases 0 to 6.
- **MMF token behavior.** The docs say refresh tokens are optional ("MAY"), and the live response has to be tested with your client in Phase 2.
