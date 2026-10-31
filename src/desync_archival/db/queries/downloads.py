"""Raw SQL for the downloads API routes. Names with braces take format() values, never user input."""

GET_DOWNLOADS_HISTORY_1 = "SELECT COUNT(*) FROM download_jobs dj {where_clause};"

GET_DOWNLOADS_HISTORY_2 = """
        SELECT dj.*,
               m.name AS model_name,
               COALESCE(NULLIF(m.designer_name, ''), NULLIF(m.designer_username, ''), 'Unknown') AS creator_name,
               m.designer_name
        FROM download_jobs dj
        LEFT JOIN models m ON dj.model_id = m.id
        {where_clause}
        {order_by}
        LIMIT ? OFFSET ?;
    """

POST_DOWNLOADS_QUEUE_1 = "SELECT id FROM models WHERE designer_name = ? AND release_month = ? AND is_active = 1;"

POST_DOWNLOADS_QUEUE_2 = "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM model_files WHERE model_id = ?;"

POST_DOWNLOADS_QUEUE_3 = """
                UPDATE download_jobs
                SET status = 'pending', total_files = ?, total_bytes = ?,
                    enqueued_at = CURRENT_TIMESTAMP, error_message = NULL
                WHERE model_id = ?;
            """

POST_DOWNLOADS_QUEUE_4 = """
                INSERT INTO download_jobs (model_id, status, total_files, total_bytes, enqueued_at)
                VALUES (?, 'pending', ?, ?, CURRENT_TIMESTAMP);
            """

DELETE_DOWNLOADS_QUEUE_1 = "DELETE FROM download_jobs WHERE status IN ('pending', 'downloading', 'failed');"

DELETE_DOWNLOADS_QUEUE_2 = "DELETE FROM download_jobs WHERE status = ?;"

POST_DOWNLOADS_CONTROL_1 = """
                UPDATE download_jobs
                SET status = 'pending', error_message = NULL, enqueued_at = CURRENT_TIMESTAMP
                WHERE status = 'failed';
            """

POST_DOWNLOADS_CONTROL_2 = "DELETE FROM download_jobs WHERE status IN ('pending', 'downloading', 'failed');"
