# Enhancements

Ideas found during migration. None are scheduled unless a step or the user asks for them.

| # | Idea | Benefit | Source |
|---|---|---|---|
| E1 | Scheduled delta sync: scrape, diff, ingest, download | Removes the manual run of the full pipeline | Archeology section 9 |
| E2 | Scheduled integrity checks using the reconciler's SHA-256 and STL validation | Finds silent NAS corruption early | Archeology section 9 |
| E3 | Health endpoint and basic metrics | Faster diagnosis of long-running jobs | Archeology section 9 |
| E4 | Back up `metadata/` as well as `library.db` | Raw API JSON is the source of truth for the catalog | Migration prompt review |
| E5 | Backup and restore controls in the Settings view | Runs and checks backups without a terminal | Migration prompt review |
| E7 | Rename the misleading "Running database startup analytics" startup message | It runs schema setup, not analytics | Step 0A |
| E8 | Measure catalog search under concurrent download writes before adding FTS5 | Search is 7.6 ms on an idle app, so the idle gain may be small | Step 0A |
| E9 | Redact task log text before it is written to `pipeline_tasks.log_output` | Task logs bypass the logger's redaction filter | Step 0C |
| E10 | Replace `fastapi.testclient`'s deprecated `httpx` client path when Starlette's replacement settles | Clears a test warning | Step 1 |
| E11 | Lower the INFO log volume from UI status polling | Polling fills `vault.log` faster now that every request is logged | Step 1 |
| E13 | Add a health check for refresh token failures in Settings | A failed refresh only shows up in logs today | Step 2 |
| E16 | Add an audit history view in Settings | `audit_log` is only readable with SQL today | Step 4 |
| E17 | Give query constants descriptive names | Names like `GET_METRICS_1` came from the script that moved them | Step 4 |
| E18 | Move service-layer SQL into query modules | Step 4 covered route handlers only | Step 4 |
| E19 | Save refreshed Cloudflare cookies from a scrape back to settings | The scraper writes new cookies to its run folder, and nothing reads them | Step 5 |
| E6 | Accessibility and responsive layout review | Neither has been assessed | Archeology section 9 |
| E20 | Import channel for single files | Add a loose STL or archive from anywhere, such as a Google Drive download, into the catalog. These files get a SHA-256 so duplicates can be found. Linked libraries are not hashed | Key decoupling plan |
| E21 | Export tool for moving off this Mac | Production stays on this Mac for now. If it moves, a tool should package the database, metadata, previews, secrets and settings, and rewrite stored paths for the new machine | User decision, 2026-10-31 |
| E22 | Rename detection for linked libraries | Match a renamed folder to its inactive model by file names and sizes, and suggest linking them. It needs the user's confirmation. Until then a rename is a new model, and the old one goes inactive. |
| E23 | Covers for archive-only linked models | The image worker renders loose STLs only. Extract the best mesh from the archive, as the on-demand renderer already can, so these models get a cover. |
| E24 | Review models already in the catalog | The review screen covers previews only. Splitting an added model needs a rule for which card keeps the ID, and what happens to stashes holding it. |
| E25 | Choose variants at stash export | A linked character spans every scale and support type, and export copies every file. Let a stash pick a scale and support type. |
| E26 | Render covers only from a card's own files | A card that had a folder carved out can still pick an STL from that folder for its cover. |
| E27 | Tags page | Rules are in `GLOSSARY.md`. A removed tag must stay removed: scans add tags, so record removals or a rescan brings them back. That changes the rule that scanner tags are never deleted. Clean up word-split junk such as `of` and `the` first (4,710 distinct tags, 2,204 used once). |
