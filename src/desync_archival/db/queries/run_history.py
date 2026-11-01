"""Raw SQL for services/run_history.py."""

# One task type covers both directions of a reconcile, and only its params tell them apart.
_MODE = "CASE WHEN json_valid(params_json) THEN json_extract(params_json, '$.scan_mode') END"
SELECT_LATEST_RUNS = f"""
    SELECT CASE WHEN task_type != 'vault_reconcile' THEN task_type
                WHEN {_MODE} = 'reverse' THEN 'reverse' ELSE 'forward' END AS card,
           MAX(id) AS task_id, status, progress_label, started_at, completed_at, {_MODE} AS scan_mode
    FROM pipeline_tasks
    WHERE task_type IN ('vault_reconcile', 'archive_peek_backfill', 'catalog_preview_backfill', 'document_update',
                        'staging_scan', 'path_drift_scan', 'duplicate_scan')
    GROUP BY card;
"""
