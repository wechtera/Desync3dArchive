-- Every destructive route records here, for dry runs and real runs alike.
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    request_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_log_action ON audit_log(action);
