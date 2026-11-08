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

-- E54: grouping by LLM. An export goes out as a zip the owner runs through any LLM, and the answer
-- comes back to be checked before anything changes. Each applied unit keeps a snapshot for undo.
-- Uniqueness is by trigger, as elsewhere since 0007.
CREATE TABLE grouping_exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    export_key TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('delta', 'full', 'single')),
    scope TEXT NOT NULL CHECK (scope IN ('archives', 'libraries', 'both')),
    include_reviewed INTEGER NOT NULL DEFAULT 0,
    batch_lines INTEGER NOT NULL,
    unit_count INTEGER NOT NULL DEFAULT 0,
    batch_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_grouping_exports_key ON grouping_exports(export_key);
CREATE TRIGGER no_duplicate_grouping_export BEFORE INSERT ON grouping_exports
WHEN EXISTS (SELECT 1 FROM grouping_exports WHERE export_key = NEW.export_key)
BEGIN
    SELECT RAISE(ABORT, 'duplicate export key');
END;

-- What each unit looked like when it went out: its fingerprint, and what each short ID meant.
CREATE TABLE grouping_export_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    export_id INTEGER NOT NULL REFERENCES grouping_exports(id) ON DELETE CASCADE,
    unit_key TEXT NOT NULL,
    batch INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    ids_json TEXT NOT NULL
);
CREATE INDEX idx_grouping_export_units ON grouping_export_units(export_id, unit_key);

-- One row per unit ever exported. Delta reads it to leave out what hasn't changed.
CREATE TABLE grouping_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('archive', 'library')),
    status TEXT NOT NULL DEFAULT 'exported'
        CHECK (status IN ('exported', 'applied', 'suggested', 'rejected', 'refused', 'undone')),
    last_export_id INTEGER REFERENCES grouping_exports(id) ON DELETE SET NULL,
    exported_fingerprint TEXT,
    decided_fingerprint TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_grouping_units_key ON grouping_units(unit_key);
CREATE TRIGGER no_duplicate_grouping_unit BEFORE INSERT ON grouping_units
WHEN EXISTS (SELECT 1 FROM grouping_units WHERE unit_key = NEW.unit_key)
BEGIN
    SELECT RAISE(ABORT, 'duplicate grouping unit');
END;

-- An uploaded answer. It's kept as checked, so applying uses exactly what the dry run showed.
CREATE TABLE grouping_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    export_id INTEGER REFERENCES grouping_exports(id) ON DELETE SET NULL,
    source_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'checked' CHECK (status IN ('checked', 'applied', 'undone', 'discarded')),
    report_json TEXT NOT NULL,
    answer_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_at TIMESTAMP,
    undone_at TIMESTAMP
);

-- A unit's grouping before an import applied, and a hash of it after, which tells undo whether a
-- person has edited it since.
CREATE TABLE grouping_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL REFERENCES grouping_imports(id) ON DELETE CASCADE,
    unit_key TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    restored_at TIMESTAMP
);
CREATE INDEX idx_grouping_snapshots_import ON grouping_snapshots(import_id);

-- An answer waiting for a person: medium or low, a reviewed unit, or one the checks doubted.
CREATE TABLE grouping_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL REFERENCES grouping_imports(id) ON DELETE CASCADE,
    unit_key TEXT NOT NULL,
    bucket TEXT NOT NULL CHECK (bucket IN ('high', 'medium', 'low')),
    cards_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'accepted', 'rejected', 'replaced')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    decided_at TIMESTAMP
);
CREATE INDEX idx_grouping_suggestions_unit ON grouping_suggestions(unit_key, status);

-- Images a linked library holds, recorded by its scan so they can go out with a release.
CREATE TABLE library_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL REFERENCES external_libraries(id) ON DELETE CASCADE,
    release_name TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_library_images_release ON library_images(library_id, release_name);

-- Pictures that need the NAS: an archive image never extracted, or a render of a chosen artifact.
CREATE TABLE picture_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('extract', 'render', 'copy')),
    model_id INTEGER REFERENCES models(id) ON DELETE CASCADE,
    group_id INTEGER REFERENCES model_groups(id) ON DELETE CASCADE,
    collection_id INTEGER,
    source_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_picture_jobs_status ON picture_jobs(status, id);

-- Where a picture came from: owner, archive, llm, group, cover or render. Locked means a person
-- chose it, and nothing automatic replaces it. Every picture before this came from the matcher.
ALTER TABLE models ADD COLUMN image_source TEXT;
ALTER TABLE models ADD COLUMN image_locked INTEGER NOT NULL DEFAULT 0;
UPDATE models SET image_source = 'archive' WHERE image_path IS NOT NULL;
ALTER TABLE model_groups ADD COLUMN image_path TEXT;
ALTER TABLE model_groups ADD COLUMN image_source TEXT;
