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

# Apply and undo.
SELECT_IMPORT = "SELECT id, export_id, status, report_json, answer_json FROM grouping_imports WHERE id = ?;"
UPDATE_IMPORT_STATUS = "UPDATE grouping_imports SET status = ?, applied_at = COALESCE(applied_at, CASE WHEN ? = 'applied' THEN CURRENT_TIMESTAMP END), undone_at = CASE WHEN ? = 'undone' THEN CURRENT_TIMESTAMP ELSE undone_at END WHERE id = ?;"
UPDATE_UNIT_DECIDED = "UPDATE grouping_units SET status = ?, decided_fingerprint = ?, updated_at = CURRENT_TIMESTAMP WHERE unit_key = ?;"
INSERT_SNAPSHOT = "INSERT INTO grouping_snapshots (import_id, unit_key, before_json, after_hash) VALUES (?, ?, ?, '');"
UPDATE_SNAPSHOT_HASH = "UPDATE grouping_snapshots SET after_hash = ? WHERE id = ?;"
DELETE_SNAPSHOT = "DELETE FROM grouping_snapshots WHERE id = ?;"
SELECT_SNAPSHOTS = "SELECT id, unit_key, before_json, after_hash, restored_at FROM grouping_snapshots WHERE import_id = ? ORDER BY id;"
UPDATE_SNAPSHOT_RESTORED = "UPDATE grouping_snapshots SET restored_at = CURRENT_TIMESTAMP WHERE id = ?;"
INSERT_SUGGESTION = """
    INSERT INTO grouping_suggestions (import_id, unit_key, bucket, cards_json, reasons_json) VALUES (?, ?, ?, ?, ?);
"""
REPLACE_WAITING_SUGGESTIONS = """
    UPDATE grouping_suggestions SET status = 'replaced', decided_at = CURRENT_TIMESTAMP
    WHERE unit_key = ? AND status = 'waiting';
"""

# A unit's state, for snapshots and for telling whether a person edited it since.
SELECT_UNIT_ARCHIVE_ARTIFACTS = """
    SELECT a.id, a.model_id FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id
    WHERE f.model_id = ? AND a.archive_member_id IS NOT NULL ORDER BY a.id;
"""
SELECT_UNIT_LIBRARY_ARTIFACTS = """
    SELECT a.id, a.model_id FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id JOIN collections c ON c.id = f.model_id
    WHERE c.library_id = ? AND substr(c.external_key, 1, length(?)) = ? AND a.archive_member_id IS NULL ORDER BY a.id;
"""
SELECT_MODEL_ROWS = """
    SELECT id, name, category, state, merged_into, image_path, image_source, image_locked FROM models WHERE id IN ({marks}) ORDER BY id;
"""
SELECT_LINKS = "SELECT model_id, collection_id FROM model_collections WHERE model_id IN ({marks}) ORDER BY model_id, collection_id;"
SELECT_ARCHIVE_GROUP_ROWS = "SELECT id, name, image_path, image_source FROM model_groups WHERE collection_id = ? ORDER BY name;"
SELECT_LIBRARY_GROUP_ROWS = "SELECT id, name, image_path, image_source FROM model_groups WHERE library_id = ? AND release_name = ? ORDER BY name;"
SELECT_GROUP_MEMBERS = "SELECT model_id FROM model_group_members WHERE group_id = ? ORDER BY model_id;"
SELECT_TAG_ROWS = "SELECT model_id, tag FROM model_tags WHERE model_id IN ({marks}) ORDER BY model_id, tag;"
SELECT_REVIEW_ROW = "SELECT reviewed FROM collection_reviews WHERE collection_id = ?;"

