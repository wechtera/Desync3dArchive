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
