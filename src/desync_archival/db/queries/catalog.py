"""Raw SQL for the catalog API routes. Names with braces take format() values, never user input."""

GET_METRICS_1 = """
        SELECT 
            (SELECT COUNT(*) FROM models WHERE {VALID_MODEL_RAW}) AS total_models,
            (SELECT COUNT(*) FROM models WHERE is_bonus = 1 AND {VALID_MODEL_RAW}) AS total_bonus_models,
            (SELECT COUNT(DISTINCT designer_name) FROM models WHERE {VALID_MODEL_RAW}) AS total_creators,
            (SELECT COUNT(*) FROM model_files mf JOIN models m ON mf.model_id = m.id WHERE {VALID_MODEL_M}) AS total_files,
            (SELECT COALESCE(SUM(size_bytes), 0) FROM model_files mf JOIN models m ON mf.model_id = m.id WHERE {VALID_MODEL_M}) AS total_library_size_bytes,
            (SELECT COALESCE(SUM(df.actual_bytes), 0) FROM downloaded_files df JOIN models m ON df.model_id = m.id WHERE {VALID_MODEL_M}) AS total_downloaded_bytes,
            (SELECT COUNT(*) FROM download_jobs j JOIN models m ON j.model_id = m.id WHERE j.status = 'completed' AND {VALID_MODEL_M}) AS total_downloaded_models,
            (SELECT COUNT(*) FROM download_jobs j JOIN models m ON j.model_id = m.id WHERE j.downloaded_files > 0 AND j.downloaded_files < j.total_files AND {VALID_MODEL_M}) AS total_partial_models,
            (SELECT COUNT(*) FROM models WHERE pre_supported = 1 AND {VALID_MODEL_RAW}) AS supported_models,
            (SELECT COUNT(*) FROM releases WHERE {VALID_RELEASE_COND}) AS total_releases,
            (SELECT COUNT(*) FROM releases WHERE is_likely_bundle = 1 AND {VALID_RELEASE_COND}) AS likely_bundles;
    """

GET_CREATORS_1 = """
        SELECT 
            designer_name,
            designer_id,
            COUNT(DISTINCT CASE WHEN is_bonus = 0 THEN m.id END) AS total_models,
            COUNT(DISTINCT CASE WHEN is_bonus = 1 THEN m.id END) AS bonus_models,
            COUNT(DISTINCT m.release_month) AS active_months,
            SUM(CASE WHEN m.pre_supported = 1 THEN 1 ELSE 0 END) AS supported_models,
            MIN(m.release_month) AS first_seen,
            MAX(m.release_month) AS latest_seen
        FROM models m
        WHERE {VALID_MODEL_M}
        GROUP BY designer_name
        ORDER BY total_models DESC;
    """

GET_RELEASES_1 = "SELECT COUNT(*) FROM releases r {where_clause};"

GET_RELEASES_2 = """
        SELECT r.*,
            (SELECT m.id FROM models m 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month 
               AND m.primary_image_url IS NOT NULL 
               AND m.primary_image_url != ''
               AND {VALID_MODEL_M}
             ORDER BY m.id DESC LIMIT 1) AS sample_model_id,
            (SELECT COUNT(*) FROM models m JOIN download_jobs j ON m.id = j.model_id 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month 
               AND j.status = 'completed'
               AND {VALID_MODEL_M}) AS completed_jobs,
            (SELECT COUNT(*) FROM models m JOIN download_jobs j ON m.id = j.model_id 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month 
               AND j.status IN ('pending', 'downloading')
               AND {VALID_MODEL_M}) AS queued_jobs,
            (SELECT COUNT(DISTINCT df.model_id) FROM models m JOIN downloaded_files df ON m.id = df.model_id 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month
               AND {VALID_MODEL_M}) AS downloaded_models
        FROM releases r
        {where_clause}
        ORDER BY r.release_month DESC, r.model_count DESC
        LIMIT ? OFFSET ?;
    """