# Restoring a snapshot.
MOVE_ARTIFACT = "UPDATE model_artifacts SET model_id = ? WHERE id = ?;"
RESTORE_MODEL = """
    UPDATE models SET name = ?, category = ?, state = ?, merged_into = ?, image_path = ?, image_source = ?, image_locked = ?
    WHERE id = ?;
"""
REDIRECT_MODEL = "UPDATE models SET state = 'merged', merged_into = ? WHERE id = ?;"
DELETE_LINKS_TO = "DELETE FROM model_collections WHERE model_id = ? AND collection_id = ?;"
INSERT_LINK = "INSERT INTO model_collections (model_id, collection_id) VALUES (?, ?);"
DELETE_ARCHIVE_GROUPS = "DELETE FROM model_groups WHERE collection_id = ?;"
DELETE_LIBRARY_GROUPS = "DELETE FROM model_groups WHERE library_id = ? AND release_name = ?;"
INSERT_ARCHIVE_GROUP = "INSERT INTO model_groups (collection_id, name, image_path, image_source) VALUES (?, ?, ?, ?);"
INSERT_LIBRARY_GROUP = "INSERT INTO model_groups (library_id, release_name, name, image_path, image_source) VALUES (?, ?, ?, ?, ?);"
INSERT_GROUP_MEMBER = "INSERT INTO model_group_members (group_id, model_id) VALUES (?, ?);"
DELETE_TAG = "DELETE FROM model_tags WHERE model_id = ? AND tag = ?;"
INSERT_TAG = "INSERT INTO model_tags (model_id, tag) VALUES (?, ?);"
UPSERT_REVIEW = "UPDATE collection_reviews SET reviewed = ? WHERE collection_id = ?;"
DELETE_REVIEW = "DELETE FROM collection_reviews WHERE collection_id = ?;"
SKIP_PICTURE_JOBS = """
    UPDATE picture_jobs SET status = 'skipped', detail = 'Undone', updated_at = CURRENT_TIMESTAMP
    WHERE status = 'pending' AND (model_id IN ({marks}) OR collection_id = ?);
"""

# Pictures.
SET_MODEL_PICTURE = "UPDATE models SET image_path = ?, image_source = ? WHERE id = ? AND image_locked = 0;"
SET_GROUP_PICTURE = "UPDATE model_groups SET image_path = ?, image_source = ? WHERE id = ?;"
SELECT_GROUP_ID = "SELECT id FROM model_groups WHERE collection_id = ? AND name = ?;"
SELECT_LIBRARY_GROUP_ID = "SELECT id FROM model_groups WHERE library_id = ? AND release_name = ? AND name = ?;"
INSERT_PICTURE_JOB = "INSERT INTO picture_jobs (kind, model_id, group_id, collection_id, source_json) VALUES (?, ?, ?, ?, ?);"
SELECT_ARTIFACT_MODEL = "SELECT model_id FROM model_artifacts WHERE id = ?;"
SELECT_COLLECTION_SOURCE = "SELECT source FROM collections WHERE id = ?;"

# Suggestions.
SELECT_WAITING_SUGGESTIONS = """
    SELECT s.id, s.import_id, s.unit_key, s.bucket, s.reasons_json, s.cards_json, s.created_at
    FROM grouping_suggestions s WHERE s.status = 'waiting' ORDER BY CASE s.bucket WHEN 'low' THEN 1 ELSE 0 END, s.id;
"""
SELECT_SUGGESTION = "SELECT id, import_id, unit_key, bucket, cards_json, reasons_json, status FROM grouping_suggestions WHERE id = ?;"
DECIDE_SUGGESTION = "UPDATE grouping_suggestions SET status = ?, decided_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'waiting';"
SELECT_ARTIFACT_BY_ID = "SELECT model_id FROM model_artifacts WHERE id = ?;"
LOCK_MODEL_PICTURE = "UPDATE models SET image_path = ?, image_source = 'owner', image_locked = 1 WHERE id = ?;"

# Imports for the screen.
SELECT_IMPORTS = """
    SELECT i.id, i.source_name, i.status, i.created_at, i.applied_at, i.undone_at, i.report_json, e.export_key
    FROM grouping_imports i LEFT JOIN grouping_exports e ON e.id = i.export_id ORDER BY i.id DESC LIMIT ?;
"""
SELECT_EXPORTS = "SELECT export_key, mode, scope, unit_count, batch_count, created_at FROM grouping_exports ORDER BY id DESC LIMIT ?;"
DISCARD_IMPORT = "UPDATE grouping_imports SET status = 'discarded' WHERE id = ? AND status = 'checked';"
SELECT_MODEL_IMAGE = "SELECT image_path FROM models WHERE id = ?;"
SELECT_REMOVED_TAG = """
    SELECT 1 FROM removed_tags WHERE tag = ? AND collection_id IN (SELECT collection_id FROM model_collections WHERE model_id = ?);
"""
