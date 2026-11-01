"""Raw SQL for the downloads API routes. Names with braces take format() values, never user input."""

COUNT_DOWNLOAD_JOBS = "SELECT COUNT(*) FROM download_jobs dj {where_clause};"

SELECT_DOWNLOAD_JOBS = """
        SELECT dj.*,
               m.name AS model_name,
               COALESCE(NULLIF(m.designer_name, ''), NULLIF(m.designer_username, ''), 'Unknown') AS creator_name,
               m.designer_name
        FROM download_jobs dj
        LEFT JOIN collections m ON dj.model_id = m.id
        {where_clause}
        {order_by}
        LIMIT ? OFFSET ?;
    """

SELECT_COLLECTIONS_ID_BY_DESIGNER_NAME = "SELECT id FROM collections WHERE designer_name = ? AND release_month = ? AND is_active = 1;"

COUNT_MODEL_FILES_BY_MODEL_ID = "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM model_files WHERE model_id = ?;"

UPDATE_DOWNLOAD_JOBS_STATUS_BY_MODEL_ID = """
                UPDATE download_jobs
                SET status = 'pending', total_files = ?, total_bytes = ?,
                    enqueued_at = CURRENT_TIMESTAMP, error_message = NULL
                WHERE model_id = ?;
            """

INSERT_DOWNLOAD_JOBS = """
                INSERT INTO download_jobs (model_id, status, total_files, total_bytes, enqueued_at)
                VALUES (?, 'pending', ?, ?, CURRENT_TIMESTAMP);
            """

DELETE_DOWNLOAD_JOBS_BY_STATUS = "DELETE FROM download_jobs WHERE status IN ('pending', 'downloading', 'failed');"

DELETE_DOWNLOAD_JOBS_BY_STATUS_V2 = "DELETE FROM download_jobs WHERE status = ?;"

UPDATE_DOWNLOAD_JOBS_STATUS_BY_STATUS = """
                UPDATE download_jobs
                SET status = 'pending', error_message = NULL, enqueued_at = CURRENT_TIMESTAMP
                WHERE status = 'failed';
            """

DELETE_DOWNLOAD_JOBS_BY_STATUS_V3 = "DELETE FROM download_jobs WHERE status IN ('pending', 'downloading', 'failed');"
