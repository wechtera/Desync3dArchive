"""Raw SQL for services/download_issues.py and services/issue_upload.py."""

UPDATE_OPEN_ISSUE = """
    UPDATE download_issues
    SET kind = ?, listed_bytes = ?, received_bytes = ?, detail = ?, found_at = CURRENT_TIMESTAMP
    WHERE file_id = ? AND resolved_at IS NULL;
"""
INSERT_ISSUE = """
    INSERT INTO download_issues (file_id, model_id, kind, listed_bytes, received_bytes, detail)
    VALUES (?, ?, ?, ?, ?, ?);
"""
RESOLVE_FILE = """
    UPDATE download_issues SET resolved_at = CURRENT_TIMESTAMP, resolution = ?
    WHERE file_id = ? AND resolved_at IS NULL;
"""
COUNT_OPEN = "SELECT COUNT(*) FROM download_issues WHERE resolved_at IS NULL;"

_ISSUE_COLUMNS = """
    SELECT i.id, i.file_id, i.model_id, i.kind, i.listed_bytes, i.received_bytes, i.detail, i.found_at,
           i.resolved_at, i.resolution, f.filename, f.download_url, c.name AS model_name, c.url AS model_url,
           COALESCE(NULLIF(c.designer_name, ''), NULLIF(c.designer_username, ''), 'Unknown') AS creator_name,
           d.actual_bytes AS stored_bytes, d.vault_path, d.storage_tier, d.sha256, f.external_key
    FROM download_issues i
    JOIN model_files f ON f.file_id = i.file_id
    JOIN collections c ON c.id = i.model_id
    LEFT JOIN downloaded_files d ON d.file_id = i.file_id
"""
SELECT_OPEN = _ISSUE_COLUMNS + " WHERE i.resolved_at IS NULL ORDER BY i.found_at DESC, i.id DESC;"
SELECT_RESOLVED = _ISSUE_COLUMNS + " WHERE i.resolved_at IS NOT NULL ORDER BY i.resolved_at DESC, i.id DESC LIMIT ?;"
SELECT_OPEN_FOR_MODEL = _ISSUE_COLUMNS + " WHERE i.resolved_at IS NULL AND i.model_id = ? ORDER BY f.filename;"
SELECT_ISSUE = _ISSUE_COLUMNS + " WHERE i.id = ?;"

CONFIRM_SIZE = "UPDATE model_files SET size_bytes = ?, size_confirmed_at = CURRENT_TIMESTAMP WHERE file_id = ?;"
RESOLVE_ISSUE = """
    UPDATE download_issues SET resolved_at = CURRENT_TIMESTAMP, resolution = ?
    WHERE id = ? AND resolved_at IS NULL;
"""

# The job, if any, so an upload never races a download of the same model.
SELECT_JOB_STATUS = "SELECT status FROM download_jobs WHERE model_id = ?;"
SELECT_VAULT_ROOT = "SELECT value FROM app_settings WHERE key = 'download_root_path';"
SELECT_COLLECTION = """
    SELECT id, name, external_key, vault_path,
           COALESCE(NULLIF(designer_name, ''), NULLIF(designer_username, ''), 'Unknown_Creator') AS creator_name,
           COALESCE(NULLIF(release_month, ''), 'Unassigned') AS release_month
    FROM collections WHERE id = ?;
"""
SELECT_STORED_NAMES = "SELECT cleaned_filename FROM downloaded_files WHERE model_id = ? AND file_id != ?;"
UPDATE_STORED_FILE = """
    UPDATE downloaded_files
    SET actual_bytes = ?, expected_bytes = ?, sha256 = ?, downloaded_at = CURRENT_TIMESTAMP
    WHERE file_id = ?;
"""
INSERT_STORED_FILE = """
    INSERT INTO downloaded_files (
        file_id, model_id, original_filename, cleaned_filename, local_relative_path, expected_bytes,
        actual_bytes, file_extension, storage_tier, vault_path, sha256, downloaded_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nas', ?, ?, CURRENT_TIMESTAMP);
"""
SET_VAULT_PATH_IF_NONE = "UPDATE collections SET vault_path = ? WHERE id = ? AND vault_path IS NULL;"
INSERT_UPLOAD_ATTEMPT = """
    INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, error_detail)
    VALUES (?, ?, 1, 'uploaded', ?);
"""
