"""Raw SQL for services/identity.py. Names with braces take format() values."""

SELECT_COLLECTIONS_ID_BY_SOURCE = "SELECT id FROM collections WHERE source = ? AND external_key = ?;"

SELECT_COLLECTIONS_EXTERNAL_KEY_BY_ID = "SELECT external_key FROM collections WHERE id = ? AND source = ?;"

UPDATE_MODEL_FILES_MODEL_ID_BY_SOURCE = """
            UPDATE model_files SET model_id = ?, filename = ?, size_bytes = ?, download_url = ?, created_at = ?
            WHERE source = ? AND external_key = ?;
        """

INSERT_MODEL_FILES = """
                INSERT INTO model_files (model_id, filename, size_bytes, download_url, created_at, source, external_key)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """

SELECT_MODEL_FILES_BY_MODEL_ID = "SELECT file_id, external_key FROM model_files WHERE model_id = ?;"

SELECT_DOWNLOADED_FILES_1_BY_FILE_ID = "SELECT 1 FROM downloaded_files WHERE file_id = ?;"

DELETE_MODEL_FILES_BY_FILE_ID = "DELETE FROM model_files WHERE file_id = ?;"
