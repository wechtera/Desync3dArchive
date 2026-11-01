"""Raw SQL for services/locations.py."""

SELECT_PATH_SETTINGS = """
    SELECT key, value FROM app_settings
    WHERE key IN ('download_root_path', 'download_work_path', 'stash_download_path', 'metadata_backup_path');
"""
SELECT_ACTIVE_LIBRARIES = "SELECT name, path FROM external_libraries WHERE is_active = 1 ORDER BY name;"
