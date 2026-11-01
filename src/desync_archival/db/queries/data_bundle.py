"""Raw SQL for services/data_bundle.py. Names with braces take format() values."""

SELECT_EXTERNAL_LIBRARIES_PATH = "SELECT path FROM external_libraries"
SELECT_DELETED_FILE_SNAPSHOTS = "SELECT id, snapshot_json FROM deleted_files WHERE snapshot_json IS NOT NULL AND snapshot_json != '';"
UPDATE_DELETED_FILE_SNAPSHOT = "UPDATE deleted_files SET snapshot_json = ? WHERE id = ?;"
