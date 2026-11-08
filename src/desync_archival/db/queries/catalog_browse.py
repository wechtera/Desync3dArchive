"""Raw SQL for services/catalog_browse.py. Names with braces take format() values."""

SELECT_MODEL_GROUPS_BY_MODEL_ID = """
        SELECT g.id, g.name, g.image_path FROM model_groups g JOIN model_group_members m ON m.group_id = g.id
        WHERE m.model_id = ? ORDER BY g.name;
    """

SELECT_COLLECTIONS_BY_MODEL_ID = """
        SELECT c.id, c.name, c.designer_name, c.primary_image_url FROM collections c
        JOIN model_collections l ON l.collection_id = c.id
        WHERE l.model_id = ? ORDER BY c.id;
    """

SELECT_MODELS_BY_ID = "SELECT id, name, category, state, merged_into, image_path FROM models WHERE id = ?;"

SELECT_MODEL_ARTIFACTS_BY_MODEL_ID = """
        SELECT a.id, a.filename, a.path, a.size_bytes, a.scale, a.supported, a.hollow, a.file_id, a.archive_member_id,
               f.filename AS archive, f.model_id AS collection_id, m.submodel_id
        FROM model_artifacts a LEFT JOIN model_files f ON f.file_id = a.file_id
        LEFT JOIN file_archive_members m ON m.id = a.archive_member_id
        WHERE a.model_id = ? ORDER BY a.scale, a.filename;
    """

SELECT_MODEL_TAGS_TAG_BY_MODEL_ID = """
        SELECT tag FROM model_tags WHERE model_id = ?1
        UNION SELECT t.tag FROM collection_tags t JOIN model_collections l ON l.collection_id = t.model_id
        WHERE l.model_id = ?1 ORDER BY 1;
    """

SELECT_COLLECTIONS_BY_MODEL_ID_V2 = """
        SELECT c.release_month, c.url, c.published_at FROM collections c JOIN model_collections l ON l.collection_id = c.id
        WHERE l.model_id = ? ORDER BY c.id LIMIT 1;
    """

SELECT_MODEL_GROUPS_BY_ID = "SELECT id, name, collection_id, release_name, image_path FROM model_groups WHERE id = ?;"

SELECT_MODEL_ARTIFACTS_BY_MODEL_ID_STATE = """
        SELECT m.id, m.name, m.category, m.state, m.image_path,
               (SELECT COUNT(*) FROM model_artifacts a WHERE a.model_id = m.id) AS artifacts
        FROM models m JOIN model_group_members g ON g.model_id = m.id
        WHERE g.group_id = ? AND m.state NOT IN (?, ?) ORDER BY m.name COLLATE NOCASE;
    """
