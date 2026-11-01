-- Deleting cascades through artifacts and archive contents, so a restore needs those rows back.
-- Older tombstones have no snapshot, and restore falls back to what they do hold.
ALTER TABLE deleted_files ADD COLUMN snapshot_json TEXT;
ALTER TABLE deleted_files ADD COLUMN restored_at TIMESTAMP;
