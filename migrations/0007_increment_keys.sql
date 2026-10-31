-- no-transaction
-- Every table gets an increment column as its key, and business values stop being unique. The
-- triggers at the end reject repeats instead. Existing ID values do not change, and AUTOINCREMENT
-- never reuses one, so a stale preview folder or stash row can't attach to a new row.
-- Foreign keys must be off while parents are rebuilt, and PRAGMA has no effect inside a transaction.
PRAGMA foreign_keys = OFF;
BEGIN;

DROP TRIGGER catalog_search_model_insert;
DROP TRIGGER catalog_search_model_update;
DROP TRIGGER catalog_search_model_delete;
DROP TRIGGER catalog_search_tag_insert;
DROP TRIGGER catalog_search_tag_delete;

-- models
CREATE TABLE new_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT,
    description TEXT,
    published_at TEXT,
    designer_id INTEGER,
    designer_name TEXT,
    designer_username TEXT,
    primary_category TEXT,
    parent_category TEXT,
    pre_supported BOOLEAN DEFAULT 0,
    featured BOOLEAN DEFAULT 0,
    views INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    price_value REAL,
    price_currency TEXT,
    primary_image_url TEXT,
    raw_backup_path TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
, release_month TEXT, is_bonus BOOLEAN DEFAULT 0, vault_path TEXT, archive_download_url TEXT, source TEXT DEFAULT 'mmf', is_active INTEGER NOT NULL DEFAULT 1, external_key TEXT, library_id INTEGER REFERENCES external_libraries(id));
INSERT INTO new_models (id, name, url, description, published_at, designer_id, designer_name, designer_username, primary_category, parent_category, pre_supported, featured, views, likes, price_value, price_currency, primary_image_url, raw_backup_path, updated_at, release_month, is_bonus, vault_path, archive_download_url, source, is_active, external_key, library_id) SELECT id, name, url, description, published_at, designer_id, designer_name, designer_username, primary_category, parent_category, pre_supported, featured, views, likes, price_value, price_currency, primary_image_url, raw_backup_path, updated_at, release_month, is_bonus, vault_path, archive_download_url, source, is_active, external_key, library_id FROM models;
DROP TABLE models;
ALTER TABLE new_models RENAME TO models;

-- model_files
CREATE TABLE new_model_files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL,
    filename TEXT,
    size_bytes INTEGER,
    download_url TEXT,
    created_at TEXT, source TEXT NOT NULL DEFAULT 'mmf', external_key TEXT,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
);
INSERT INTO new_model_files (file_id, model_id, filename, size_bytes, download_url, created_at, source, external_key) SELECT file_id, model_id, filename, size_bytes, download_url, created_at, source, external_key FROM model_files;
DROP TABLE model_files;
ALTER TABLE new_model_files RENAME TO model_files;

-- downloaded_files
CREATE TABLE new_downloaded_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES model_files(file_id) ON DELETE CASCADE,
    model_id INTEGER NOT NULL,
    original_filename TEXT NOT NULL,
    cleaned_filename TEXT NOT NULL,
    local_relative_path TEXT NOT NULL,
    expected_bytes INTEGER,
    actual_bytes INTEGER NOT NULL,
    has_size_prefix BOOLEAN DEFAULT 0,
    has_size_suffix BOOLEAN DEFAULT 0,
    file_extension TEXT,
    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, storage_tier TEXT NOT NULL DEFAULT 'local', staging_path TEXT, vault_path TEXT, sha256 TEXT,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
);
INSERT INTO new_downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, expected_bytes, actual_bytes, has_size_prefix, has_size_suffix, file_extension, downloaded_at, storage_tier, staging_path, vault_path, sha256) SELECT file_id, model_id, original_filename, cleaned_filename, local_relative_path, expected_bytes, actual_bytes, has_size_prefix, has_size_suffix, file_extension, downloaded_at, storage_tier, staging_path, vault_path, sha256 FROM downloaded_files;
DROP TABLE downloaded_files;
ALTER TABLE new_downloaded_files RENAME TO downloaded_files;

