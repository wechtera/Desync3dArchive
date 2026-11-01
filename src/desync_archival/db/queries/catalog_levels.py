"""Raw SQL for services/catalog_levels.py. Names with braces take format() values."""

UPDATE_MODELS_NAME_BY_ID = "UPDATE models SET name = ?, state = ? WHERE id = ? AND state = ?;"

SELECT_MODEL_FILES_BY_MODEL_ID = "SELECT filename, file_id FROM model_files WHERE model_id = ?;"

SELECT_MODEL_FILES_BY_MODEL_ID_V2 = "SELECT external_key, file_id FROM model_files WHERE model_id = ?;"

INSERT_MODEL_ARTIFACTS = (
    "INSERT INTO model_artifacts (model_id, file_id, path, filename, size_bytes, scale, supported, hollow)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?);"
)

SELECT_MODEL_ARTIFACTS_BY_MODEL_ID = "SELECT id, file_id FROM model_artifacts WHERE model_id = ? AND archive_member_id IS NULL;"

DELETE_MODEL_ARTIFACTS_BY_ID = "DELETE FROM model_artifacts WHERE id = ?;"

SELECT_MODEL_GROUPS_ID_BY_LIBRARY_ID = "SELECT id FROM model_groups WHERE library_id = ? AND release_name = ? AND name = ?;"

INSERT_MODEL_GROUPS = "INSERT INTO model_groups (library_id, release_name, name) VALUES (?, ?, ?);"

INSERT_MODEL_GROUP_MEMBERS = "INSERT INTO model_group_members (group_id, model_id) VALUES (?, ?);"

INSERT_MODEL_TAGS = "INSERT INTO model_tags (model_id, tag) VALUES (?, ?);"

SELECT_MODEL_ARTIFACTS = "SELECT model_id, filename, size_bytes FROM model_artifacts;"

SELECT_MODEL_GROUPS_ID_BY_COLLECTION_ID = "SELECT id FROM model_groups WHERE collection_id = ? AND name = ?;"

INSERT_MODEL_GROUPS_V2 = "INSERT INTO model_groups (collection_id, name) VALUES (?, ?);"

INSERT_MODEL_ARTIFACTS_V2 = (
    "INSERT INTO model_artifacts (model_id, file_id, archive_member_id, path, filename, size_bytes, scale,"
    " supported, hollow) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);"
)

INSERT_MODEL_COLLECTIONS = "INSERT INTO model_collections (model_id, collection_id) VALUES (?, ?);"

UPDATE_MODELS_NAME_BY_ID_V2 = "UPDATE models SET name = ?, state = ? WHERE id = ?;"

INSERT_MODELS = "INSERT INTO models (name, state) VALUES (?, ?);"

SELECT_FILE_ARCHIVE_MEMBERS_BY_MODEL_ID = """
            SELECT a.file_id, a.internal_path FROM file_archive_members a JOIN model_files f ON f.file_id = a.file_id
            WHERE f.model_id = ?;
        """

SELECT_MODELS_BY_MODEL_ID = """
        SELECT DISTINCT m.id, m.name FROM models m JOIN model_artifacts a ON a.model_id = m.id
        JOIN model_files f ON f.file_id = a.file_id WHERE f.model_id = ? AND m.image_path IS NULL;
    """

SELECT_MODEL_ARTIFACTS_PATH_BY_MODEL_ID = "SELECT path FROM model_artifacts WHERE model_id = ?;"

UPDATE_MODELS_IMAGE_PATH_BY_ID = "UPDATE models SET image_path = ? WHERE id = ?;"

SELECT_COLLECTION_REVIEWS_COLLECTION_ID_BY_REVIEWED = "SELECT collection_id FROM collection_reviews WHERE reviewed = 1;"

SELECT_MODEL_ARTIFACTS_MODEL_ID_BY_ARCHIVE_MEMBER_ID = """
            SELECT DISTINCT f.model_id FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id
            WHERE a.archive_member_id IS NOT NULL;
        """

DELETE_MODEL_ARTIFACTS_BY_ARCHIVE_MEMBER_ID = """
                DELETE FROM model_artifacts WHERE archive_member_id IS NOT NULL
                    AND file_id IN (SELECT file_id FROM model_files WHERE model_id = ?);
            """

DELETE_MODEL_GROUPS_BY_COLLECTION_ID = "DELETE FROM model_groups WHERE collection_id = ?;"

DELETE_MODEL_COLLECTIONS_BY_COLLECTION_ID = "DELETE FROM model_collections WHERE collection_id = ? AND model_id != ?;"

UPDATE_MODELS_NAME_BY_ID_V3 = """
                UPDATE models SET name = (SELECT name FROM collections WHERE id = ?), state = ?, merged_into = NULL,
                    image_path = NULL
                WHERE id = ?;
            """

DELETE_MODELS_BY_STATE = """
            DELETE FROM models WHERE state = ? AND id >= ? AND NOT EXISTS (
                SELECT 1 FROM model_artifacts a WHERE a.model_id = models.id
            );
        """

DELETE_MODEL_GROUP_MEMBERS_BY_GROUP_ID = """
            DELETE FROM model_group_members
            WHERE group_id NOT IN (SELECT id FROM model_groups)
               OR model_id NOT IN (SELECT id FROM models);
        """

DELETE_MODEL_TAGS_BY_MODEL_ID = "DELETE FROM model_tags WHERE model_id NOT IN (SELECT id FROM models);"

SELECT_FILE_ARCHIVE_MEMBERS = """
        SELECT a.id, a.file_id, a.internal_path, a.filename, a.size_bytes, f.model_id AS collection_id
        FROM file_archive_members a JOIN model_files f ON f.file_id = a.file_id;
    """

SELECT_MODEL_ARTIFACTS_ARCHIVE_MEMBER_ID_BY_ARCHIVE_MEMBER_ID = "SELECT archive_member_id FROM model_artifacts WHERE archive_member_id IS NOT NULL;"

UPDATE_COLLECTION_REVIEWS_REVIEWED_BY_COLLECTION_ID = "UPDATE collection_reviews SET reviewed = 0 WHERE collection_id = ?;"

SELECT_MODELS_STATE_BY_ID = "SELECT state FROM models WHERE id = ?;"

SELECT_MODELS_BY_COLLECTION_ID = """
                SELECT m.id, m.name FROM models m JOIN model_collections l ON l.model_id = m.id
                WHERE l.collection_id = ? AND m.state IN (?, ?);
            """

UPDATE_MODELS_STATE_BY_ID = "UPDATE models SET state = ?, merged_into = ? WHERE id = ?;"

DELETE_MODEL_COLLECTIONS_BY_MODEL_ID = "DELETE FROM model_collections WHERE model_id = ? AND collection_id = ?;"
