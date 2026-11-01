"""Raw SQL for the catalog API routes. Names with braces take format() values, never user input."""

SELECT_COLLECTIONS_COUNT_BY_X = """
        SELECT 
            (SELECT COUNT(*) FROM collections WHERE {VALID_MODEL_RAW}) AS total_models,
            (SELECT COUNT(*) FROM collections WHERE is_bonus = 1 AND {VALID_MODEL_RAW}) AS total_bonus_models,
            (SELECT COUNT(DISTINCT designer_name) FROM collections WHERE {VALID_MODEL_RAW}) AS total_creators,
            (SELECT COUNT(*) FROM model_files mf JOIN collections m ON mf.model_id = m.id WHERE {VALID_MODEL_M}) AS total_files,
            (SELECT COALESCE(SUM(size_bytes), 0) FROM model_files mf JOIN collections m ON mf.model_id = m.id WHERE {VALID_MODEL_M}) AS total_library_size_bytes,
            (SELECT COALESCE(SUM(df.actual_bytes), 0) FROM downloaded_files df JOIN collections m ON df.model_id = m.id WHERE {VALID_MODEL_M}) AS total_downloaded_bytes,
            (SELECT COUNT(*) FROM download_jobs j JOIN collections m ON j.model_id = m.id WHERE j.status = 'completed' AND {VALID_MODEL_M}) AS total_downloaded_models,
            (SELECT COUNT(*) FROM download_jobs j JOIN collections m ON j.model_id = m.id WHERE j.downloaded_files > 0 AND j.downloaded_files < j.total_files AND {VALID_MODEL_M}) AS total_partial_models,
            (SELECT COUNT(*) FROM collections WHERE pre_supported = 1 AND {VALID_MODEL_RAW}) AS supported_models,
            (SELECT COUNT(*) FROM releases WHERE {VALID_RELEASE_COND}) AS total_releases,
            (SELECT COUNT(*) FROM releases WHERE is_likely_bundle = 1 AND {VALID_RELEASE_COND}) AS likely_bundles;
    """

SELECT_COLLECTIONS_BY_X = """
        SELECT 
            designer_name,
            designer_id,
            COUNT(DISTINCT CASE WHEN is_bonus = 0 THEN m.id END) AS total_models,
            COUNT(DISTINCT CASE WHEN is_bonus = 1 THEN m.id END) AS bonus_models,
            COUNT(DISTINCT m.release_month) AS active_months,
            SUM(CASE WHEN m.pre_supported = 1 THEN 1 ELSE 0 END) AS supported_models,
            MIN(m.release_month) AS first_seen,
            MAX(m.release_month) AS latest_seen
        FROM collections m
        WHERE {VALID_MODEL_M}
        GROUP BY designer_name
        ORDER BY total_models DESC;
    """

COUNT_RELEASES = "SELECT COUNT(*) FROM releases r {where_clause};"

SELECT_COLLECTIONS_BY_DESIGNER_NAME = """
        SELECT r.*,
            (SELECT m.id FROM collections m 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month 
               AND m.primary_image_url IS NOT NULL 
               AND m.primary_image_url != ''
               AND {VALID_MODEL_M}
             ORDER BY m.published_at DESC, m.id DESC LIMIT 1) AS sample_model_id,
            (SELECT COUNT(*) FROM collections m JOIN download_jobs j ON m.id = j.model_id 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month 
               AND j.status = 'completed'
               AND {VALID_MODEL_M}) AS completed_jobs,
            (SELECT COUNT(*) FROM collections m JOIN download_jobs j ON m.id = j.model_id 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month 
               AND j.status IN ('pending', 'downloading')
               AND {VALID_MODEL_M}) AS queued_jobs,
            (SELECT COUNT(DISTINCT df.model_id) FROM collections m JOIN downloaded_files df ON m.id = df.model_id 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month
               AND {VALID_MODEL_M}) AS downloaded_models
        FROM releases r
        {where_clause}
        ORDER BY r.release_month DESC, r.model_count DESC
        LIMIT ? OFFSET ?;
    """

