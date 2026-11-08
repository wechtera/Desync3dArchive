"""Raw SQL for services/picture_jobs.py."""

SELECT_PENDING = "SELECT id, kind, model_id, group_id, collection_id, source_json FROM picture_jobs WHERE status = 'pending' ORDER BY id LIMIT 1;"
FINISH_JOB = """
    UPDATE picture_jobs SET status = ?, detail = ?, attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?;
"""
COUNT_BY_STATUS = "SELECT status, COUNT(*) FROM picture_jobs GROUP BY status;"

# Only over nothing or a stand-in: a locked picture, or one a person or the matcher set, stays.
SET_MODEL_PICTURE = """
    UPDATE models SET image_path = ?, image_source = ?
    WHERE id = ? AND image_locked = 0 AND COALESCE(image_source, '') IN ('', 'group', 'cover', 'render');
"""
# A person's choice that had to wait for the NAS: it replaces anything, and nothing replaces it.
SET_OWNER_PICTURE = "UPDATE models SET image_path = ?, image_source = 'owner', image_locked = 1 WHERE id = ?;"
SET_GROUP_PICTURE = """
    UPDATE model_groups SET image_path = ?, image_source = ? WHERE id = ? AND COALESCE(image_source, '') != 'owner';
"""
SELECT_MODEL_PICTURE = "SELECT image_path, image_source, image_locked FROM models WHERE id = ?;"
# The apply that queued a job, while its import still stands and the unit hasn't been restored.
SELECT_OPEN_SNAPSHOT = """
    SELECT s.unit_key, s.after_hash FROM grouping_snapshots s JOIN grouping_imports i ON i.id = s.import_id
    WHERE s.id = ? AND s.restored_at IS NULL AND i.status = 'applied';
"""
UPDATE_SNAPSHOT_HASH = "UPDATE grouping_snapshots SET after_hash = ? WHERE id = ?;"
SELECT_ARCHIVE_FILE = "SELECT vault_path, staging_path FROM downloaded_files WHERE file_id = ?;"
SELECT_MEMBER = "SELECT internal_path FROM file_archive_members WHERE id = ?;"
SELECT_ARTIFACT = "SELECT id, file_id, archive_member_id, path, filename FROM model_artifacts WHERE id = ?;"
SELECT_LIBRARY_ROOT = """
    SELECT l.path FROM model_files f JOIN collections c ON c.id = f.model_id JOIN external_libraries l ON l.id = c.library_id
    WHERE f.file_id = ?;
"""
