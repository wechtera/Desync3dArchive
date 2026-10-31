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

## Dry run on the real library, 2026-10-31

Read-only, nothing written. The classifier took 73 seconds, and walking every proposed folder took 14 minutes: 37,784 files, about 790 GB, 49 archives.

**Do not deploy or approve yet. The classifier is wrong for 60 of the 77 releases.** Only the 17 `2020-08` to `2021-12` releases have one folder per model. From 2022 on, a release is laid out by scale, then category, then support variant, then model, such as `April 2024/32mm_PhoenixForge/All_PhoenixForge_Construct_32mm/PhoenixForge_Construct_32mm_LYCHEE/BrazenBull_32mm_LYCHEE`. The same character repeats across scales and variants.

- 17 releases collapse into one model each, with up to 1,261 files. Many others become 3 models named after a scale or bust folder.
- Releases named like `April 2022` get no month, because only `2022-04` style names are parsed. They land in `Unassigned`.
- `Loyalty` is empty.

The user chose one model per character across scales and support types, with busts assumed separate.

**Second preview, same day, with character grouping:** 2,426 new, 25 known, 384 seconds. Still not approvable: a name like `No` means the Tribe sets were read as scale layouts, `Bases Bust` repeats across releases, categories such as `NoSuports` leak through, and `Ready To Print` survives in names.

**Open: the classifier is Loot-specific.** The user wants a separate model resolver that does not depend on one library's layout or on where the files came from, with better pattern detection, possibly ML. Do not extend the Loot rules further until that is decided.

**The resolver must also cover archive peeks.** `ArchivePeeker` already resolves MMF archives into submodels, 498 across 173 archives in production, with its own rules: creator-specific strings in `_derive_submodel_identity` and `_segment_image_driven`, such as Comet Lord, Jurakin and `Mummy Base`, plus `STUDIO_ALIASES`. It also uses renders as evidence, so the resolver's input needs images as well as files. The result only differs in where it lands: models for a library, submodels for an archive. Existing submodels must not change silently: compare old and new output before switching.

## Before deploying

1. Merge `feature/library-scan`. Production runs from `migration/09-cutover` in `DESYNC_3D_Archival`, so the checkout must move with it.
2. Stop production, then `scripts/production.sh python -m desync_archival.dbtool migrate --confirm-production`. It backs up first, and the app refuses to start until this runs.
3. Start production, run Preview New Folders, review it with the user, then approve.

## Known gaps

- Models that only hold archives get no cover. They are recorded as `no_source`, because the worker renders loose STLs only.
- A file that stays missing counts as a change on each rescan, so `models_changed` can read high.
