"""Raw SQL for services/external_indexer.py. Names with braces take format() values."""

SELECT_COLLECTIONS_ID_BY_LIBRARY_ID = "SELECT id FROM collections WHERE library_id = ?;"

UPDATE_COLLECTIONS_IS_ACTIVE_BY_ID = "UPDATE collections SET is_active = 0 WHERE id = ?;"

UPDATE_EXTERNAL_LIBRARIES_IS_ACTIVE_BY_ID = "UPDATE external_libraries SET is_active = 0 WHERE id = ?;"

SELECT_COLLECTIONS_BY_LIBRARY_ID = "SELECT id, name FROM collections WHERE library_id = ?;"

UPDATE_RELEASES_IS_ACTIVE_BY_RELEASE_KEY = """
            UPDATE releases SET is_active = 0
            WHERE release_key LIKE 'external\\_%' ESCAPE '\\' AND NOT EXISTS (
                SELECT 1 FROM collections m
                WHERE m.source = 'external' AND m.is_active = 1
                  AND m.designer_name = releases.designer_name AND m.release_month = releases.release_month
            );
        """

SELECT_EXTERNAL_LIBRARIES_BY_ID = "SELECT * FROM external_libraries WHERE id = ? AND is_active = 1;"

SELECT_LIBRARY_MODEL_FOLDERS_BY_LIBRARY_ID = """
            SELECT m.external_key, f.rel_path FROM library_model_folders f JOIN collections m ON m.id = f.model_id
            WHERE m.library_id = ? AND m.external_key IS NOT NULL AND f.excluded = 0;
        """

SELECT_LIBRARY_MODEL_FOLDERS_REL_PATH_BY_MODEL_ID = "SELECT rel_path FROM library_model_folders WHERE model_id = ? AND excluded = 0 ORDER BY id;"

SELECT_LIBRARY_MODEL_FOLDERS_REL_PATH_BY_MODEL_ID_EXCLUDED = "SELECT rel_path FROM library_model_folders WHERE model_id = ? AND excluded = 1;"

SELECT_COLLECTIONS_BY_SOURCE = "SELECT id, vault_path FROM collections WHERE source = 'external' AND library_id = ? AND external_key IS NULL;"

UPDATE_COLLECTIONS_EXTERNAL_KEY_BY_ID = "UPDATE collections SET external_key = ? WHERE id = ?;"

SELECT_LIBRARY_SCAN_RUNS_BY_LIBRARY_ID = "SELECT id, status FROM library_scan_runs WHERE library_id = ? AND mode = ? ORDER BY id DESC LIMIT 1;"

UPDATE_LIBRARY_SCAN_RUNS_STATUS_BY_ID = "UPDATE library_scan_runs SET status = 'running', finished_at = NULL WHERE id = ?;"

INSERT_LIBRARY_SCAN_RUNS = "INSERT INTO library_scan_runs (library_id, mode) VALUES (?, ?);"

UPDATE_LIBRARY_SCAN_RUNS_STATUS_BY_ID_V2 = "UPDATE library_scan_runs SET status = ?, report_json = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?;"

COUNT_COLLECTIONS_BY_SOURCE = """
            SELECT COUNT(*) FROM collections
            WHERE source = 'external' AND is_active = 1 AND designer_name = ? AND release_month = ?;
        """

UPDATE_RELEASES_MODEL_COUNT_BY_RELEASE_KEY = "UPDATE releases SET model_count = ?, is_active = 1 WHERE release_key = ?;"

INSERT_RELEASES = """
                INSERT INTO releases (
                    release_key, designer_name, designer_id, release_month,
                    model_count, total_files, total_size_bytes
                ) VALUES (?, ?, 0, ?, ?, 0, 0);
            """

COUNT_COLLECTIONS_BY_LIBRARY_ID = """
            SELECT COUNT(DISTINCT m.id), COUNT(df.file_id), COALESCE(SUM(df.actual_bytes), 0)
            FROM collections m LEFT JOIN downloaded_files df ON df.model_id = m.id
            WHERE m.library_id = ? AND m.is_active = 1;
        """

UPDATE_EXTERNAL_LIBRARIES_TOTAL_MODELS_BY_ID = "UPDATE external_libraries SET total_models = ?, total_files = ?, disk_bytes = ? WHERE id = ?;"

UPDATE_EXTERNAL_LIBRARIES_LAST_SCANNED_AT_BY_ID = "UPDATE external_libraries SET last_scanned_at = CURRENT_TIMESTAMP WHERE id = ?;"

INSERT_COLLECTIONS = """
                    INSERT INTO collections (
                        library_id, name, designer_name, designer_username, primary_category, pre_supported,
                        release_month, vault_path, source, external_key, raw_backup_path, updated_at, last_scan_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'external', ?, '', CURRENT_TIMESTAMP, ?)
                """

UPDATE_COLLECTIONS_VAULT_PATH_BY_ID = """
                        UPDATE collections SET vault_path = ?, pre_supported = ?, is_active = 1, library_id = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?;
                    """

UPDATE_COLLECTIONS_LAST_SCAN_RUN_ID_BY_ID = "UPDATE collections SET last_scan_run_id = ? WHERE id = ?;"

INSERT_LIBRARY_MODEL_FOLDERS = "INSERT INTO library_model_folders (model_id, rel_path) VALUES (?, ?);"

INSERT_LIBRARY_MODEL_FOLDERS_V2 = "INSERT INTO library_model_folders (model_id, rel_path, excluded) VALUES (?, ?, 1);"

