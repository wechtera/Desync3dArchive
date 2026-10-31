# Linked library scan plan

**Status:** Design in review, nothing built. Do not rerun the Loot Studios scan until this ships.

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

## Open choices

Listed in the session on 2026-10-31: the inactive record, how the image task runs, how a scan resumes, and how new folders are classified.
