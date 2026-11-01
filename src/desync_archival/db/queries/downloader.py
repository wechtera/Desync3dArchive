"""Raw SQL for services/downloader.py. Names with braces take format() values."""

DELETE_DOWNLOADED_FILES_BY_FILE_ID = "DELETE FROM downloaded_files WHERE file_id = ?;"

INSERT_DOWNLOAD_AUDIT_LOG = """
                INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, logged_at)
                VALUES (?, ?, 1, 'adopted', 200, CURRENT_TIMESTAMP);
            """

SELECT_APP_SETTINGS_VALUE_BY_KEY = "SELECT value FROM app_settings WHERE key = ?;"

DELETE_DOWNLOAD_JOBS_BY_STATUS = "DELETE FROM download_jobs WHERE status = ?;"

DELETE_DOWNLOAD_JOBS_BY_STATUS_V2 = "DELETE FROM download_jobs WHERE status NOT IN ('downloading', 'completed');"

UPDATE_APP_SETTINGS_VALUE_BY_KEY = "UPDATE app_settings SET value = '1' WHERE key = 'circuit_breaker_tripped';"

INSERT_DOWNLOAD_AUDIT_LOG_V2 = """
                                        INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, error_detail, logged_at)
                                        VALUES (?, ?, ?, 'circuit_breaker_engaged', ?, ?, CURRENT_TIMESTAMP);
                                    """

INSERT_DOWNLOAD_AUDIT_LOG_V3 = """
                                        INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, error_detail, logged_at)
                                        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                                    """

INSERT_DOWNLOAD_AUDIT_LOG_V4 = """
                                    INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, error_detail, logged_at)
                                    VALUES (?, ?, ?, ?, 200, ?, CURRENT_TIMESTAMP);
                                """

INSERT_DOWNLOAD_AUDIT_LOG_V5 = """
                            INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, logged_at)
                            VALUES (?, ?, ?, 'completed', 200, CURRENT_TIMESTAMP);
                        """

INSERT_DOWNLOAD_AUDIT_LOG_V6 = """
                            INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, error_detail, logged_at)
                            VALUES (?, ?, ?, ?, 500, ?, CURRENT_TIMESTAMP);
                        """

SELECT_APP_SETTINGS = "SELECT key, value FROM app_settings"

SELECT_APP_SETTINGS_VALUE_BY_KEY_V2 = "SELECT value FROM app_settings WHERE key = 'circuit_breaker_tripped'"

INSERT_APP_SETTINGS = """
                INSERT INTO app_settings (key, value, updated_at) VALUES ('circuit_breaker_tripped', '1', CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = CURRENT_TIMESTAMP;
            """

INSERT_APP_SETTINGS_V2 = """
                INSERT INTO app_settings (key, value, updated_at) VALUES ('circuit_breaker_reason', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """

UPDATE_APP_SETTINGS_VALUE_BY_KEY_V2 = "UPDATE app_settings SET value = '0' WHERE key = 'circuit_breaker_tripped';"

UPDATE_APP_SETTINGS_VALUE_BY_KEY_V3 = "UPDATE app_settings SET value = '' WHERE key = 'circuit_breaker_reason';"

UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID = """
                UPDATE download_jobs
                SET status = 'downloading', started_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending';
            """

UPDATE_COLLECTIONS_ARCHIVE_DOWNLOAD_URL_BY_ID = "UPDATE collections SET archive_download_url = ? WHERE id = ?;"

UPDATE_MODEL_FILES_DOWNLOAD_URL_BY_SOURCE = """
                                            UPDATE model_files
                                            SET download_url = ?,
                                                size_bytes = COALESCE(?, size_bytes)
                                            WHERE source = 'mmf' AND external_key = ?;
                                        """

SELECT_MODEL_FILES_BY_MODEL_ID = """
                SELECT mf.file_id, mf.filename, mf.download_url, mf.size_bytes
                FROM model_files mf WHERE mf.model_id = ?;
            """

SELECT_COLLECTIONS_BY_ID = "SELECT name, archive_download_url FROM collections WHERE id = ?;"

