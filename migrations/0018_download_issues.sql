-- MMF sometimes lists the wrong size for a file, and serves the same bytes every time. A download
-- now keeps those after a second try agrees, and carries on past any file that fails. Each such
-- file waits here until it's kept, replaced by an upload, or downloads cleanly later.
-- One open row per file. Resolved rows stay as the record of what happened.
CREATE TABLE download_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES model_files(file_id) ON DELETE CASCADE,
    model_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('size_differs', 'not_downloaded')),
    listed_bytes INTEGER,
    received_bytes INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    resolution TEXT CHECK (resolution IN ('kept', 'uploaded', 'downloaded'))
);
CREATE INDEX idx_download_issues_file ON download_issues(file_id);
CREATE INDEX idx_download_issues_model ON download_issues(model_id);
-- A trigger, not UNIQUE, so primary keys stay the only uniqueness in the schema (0007).
CREATE TRIGGER no_second_open_issue_insert BEFORE INSERT ON download_issues
WHEN NEW.resolved_at IS NULL AND EXISTS (
    SELECT 1 FROM download_issues WHERE file_id = NEW.file_id AND resolved_at IS NULL
)
BEGIN
    SELECT RAISE(ABORT, 'file already has an open download issue');
END;
CREATE TRIGGER no_second_open_issue_update BEFORE UPDATE OF file_id, resolved_at ON download_issues
WHEN NEW.resolved_at IS NULL AND EXISTS (
    SELECT 1 FROM download_issues WHERE file_id = NEW.file_id AND resolved_at IS NULL AND id != OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'file already has an open download issue');
END;

-- Set when the owner keeps or uploads a file. From then on its real size outranks MMF's listing,
-- so a metadata refresh can't bring the wrong size back.
ALTER TABLE model_files ADD COLUMN size_confirmed_at TIMESTAMP;
