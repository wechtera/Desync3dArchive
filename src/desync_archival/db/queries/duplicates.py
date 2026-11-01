"""Raw SQL for services/duplicates.py. Names with braces take format() values."""

SELECT_MODEL_FILES_BY_IS_ACTIVE = """
        SELECT mf.file_id, mf.model_id, mf.filename, COALESCE(df.actual_bytes, mf.size_bytes) AS size_bytes,
               df.sha256, m.name AS model_name, m.source, COALESCE(df.vault_path, df.staging_path) AS disk_path
        FROM model_files mf
        JOIN collections m ON m.id = mf.model_id
        LEFT JOIN downloaded_files df ON df.file_id = mf.file_id
        WHERE m.is_active = 1 AND mf.filename IS NOT NULL AND mf.filename != '';
    """

SELECT_APP_SETTINGS_VALUE_BY_KEY = "SELECT value FROM app_settings WHERE key = 'duplicate_ignore_names';"

SELECT_DECISIONS = "SELECT first_file_id, second_file_id FROM duplicate_decisions;"
INSERT_DECISION = "INSERT INTO duplicate_decisions (first_file_id, second_file_id, decision) VALUES (?, ?, ?);"
COUNT_FILES = "SELECT COUNT(*) FROM model_files WHERE file_id IN (?, ?);"
SELECT_KNOWN_FILE_IDS = "SELECT file_id FROM model_files;"
SELECT_LATEST_RUN = """
    SELECT id, result_summary_json, completed_at FROM pipeline_tasks
    WHERE task_type = 'duplicate_scan' AND status = 'completed' ORDER BY id DESC LIMIT 1;
"""
