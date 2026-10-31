# Catalog hierarchy plan

**Status:** Agreed on 2026-10-31. Levels, search, the archive builder, archive review, the Loot add and Browse Models are on `feature/catalog-hierarchy`, not deployed. The final rename and tag page are still to do. Do not approve the Loot Studios add until stage 3, because `models` rows would mean two things.

## Facts from production, 2026-10-31

- 4,869 MMF rows, of which 79 have been downloaded and peeked. The other 4,790 have no known contents.
- 173 peeked archives, 498 sub-models, 3,883 stored archive entries, 3,215 of them printable. Every peeked archive has its entries stored.
- 335 sub-models have rendered previews.
- 8 tables point at `models`. Stash items point at sub-models and archive entries with `ON DELETE CASCADE`, but the peeker's connection doesn't enforce foreign keys, so its old delete-and-rewrite left those items pointing at nothing. A peek now updates rows in place.

## Decision: rebuild from stored data, don't wipe

The peek stored raw facts (`file_archive_members`) and an interpretation (`file_archive_submodels`). Only the interpretation is wrong. New grouping is computed from stored paths, written beside the old rows and compared. It needs no NAS and can rerun at will. The old rows stay until stashes and previews have moved and the user has accepted the result.

## Decisions, 2026-10-31

- **One model, many collections.** A bundle and the same item sold alone share one model. Files and collections stay separate. Automatic only when printable filenames and exact sizes all match. Anything close is flagged for a human. An unpeeked collection stays a placeholder until peeked, then joins, and its old ID redirects so stashes hold.
- **Archives get human review,** on the same screen as Loot, worst first.
- **Search covers models, model groups and artifacts,** loose or inside an archive, by name. This matters more than perfect grouping: a wrong group can be fixed later, but an artifact that can't be found is lost.
- **Busts join their character's group.** Groups never span collections automatically. Tags do that.

## Stages

1. **Archive resolution.** The resolver works on folders, and on archives it agreed with the peeker 65% of the time. Add file-level resolution, run on the 173 archives without writing, and show the user the differences.
2. **Schema.** A tested migration on a production copy. `collections` keeps every current ID. New `models`, `model_groups` and `artifacts` tables, with print attributes on artifacts. Each unpeeked collection gets one placeholder model named after it, so it can still be browsed. A peek later turns that placeholder into the first real model, so its ID survives. Existing stash items, previews and ID links are carried across.
3. **Loot add.** Review saves a model group on a card. Then preview, review and add through the new tables.
4. **UI.** Browse models, group modal, Collections view, and picking scale and support at stash export.
5. **Retire the old tables.** Only after acceptance, and with separate approval.

## How Loot sits in this until the final rename

Each Loot card keeps its own `models` row, because rescans, the image worker and stash export work per row. It gets a real model under the same ID, and its group is scoped to the library and release. To the user, the release acts as the collection.

Real models get IDs above 1,000,000,000,000. A placeholder shares its collection's ID, and `models` hands out its own IDs, so the two ranges must never meet.