GET_MODELS_1 = """
        SELECT m.id, m.name, m.designer_name, m.designer_username, m.primary_category, m.release_month,
               m.published_at, m.is_bonus, m.pre_supported, m.primary_image_url, m.url,
               (SELECT COUNT(*) FROM model_files f WHERE f.model_id = m.id) AS file_count,
               (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) AS downloaded_file_count,
               (SELECT GROUP_CONCAT(mt.tag, '||') FROM model_tags mt WHERE mt.model_id = m.id) AS tags_raw,
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
        FROM models m
        LEFT JOIN download_jobs j ON j.id = (SELECT MAX(id) FROM download_jobs WHERE model_id = m.id)
        WHERE {VALID_MODEL_M}
    """

GET_MODELS_2 = """
        SELECT COUNT(*)
        FROM models m
        LEFT JOIN download_jobs j ON j.id = (SELECT MAX(id) FROM download_jobs WHERE model_id = m.id)
        WHERE {VALID_MODEL_M}
    """

GET_MODEL_DETAIL_1 = "SELECT * FROM models WHERE id = ? AND {VALID_MODEL_RAW};"

GET_MODEL_DETAIL_2 = "SELECT tag FROM model_tags WHERE model_id = ? ORDER BY tag ASC;"

GET_MODEL_DETAIL_3 = "SELECT name FROM sqlite_master WHERE type='table' AND name='downloaded_files';"

GET_MODEL_DETAIL_4 = """
                SELECT 
                    f.file_id, f.filename, f.size_bytes, f.download_url, f.created_at,
                    df.storage_tier, df.local_relative_path, df.actual_bytes,
                    CASE WHEN df.file_id IS NOT NULL THEN 1 ELSE 0 END AS is_downloaded
                FROM model_files f
                LEFT JOIN downloaded_files df ON f.file_id = df.file_id
                WHERE f.model_id = ?
                ORDER BY f.size_bytes DESC, f.filename ASC;
            """

GET_MODEL_DETAIL_5 = """
                SELECT file_id, filename, size_bytes, download_url, created_at,
                       NULL AS storage_tier, NULL AS local_relative_path, NULL AS actual_bytes,
                       0 AS is_downloaded
                FROM model_files
                WHERE model_id = ?
                ORDER BY size_bytes DESC, filename ASC;
            """

GET_MODEL_DETAIL_6 = "SELECT name FROM sqlite_master WHERE type='table' AND name='download_jobs';"

GET_MODEL_DETAIL_7 = "SELECT status, downloaded_files, downloaded_bytes, total_files, total_bytes, enqueued_at, completed_at, error_message FROM download_jobs WHERE model_id = ? ORDER BY id DESC LIMIT 1;"

GET_MODEL_FILES_1 = "SELECT id, name, designer_name, url, primary_image_url FROM models WHERE id = ? AND {VALID_MODEL_RAW};"

GET_MODEL_FILES_2 = """
        SELECT 
            f.file_id, f.filename, f.size_bytes, f.download_url, f.created_at,
            df.local_relative_path, df.actual_bytes,
            CASE WHEN df.file_id IS NOT NULL THEN 1 ELSE 0 END AS is_downloaded
        FROM model_files f
        LEFT JOIN downloaded_files df ON f.file_id = df.file_id
        WHERE f.model_id = ?
        ORDER BY f.size_bytes DESC, f.filename ASC;
    """

POST_MODEL_RENDER_VIEW_1 = """
        SELECT id, name, designer_name, vault_path, primary_image_url,
               {source_expr}
        FROM models WHERE id = ?;
    """

POST_MODEL_RENDER_VIEW_2 = """
                SELECT df.vault_path, df.staging_path
                FROM downloaded_files df
                WHERE df.model_id = ?
                  AND (LOWER(df.file_extension) = '.stl' OR LOWER(df.cleaned_filename) LIKE '%.stl')
                ORDER BY df.actual_bytes DESC;
            """

POST_MODEL_RENDER_VIEW_3 = "UPDATE models SET primary_image_url = ? WHERE id = ?;"
