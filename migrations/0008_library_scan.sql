-- One place to see what went inactive or failed, from any process. Rows are never deleted: a
-- resolved item keeps its history. Only one open row can exist per item and reason.
CREATE TABLE inactive_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    item_name TEXT,
    process TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);
CREATE INDEX idx_inactive_log_open ON inactive_log(resolved_at, process);
CREATE INDEX idx_inactive_log_item ON inactive_log(item_type, item_id);
CREATE TRIGGER no_duplicate_open_inactive BEFORE INSERT ON inactive_log
WHEN NEW.resolved_at IS NULL AND EXISTS (
    SELECT 1 FROM inactive_log
    WHERE item_type = NEW.item_type AND item_id = NEW.item_id AND reason = NEW.reason AND resolved_at IS NULL
)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- Each preview, add or rescan. A stopped run resumes under the same row.
CREATE TABLE library_scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    report_json TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP
);
CREATE INDEX idx_library_scan_runs_library ON library_scan_runs(library_id, mode, id);

-- Folders a preview proposed. Only approved folders become models, so a bad guess never reaches
-- the catalog, and adding them resumes by working through what is still approved.
CREATE TABLE library_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    release_name TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    folders_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_library_candidates_status ON library_candidates(library_id, status);
CREATE TRIGGER no_duplicate_candidate BEFORE INSERT ON library_candidates
WHEN EXISTS (SELECT 1 FROM library_candidates WHERE library_id = NEW.library_id AND rel_path = NEW.rel_path)
BEGIN
    SELECT RAISE(ABORT, 'duplicate library candidate');
END;

-- Priority 2 work: covers and archive peeks, kept apart from the inventory scan.
CREATE TABLE library_work_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    model_id INTEGER NOT NULL,
    file_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
);
CREATE INDEX idx_library_work_queue_status ON library_work_queue(status, kind, id);
CREATE TRIGGER no_duplicate_library_work BEFORE INSERT ON library_work_queue
WHEN EXISTS (
    SELECT 1 FROM library_work_queue
    WHERE kind = NEW.kind AND model_id = NEW.model_id AND COALESCE(file_id, 0) = COALESCE(NEW.file_id, 0)
)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- Later Loot releases repeat a character across scales and support types, and one character is
-- one model. Single-folder models have no rows here: their key is their folder.
CREATE TABLE library_model_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
);
CREATE INDEX idx_library_model_folders_model ON library_model_folders(model_id);
CREATE TRIGGER no_duplicate_model_folder BEFORE INSERT ON library_model_folders
WHEN EXISTS (SELECT 1 FROM library_model_folders WHERE model_id = NEW.model_id AND rel_path = NEW.rel_path)
BEGIN
    SELECT RAISE(IGNORE);
END;

ALTER TABLE models ADD COLUMN last_scan_run_id INTEGER;
ALTER TABLE downloaded_files ADD COLUMN modified_ns INTEGER;

-- A linked model's identity becomes its path inside the library, so a changed mount path can't
-- duplicate it. The library ID is part of the key because two libraries can share a folder name.
UPDATE models SET external_key = library_id || '/' || substr(
    vault_path, length((SELECT path FROM external_libraries l WHERE l.id = models.library_id)) + 2
)
WHERE source = 'external' AND library_id IS NOT NULL AND vault_path IS NOT NULL AND EXISTS (
    SELECT 1 FROM external_libraries l
    WHERE l.id = models.library_id AND substr(models.vault_path, 1, length(l.path) + 1) = l.path || '/'
);

UPDATE model_files SET external_key = (
    SELECT m.library_id || '/' || substr(df.vault_path, length(l.path) + 2)
    FROM downloaded_files df
    JOIN models m ON m.id = df.model_id
    JOIN external_libraries l ON l.id = m.library_id
    WHERE df.file_id = model_files.file_id AND substr(df.vault_path, 1, length(l.path) + 1) = l.path || '/'
)
WHERE source = 'external';

INSERT INTO inactive_log (item_type, item_id, item_name, process, reason, detail, created_at)
SELECT 'model', id, name, 'metadata_ingest', 'unavailable', description, updated_at
FROM models WHERE name LIKE '[Unavailable Model %]';

INSERT INTO inactive_log (item_type, item_id, item_name, process, reason, created_at)
SELECT 'model', id, name, 'library_unlink', 'library_unlinked', updated_at
FROM models WHERE is_active = 0;