-- model_tags
CREATE TABLE new_model_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER,
    tag TEXT NOT NULL,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
);
INSERT INTO new_model_tags (model_id, tag) SELECT model_id, tag FROM model_tags;
DROP TABLE model_tags;
ALTER TABLE new_model_tags RENAME TO model_tags;

-- releases
CREATE TABLE new_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_key TEXT NOT NULL,
    designer_name TEXT NOT NULL,
    designer_id INTEGER,
    release_month TEXT NOT NULL,
    model_count INTEGER NOT NULL,
    bonus_count INTEGER NOT NULL DEFAULT 0,
    total_files INTEGER NOT NULL,
    total_size_bytes INTEGER NOT NULL,
    is_likely_bundle BOOLEAN DEFAULT 0,
    sample_image_url TEXT,
    common_tags TEXT,
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
, is_active INTEGER NOT NULL DEFAULT 1);
INSERT INTO new_releases (release_key, designer_name, designer_id, release_month, model_count, bonus_count, total_files, total_size_bytes, is_likely_bundle, sample_image_url, common_tags, computed_at, is_active) SELECT id, designer_name, designer_id, release_month, model_count, bonus_count, total_files, total_size_bytes, is_likely_bundle, sample_image_url, common_tags, computed_at, is_active FROM releases;
DROP TABLE releases;
ALTER TABLE new_releases RENAME TO releases;

-- download_jobs
CREATE TABLE new_download_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    total_files INTEGER NOT NULL DEFAULT 0,
    downloaded_files INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    enqueued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP, destination_path TEXT,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
);
INSERT INTO new_download_jobs (id, model_id, status, retry_count, total_files, downloaded_files, total_bytes, downloaded_bytes, error_message, enqueued_at, started_at, completed_at, destination_path) SELECT id, model_id, status, retry_count, total_files, downloaded_files, total_bytes, downloaded_bytes, error_message, enqueued_at, started_at, completed_at, destination_path FROM download_jobs;
DELETE FROM sqlite_sequence WHERE name = 'new_download_jobs';
INSERT INTO sqlite_sequence (name, seq) SELECT 'new_download_jobs', seq FROM sqlite_sequence WHERE name = 'download_jobs';
DROP TABLE download_jobs;
ALTER TABLE new_download_jobs RENAME TO download_jobs;

-- external_libraries
CREATE TABLE new_external_libraries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    default_creator TEXT,
    disk_bytes INTEGER DEFAULT 0,
    total_models INTEGER DEFAULT 0,
    total_files INTEGER DEFAULT 0,
    last_scanned_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
, is_active INTEGER NOT NULL DEFAULT 1);
INSERT INTO new_external_libraries (id, name, path, default_creator, disk_bytes, total_models, total_files, last_scanned_at, created_at, is_active) SELECT id, name, path, default_creator, disk_bytes, total_models, total_files, last_scanned_at, created_at, is_active FROM external_libraries;
DELETE FROM sqlite_sequence WHERE name = 'new_external_libraries';
INSERT INTO sqlite_sequence (name, seq) SELECT 'new_external_libraries', seq FROM sqlite_sequence WHERE name = 'external_libraries';
DROP TABLE external_libraries;
ALTER TABLE new_external_libraries RENAME TO external_libraries;

DROP INDEX idx_stash_items_unique;

