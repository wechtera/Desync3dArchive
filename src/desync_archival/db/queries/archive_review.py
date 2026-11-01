"""Raw SQL for services/archive_review.py. Names with braces take format() values."""

SELECT_MODEL_ARTIFACTS_BY_MODEL_ID = """
        SELECT a.id, a.model_id, a.path, f.filename AS archive
        FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id
        WHERE f.model_id = ? AND a.archive_member_id IS NOT NULL ORDER BY a.path;
    """

SELECT_MODEL_GROUPS_BY_COLLECTION_ID = """
        SELECT m.id, m.name, COUNT(DISTINCT a.model_id) AS models, COUNT(a.id) AS artifacts,
               COALESCE(MAX(r.reviewed), 0) AS reviewed, MAX(f.peeked_at) AS peeked_at,
               (SELECT COUNT(*) FROM model_groups g WHERE g.collection_id = m.id) AS model_groups
        FROM collections m
        JOIN model_files f ON f.model_id = m.id
        JOIN model_artifacts a ON a.file_id = f.file_id AND a.archive_member_id IS NOT NULL
        LEFT JOIN collection_reviews r ON r.collection_id = m.id
        GROUP BY m.id;
    """

SELECT_COLLECTIONS_NAME_BY_ID = "SELECT name FROM collections WHERE id = ?;"

SELECT_MODEL_GROUP_MEMBERS_BY_COLLECTION_ID = (
    "SELECT m.model_id, g.name FROM model_group_members m JOIN model_groups g ON g.id = m.group_id"
    " WHERE g.collection_id = ?;"
)

SELECT_COLLECTION_REVIEWS_REVIEWED_BY_COLLECTION_ID = "SELECT reviewed FROM collection_reviews WHERE collection_id = ?;"

MAX_MODEL_FILES_BY_MODEL_ID = "SELECT MAX(peeked_at) FROM model_files WHERE model_id = ?;"

DELETE_MODEL_GROUP_MEMBERS_BY_GROUP_ID = "DELETE FROM model_group_members WHERE group_id IN (SELECT id FROM model_groups WHERE collection_id = ?);"

SELECT_MODEL_GROUPS_BY_COLLECTION_ID_V2 = "SELECT name, id FROM model_groups WHERE collection_id = ?;"

DELETE_MODEL_GROUPS_BY_ID = "DELETE FROM model_groups WHERE id = ?;"

INSERT_MODEL_GROUPS = "INSERT INTO model_groups (collection_id, name) VALUES (?, ?);"

INSERT_MODEL_GROUP_MEMBERS = "INSERT INTO model_group_members (group_id, model_id) VALUES (?, ?);"

UPDATE_MODELS_NAME_BY_ID = "UPDATE models SET name = ?, category = ?, state = ? WHERE id = ?;"

INSERT_MODELS = "INSERT INTO models (name, category, state) VALUES (?, ?, ?);"

INSERT_MODEL_COLLECTIONS = "INSERT INTO model_collections (model_id, collection_id) VALUES (?, ?);"

UPDATE_MODEL_ARTIFACTS_MODEL_ID_BY_ID = "UPDATE model_artifacts SET model_id = ? WHERE id = ?;"

COUNT_MODEL_ARTIFACTS_BY_MODEL_ID = "SELECT COUNT(*) FROM model_artifacts WHERE model_id = ?;"

DELETE_MODEL_COLLECTIONS_BY_MODEL_ID = "DELETE FROM model_collections WHERE model_id = ? AND collection_id = ?;"

UPDATE_MODELS_STATE_BY_ID = "UPDATE models SET state = ?, merged_into = ? WHERE id = ?;"

UPDATE_COLLECTION_REVIEWS_REVIEWED_BY_COLLECTION_ID = "UPDATE collection_reviews SET reviewed = ?, updated_at = CURRENT_TIMESTAMP WHERE collection_id = ?;"

INSERT_COLLECTION_REVIEWS = "INSERT INTO collection_reviews (collection_id, reviewed) VALUES (?, ?);"
