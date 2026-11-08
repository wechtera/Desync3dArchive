"""Raw SQL for the downloads API routes. Names with braces take format() values, never user input."""

from desync_archival.db.queries import analytics

COUNT_DOWNLOAD_JOBS = "SELECT COUNT(*) FROM download_jobs dj {where_clause};"

SELECT_DOWNLOAD_JOBS = """
        SELECT dj.*,
               m.name AS model_name,
               COALESCE(NULLIF(m.designer_name, ''), NULLIF(m.designer_username, ''), 'Unknown') AS creator_name,
               m.designer_name,
               (SELECT COUNT(*) FROM download_issues i WHERE i.model_id = dj.model_id AND i.resolved_at IS NULL) AS open_issues
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

COUNT_INCOMPLETE_JOBS = "SELECT COUNT(*) FROM download_jobs WHERE status IN ('pending', 'failed');"

COUNT_JOBS_BY_STATUS = "SELECT COUNT(*) FROM download_jobs WHERE status = ?;"

# Completed with issues isn't a stored status. It's a finished job whose model still has a flagged file,
# so every place that counts 'completed' as done keeps working, and it clears itself.
WITH_ISSUES = (
    " AND dj.status = 'completed' AND EXISTS "
    "(SELECT 1 FROM download_issues i WHERE i.model_id = dj.model_id AND i.resolved_at IS NULL)"
)

SELECT_DOWNLOAD_JOB_DETAIL = """
    SELECT dj.id, dj.model_id, dj.status, dj.enqueued_at, dj.completed_at, dj.retry_count, dj.error_message,
           c.name AS model_name, COALESCE(NULLIF(c.designer_name, ''), c.designer_username) AS creator_name
    FROM download_jobs dj LEFT JOIN collections c ON c.id = dj.model_id
    WHERE dj.id = ?;
"""
SELECT_DOWNLOAD_ATTEMPTS = """
    SELECT a.logged_at, a.attempt_number, a.status, a.http_status, a.error_detail, f.filename
    FROM download_audit_log a LEFT JOIN model_files f ON f.file_id = a.file_id
    WHERE a.model_id = ? ORDER BY a.id DESC LIMIT ?;
"""

