# Linked library scan plan

**Status:** Built on `feature/library-scan`, passing `check.sh`, not merged or deployed. Production still has the old scan, so do not run a Loot Studios scan there until this ships.

## Why

The scan is one loop that walks the NAS, writes the catalog, copies art, renders with Blender and peeks archives for each model. It cannot stop mid-model or resume, and it has no quick way to find a new folder. A NAS read error can retag a model, a changed mount path would duplicate every model, and a change in folder layout can create overlapping models.

Library facts from 2026-10-31: 77 release folders, 712 second-level folders, 39,238 files, about 848 GB over SMB. Only the first release is indexed, as 25 models and 334 files.

## Decisions from the user, 2026-10-31

1. **Missing folders** flag the model inactive when the mount is healthy. It reactivates with the same ID if the folder returns, and it is never deleted. The user must be able to see these, so a viewable record of tombstoned and inactive items across processes is required.
2. **Moved or renamed folders** are a new model, and the old one goes inactive.
3. **Every model with no image gets a render,** in a separate background task. Inventory is priority 1, images are priority 2.
4. **Scanner tags** are added, never deleted and rewritten.

## Agreed design

- **Inventory pass:** walks the NAS and records models, files, sizes and modified times. It saves after each model, stops at once, and never renders or opens an archive.
- **Image pass:** works from a database queue, one model at a time, and finds art or renders with Blender. Archive peeks run here too. Each model has a status, so failures are not retried forever.
- **Find new folders:** lists only the top two levels. That took 2.7 seconds on 2026-10-31.
- **Full rescan:** compares size and modified time and rewrites only what changed. No hashing.
- **Identity:** the path relative to the library root, stored in `external_key`, so a mount change cannot duplicate models.
- **Mount failure:** the pass stops and changes nothing.
- **Speed:** an index on `downloaded_files.vault_path`, and one connection per model instead of per file.

## Choices made by the user, 2026-10-31

1. **Inactive record:** one log table, `inactive_log`, with a Settings tab. Rows are never deleted, and a returning item is resolved.
2. **Image task:** a separate low-priority worker, `services/image_worker.py`, with pause and resume. It waits while inventory runs or storage is offline, and it resumes after a restart.
3. **Resume:** per model. Each model records the last scan run that finished it.
4. **New folders:** preview, then approve, then add. Known folders are never reclassified.

## As built

- **Migration 0008** adds `inactive_log`, `library_scan_runs`, `library_candidates` and `library_work_queue`, plus `models.last_scan_run_id` and `downloaded_files.modified_ns`. It gives existing linked rows a relative `external_key` and logs the existing tombstones.
- **Modes:** `POST /api/settings/libraries/{id}/scan` takes `mode`: `preview`, `add` or `full`. `full` never creates a model. New folders only arrive through an approved preview.
- **`external_key` now also covers linked rows,** as `<library id>/<relative path>`. This changes the earlier rule that it was for MMF only. `source` still decides what a row is.
- **Instead of an index on `vault_path`,** files load once per model through `model_id`.
- **A guard the user did not ask for:** if more than 20 percent of a library, and at least 10 folders, look missing at once, nothing is flagged and the run ends as `held`. That many at once points at the mount.
- **An empty library root counts as unmounted,** so a mount that lists as empty cannot flag anything.
- **API contract:** 110 of 110. `last_scan_run_id` is removed from model responses.

## Before deploying

1. Merge `feature/library-scan`. Production runs from `migration/09-cutover` in `DESYNC_3D_Archival`, so the checkout must move with it.
2. Stop production, then `scripts/production.sh python -m desync_archival.dbtool migrate --confirm-production`. It backs up first, and the app refuses to start until this runs.
3. Start production, run Preview New Folders, review it with the user, then approve.

## Known gaps

- Models that only hold archives get no cover. They are recorded as `no_source`, because the worker renders loose STLs only.
- A file that stays missing counts as a change on each rescan, so `models_changed` can read high.
