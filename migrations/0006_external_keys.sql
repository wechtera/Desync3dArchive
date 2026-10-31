-- MMF IDs become data. external_key holds the ID a source knows a row by, and is NULL for files in
-- linked libraries, which have no outside ID. library_id replaces matching models by path prefix.
ALTER TABLE models ADD COLUMN external_key TEXT;
ALTER TABLE models ADD COLUMN library_id INTEGER REFERENCES external_libraries(id);
ALTER TABLE model_files ADD COLUMN source TEXT NOT NULL DEFAULT 'mmf';
ALTER TABLE model_files ADD COLUMN external_key TEXT;

UPDATE models SET external_key = CAST(id AS TEXT) WHERE source = 'mmf';

UPDATE models SET library_id = (
    SELECT l.id FROM external_libraries l
    WHERE models.vault_path = l.path OR substr(models.vault_path, 1, length(l.path) + 1) = l.path || '/'
    ORDER BY length(l.path) DESC LIMIT 1
) WHERE source = 'external';

UPDATE model_files SET source = (SELECT m.source FROM models m WHERE m.id = model_files.model_id)
WHERE EXISTS (SELECT 1 FROM models m WHERE m.id = model_files.model_id);
UPDATE model_files SET external_key = CAST(file_id AS TEXT) WHERE source = 'mmf';

CREATE INDEX idx_models_external_key ON models(source, external_key);
CREATE INDEX idx_models_library ON models(library_id);
CREATE INDEX idx_models_vault_path ON models(vault_path);
CREATE INDEX idx_files_external_key ON model_files(source, external_key);
CREATE INDEX idx_downloaded_sha256 ON downloaded_files(sha256);
