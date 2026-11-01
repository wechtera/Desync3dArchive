"""Raw SQL for services/health.py."""

SELECT_LONG_RUNNING = """
    SELECT id, task_type FROM pipeline_tasks
    WHERE status = 'running' AND started_at < datetime('now', ?);
"""
SELECT_TASK_METRICS = """
    SELECT task_type, COUNT(*) AS runs, SUM(status = 'failed') AS failed,
           ROUND(AVG((julianday(completed_at) - julianday(started_at)) * 86400)) AS average_seconds,
           MAX(completed_at) AS last_run
    FROM pipeline_tasks
    WHERE completed_at >= datetime('now', '-30 days')
    GROUP BY task_type ORDER BY task_type;
"""
SELECT_TASK_STATUS = "SELECT status, progress_label FROM pipeline_tasks WHERE id = ?;"
