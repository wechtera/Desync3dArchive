-- A pair of files someone has looked at, so later duplicate checks stop raising it.
CREATE TABLE duplicate_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_file_id INTEGER NOT NULL,
    second_file_id INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('keep', 'ignore')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (first_file_id) REFERENCES model_files(file_id) ON DELETE CASCADE,
    FOREIGN KEY (second_file_id) REFERENCES model_files(file_id) ON DELETE CASCADE
);
CREATE INDEX idx_duplicate_decisions_pair ON duplicate_decisions(first_file_id, second_file_id);
CREATE TRIGGER no_duplicate_decision BEFORE INSERT ON duplicate_decisions
WHEN EXISTS (
    SELECT 1 FROM duplicate_decisions WHERE first_file_id = NEW.first_file_id AND second_file_id = NEW.second_file_id
)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- The tombstone of a file taken out of the catalog. It has no foreign keys because it has to
-- outlive the rows it describes.
CREATE TABLE deleted_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    collection_id INTEGER NOT NULL,
    collection_name TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    original_path TEXT NOT NULL DEFAULT '',
    pending_path TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    external_key TEXT,
    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    purged_at TIMESTAMP
);
CREATE INDEX idx_deleted_files_purged ON deleted_files(purged_at);