-- Indexes, including the ones that keep the duplicate triggers fast.
CREATE INDEX idx_models_designer ON models(designer_name);
CREATE INDEX idx_models_category ON models(primary_category);
CREATE INDEX idx_models_release_month ON models(release_month);
CREATE INDEX idx_models_bonus ON models(is_bonus);
CREATE INDEX idx_models_external_key ON models(source, external_key);
CREATE INDEX idx_models_library ON models(library_id);
CREATE INDEX idx_models_vault_path ON models(vault_path);
CREATE INDEX idx_files_model ON model_files(model_id);
CREATE INDEX idx_files_external_key ON model_files(source, external_key);
CREATE INDEX idx_downloaded_model ON downloaded_files(model_id);
CREATE INDEX idx_downloaded_tier ON downloaded_files(storage_tier);
CREATE INDEX idx_downloaded_sha256 ON downloaded_files(sha256);
CREATE INDEX idx_tags_tag ON model_tags(tag);
CREATE INDEX idx_releases_designer ON releases(designer_name);
CREATE INDEX idx_releases_month ON releases(release_month);
CREATE INDEX idx_jobs_status ON download_jobs(status);
CREATE INDEX idx_tags_model_tag ON model_tags(model_id, tag);
CREATE INDEX idx_downloaded_file ON downloaded_files(file_id);
CREATE INDEX idx_releases_key ON releases(release_key);
CREATE INDEX idx_jobs_model ON download_jobs(model_id);
CREATE INDEX idx_libraries_path ON external_libraries(path);
CREATE INDEX idx_stash_items_identity ON stash_items(stash_id, model_id, item_type);

-- Search index triggers, dropped with their tables.
CREATE TRIGGER catalog_search_model_insert AFTER INSERT ON models BEGIN
    INSERT INTO catalog_search (rowid, name, designer_name, tags)
    VALUES (new.id, new.name, new.designer_name,
            (SELECT group_concat(tag, char(10)) FROM model_tags WHERE model_id = new.id));
END;

CREATE TRIGGER catalog_search_model_update AFTER UPDATE OF name, designer_name ON models BEGIN
    UPDATE catalog_search SET name = new.name, designer_name = new.designer_name WHERE rowid = new.id;
END;

CREATE TRIGGER catalog_search_model_delete AFTER DELETE ON models BEGIN
    DELETE FROM catalog_search WHERE rowid = old.id;
END;

CREATE TRIGGER catalog_search_tag_insert AFTER INSERT ON model_tags BEGIN
    UPDATE catalog_search
    SET tags = (SELECT group_concat(tag, char(10)) FROM model_tags WHERE model_id = new.model_id)
    WHERE rowid = new.model_id;
END;

CREATE TRIGGER catalog_search_tag_delete AFTER DELETE ON model_tags BEGIN
    UPDATE catalog_search
    SET tags = (SELECT group_concat(tag, char(10)) FROM model_tags WHERE model_id = old.model_id)
    WHERE rowid = old.model_id;
END;

-- Duplicate rules. These replace UNIQUE, so primary keys stay the only uniqueness in the schema.
CREATE TRIGGER no_duplicate_model_key_insert BEFORE INSERT ON models
WHEN NEW.external_key IS NOT NULL AND EXISTS (SELECT 1 FROM models WHERE source = NEW.source AND external_key = NEW.external_key)
BEGIN
    SELECT RAISE(ABORT, 'duplicate model external key');
END;

CREATE TRIGGER no_duplicate_model_key_update BEFORE UPDATE OF source, external_key ON models
WHEN NEW.external_key IS NOT NULL AND EXISTS (SELECT 1 FROM models WHERE source = NEW.source AND external_key = NEW.external_key AND id != OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'duplicate model external key');
END;

CREATE TRIGGER no_duplicate_model_path_insert BEFORE INSERT ON models
WHEN NEW.source = 'external' AND NEW.vault_path IS NOT NULL AND EXISTS (SELECT 1 FROM models WHERE source = 'external' AND vault_path = NEW.vault_path)
BEGIN
    SELECT RAISE(ABORT, 'duplicate linked library model path');
END;

CREATE TRIGGER no_duplicate_model_path_update BEFORE UPDATE OF source, vault_path ON models
WHEN NEW.source = 'external' AND NEW.vault_path IS NOT NULL AND EXISTS (SELECT 1 FROM models WHERE source = 'external' AND vault_path = NEW.vault_path AND id != OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'duplicate linked library model path');
END;

