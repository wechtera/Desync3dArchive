-- The picture a model shows, taken from beside its files in the archive.
ALTER TABLE catalog_models ADD COLUMN image_path TEXT;

-- When an archive was last peeked, so a review can say which peek it's looking at.
ALTER TABLE model_files ADD COLUMN peeked_at TIMESTAMP;
UPDATE model_files SET peeked_at = (
    SELECT MAX(created_at) FROM file_archive_submodels s WHERE s.file_id = model_files.file_id
);
