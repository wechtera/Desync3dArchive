"""Raw SQL for the settings API routes. Names with braces take format() values, never user input."""

BG_CALCULATE_LIBRARY_TELEMETRY_1 = "PRAGMA busy_timeout = 45000;"

BG_CALCULATE_LIBRARY_TELEMETRY_2 = """
                UPDATE external_libraries
                SET disk_bytes = ?, total_files = ?
                WHERE id = ?;
            """

GET_SETTINGS_1 = "SELECT key, value FROM app_settings;"

POST_SETTINGS_1 = """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """

GET_EXTERNAL_LIBRARIES_1 = """
        SELECT id, name, path, default_creator, disk_bytes, total_models,
               total_files, last_scanned_at, created_at
        FROM external_libraries
        ORDER BY id ASC;
    """

POST_EXTERNAL_LIBRARY_1 = """
            INSERT INTO external_libraries (name, path, default_creator)
            VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name = excluded.name,
                default_creator = excluded.default_creator;
        """

POST_EXTERNAL_LIBRARY_2 = "SELECT id FROM external_libraries WHERE path = ?;"

POST_EXTERNAL_LIBRARY_3 = "PRAGMA database_list;"

DELETE_EXTERNAL_LIBRARY_1 = "SELECT id, name, path FROM external_libraries WHERE id = ?;"

DELETE_EXTERNAL_LIBRARY_2 = "DELETE FROM external_libraries WHERE id = ?;"

POST_EXTERNAL_LIBRARY_SCAN_1 = "SELECT id, name, path, default_creator FROM external_libraries WHERE id = ?;"

GET_DIRECTORY_USAGE_1 = "SELECT key, value FROM app_settings;"

GET_DIRECTORY_USAGE_2 = "SELECT name FROM sqlite_master WHERE type='table';"

GET_DIRECTORY_USAGE_3 = "SELECT COUNT(*) FROM download_audit_log;"

GET_DIRECTORY_USAGE_4 = "SELECT COUNT(*) FROM pipeline_tasks;"

POST_LOGGING_CONFIG_1 = """
            INSERT INTO app_settings (key, value, updated_at) VALUES ('log_level', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
        """

POST_LOGGING_CONFIG_2 = """
            INSERT INTO app_settings (key, value, updated_at) VALUES ('log_layers', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
        """

POST_LOGS_PURGE_1 = "SELECT name FROM sqlite_master WHERE type='table';"

POST_LOGS_PURGE_2 = "DELETE FROM download_audit_log WHERE logged_at < datetime('now', ?);"

POST_LOGS_PURGE_3 = "DELETE FROM pipeline_tasks WHERE status IN ('completed', 'failed', 'cancelled') AND completed_at < datetime('now', ?);"

POST_STASHES_PURGE_1 = "SELECT key, value FROM app_settings WHERE key = 'stash_download_path';"
