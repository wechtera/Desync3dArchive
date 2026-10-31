# Cutover runbook

**Status:** Draft from the key decoupling work. Step 9 finishes it, including the secrets file. Every stage marked **Approval** waits for the user.

**Rule:** a small sample goes first and is inspected before the rest. Rollback is tested before anything touches production.

## What moves and what stays

| Data | Before | After | On rollback |
|---|---|---|---|
| `library.db`, `metadata/`, `previews/`, `scrapes/`, `logs/`, cookies | `../scrapepage` | Copied to `~/DESYNC_Data` | The originals were never changed |
| NAS vault | Shared | Shared | **Not undone.** Files the new app added stay there |
| Stash export folder | Shared | Shared | **Not undone** |
| External libraries | Read in place | Read in place | Nothing to undo |

The old app and its data stay in place until the user accepts the result.

## Stages

Commands use `uv run python scripts/cutover.py`. Reports are written to the stage folder as `report.json`.

### 1. Preflight: read-only, safe at any time

```bash
uv run python scripts/cutover.py preflight --source ../scrapepage/library.db
```

**Stop if** `passed` is false. It finds broken references, repeated library paths, downloads with no file row, and models with no source. Any of these would stop or damage the migration.

Last run, 2026-10-31: passed, with 4,893 models and 496 downloaded files.

### 2. Sample: a small set on a throwaway copy

```bash
uv run python scripts/cutover.py sample --source ../scrapepage/library.db --workdir ~/DESYNC_Rehearsal
```

Takes 25 models, starting with every awkward kind: stash members, hashed and unhashed downloads, archives, linked library models, tombstones, bonus models, models with no files, and unfinished jobs. It then migrates them, compares every value, and runs the real app on the result with workers off and outside network blocked.

**Stop and look.** Read `sample/report.json`, then open the app on the sample and check a few models by eye:

```bash
DESYNC_ALLOWED_ROOTS=~/DESYNC_Rehearsal/sample uv run python scripts/baseline/old_app_server.py --app-root ~/DESYNC_Rehearsal/sample/old_app --code new --port 8099
```

**Approval** to continue.

### 3. Full: the whole database on a throwaway copy

```bash
uv run python scripts/cutover.py full --source ../scrapepage/library.db --workdir ~/DESYNC_Rehearsal
```

Same checks with every row. **Stop if** `passed` is false.

### 4. Rollback rehearsal: prove it before it is needed

Add a row to the full copy through the app or Python, then:

```bash
uv run python scripts/cutover.py rollback --data-dir ~/DESYNC_Rehearsal/full/old_app --backup BACKUP --port 8099
uv run python scripts/cutover.py rollback --data-dir ~/DESYNC_Rehearsal/full/old_app --backup BACKUP --port 8099 --confirm
uv run python scripts/cutover.py verify ~/DESYNC_Rehearsal/full/before.db ~/DESYNC_Rehearsal/full/old_app/library.db --rows-only
```

The first command is a dry run. Expect the added row in `dropped`, and `passed: true` from `verify`.

### 5. Production cutover: **Approval**

1. Stop the old app.
2. Take a verified backup of the old `library.db`, and confirm the row counts.
3. Copy production data to `~/DESYNC_Data`. The originals stay unchanged.
4. Run `uv run python -m desync_archival.dbtool migrate --confirm-production` with `APP_ENV=production` on the copy.
5. Run `verify` between the backup and the migrated copy.
6. Set `DESYNC_ALLOWED_ROOTS` to the data folder, NAS vault, staging, stash export folder and every external library.
7. Start the new app. Check the catalog, a stash, and a model on each storage tier.

### 6. After cutover

Rescan Loot Studios, because its totals are blank from the Step 3 indexer bug. Confirm the scheduled backup runs, restore the first one to a new path, and do a real scrape, ingest, download and stash export before sign-off.

## Rollback

### During stages 1 to 4

Nothing to roll back. Everything is a copy. Delete the work folder.

### New app on the new schema, after a bad migration or bad data

```bash
# Stop the app first. The tool refuses if the port is in use.
uv run python scripts/cutover.py rollback --data-dir ~/DESYNC_Data --backup BACKUP
uv run python scripts/cutover.py rollback --data-dir ~/DESYNC_Data --backup BACKUP --confirm
```

The current database moves to `rolled_back_<time>/` and is kept, with `rollback_report.json` listing every model and file the restore dropped. A failed restore puts the database back. Note that the app on current code refuses to start on an older schema, so go back to the matching commit or back to the old app.

### Back to the old app

1. Stop the new app.
2. Run `rollback` above if the new app added anything, to get the report of what it added.
3. Start the old app from `../scrapepage`. Its data was never changed.
4. **Do not run the old app's delete-orphans.** Files the new app added to the NAS are unknown to the old database, so it would treat them as orphans. Run a reconcile and review the files in `rollback_report.json` first.

## Things that are easy to get wrong

- **New rows get IDs above 2,000,000,000.** That no longer means external. Use `source`.
- **The `sqlite3` command-line tool cannot write to `models` or `model_tags`.** It refuses to run the search triggers. Reading is fine. Write through the app or Python.
- **Keep the backup from before the migration.** The tool does not need the network or the old app to restore it.
- **Unlinked libraries are not offline libraries.** Unlinking flags contents inactive. An offline mount changes nothing.
