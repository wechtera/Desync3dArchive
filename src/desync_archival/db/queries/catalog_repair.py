"""Raw SQL for services/catalog_repair.py."""

SELECT_SELF_MERGED = "SELECT id, name FROM models WHERE merged_into = id;"
SELECT_ARTIFACT_PATHS = "SELECT path FROM model_artifacts WHERE model_id = ? AND archive_member_id IS NOT NULL;"
COUNT_REVIEWED_COLLECTIONS_OF_MODEL = """
    SELECT COUNT(*) FROM model_collections mc JOIN collection_reviews r ON r.collection_id = mc.collection_id
    WHERE mc.model_id = ? AND r.reviewed = 1;
"""
UNMERGE_MODEL = "UPDATE models SET name = ?, state = ?, merged_into = NULL WHERE id = ?;"

# Live archive models sharing a name in one collection. Each holds archive files of its own.
SELECT_SAME_NAME = """
    SELECT mc.collection_id, lower(m.name) AS name_key, group_concat(m.id) AS ids
    FROM model_collections mc JOIN models m ON m.id = mc.model_id
    WHERE m.merged_into IS NULL AND m.state IN ('proposed', 'resolved')
      AND EXISTS (SELECT 1 FROM model_artifacts a WHERE a.model_id = m.id AND a.archive_member_id IS NOT NULL)
    GROUP BY mc.collection_id, lower(m.name) HAVING COUNT(*) > 1;
"""
SELECT_MODEL_NAME = "SELECT name FROM models WHERE id = ?;"
MOVE_ARTIFACTS = "UPDATE model_artifacts SET model_id = ? WHERE model_id = ?;"
COPY_COLLECTION_LINKS = "INSERT INTO model_collections (model_id, collection_id) SELECT ?, collection_id FROM model_collections WHERE model_id = ?;"
DELETE_COLLECTION_LINKS = "DELETE FROM model_collections WHERE model_id = ?;"
COPY_GROUP_MEMBERS = "INSERT INTO model_group_members (group_id, model_id) SELECT group_id, ? FROM model_group_members WHERE model_id = ?;"
DELETE_GROUP_MEMBERS = "DELETE FROM model_group_members WHERE model_id = ?;"
COPY_TAGS = "INSERT INTO model_tags (model_id, tag) SELECT ?, tag FROM model_tags WHERE model_id = ?;"
DELETE_TAGS = "DELETE FROM model_tags WHERE model_id = ?;"
TAKE_IMAGE_IF_NONE = "UPDATE models SET image_path = COALESCE(image_path, (SELECT image_path FROM models WHERE id = ?)) WHERE id = ?;"
REDIRECT_MERGES = "UPDATE models SET merged_into = ? WHERE merged_into = ?;"
MERGE_MODEL = "UPDATE models SET state = 'merged', merged_into = ? WHERE id = ?;"

# One lowercase word, which is how a folder's slug looks. A name a person typed has spaces or capitals.
# Reviewed collections only. A rebuild names an unreviewed collection's groups again from scratch.
SELECT_SLUG_GROUPS = """
    SELECT g.id, g.collection_id, g.name FROM model_groups g
    JOIN collection_reviews r ON r.collection_id = g.collection_id AND r.reviewed = 1
    WHERE g.name GLOB '[a-z0-9]*' AND g.name NOT GLOB '*[ A-Z]*';
"""
SELECT_GROUP_PATHS = """
    SELECT a.path FROM model_group_members gm JOIN model_artifacts a ON a.model_id = gm.model_id
    WHERE gm.group_id = ? AND a.archive_member_id IS NOT NULL;
"""
SELECT_GROUP_BY_NAME = "SELECT id FROM model_groups WHERE collection_id = ? AND name = ? AND id != ?;"
RENAME_GROUP = "UPDATE model_groups SET name = ? WHERE id = ?;"
MOVE_GROUP_MEMBERS = "INSERT INTO model_group_members (group_id, model_id) SELECT ?, model_id FROM model_group_members WHERE group_id = ?;"
DELETE_GROUP = "DELETE FROM model_groups WHERE id = ?;"

COUNT_LIVE_ARCHIVE_MODELS = """
    SELECT COUNT(*) FROM models m WHERE m.merged_into IS NULL
      AND EXISTS (SELECT 1 FROM model_artifacts a WHERE a.model_id = m.id AND a.archive_member_id IS NOT NULL);
"""
