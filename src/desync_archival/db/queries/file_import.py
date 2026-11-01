"""Raw SQL for services/file_import.py."""

SELECT_VAULT_ROOT = "SELECT value FROM app_settings WHERE key = 'download_root_path';"
SELECT_FILE_BY_HASH = "SELECT model_id FROM downloaded_files WHERE sha256 = ? LIMIT 1;"
INSERT_COLLECTION = """
    INSERT INTO collections (name, designer_name, designer_username, primary_category, release_month, vault_path,
        source, external_key, raw_backup_path, updated_at)
    VALUES (?, ?, ?, 'Imported', ?, ?, 'import', ?, '', CURRENT_TIMESTAMP);
"""
INSERT_MODEL_FILE = """
    INSERT INTO model_files (model_id, filename, size_bytes, download_url, created_at, source, external_key)
    VALUES (?, ?, ?, '', CURRENT_TIMESTAMP, 'import', ?);
"""
INSERT_DOWNLOADED_FILE = """
    INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,
        expected_bytes, actual_bytes, file_extension, storage_tier, vault_path, sha256, downloaded_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nas', ?, ?, CURRENT_TIMESTAMP);
"""
