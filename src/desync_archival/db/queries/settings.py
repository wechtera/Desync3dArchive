"""Raw SQL for the settings API routes. Names with braces take format() values, never user input."""

PRAGMA_ROW = "PRAGMA busy_timeout = 45000;"

UPDATE_EXTERNAL_LIBRARIES_DISK_BYTES_BY_ID = """
                UPDATE external_libraries
                SET disk_bytes = ?, total_files = ?
                WHERE id = ?;
            """

SELECT_APP_SETTINGS = "SELECT key, value FROM app_settings;"

INSERT_APP_SETTINGS = """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """

SELECT_EXTERNAL_LIBRARIES_BY_IS_ACTIVE = """
        SELECT id, name, path, default_creator, disk_bytes, total_models,
               total_files, last_scanned_at, created_at
        FROM external_libraries
        WHERE is_active = 1
        ORDER BY id ASC;
    """

UPDATE_EXTERNAL_LIBRARIES_NAME_BY_PATH = """
            UPDATE external_libraries SET name = ?, default_creator = ?, is_active = 1 WHERE path = ?;
        """

POST_EXTERNAL_LIBRARY_1B = "INSERT INTO external_libraries (name, path, default_creator) VALUES (?, ?, ?);"

SELECT_EXTERNAL_LIBRARIES_ID_BY_PATH = "SELECT id FROM external_libraries WHERE path = ?;"

PRAGMA_ROW_V2 = "PRAGMA database_list;"

SELECT_EXTERNAL_LIBRARIES_BY_ID = "SELECT id, name, path FROM external_libraries WHERE id = ? AND is_active = 1;"

SELECT_EXTERNAL_LIBRARIES_BY_ID_IS_ACTIVE = "SELECT id, name, path, default_creator FROM external_libraries WHERE id = ? AND is_active = 1;"

SELECT_APP_SETTINGS_V2 = "SELECT key, value FROM app_settings;"

SELECT_SQLITE_MASTER_NAME_BY_TYPE = "SELECT name FROM sqlite_master WHERE type='table';"

COUNT_DOWNLOAD_AUDIT_LOG = "SELECT COUNT(*) FROM download_audit_log;"

COUNT_PIPELINE_TASKS = "SELECT COUNT(*) FROM pipeline_tasks;"

INSERT_APP_SETTINGS_V2 = """
            INSERT INTO app_settings (key, value, updated_at) VALUES ('log_level', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
        """

INSERT_APP_SETTINGS_V3 = """
            INSERT INTO app_settings (key, value, updated_at) VALUES ('log_layers', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
        """

SELECT_SQLITE_MASTER_NAME_BY_TYPE_V2 = "SELECT name FROM sqlite_master WHERE type='table';"

DELETE_DOWNLOAD_AUDIT_LOG_BY_LOGGED_AT = "DELETE FROM download_audit_log WHERE logged_at < datetime('now', ?);"

DELETE_PIPELINE_TASKS_BY_STATUS = "DELETE FROM pipeline_tasks WHERE status IN ('completed', 'failed', 'cancelled') AND completed_at < datetime('now', ?);"

SELECT_APP_SETTINGS_BY_KEY = "SELECT key, value FROM app_settings WHERE key = 'stash_download_path';"

SELECT_PROTECTED_ROOTS = """
    SELECT value FROM app_settings WHERE key = 'download_root_path' AND value != ''
    UNION
    SELECT path FROM external_libraries WHERE is_active = 1
"""

SUM_VAULT_FILES = "SELECT COUNT(*), COALESCE(SUM(actual_bytes), 0) FROM downloaded_files WHERE vault_path LIKE ?;"

COUNT_OLD_AUDIT_ROWS = """
    SELECT (SELECT COUNT(*) FROM download_audit_log WHERE logged_at < datetime('now', ?1))
         + (SELECT COUNT(*) FROM pipeline_tasks WHERE status IN ('completed', 'failed', 'cancelled') AND created_at < datetime('now', ?1));
"""
