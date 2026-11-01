"""Raw SQL for services/duplicates.py. Names with braces take format() values."""

SELECT_MODEL_FILES_BY_IS_ACTIVE = """
        SELECT mf.file_id, mf.model_id, mf.filename, COALESCE(df.actual_bytes, mf.size_bytes) AS size_bytes,
               df.sha256, m.name AS model_name, m.source
        FROM model_files mf
        JOIN collections m ON m.id = mf.model_id
        LEFT JOIN downloaded_files df ON df.file_id = mf.file_id
        WHERE m.is_active = 1 AND mf.filename IS NOT NULL AND mf.filename != '';
    """

SELECT_APP_SETTINGS_VALUE_BY_KEY = "SELECT value FROM app_settings WHERE key = 'duplicate_ignore_names';"
