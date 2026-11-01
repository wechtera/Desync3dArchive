"""Raw SQL for services/pending_deletion.py."""

SELECT_FILE = """
    SELECT f.file_id, f.filename, f.external_key, COALESCE(d.actual_bytes, f.size_bytes, 0) AS size_bytes, d.sha256,
           d.vault_path, d.staging_path, c.id AS collection_id, c.name AS collection_name, c.source
    FROM model_files f
    JOIN collections c ON c.id = f.model_id
    LEFT JOIN downloaded_files d ON d.file_id = f.file_id
    WHERE f.file_id = ?;
"""
COUNT_STASH_ITEMS = "SELECT COUNT(*) FROM stash_items WHERE file_id = ?;"
SELECT_VAULT_ROOT = "SELECT value FROM app_settings WHERE key = 'download_root_path';"
INSERT_TOMBSTONE = """
    INSERT INTO deleted_files (file_id, collection_id, collection_name, filename, original_path, size_bytes, sha256, external_key)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
"""
UPDATE_PENDING_PATH = "UPDATE deleted_files SET pending_path = ? WHERE id = ?;"
UPDATE_PURGED = "UPDATE deleted_files SET purged_at = CURRENT_TIMESTAMP WHERE id = ?;"
DELETE_MODEL_FILE = "DELETE FROM model_files WHERE file_id = ?;"
SELECT_PENDING = """
    SELECT id, filename, collection_name, original_path, pending_path, size_bytes, deleted_at
    FROM deleted_files WHERE purged_at IS NULL AND pending_path != '' ORDER BY deleted_at;
"""
SUM_PENDING = """
    SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM deleted_files WHERE purged_at IS NULL AND pending_path != '';
"""
SELECT_STASHED_FILE_IDS = "SELECT DISTINCT file_id FROM stash_items WHERE file_id IS NOT NULL;"
