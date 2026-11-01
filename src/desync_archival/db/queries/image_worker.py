"""Raw SQL for services/image_worker.py. Names with braces take format() values."""

UPDATE_APP_SETTINGS_VALUE_BY_KEY = "UPDATE app_settings SET value = ? WHERE key = ?;"

INSERT_APP_SETTINGS = "INSERT INTO app_settings (key, value) VALUES (?, ?);"

SELECT_APP_SETTINGS_VALUE_BY_KEY = "SELECT value FROM app_settings WHERE key = ?;"

UPDATE_LIBRARY_WORK_QUEUE_STATUS_BY_STATUS = "UPDATE library_work_queue SET status = 'pending', updated_at = CURRENT_TIMESTAMP WHERE status = 'failed';"

SELECT_LIBRARY_WORK_QUEUE = "SELECT status, COUNT(*) FROM library_work_queue GROUP BY status;"

SELECT_PIPELINE_TASKS_1_BY_STATUS = "SELECT 1 FROM pipeline_tasks WHERE status = 'running' AND task_type = 'external_library_reconcile' LIMIT 1;"

SELECT_LIBRARY_WORK_QUEUE_BY_STATUS = """
                SELECT q.id, q.kind, q.model_id, q.file_id, m.name, m.vault_path, m.is_active, m.external_key,
                       l.path AS library_path
                FROM library_work_queue q
                JOIN collections m ON m.id = q.model_id
                JOIN external_libraries l ON l.id = m.library_id
                WHERE q.status = 'pending'
                ORDER BY CASE q.kind WHEN 'cover' THEN 0 ELSE 1 END, q.id
                LIMIT 1;
            """

UPDATE_LIBRARY_WORK_QUEUE_STATUS_BY_ID = (
    "UPDATE library_work_queue SET status = ?, attempts = attempts + 1, detail = ?,"
    " updated_at = CURRENT_TIMESTAMP WHERE id = ?;"
)

SELECT_LIBRARY_MODEL_FOLDERS_REL_PATH_BY_MODEL_ID = "SELECT rel_path FROM library_model_folders WHERE model_id = ? AND excluded = 0 ORDER BY id;"

UPDATE_COLLECTIONS_PRIMARY_IMAGE_URL_BY_ID = "UPDATE collections SET primary_image_url = ? WHERE id = ?;"

SELECT_DOWNLOADED_FILES_VAULT_PATH_BY_FILE_ID = "SELECT vault_path FROM downloaded_files WHERE file_id = ?;"
