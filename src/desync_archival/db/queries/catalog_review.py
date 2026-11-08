"""Raw SQL for services/catalog_review.py. Names with braces take format() values."""

SELECT_RELEASE_ARTIFACTS = """
    SELECT a.id, a.model_id, a.path, f.model_id AS collection_id
    FROM model_artifacts a
    JOIN model_files f ON f.file_id = a.file_id
    JOIN collections c ON c.id = f.model_id
    WHERE c.library_id = ? AND substr(c.external_key, 1, length(?2)) = ?2 AND a.archive_member_id IS NULL
    ORDER BY a.path;
"""
SELECT_MODELS = "SELECT id, name, category, state FROM models WHERE id IN ({marks});"
UPDATE_REVIEWED = "UPDATE library_catalog_reviews SET reviewed_at = CURRENT_TIMESTAMP WHERE library_id = ? AND release_name = ?;"
INSERT_REVIEWED = "INSERT INTO library_catalog_reviews (library_id, release_name) VALUES (?, ?);"
SELECT_REVIEWED = "SELECT 1 FROM library_catalog_reviews WHERE library_id = ? AND release_name = ?;"
SELECT_RELEASE_GROUPS = """
    SELECT m.model_id, g.name FROM model_group_members m JOIN model_groups g ON g.id = m.group_id
    WHERE g.library_id = ? AND g.release_name = ?;
"""
SELECT_RELEASE_GROUP_IDS = "SELECT name, id FROM model_groups WHERE library_id = ? AND release_name = ?;"
DELETE_RELEASE_GROUP_MEMBERS = """
    DELETE FROM model_group_members WHERE group_id IN (
        SELECT id FROM model_groups WHERE library_id = ? AND release_name = ?
    );
"""
DELETE_GROUP = "DELETE FROM model_groups WHERE id = ?;"
INSERT_RELEASE_GROUP = "INSERT INTO model_groups (library_id, release_name, name) VALUES (?, ?, ?);"
INSERT_GROUP_MEMBER = "INSERT INTO model_group_members (group_id, model_id) VALUES (?, ?);"
UPDATE_MODEL = "UPDATE models SET name = ?, category = ?, state = ? WHERE id = ?;"
UPDATE_COLLECTION_NAME = "UPDATE collections SET name = ?, primary_category = ? WHERE id = ?;"
INSERT_MODEL = "INSERT INTO models (name, category, state) VALUES (?, ?, ?);"
MOVE_ARTIFACT = "UPDATE model_artifacts SET model_id = ? WHERE id = ?;"
LINK_MODEL = "INSERT INTO model_collections (model_id, collection_id) VALUES (?, ?);"
REDIRECT_MODEL = "UPDATE models SET state = ?, merged_into = ? WHERE id = ?;"
UNLINK_MODEL = "DELETE FROM model_collections WHERE model_id = ?;"
