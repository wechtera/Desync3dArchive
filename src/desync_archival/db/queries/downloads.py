"""Raw SQL for the downloads API routes. Names with braces take format() values, never user input."""

from desync_archival.db.queries import analytics

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

# Models a release can still queue. Skipping anything with a live or finished job matters, because
# queuing resets a job's status, and a pack can hold models that are mid-download or already archived.
SELECT_QUEUEABLE_RELEASE_MODELS = f"""
    SELECT m.id FROM collections m
    WHERE {analytics.RELEASE_FILTER} AND {analytics.RELEASE_KEY} = ?
      AND NOT EXISTS (
          SELECT 1 FROM download_jobs j WHERE j.model_id = m.id
          AND j.status IN ('pending', 'downloading', 'staging_complete', 'offloading', 'completed')
      );
"""

# Only waiting jobs. A job that has started owns files on disk that a delete would strand.
DELETE_PENDING_RELEASE_JOBS = f"""
    DELETE FROM download_jobs WHERE status = 'pending' AND model_id IN (
        SELECT m.id FROM collections m WHERE {analytics.RELEASE_FILTER} AND {analytics.RELEASE_KEY} = ?
    );
"""

COUNT_MODEL_FILES_BY_MODEL_ID = "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM model_files WHERE model_id = ?;"

UPDATE_DOWNLOAD_JOBS_STATUS_BY_MODEL_ID = """
                UPDATE download_jobs
                SET status = 'pending', total_files = ?, total_bytes = ?,
                    enqueued_at = CURRENT_TIMESTAMP, error_message = NULL
                WHERE model_id = ? AND status NOT IN ('downloading', 'staging_complete', 'offloading');
            """

INSERT_DOWNLOAD_JOBS = """
                INSERT INTO download_jobs (model_id, status, total_files, total_bytes, enqueued_at)
                VALUES (?, 'pending', ?, ?, CURRENT_TIMESTAMP);
            """

DELETE_DOWNLOAD_JOBS_BY_STATUS = "DELETE FROM download_jobs WHERE status IN ('pending', 'failed');"

DELETE_DOWNLOAD_JOBS_BY_STATUS_V2 = "DELETE FROM download_jobs WHERE status = ?;"

UPDATE_DOWNLOAD_JOBS_STATUS_BY_STATUS = """
                UPDATE download_jobs
                SET status = 'pending', error_message = NULL, enqueued_at = CURRENT_TIMESTAMP
                WHERE status = 'failed';
            """

DELETE_DOWNLOAD_JOBS_BY_STATUS_V3 = "DELETE FROM download_jobs WHERE status IN ('pending', 'failed');"