INSERT_COLLECTION_TAGS = "INSERT INTO collection_tags (model_id, tag) VALUES (?, ?);"

INSERT_LIBRARY_WORK_QUEUE = "INSERT INTO library_work_queue (kind, model_id) VALUES (?, ?);"

SELECT_MODEL_FILES_BY_MODEL_ID = """
            SELECT mf.file_id, mf.filename, mf.external_key, df.vault_path, df.actual_bytes, df.modified_ns
            FROM model_files mf LEFT JOIN downloaded_files df ON df.file_id = mf.file_id
            WHERE mf.model_id = ?;
        """

UPDATE_MODEL_FILES_EXTERNAL_KEY_BY_FILE_ID = "UPDATE model_files SET external_key = ? WHERE file_id = ?;"

INSERT_MODEL_FILES = """
                    INSERT INTO model_files (model_id, filename, size_bytes, download_url, created_at, source, external_key)
                    VALUES (?, ?, ?, '', CURRENT_TIMESTAMP, 'external', ?);
                """

INSERT_DOWNLOADED_FILES = """
                    INSERT INTO downloaded_files (
                        file_id, model_id, original_filename, cleaned_filename, local_relative_path, expected_bytes,
                        actual_bytes, file_extension, storage_tier, vault_path, modified_ns, downloaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nas', ?, ?, CURRENT_TIMESTAMP);
                """

INSERT_LIBRARY_WORK_QUEUE_V2 = "INSERT INTO library_work_queue (kind, model_id, file_id) VALUES (?, ?, ?);"

UPDATE_MODEL_FILES_SIZE_BYTES_BY_FILE_ID = "UPDATE model_files SET size_bytes = ? WHERE file_id = ?;"

UPDATE_DOWNLOADED_FILES_ACTUAL_BYTES_BY_FILE_ID = "UPDATE downloaded_files SET actual_bytes = ?, expected_bytes = ?, vault_path = ?, modified_ns = ? WHERE file_id = ?;"

UPDATE_LIBRARY_WORK_QUEUE_STATUS_BY_KIND = "UPDATE library_work_queue SET status = 'pending', attempts = 0 WHERE kind = ? AND file_id = ?;"

UPDATE_DOWNLOAD_JOBS_STATUS_BY_MODEL_ID = """
            UPDATE download_jobs
            SET status = 'completed', total_files = ?, downloaded_files = ?, total_bytes = ?, downloaded_bytes = ?,
                destination_path = ?, completed_at = CURRENT_TIMESTAMP
            WHERE model_id = ?;
        """

INSERT_DOWNLOAD_JOBS = """
                INSERT INTO download_jobs (
                    model_id, status, total_files, downloaded_files, total_bytes, downloaded_bytes,
                    destination_path, enqueued_at, completed_at
                ) VALUES (?, 'completed', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            """

SELECT_COLLECTIONS_EXTERNAL_KEY_BY_LIBRARY_ID = "SELECT external_key FROM collections WHERE library_id = ? AND external_key IS NOT NULL;"

DELETE_LIBRARY_CANDIDATES_BY_LIBRARY_ID = "DELETE FROM library_candidates WHERE library_id = ? AND status = ?;"

SELECT_LIBRARY_CANDIDATES_REL_PATH_BY_LIBRARY_ID = "SELECT rel_path FROM library_candidates WHERE library_id = ?;"

UPDATE_LIBRARY_CANDIDATES_STATUS_BY_LIBRARY_ID = "UPDATE library_candidates SET status = ?, folders_json = ? WHERE library_id = ? AND rel_path = ?;"

INSERT_LIBRARY_CANDIDATES = (
    "INSERT INTO library_candidates (library_id, run_id, rel_path, release_name, name, category,"
    " folders_json, exclude_json, status, group_name, tags_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
)

UPDATE_LIBRARY_CANDIDATES_STATUS_BY_LIBRARY_ID_V2 = "UPDATE library_candidates SET status = ? WHERE library_id = ? AND status = ?;"

SELECT_LIBRARY_CANDIDATES_BY_LIBRARY_ID = (
    "SELECT id, rel_path, name, category, folders_json, exclude_json, group_name, tags_json, release_name, relink_id"
    " FROM library_candidates"
    " WHERE library_id = ? AND status = ? ORDER BY rel_path;"
)

UPDATE_LIBRARY_CANDIDATES_STATUS_BY_ID = "UPDATE library_candidates SET status = ? WHERE id = ?;"

SELECT_COLLECTIONS_BY_SOURCE_VAULT_PATH = "SELECT * FROM collections WHERE source = 'external' AND (external_key = ? OR vault_path = ?);"

SELECT_COLLECTIONS_ID_BY_EXTERNAL_KEY = "SELECT id FROM collections WHERE external_key = ?;"

SELECT_COLLECTIONS_BY_LIBRARY_ID_SOURCE = """
                SELECT * FROM collections
                WHERE library_id = ? AND source = 'external' AND external_key IS NOT NULL
                  AND COALESCE(last_scan_run_id, 0) != ?
                ORDER BY external_key;
            """

COUNT_COLLECTIONS_BY_LIBRARY_ID_V2 = "SELECT COUNT(*) FROM collections WHERE library_id = ?;"

SELECT_EXTERNAL_LIBRARIES_BY_ID_V2 = "SELECT id, name, path FROM external_libraries WHERE id = ?;"

SELECT_COLLECTION_BY_ID = "SELECT * FROM collections WHERE id = ?;"
DELETE_MODEL_FOLDERS = "DELETE FROM library_model_folders WHERE model_id = ?;"