INSERT_MODEL_FILES = """
                            INSERT INTO model_files (source, external_key, model_id, filename, size_bytes, download_url, created_at)
                            VALUES (?, ?, ?, ?, 0, ?, CURRENT_TIMESTAMP);
                        """

SELECT_MODEL_FILES_BY_MODEL_ID_V2 = """
                        SELECT mf.file_id, mf.filename, mf.download_url, mf.size_bytes
                        FROM model_files mf WHERE mf.model_id = ?;
                    """

INSERT_DOWNLOAD_AUDIT_LOG_V7 = """
                                    INSERT INTO download_audit_log (model_id, attempt_number, status, error_detail)
                                    VALUES (?, 1, 'warning', ?);
                                """

UPDATE_DOWNLOADED_FILES_STORAGE_TIER_BY_FILE_ID = """
                        UPDATE downloaded_files
                        SET storage_tier = 'local', staging_path = ?, local_relative_path = ?,
                            actual_bytes = ?, sha256 = ?, downloaded_at = CURRENT_TIMESTAMP
                        WHERE file_id = ?;
                    """

INSERT_DOWNLOADED_FILES = """
                        INSERT INTO downloaded_files (
                            file_id, model_id, original_filename, cleaned_filename,
                            local_relative_path, expected_bytes, actual_bytes,
                            file_extension, storage_tier, staging_path, sha256, downloaded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'local', ?, ?, CURRENT_TIMESTAMP);
                    """

UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V2 = """
                    UPDATE download_jobs
                    SET status = 'staging_complete',
                        downloaded_files = ?,
                        downloaded_bytes = ?,
                        total_bytes = ?
                    WHERE id = ?;
                """

UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V3 = """
                UPDATE download_jobs
                SET status = 'failed', retry_count = retry_count + 1, error_message = ?
                WHERE id = ?;
            """

INSERT_DOWNLOAD_AUDIT_LOG_V8 = """
                INSERT INTO download_audit_log (model_id, attempt_number, status, error_detail)
                VALUES (?, 1, 'failed', ?);
            """

UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V4 = "UPDATE download_jobs SET status = 'offloading' WHERE id = ? AND status = 'staging_complete';"

UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V5 = """
                        UPDATE download_jobs SET status = 'staging_complete', error_message = ? WHERE id = ?;
                    """

SELECT_COLLECTIONS_BY_ID_V2 = "SELECT * FROM collections WHERE id = ?"

SELECT_COLLECTION_TAGS_TAG_BY_MODEL_ID = "SELECT tag FROM collection_tags WHERE model_id = ?"

SELECT_DOWNLOADED_FILES_BY_MODEL_ID = """
                SELECT * FROM downloaded_files
                WHERE model_id = ? AND storage_tier = 'local';
            """

UPDATE_DOWNLOADED_FILES_STORAGE_TIER_BY_FILE_ID_V2 = """
                    UPDATE downloaded_files
                    SET storage_tier = 'nas',
                        staging_path = NULL,
                        vault_path = ?,
                        local_relative_path = ?
                    WHERE file_id = ?;
                """

UPDATE_DOWNLOAD_JOBS_STATUS_BY_ID_V6 = """
                    UPDATE download_jobs
                    SET status = 'completed',
                        completed_at = CURRENT_TIMESTAMP,
                        destination_path = ?
                    WHERE id = ?;
                """

UPDATE_COLLECTIONS_VAULT_PATH_BY_ID = """
                    UPDATE collections
                    SET vault_path = ?
                    WHERE id = ?;
                """

INSERT_DOWNLOAD_AUDIT_LOG_V9 = """
                    INSERT INTO download_audit_log (model_id, attempt_number, status, error_detail)
                    VALUES (?, 1, 'completed', ?);
                """

SELECT_APP_SETTINGS_VALUE_BY_KEY_V3 = "SELECT value FROM app_settings WHERE key = 'circuit_breaker_reason'"

SELECT_DOWNLOAD_JOBS = """
                SELECT status, COUNT(*) as c FROM download_jobs GROUP BY status;
            """
