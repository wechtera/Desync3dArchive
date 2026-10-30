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
| E6 | Accessibility and responsive layout review | Neither has been assessed | Archeology section 9 |