CREATE TRIGGER no_duplicate_file_key_insert BEFORE INSERT ON model_files
WHEN NEW.external_key IS NOT NULL AND EXISTS (SELECT 1 FROM model_files WHERE source = NEW.source AND external_key = NEW.external_key)
BEGIN
    SELECT RAISE(ABORT, 'duplicate file external key');
END;

CREATE TRIGGER no_duplicate_file_key_update BEFORE UPDATE OF source, external_key ON model_files
WHEN NEW.external_key IS NOT NULL AND EXISTS (SELECT 1 FROM model_files WHERE source = NEW.source AND external_key = NEW.external_key AND file_id != OLD.file_id)
BEGIN
    SELECT RAISE(ABORT, 'duplicate file external key');
END;

CREATE TRIGGER no_duplicate_downloaded_file_insert BEFORE INSERT ON downloaded_files
WHEN EXISTS (SELECT 1 FROM downloaded_files WHERE file_id = NEW.file_id)
BEGIN
    SELECT RAISE(ABORT, 'duplicate downloaded file');
END;

CREATE TRIGGER no_duplicate_downloaded_file_update BEFORE UPDATE OF file_id ON downloaded_files
WHEN EXISTS (SELECT 1 FROM downloaded_files WHERE file_id = NEW.file_id AND id != OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'duplicate downloaded file');
END;

CREATE TRIGGER no_duplicate_release_insert BEFORE INSERT ON releases
WHEN EXISTS (SELECT 1 FROM releases WHERE release_key = NEW.release_key)
BEGIN
    SELECT RAISE(ABORT, 'duplicate release');
END;

CREATE TRIGGER no_duplicate_release_update BEFORE UPDATE OF release_key ON releases
WHEN EXISTS (SELECT 1 FROM releases WHERE release_key = NEW.release_key AND id != OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'duplicate release');
END;

CREATE TRIGGER no_duplicate_job_insert BEFORE INSERT ON download_jobs
WHEN EXISTS (SELECT 1 FROM download_jobs WHERE model_id = NEW.model_id)
BEGIN
    SELECT RAISE(ABORT, 'duplicate download job');
END;

CREATE TRIGGER no_duplicate_job_update BEFORE UPDATE OF model_id ON download_jobs
WHEN EXISTS (SELECT 1 FROM download_jobs WHERE model_id = NEW.model_id AND id != OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'duplicate download job');
END;

CREATE TRIGGER no_duplicate_library_insert BEFORE INSERT ON external_libraries
WHEN EXISTS (SELECT 1 FROM external_libraries WHERE path = NEW.path)
BEGIN
    SELECT RAISE(ABORT, 'duplicate library path');
END;

CREATE TRIGGER no_duplicate_library_update BEFORE UPDATE OF path ON external_libraries
WHEN EXISTS (SELECT 1 FROM external_libraries WHERE path = NEW.path AND id != OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'duplicate library path');
END;

-- A repeated tag was already skipped with INSERT OR IGNORE, so it stays a quiet skip.
CREATE TRIGGER no_duplicate_tag_insert BEFORE INSERT ON model_tags
WHEN EXISTS (SELECT 1 FROM model_tags WHERE model_id = NEW.model_id AND tag = NEW.tag)
BEGIN
    SELECT RAISE(IGNORE);
END;

CREATE TRIGGER no_duplicate_stash_item_insert BEFORE INSERT ON stash_items
WHEN EXISTS (
    SELECT 1 FROM stash_items
    WHERE stash_id = NEW.stash_id AND item_type = NEW.item_type AND model_id = NEW.model_id
      AND COALESCE(file_id, 0) = COALESCE(NEW.file_id, 0)
      AND COALESCE(submodel_id, 0) = COALESCE(NEW.submodel_id, 0)
      AND COALESCE(archive_member_id, 0) = COALESCE(NEW.archive_member_id, 0)
)
BEGIN
    SELECT RAISE(ABORT, 'duplicate stash item');
END;

PRAGMA user_version = 7;
COMMIT;
PRAGMA foreign_keys = ON;
