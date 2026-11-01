"""Raw SQL for services/tags.py."""

SELECT_TAGS = """
    SELECT t.name,
           (SELECT COUNT(*) FROM collection_tags c WHERE c.tag = t.name)
           + (SELECT COUNT(*) FROM model_tags m WHERE m.tag = t.name) AS uses
    FROM tags t WHERE t.blocked = 0 AND t.name LIKE ? ORDER BY t.name LIMIT ?;
"""
COUNT_TAGS = "SELECT COUNT(*) FROM tags WHERE blocked = 0;"
SELECT_TAG = "SELECT id, blocked FROM tags WHERE name = ?;"
INSERT_TAG = "INSERT INTO tags (name) VALUES (?);"
UNBLOCK_TAG = "UPDATE tags SET blocked = 0 WHERE name = ?;"
BLOCK_TAG = "UPDATE tags SET blocked = 1 WHERE name = ?;"
DELETE_TAG = "DELETE FROM tags WHERE name = ?;"

# Copy then delete, because the search index only follows inserts and deletes on these tables.
COPY_COLLECTION_TAG = "INSERT INTO collection_tags (model_id, tag) SELECT model_id, ? FROM collection_tags WHERE tag = ?;"
COPY_MODEL_TAG = "INSERT INTO model_tags (model_id, tag) SELECT model_id, ? FROM model_tags WHERE tag = ?;"
DELETE_COLLECTION_TAG_EVERYWHERE = "DELETE FROM collection_tags WHERE tag = ?;"
DELETE_MODEL_TAG_EVERYWHERE = "DELETE FROM model_tags WHERE tag = ?;"

INSERT_MODEL_TAG = "INSERT INTO model_tags (model_id, tag) VALUES (?, ?);"
DELETE_MODEL_TAG = "DELETE FROM model_tags WHERE model_id = ? AND tag = ?;"
SELECT_MODEL_COLLECTIONS = "SELECT collection_id FROM model_collections WHERE model_id = ?;"
DELETE_COLLECTION_TAG = "DELETE FROM collection_tags WHERE model_id = ? AND tag = ?;"
INSERT_REMOVED_TAG = "INSERT INTO removed_tags (collection_id, tag) VALUES (?, ?);"
DELETE_REMOVED_TAG = "DELETE FROM removed_tags WHERE collection_id = ? AND tag = ?;"
SELECT_GROUP_MEMBERS = "SELECT model_id FROM model_group_members WHERE group_id = ?;"
SELECT_ALL_USED_TAGS = "SELECT tag FROM collection_tags UNION SELECT tag FROM model_tags;"
