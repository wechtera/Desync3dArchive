"""Raw SQL for services/scheduler.py."""

SELECT_TASK = "SELECT status, result_summary_json FROM pipeline_tasks WHERE id = ?;"
SELECT_EXTERNAL_KEYS = "SELECT external_key FROM collections WHERE source = 'mmf';"
