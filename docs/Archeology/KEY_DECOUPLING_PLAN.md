# Key decoupling plan

**Status:** Proposed, nothing implemented. Runs after Step 8 merges and before Step 9.
**Branch:** `migration/08b-keys`.
**Goal:** no source of models, present or future, can collide with another. MMF IDs stop being keys and become data.

## Rule for this work

The primary key of a table is an auto-incrementing integer with no outside meaning, and uniqueness belongs to that column alone. Business values such as `external_key`, a tag or a path get plain indexes and are never `UNIQUE` or part of a primary key. Duplicates are blocked by database triggers, covered in phase 5.

## What was found

All numbers come from the sanitized copy, which has the same row counts as production.

### The collision is real today

This was reproduced on a throwaway copy, using the app's own `unlink_library` and `_alloc_model_id`:

1. A stash holds an item from an external library.
2. The library is unlinked. Its 25 models are removed, and the stash row is left behind.
3. The next external model is allocated `2000000000`, the same ID the removed model had.
4. The stash now points at an unrelated model. `previews/2000000000/cover.jpg` is also still on disk, so the new model can show the old cover.

Three causes:

- `_alloc_model_id` uses `MAX(id) + 1`, so freed IDs come back ([external_indexer.py:69](../../src/desync_archival/services/external_indexer.py#L69)).
- `ExternalLibraryIndexer._get_db` never turns on `foreign_keys`, so cascades don't run ([external_indexer.py:63](../../src/desync_archival/services/external_indexer.py#L63)). Only `services/db.py` and `stash_service.py` turn it on.
- `unlink_library` deletes from a fixed list of tables and misses `stash_items` ([external_indexer.py:464](../../src/desync_archival/services/external_indexer.py#L464)).

### Current keys

| Table | Rows | Primary key today | Where the value comes from | Other uniqueness |
|---|---:|---|---|---|
| `models` | 4,893 | `id` | MMF ID, or 2,000,000,000 and up for external | |
| `model_files` | 23,572 | `file_id` | MMF file ID, or 2,000,000,000 and up | |
| `downloaded_files` | 496 | `file_id` | Copy of `model_files.file_id`, with no foreign key to it | |
| `model_tags` | 73,545 | `(model_id, tag)` | Composite | |
| `releases` | 587 | `id` TEXT | `designer_month`, such as `362414_2021-05` | |
| `app_settings` | | `key` TEXT | Setting name | |
| `download_jobs` | 103 | `id`, increment | | `model_id` UNIQUE |
| `external_libraries` | | `id`, increment | | `path` UNIQUE |
| Other 9 tables | | `id`, increment | | |

### How the keys are used

| Use | Where | What must happen |
|---|---|---|
| Foreign keys and joins | `model_tags`, `model_files`, `downloaded_files`, `download_jobs`, `download_audit_log`, `schema_anomalies`, `file_archive_*`, `stash_items` | Nothing, because the values stay the same |
| MMF API calls | [downloader.py:625](../../src/desync_archival/services/downloader.py#L625), [task_manager.py:932](../../src/desync_archival/services/task_manager.py#L932) | Use `external_key` |
| Scrape and ingest input | `model_ids.txt`, `start_metadata_ingest` | These are MMF IDs, so resolve through `external_key` |
| Upserts | [task_manager.py:1026](../../src/desync_archival/services/task_manager.py#L1026), `external_indexer.py:428`, `reconciler.py:637`, `downloader.py:856` | Look up by business value, then insert or update |
| `INSERT OR REPLACE` on `model_files` | [downloader.py:685](../../src/desync_archival/services/downloader.py#L685), [task_manager.py:1058](../../src/desync_archival/services/task_manager.py#L1058) | Replace with lookup and update. REPLACE deletes first, so with foreign keys on it would cascade into archive members and stash items |
| Metadata files | `metadata/model_{id}.json` at `catalog.py:370`, `downloader.py:972`, `task_manager.py:917` | Use `external_key` |
| Vault folders | `[{id}] name` at [downloader.py:202](../../src/desync_archival/services/downloader.py#L202) | Use `external_key`, and keep existing folders as they are |
| Parsing IDs from folder names | [reconciler.py:150](../../src/desync_archival/services/reconciler.py#L150), `:661`, `:676` | The number is an MMF ID, so look up by `external_key` |
| Preview folders | `previews/{id}/` in routes, reconciler, stash exporter, `models.js`, `releases.js` | Keep, because this really is the internal ID |
| "External means 2 billion or more" | [catalog.py:462](../../src/desync_archival/api/routes/catalog.py#L462), [models.js:418](../../web/static/js/views/models.js#L418), `external_indexer.py:482` | Remove and use `source`. **This is the worst trap: every new MMF model would be treated as external** |
| Sorting by ID | `ORDER BY m.id DESC` at [catalog.py:229](../../src/desync_archival/api/routes/catalog.py#L229), `reconciler.py:293`, `queries/catalog.py:44` | Default order stops meaning "newest on MMF" and becomes "newest added". Sort by `published_at` where date is intended |
| API and UI | `/api/models/{model_id}`, `model_ids` in queue and stash bodies, `file_id` routes | Keep. They carry the internal ID, and the API adds `external_key` |

References to `model_id` or `file_id`: `downloader.py` 111, `reconciler.py` 100, `stash_service.py` 77, `external_indexer.py` 54, `task_manager.py` 47, `archive_peeker.py` 46, `stash_exporter.py` 41, `routes/catalog.py` 40, `downloads.js` 37, `queries/catalog.py` 31, `models.js` 30. Phase 1 classifies every one. Most are internal joins and need no change.

### What duplicate detection has to work with

- **Few hashes exist.** 162 of 496 `downloaded_files` rows have a SHA-256, all from MMF. No external file has one. `model_files` has no hash column, so the 23,076 files not yet downloaded have none.
- **Sizes have gaps.** 3,626 MMF files have no size.
- **The same file legitimately belongs to several models.** 43 hashes appear in 2 or 3 models, such as `100mm Base.zip`. 1,814 name and size pairs over 1 MB appear in more than one model, and `__Goblin_Base_Supports.zip` is in 65. Refusing these rows would lose catalog entries.
- **Hashes can't match across sources today.** MMF files are zips, and Loot Studios files are loose STLs. A zip's hash never equals the hash of a file inside it. `file_archive_members` stores name and size but not the CRC-32 that zip headers already carry.
- **Same-name different-content is common in principle,** with generic names like `Base_Supports.zip`, so a name alone proves nothing.

## Decisions

1. **Keep the current ID values and don't renumber.** Existing rows keep their numbers as opaque internal IDs, and `external_key` gets a copy, as you outlined. New rows continue above the highest ID. Renumbering would touch every foreign key and rename preview folders for no functional gain, and that risk lands on production at cutover.
   - **Cost:** for old MMF rows, `id` still equals the MMF ID, so a bug that confuses them stays hidden until a new row arrives. The tests in phase 7 exist to catch that.
2. **`external_key` is TEXT,** because a future source may not use numbers. Local libraries leave it NULL, as you said it is for MMF only.
3. **Identity lookup is `source` + `external_key` for MMF, and `library_id` + `vault_path` for local files.** Models gain `library_id`, which replaces matching by path prefix.
4. **File hash decides exact duplicates, not the name.** You suggested hashes and names combined. The findings above point to separate roles: same hash means same content whatever the name, and a name and size match only marks a candidate for review.
5. **Content duplicates are reported, never blocked or merged.** Only identity duplicates are blocked. Nothing is removed automatically, per the project rule.

## Plan

Each phase is a commit gated on `check.sh`. The contract is compared at every phase, and any API change is reviewed before `--accept`.

### Phase 1: Finish the inventory

Classify every reference above as internal ID, MMF ID, or path, in a table added to this document. Audit every SQLite connection for `foreign_keys`. Confirm what `download_jobs.model_id UNIQUE` is relied on for.

**Exit check:** no reference left unclassified.

### Phase 2: Fix the live bug first

Turn on `foreign_keys` in the indexer, make `unlink_library` handle stash items, and add a test that reproduces the sequence above. This stands alone, so it doesn't wait for the schema work.

### Phase 3: Add the new columns, with no behavior change

Migration 0005:

- `models.external_key`, `models.library_id`
- `model_files.source`, `model_files.external_key`
- Copy the current ID into `external_key` for MMF rows only
- Plain indexes on `(source, external_key)` on both tables, and on `models.library_id`, `models(vault_path)`, `downloaded_files.sha256`

**Exit check:** every MMF row has a key, every external row has NULL, and row counts are unchanged.

### Phase 4: Move the code to the right key

Work through the usage table. Remove every "2 billion" check and the `SYNTHETIC_ID_OFFSET` allocators. Replace the upserts and `INSERT OR REPLACE` with one shared lookup-then-write helper for each table, inside `BEGIN IMMEDIATE`. Add `external_key` to API responses.

### Phase 5: Switch keys to increment columns, and add duplicate triggers

Migration 0006 rebuilds tables, using the migration runner's existing no-transaction mode because foreign keys must be off during a rebuild. FTS triggers and indexes are recreated.

| Table | Change |
|---|---|
| `models` | `id` becomes AUTOINCREMENT |
| `model_files` | `file_id` becomes AUTOINCREMENT |
| `downloaded_files` | New `id` AUTOINCREMENT. `file_id` becomes an indexed foreign key to `model_files`, which it lacks today. 0 orphans exist |
| `model_tags` | New `id` AUTOINCREMENT. `(model_id, tag)` becomes a plain index |
| `releases` | New `id` AUTOINCREMENT. The text value becomes `release_key`, with API output unchanged |
| `download_jobs` | Remove UNIQUE from `model_id` |
| `external_libraries` | Remove UNIQUE from `path` |

- Seed `sqlite_sequence` from live maximums. AUTOINCREMENT never reuses an ID, unlike today's `MAX + 1`, and that's what stops stale previews and stash rows.
- `BEFORE INSERT` and `BEFORE UPDATE` triggers reject a repeat of each business value that used to be unique, and also `(source, external_key)`. These are the "duplication logic in the database" from your list. They cover every code path, including ones that bypass the helper.

**Trade-off:** a unique index would give this protection with less code, and it's what `ON CONFLICT` needs. Under your rule the triggers do that job instead, and the cost is more schema to maintain plus the helper from phase 4. SQLite has a single writer, so with `BEGIN IMMEDIATE` a lookup followed by an insert can't race.

### Phase 6: Duplicate detection

- **Hash coverage:** a resumable background job computes SHA-256 for files that lack one. Reconcile already has `compute_file_sha256`. Zero or missing sizes are ignored.
- **Inside archives:** store each member's CRC-32 from the zip or rar listing, without extracting. This is the only cheap way to match a zip against loose files. Existing archives need a re-peek.
- **File levels:** `exact` is the same SHA-256. `member` is the same CRC-32 and size. `candidate` is the same normalized name and size, with no hash available.
- **Model levels:** `duplicate` means all files match. `overlap` means some do, which is the normal case for shared bases.
- **Output:** a read-only report and a Settings or Reconcile view, with a shared-file allowlist for things like bases and supports. No automatic action.

### Phase 7: Prove it

- Test fixtures where `id` and `external_key` always differ, so any code mixing them fails. Build the dev test data the same way.
- A real ingest and download of a model that gets a new high ID, through vault folder, reconcile, preview and stash export.
- Link, unlink and relink a library, confirming no ID returns.
- Add two libraries at once, and force duplicate inserts to confirm the triggers fire.
- Compare `dbtool backup` and restore on the rebuilt schema, plus row counts and foreign key checks.
- Contract: only additive changes, each reviewed.

### Phase 8: Feed into Step 9

Add migrations 0005 and 0006 to the rehearsal, and update `CUTOVER.md` when it's written. Production changes only at Step 9, and rollback to the old app stays safe because the old database is never modified. The new `~/DESYNC_Data` copy is what changes.

## Risks

| Risk | Mitigation |
|---|---|
| Hidden `id` means MMF ID bug in old rows | Fixtures with differing values, and a real new-model run in phase 7 |
| Rollback after cutover: new rows have IDs the old app doesn't know | Already covered by the Step 9 rollback rule, and to be added to the runbook |
| Rebuild goes wrong on production data | Verified backup first, and the migration runs on the copy |
| Catalog default order changes for new models | Decide in phase 1, and contract comparison shows it |
| Hashing a large NAS library is slow | Background, resumable, and optional per library |
| Turning on foreign keys exposes hidden orphans | Run `foreign_key_check` first. Current orphan checks are clean, but not every table has been checked |

## Open questions

Tracked as Q13 to Q16 in `Questions.md`.
