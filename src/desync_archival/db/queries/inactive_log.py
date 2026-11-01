"""Raw SQL for services/inactive_log.py. Names with braces take format() values."""

INSERT_INACTIVE_LOG = "INSERT INTO inactive_log (item_type, item_id, item_name, process, reason, detail) VALUES (?, ?, ?, ?, ?, ?);"

SELECT_INACTIVE_LOG_BY_RESOLVED_AT = (
    "SELECT process, reason, COUNT(*) FROM inactive_log WHERE resolved_at IS NULL GROUP BY process, reason"
    " ORDER BY process, reason;"
)
