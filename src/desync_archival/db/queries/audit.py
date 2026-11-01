"""Raw SQL for services/audit.py. Names with braces take format() values."""

INSERT_AUDIT_LOG = "INSERT INTO audit_log (action, dry_run, outcome, detail, request_id) VALUES (?, ?, ?, ?, ?);"