SELECT_MODEL_FILES_BY_MODEL_ID = """
        SELECT m.id, m.name, m.designer_name, m.designer_username, m.primary_category, m.release_month,
               m.published_at, m.is_bonus, m.pre_supported, m.primary_image_url, m.url,
               (SELECT COUNT(*) FROM model_files f WHERE f.model_id = m.id) AS file_count,
               (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) AS downloaded_file_count,
               (SELECT GROUP_CONCAT(mt.tag, '||') FROM collection_tags mt WHERE mt.model_id = m.id) AS tags_raw,
               j.status AS job_status,
               CASE 
                 WHEN j.status = 'completed' THEN 'completed'
                 WHEN j.status IN ('pending', 'downloading') THEN 'queued'
                 WHEN j.status = 'failed' THEN 'failed'
                 WHEN (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) > 0 
                      AND (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) < (SELECT COUNT(*) FROM model_files mf WHERE mf.model_id = m.id) THEN 'partial'
                 WHEN (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) >= (SELECT COUNT(*) FROM model_files mf WHERE mf.model_id = m.id) 
                      AND (SELECT COUNT(*) FROM model_files mf WHERE mf.model_id = m.id) > 0 THEN 'completed'
                 ELSE 'not_downloaded'
               END AS download_status
        FROM collections m
        LEFT JOIN download_jobs j ON j.id = (SELECT MAX(id) FROM download_jobs WHERE model_id = m.id)
        WHERE {VALID_MODEL_M}
    """

COUNT_COLLECTIONS_BY_MODEL_ID = """
        SELECT COUNT(*)
        FROM collections m
        LEFT JOIN download_jobs j ON j.id = (SELECT MAX(id) FROM download_jobs WHERE model_id = m.id)
        WHERE {VALID_MODEL_M}
    """

SELECT_COLLECTIONS_BY_ID = "SELECT * FROM collections WHERE id = ? AND {VALID_MODEL_RAW};"

SELECT_COLLECTION_TAGS_TAG_BY_MODEL_ID = "SELECT tag FROM collection_tags WHERE model_id = ? ORDER BY tag ASC;"

SELECT_SQLITE_MASTER_NAME_BY_TYPE = "SELECT name FROM sqlite_master WHERE type='table' AND name='downloaded_files';"

SELECT_MODEL_FILES_BY_MODEL_ID_V2 = """
                SELECT 
                    f.file_id, f.filename, f.size_bytes, f.download_url, f.created_at,
                    df.storage_tier, df.local_relative_path, df.actual_bytes,
                    CASE WHEN df.file_id IS NOT NULL THEN 1 ELSE 0 END AS is_downloaded
                FROM model_files f
                LEFT JOIN downloaded_files df ON f.file_id = df.file_id
                WHERE f.model_id = ?
                ORDER BY f.size_bytes DESC, f.filename ASC;
            """

SELECT_MODEL_FILES_BY_MODEL_ID_V3 = """
                SELECT file_id, filename, size_bytes, download_url, created_at,
                       NULL AS storage_tier, NULL AS local_relative_path, NULL AS actual_bytes,
                       0 AS is_downloaded
                FROM model_files
                WHERE model_id = ?
                ORDER BY size_bytes DESC, filename ASC;
            """

SELECT_SQLITE_MASTER_NAME_BY_TYPE_V2 = "SELECT name FROM sqlite_master WHERE type='table' AND name='download_jobs';"

SELECT_DOWNLOAD_JOBS_BY_MODEL_ID = "SELECT status, downloaded_files, downloaded_bytes, total_files, total_bytes, enqueued_at, completed_at, error_message FROM download_jobs WHERE model_id = ? ORDER BY id DESC LIMIT 1;"

SELECT_COLLECTIONS_BY_ID_V2 = "SELECT id, name, designer_name, url, primary_image_url FROM collections WHERE id = ? AND {VALID_MODEL_RAW};"

SELECT_MODEL_FILES_BY_MODEL_ID_V4 = """
        SELECT 
            f.file_id, f.filename, f.size_bytes, f.download_url, f.created_at,
            df.local_relative_path, df.actual_bytes,
            CASE WHEN df.file_id IS NOT NULL THEN 1 ELSE 0 END AS is_downloaded
        FROM model_files f
        LEFT JOIN downloaded_files df ON f.file_id = df.file_id
        WHERE f.model_id = ?
        ORDER BY f.size_bytes DESC, f.filename ASC;
    """

SELECT_COLLECTIONS_BY_ID_V3 = """
        SELECT id, name, designer_name, vault_path, primary_image_url,
               {source_expr}
        FROM collections WHERE id = ?;
    """

SELECT_DOWNLOADED_FILES_BY_MODEL_ID = """
                SELECT df.vault_path, df.staging_path
                FROM downloaded_files df
                WHERE df.model_id = ?
                  AND (LOWER(df.file_extension) = '.stl' OR LOWER(df.cleaned_filename) LIKE '%.stl')
                ORDER BY df.actual_bytes DESC;
            """

UPDATE_COLLECTIONS_PRIMARY_IMAGE_URL_BY_ID = "UPDATE collections SET primary_image_url = ? WHERE id = ?;"
