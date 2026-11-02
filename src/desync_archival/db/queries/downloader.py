"""Raw SQL for services/downloader.py. Names with braces take format() values."""

INSERT_DOWNLOAD_AUDIT_LOG = """
                INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, logged_at)
                VALUES (?, ?, 1, 'adopted', 200, CURRENT_TIMESTAMP);
            """

SELECT_APP_SETTINGS_VALUE_BY_KEY = "SELECT value FROM app_settings WHERE key = ?;"

# A job in one of these has a worker on it, or staged files that only its row keeps track of.
LIVE_JOB_STATUSES = ("downloading", "staging_complete", "offloading")
LIVE_STATUSES = str(LIVE_JOB_STATUSES)

DELETE_DOWNLOAD_JOBS_BY_STATUS = f"DELETE FROM download_jobs WHERE status = ? AND status NOT IN {LIVE_STATUSES};"

DELETE_DOWNLOAD_JOBS_BY_STATUS_V2 = f"DELETE FROM download_jobs WHERE status != 'completed' AND status NOT IN {LIVE_STATUSES};"

DELETE_WAITING_JOBS_BY_MODEL = f"DELETE FROM download_jobs WHERE model_id IN ({{marks}}) AND status != 'completed' AND status NOT IN {LIVE_STATUSES};"

UPDATE_APP_SETTINGS_VALUE_BY_KEY = "UPDATE app_settings SET value = '1' WHERE key = 'circuit_breaker_tripped';"

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
                FROM model_files mf WHERE mf.model_id = ? ORDER BY mf.file_id;
            """

SELECT_COLLECTIONS_BY_ID = "SELECT name, archive_download_url FROM collections WHERE id = ?;"

SELECT_MODEL_FILE_LINKS = "SELECT external_key, download_url FROM model_files WHERE model_id = ?;"

INSERT_MODEL_FILES = """
                            INSERT INTO model_files (source, external_key, model_id, filename, size_bytes, download_url, created_at)
                            VALUES (?, ?, ?, ?, 0, ?, CURRENT_TIMESTAMP);
                        """

SELECT_MODEL_FILES_BY_MODEL_ID_V2 = """
                        SELECT mf.file_id, mf.filename, mf.download_url, mf.size_bytes
                        FROM model_files mf WHERE mf.model_id = ? ORDER BY mf.file_id;
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
                        UPDATE download_jobs SET status = 'staging_complete', error_message = ?, retry_count = retry_count + 1 WHERE id = ?;
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

COUNT_MODEL_FILES_FOR_QUEUE = "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM model_files WHERE model_id = ?;"

SELECT_DOWNLOAD_JOB_BY_MODEL = "SELECT 1 FROM download_jobs WHERE model_id = ?;"

# The guard stops a second worker starting on a model that's mid-download, on the same .part file.
UPDATE_DOWNLOAD_JOBS_FOR_QUEUE = f"""
                UPDATE download_jobs
                SET status = 'pending', total_files = ?, total_bytes = ?,
                    enqueued_at = CURRENT_TIMESTAMP, error_message = NULL
                WHERE model_id = ? AND status NOT IN {LIVE_STATUSES};
            """

INSERT_DOWNLOAD_JOBS_FOR_QUEUE = """
                INSERT INTO download_jobs (model_id, status, total_files, total_bytes, enqueued_at)
                VALUES (?, 'pending', ?, ?, CURRENT_TIMESTAMP);
            """
