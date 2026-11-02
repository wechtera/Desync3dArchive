"""Raw SQL for services/task_manager.py. Names with braces take format() values."""

SELECT_APP_SETTINGS_VALUE_BY_KEY = "SELECT value FROM app_settings WHERE key = ?;"

UPDATE_EXTERNAL_LIBRARIES_DISK_BYTES_BY_ID = """
                UPDATE external_libraries
                SET disk_bytes = ?, total_files = ?
                WHERE id = ?;
            """

UPDATE_PIPELINE_TASKS_LOG_OUTPUT_BY_ID = """
                    UPDATE pipeline_tasks
                    SET log_output = COALESCE(log_output, '') || ? || '\n'
                    WHERE id = ?;
                """

UPDATE_PIPELINE_TASKS_PROGRESS_PCT_BY_ID = """
                    UPDATE pipeline_tasks
                    SET progress_pct = ?, progress_label = ?
                    WHERE id = ?;
                """

UPDATE_PIPELINE_TASKS_STATUS_BY_ID = """
                    UPDATE pipeline_tasks
                    SET status = ?,
                        progress_pct = CASE WHEN ? = 'completed' THEN 100.0 ELSE progress_pct END,
                        progress_label = ?,
                        result_summary_json = ?,
                        result_json = ?,
                        artifacts_json = ?,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """

SELECT_PIPELINE_TASKS_BY_STATUS = """
                SELECT * FROM pipeline_tasks
                WHERE status IN ('running', 'pending')
                ORDER BY id DESC LIMIT 1;
            """

SELECT_PIPELINE_TASKS_BY_STATUS_V2 = "SELECT id, task_type FROM pipeline_tasks WHERE status = 'running' ORDER BY id DESC LIMIT 1;"

INSERT_PIPELINE_TASKS = """
                    INSERT INTO pipeline_tasks (task_type, status, progress_pct, progress_label, params_json, log_output)
                    VALUES (?, 'running', 0.0, 'Initializing background worker...', ?, '');
                """

UPDATE_PIPELINE_TASKS_STARTED_AT_BY_ID = "UPDATE pipeline_tasks SET started_at = CURRENT_TIMESTAMP WHERE id = ?;"

UPDATE_PIPELINE_TASKS_STATUS_BY_ID_V2 = """
                        UPDATE pipeline_tasks
                        SET status = 'cancelled',
                            progress_label = 'Cancelled by operator',
                            completed_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND status = 'running';
                    """

SELECT_PIPELINE_TASKS_BY_STATUS_V3 = """
                SELECT * FROM pipeline_tasks
                WHERE status = 'running'
                ORDER BY id DESC LIMIT 1;
            """

# Status only, because every open tab reads this. Logs and reports come from SELECT_PIPELINE_TASK_BY_ID.
SELECT_PIPELINE_TASKS_V2 = """
                SELECT id, task_type, status, progress_pct, progress_label,
                       created_at, started_at, completed_at
                FROM pipeline_tasks
                ORDER BY id DESC LIMIT 10;
            """

UPDATE_PIPELINE_TASKS_STATUS_BY_STATUS = """
                        UPDATE pipeline_tasks
                        SET status = 'cancelled',
                            progress_label = 'Cancelled by operator',
                            completed_at = CURRENT_TIMESTAMP
                        WHERE status = 'running';
                    """

UPDATE_PIPELINE_TASKS_STATUS_BY_STATUS_V2 = """
                        UPDATE pipeline_tasks
                        SET status = 'cancelled',
                            progress_label = 'Terminated by operator (force kill)',
                            completed_at = CURRENT_TIMESTAMP
                        WHERE status = 'running';
                    """

COUNT_PIPELINE_TASKS_BY_STATUS = "SELECT count(*) FROM pipeline_tasks WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted');"

DELETE_PIPELINE_TASKS_BY_STATUS = """
                    DELETE FROM pipeline_tasks
                    WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted');
                """

COUNT_COLLECTIONS_BY_SOURCE = "SELECT count(*) FROM collections WHERE source = 'mmf';"

SELECT_PIPELINE_TASKS_ID_BY_STATUS = "SELECT id FROM pipeline_tasks WHERE status = 'running'"

SELECT_COLLECTIONS_EXTERNAL_KEY_BY_SOURCE = "SELECT external_key FROM collections WHERE source = 'mmf' AND name IS NOT NULL AND name NOT LIKE '[Unavailable Model%';"

INSERT_PIPELINE_TASKS_V2 = """
                        INSERT INTO pipeline_tasks (
                            task_type, status, progress_pct, progress_label,
                            log_output, started_at, completed_at, result_summary_json, artifacts_json
                        ) VALUES (
                            'metadata_ingest', 'completed', 100.0, 'Database is up to date (0 models to process)',
                            ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, '{}'
                        );
                    """

INSERT_PIPELINE_TASKS_V3 = """
                    INSERT INTO pipeline_tasks (task_type, status, progress_pct, progress_label, params_json, log_output)
                    VALUES ('metadata_ingest', 'running', 0.0, 'Initializing metadata ingestion...', ?, '');
                """

UPDATE_COLLECTIONS_ARCHIVE_DOWNLOAD_URL_BY_ID = "UPDATE collections SET archive_download_url = ? WHERE id = ?;"

DELETE_COLLECTION_TAGS_BY_MODEL_ID = "DELETE FROM collection_tags WHERE model_id = ?;"

INSERT_COLLECTION_TAGS = "INSERT OR IGNORE INTO collection_tags (model_id, tag) VALUES (?, ?);"

INSERT_SCHEMA_ANOMALIES = """
                                    INSERT INTO schema_anomalies (model_id, anomaly_type, details)
                                    VALUES (?, ?, ?);
                                """

INSERT_FINISHED_TASK = """
    INSERT INTO pipeline_tasks (task_type, status, progress_pct, progress_label, log_output, started_at, completed_at, result_summary_json)
    VALUES (?, ?, 100.0, ?, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?);
"""

SELECT_PIPELINE_TASK_BY_ID = "SELECT * FROM pipeline_tasks WHERE id = ?;"

UPDATE_RUNNING_TASK_LABEL = "UPDATE pipeline_tasks SET progress_label = ? WHERE id = ? AND status = 'running';"

UPDATE_RUNNING_TASK_RESULT = """
    UPDATE pipeline_tasks
    SET status = ?, progress_pct = CASE WHEN ? = 'completed' THEN 100.0 ELSE progress_pct END,
        progress_label = ?, completed_at = CURRENT_TIMESTAMP
    WHERE id = ? AND status = 'running';
"""
