"""Raw SQL for services/jobs.py. Names with braces take format() values."""

UPDATE_PIPELINE_TASKS_STATUS_BY_STATUS = """
            UPDATE pipeline_tasks
            SET status = ?, progress_label = 'Interrupted by an app restart', completed_at = CURRENT_TIMESTAMP
            WHERE status = ?;
            """

UPDATE_DOWNLOAD_JOBS_STATUS_BY_STATUS = "UPDATE download_jobs SET status = 'pending', started_at = NULL WHERE status = 'downloading';"

UPDATE_DOWNLOAD_JOBS_STATUS_BY_STATUS_V2 = "UPDATE download_jobs SET status = 'staging_complete' WHERE status = 'offloading';"

SELECT_PIPELINE_TASKS_BY_STATUS = "SELECT id, task_type FROM pipeline_tasks WHERE status = 'running' ORDER BY id DESC LIMIT 1;"

COUNT_LIVE_DOWNLOAD_JOBS = "SELECT COUNT(*) FROM download_jobs WHERE status IN ('downloading', 'staging_complete', 'offloading');"
