"""Raw SQL for services/stash_exporter.py. Names with braces take format() values."""

SELECT_APP_SETTINGS_VALUE_BY_KEY = "SELECT value FROM app_settings WHERE key = ?;"

SELECT_STASHES_BY_ID = "SELECT id, name, description FROM stashes WHERE id = ?;"

SELECT_STASH_ITEMS_BY_STASH_ID = "SELECT * FROM stash_items WHERE stash_id = ? ORDER BY id ASC;"

SELECT_COLLECTIONS_1_BY_ID = "SELECT 1 FROM collections WHERE id = ? AND is_active = 0;"

SELECT_COLLECTIONS_BY_ID = "SELECT id, name, designer_name, vault_path FROM collections WHERE id = ?;"

SELECT_FILE_ARCHIVE_SUBMODELS_BY_ID = "SELECT * FROM file_archive_submodels WHERE id = ?;"

SELECT_DOWNLOADED_FILES_BY_FILE_ID = "SELECT vault_path, staging_path, storage_tier FROM downloaded_files WHERE file_id = ?;"

SELECT_FILE_ARCHIVE_MEMBERS_BY_SUBMODEL_ID = "SELECT * FROM file_archive_members WHERE submodel_id = ?;"

SELECT_FILE_ARCHIVE_MEMBERS_BY_ID = "SELECT * FROM file_archive_members WHERE id = ?;"

SELECT_FILE_ARCHIVE_SUBMODELS_BY_ID_V2 = "SELECT name, preview_image_path, model_id FROM file_archive_submodels WHERE id = ?;"

SELECT_DOWNLOADED_FILES_BY_FILE_ID_V2 = "SELECT * FROM downloaded_files WHERE file_id = ?;"

SELECT_DOWNLOADED_FILES_BY_MODEL_ID = "SELECT * FROM downloaded_files WHERE model_id = ?;"

SELECT_SUBMODEL_BYTES = "SELECT total_bytes FROM file_archive_submodels WHERE id = ?;"

SELECT_MEMBER_BYTES = "SELECT size_bytes FROM file_archive_members WHERE id = ?;"

# Peeked archives list what they unpack to. Otherwise the archive's own size is all that's known.
SELECT_FILE_UNPACKED_BYTES = """
    SELECT COALESCE(
        (SELECT NULLIF(SUM(size_bytes), 0) FROM file_archive_members WHERE file_id = ?1),
        (SELECT actual_bytes FROM downloaded_files WHERE file_id = ?1)
    );
"""

SELECT_MODEL_UNPACKED_BYTES = """
    SELECT SUM(COALESCE(
        (SELECT NULLIF(SUM(m.size_bytes), 0) FROM file_archive_members m WHERE m.file_id = d.file_id),
        d.actual_bytes
    ))
    FROM downloaded_files d WHERE d.model_id = ?;
"""
