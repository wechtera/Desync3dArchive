"""Raw SQL for services/documents.py."""

_FROM_FILES = """
    FROM model_files f
    JOIN collections c ON c.id = f.model_id
    LEFT JOIN downloaded_files d ON d.file_id = f.file_id
"""
_COLUMNS = """
    f.file_id, f.filename, f.size_bytes, f.peeked_at, c.id AS collection_id, c.name AS collection, c.designer_name AS creator,
    c.source, c.primary_image_url, COALESCE(d.vault_path, d.staging_path) AS disk_path
"""
SELECT_PDF_FILES = f"""
    SELECT {_COLUMNS}, NULL AS member_id, NULL AS internal_path {_FROM_FILES}
    WHERE c.is_active = 1 AND lower(f.filename) LIKE '%.pdf';
"""
SELECT_PDF_MEMBERS = f"""
    SELECT {_COLUMNS}, a.id AS member_id, a.internal_path, a.submodel_id
    FROM file_archive_members a
    JOIN model_files f ON f.file_id = a.file_id
    JOIN collections c ON c.id = f.model_id
    LEFT JOIN downloaded_files d ON d.file_id = f.file_id
    WHERE c.is_active = 1 AND lower(a.internal_path) LIKE '%.pdf';
"""
# A product sold as a PDF often arrives zipped, so its PDFs can't be named until it's peeked.
SELECT_UNREAD_PDF_PRODUCTS = f"""
    SELECT {_COLUMNS}, NULL AS member_id, NULL AS internal_path {_FROM_FILES}
    WHERE c.is_active = 1 AND c.name LIKE '%pdf%' AND f.peeked_at IS NULL
      AND (lower(f.filename) LIKE '%.zip' OR lower(f.filename) LIKE '%.rar' OR lower(f.filename) LIKE '%.7z');
"""
SELECT_ACTIVE_LIBRARIES = "SELECT id, name, path, default_creator FROM external_libraries WHERE is_active = 1 ORDER BY id;"
SELECT_LIBRARY_PDF_KEYS = "SELECT external_key FROM model_files WHERE source = 'external' AND external_key LIKE ? AND lower(filename) LIKE '%.pdf';"
SELECT_DOCUMENT_COLLECTION = "SELECT id FROM collections WHERE source = 'document' AND external_key = ?;"
INSERT_DOCUMENT_COLLECTION = """
    INSERT INTO collections (library_id, name, designer_name, designer_username, primary_category, release_month,
        vault_path, source, external_key, raw_backup_path, updated_at)
    VALUES (?, ?, ?, ?, 'Documents', ?, ?, 'document', ?, '', CURRENT_TIMESTAMP);
"""
INSERT_MODEL_FILE = """
    INSERT INTO model_files (model_id, filename, size_bytes, download_url, created_at, source, external_key)
    VALUES (?, ?, ?, '', CURRENT_TIMESTAMP, 'external', ?);
"""
INSERT_DOWNLOADED_FILE = """
    INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,
        expected_bytes, actual_bytes, file_extension, storage_tier, vault_path, downloaded_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, '.pdf', 'nas', ?, CURRENT_TIMESTAMP);
"""
