"""Raw SQL for E54, grouping by LLM: services/grouping_units.py, grouping_export.py and the rest."""

# Archive units: a collection whose archives were peeked into artifacts.
SELECT_ARCHIVE_UNIT_IDS = """
    SELECT DISTINCT f.model_id FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id
    WHERE a.archive_member_id IS NOT NULL ORDER BY f.model_id;
"""
SELECT_COLLECTION = """
    SELECT id, name, source, release_month, primary_image_url,
           COALESCE(NULLIF(designer_name, ''), NULLIF(designer_username, ''), '') AS creator
    FROM collections WHERE id = ?;
"""
SELECT_ARCHIVE_ARTIFACTS = """
    SELECT a.id, a.model_id, a.path, a.size_bytes, a.archive_member_id, f.filename AS archive, f.file_id
    FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id
    WHERE f.model_id = ? AND a.archive_member_id IS NOT NULL ORDER BY f.filename, a.path;
"""
SELECT_ARCHIVE_MEMBERS = """
    SELECT m.id, m.file_id, m.internal_path, m.size_bytes, f.filename AS archive
    FROM file_archive_members m JOIN model_files f ON f.file_id = m.file_id
    WHERE f.model_id = ? ORDER BY f.filename, m.internal_path;
"""
SELECT_ARCHIVE_REVIEWED = "SELECT reviewed FROM collection_reviews WHERE collection_id = ?;"
SELECT_ARCHIVE_GROUPS = """
    SELECT gm.model_id, g.name FROM model_groups g JOIN model_group_members gm ON gm.group_id = g.id
    WHERE g.collection_id = ?;
"""

# Library units: a release whose models are already in the catalog.
# The release is the second part of external_key, library/release/model. Split in Python.
SELECT_LIBRARY_KEYS = """
    SELECT DISTINCT c.library_id, c.external_key FROM collections c
    WHERE c.source = 'external' AND c.library_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM model_files f JOIN model_artifacts a ON a.file_id = f.file_id
        WHERE f.model_id = c.id AND a.archive_member_id IS NULL
    );
"""
SELECT_LIBRARY = "SELECT id, name, path, default_creator FROM external_libraries WHERE id = ?;"
SELECT_RELEASE_COLLECTIONS = """
    SELECT id, name, release_month, COALESCE(NULLIF(designer_name, ''), '') AS creator
    FROM collections WHERE library_id = ? AND substr(external_key, 1, length(?)) = ? ORDER BY id;
"""
SELECT_LIBRARY_ARTIFACTS = """
    SELECT a.id, a.model_id, a.path, a.size_bytes, f.model_id AS collection_id
    FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id JOIN collections c ON c.id = f.model_id
    WHERE c.library_id = ? AND substr(c.external_key, 1, length(?)) = ? AND a.archive_member_id IS NULL
    ORDER BY a.path;
"""
SELECT_LIBRARY_IMAGES = """
    SELECT id, rel_path, size_bytes FROM library_images WHERE library_id = ? AND release_name = ? ORDER BY rel_path;
"""
SELECT_LIBRARY_REVIEWED = "SELECT reviewed FROM library_release_reviews WHERE library_id = ? AND release_name = ?;"
SELECT_LIBRARY_GROUPS = """
    SELECT gm.model_id, g.name FROM model_groups g JOIN model_group_members gm ON gm.group_id = g.id
    WHERE g.library_id = ? AND g.release_name = ?;
"""

SELECT_MODELS = "SELECT id, name, state, image_path, image_source, image_locked FROM models WHERE id IN ({marks});"
SELECT_MODEL_TAGS = "SELECT model_id, tag FROM model_tags WHERE model_id IN ({marks}) ORDER BY tag;"
SELECT_TAGS = "SELECT name FROM tags WHERE blocked = 0 ORDER BY name;"

# Export bookkeeping.
INSERT_EXPORT = """
    INSERT INTO grouping_exports (export_key, mode, scope, include_reviewed, batch_lines, unit_count, batch_count)
    VALUES (?, ?, ?, ?, ?, ?, ?);
"""
INSERT_EXPORT_UNIT = """
    INSERT INTO grouping_export_units (export_id, unit_key, batch, fingerprint, ids_json) VALUES (?, ?, ?, ?, ?);
"""
SELECT_UNIT_STATE = "SELECT unit_key, status, decided_fingerprint FROM grouping_units;"
UPDATE_UNIT_EXPORTED = """
    UPDATE grouping_units SET last_export_id = ?, exported_fingerprint = ?, updated_at = CURRENT_TIMESTAMP
    WHERE unit_key = ?;
"""
INSERT_UNIT = """
    INSERT INTO grouping_units (unit_key, kind, status, last_export_id, exported_fingerprint) VALUES (?, ?, 'exported', ?, ?);
"""
SELECT_EXPORT_BY_KEY = "SELECT id, export_key, created_at FROM grouping_exports WHERE export_key = ?;"
SELECT_EXPORT_UNIT = "SELECT fingerprint, ids_json, batch FROM grouping_export_units WHERE export_id = ? AND unit_key = ?;"

INSERT_IMPORT = "INSERT INTO grouping_imports (export_id, source_name, report_json, answer_json) VALUES (?, ?, ?, ?);"
