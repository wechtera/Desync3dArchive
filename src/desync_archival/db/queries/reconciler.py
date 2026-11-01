"""Raw SQL for services/reconciler.py. Names with braces take format() values."""

SELECT_COLLECTIONS_BY_SOURCE = "SELECT external_key, id FROM collections WHERE source = 'mmf' AND external_key IS NOT NULL;"

SELECT_COLLECTIONS_BY_ID = "SELECT name, designer_name FROM collections WHERE id = ?"

SELECT_CATALOG_VAULT_PATHS = """
    SELECT vault_path FROM collections WHERE vault_path IS NOT NULL AND vault_path != ''
    UNION
    SELECT vault_path FROM downloaded_files WHERE vault_path IS NOT NULL AND vault_path != ''
"""

SELECT_MODEL_FILES_BY_MODEL_ID = """
                        SELECT mf.filename, df.cleaned_filename
                        FROM model_files mf
                        LEFT JOIN downloaded_files df ON mf.file_id = df.file_id
                        WHERE mf.model_id = ?;
                    """

SELECT_COLLECTIONS_BY_MODEL_ID = """
                SELECT DISTINCT m.id, m.name, m.designer_name, m.designer_username, m.source,
                                m.release_month, m.vault_path, m.external_key, dj.destination_path
                FROM collections m
                LEFT JOIN download_jobs dj ON dj.id = (SELECT MAX(id) FROM download_jobs WHERE model_id = m.id)
                WHERE m.is_active = 1 AND (dj.status = 'completed' OR m.vault_path IS NOT NULL)
                ORDER BY m.id DESC;
            """

SELECT_MODEL_FILES_BY_MODEL_ID_V2 = """
                        SELECT mf.file_id, mf.filename, mf.size_bytes,
                               df.vault_path, df.local_relative_path, df.sha256
                        FROM model_files mf
                        LEFT JOIN downloaded_files df ON mf.file_id = df.file_id
                        WHERE mf.model_id = ?;
                    """

UPDATE_DOWNLOADED_FILES_SHA_BY_FILE_ID = "UPDATE downloaded_files SET sha256 = ? WHERE file_id = ?;"

INSERT_RECONCILIATION_RUNS = """
                    INSERT INTO reconciliation_runs (
                        status, scan_mode, progress_pct, current_item, total_audited,
                        healthy_count, discrepancy_count, report_json, completed_at
                    ) VALUES (?, ?, 100.0, 'Audit completed.', ?, ?, ?, ?, CURRENT_TIMESTAMP);
                """

SELECT_RECONCILIATION_RUNS = "SELECT * FROM reconciliation_runs ORDER BY id DESC LIMIT 1;"

SELECT_EXTERNAL_LIBRARIES_PATH_BY_IS_ACTIVE = "SELECT path FROM external_libraries WHERE is_active = 1;"

INSERT_DOWNLOAD_AUDIT_LOG = """
                        INSERT INTO download_audit_log (model_id, attempt_number, status, error_detail)
                        VALUES (?, 1, 'failed', 'Reconciliation reset: model marked for re-download');
                    """

SELECT_COLLECTIONS_BY_ID_V2 = "SELECT id, name, designer_name, release_month, external_key, source FROM collections WHERE id = ?"

INSERT_MODEL_FILES = """
                            INSERT INTO model_files (model_id, filename, size_bytes, download_url, created_at, source)
                            VALUES (?, ?, ?, '', CURRENT_TIMESTAMP, ?);
                        """

INSERT_DOWNLOADED_FILES = """
                            INSERT INTO downloaded_files (
                                file_id, model_id, original_filename, cleaned_filename,
                                local_relative_path, expected_bytes, actual_bytes,
                                file_extension, storage_tier, vault_path, sha256, downloaded_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nas', ?, ?, CURRENT_TIMESTAMP);
                        """

UPDATE_COLLECTIONS_VAULT_PATH_BY_ID = "UPDATE collections SET vault_path = ? WHERE id = ?;"

SELECT_DOWNLOAD_JOBS = "SELECT model_id, status FROM download_jobs;"

SELECT_DOWNLOADED_FILES_MODEL_ID_BY_STORAGE_TIER = "SELECT DISTINCT model_id FROM downloaded_files WHERE storage_tier = 'local';"

SELECT_COLLECTIONS_BY_SOURCE_VAULT_PATH = "SELECT id, name, designer_name, designer_username, release_month, vault_path, external_key FROM collections WHERE source = 'mmf' AND vault_path IS NOT NULL;"

UPDATE_DOWNLOAD_JOBS_DESTINATION_PATH_BY_MODEL_ID = "UPDATE download_jobs SET destination_path = ? WHERE model_id = ?;"

SELECT_DOWNLOADED_FILES_BY_MODEL_ID = "SELECT file_id, cleaned_filename FROM downloaded_files WHERE model_id = ?;"

UPDATE_DOWNLOADED_FILES_VAULT_PATH_BY_FILE_ID = """
                                UPDATE downloaded_files SET vault_path = ?, local_relative_path = ? WHERE file_id = ?;
                            """

SELECT_LIVE_JOB_MODELS = """
    SELECT model_id FROM download_jobs
    WHERE model_id IN ({placeholders}) AND status IN ('downloading', 'staging_complete', 'offloading');
"""

SELECT_NOT_MMF_MODELS = "SELECT id FROM collections WHERE id IN ({placeholders}) AND source != 'mmf';"
