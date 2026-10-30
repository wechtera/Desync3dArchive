# Archeology: Migration State

This directory tracks the state of the app, what has been done, and what comes next. Update this file at the end of every migration step.

## Core rules

1. No emojis.
2. Logging and testing are a birthright.
3. Do not remove functionality.
4. Do not delete or impact production data.
5. Track state, next steps and completed work in this directory using README files.
6. Only introduce complexity where required.

## Documents

| File | Purpose |
|---|---|
| `SCRAPEPAGE_ARCHEOLOGY.md` | Findings: use cases, architecture, business rules, security register V1 to V20 |
| `STACK_PROPOSAL.md` | Accepted stack, decisions, production data safeguards, LAN consequences |
| `MIGRATION_PROMPTS.md` | Ordered prompts for Steps 0 to 9, with exit checks |

## Current state

- **Phase:** Scaffold complete in `DESYNC_3D_Archival`. No application code has been migrated.
- **Source baseline:** `scrapepage` commit `9bfa46d`, frozen and left unchanged.
- **Tests:** 84 passing on 2026-10-30, before commit `9bfa46d`. Not re-run since.
- **Production data:** Untouched. `library.db` was last modified at 15:50 on 2026-10-30, before this work began.

## Done

- [x] Application archeology
- [x] Stack proposal drafted and decisions recorded
- [x] Migration prompts written
- [x] New project created: git repository, README with setup steps, `.gitignore`, Python and Node dependency manifests, `tools/` linker, and these docs

## Next

- [ ] Confirm the server OS for LAN use (does not block Steps 0 to 6)
- [ ] Run Step 0 from `MIGRATION_PROMPTS.md`

## Project decisions

- **New root.** `DESYNC_3D_Archival` is a new git repository with no history from `scrapepage`, and it sits beside `scrapepage`, not inside it. It is local only for now.
- **Old app frozen.** `scrapepage` gets no changes. It still has V1 to V3, so run it only on a trusted network until cutover.
- **Files.** Everything here is either net new or migrated from `scrapepage`.
- **Layout.** Code is organized by component under `src/desync_archival/`, `web/`, `scraper/` and `migrations/`. See the root README.
- **`tools/` holds symlinks only.** Blender, 7-Zip and unrar stay where they are installed. Adapter code lives in `src/desync_archival/external/`.
- **Production data.** It is copied to `~/DESYNC_Data` at cutover. The originals stay in `scrapepage` as the rollback copy. Development uses `~/DESYNC_Data_dev`.
- **Carried over from `Archival_DEV`.** Only `Archeology/` and `ai/`. The legacy CLI scripts and `rnd/` stay in `scrapepage`.
- **`SCRAPEPAGE_ARCHEOLOGY.md`** is a historical record. Its paths and line numbers describe `scrapepage`.

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
