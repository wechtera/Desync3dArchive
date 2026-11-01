"""Raw SQL for the archives API routes. Names with braces take format() values, never user input."""

SELECT_MODEL_FILES_BY_FILE_ID = """
        SELECT mf.file_id, mf.model_id, mf.filename, mf.size_bytes,
               df.storage_tier, df.vault_path, df.downloaded_at
        FROM model_files mf
        LEFT JOIN downloaded_files df ON mf.file_id = df.file_id
        WHERE mf.file_id = ?;
    """

SELECT_FILE_ARCHIVE_SUBMODELS_BY_FILE_ID = """
        SELECT id, file_id, model_id, name, relative_dir, preview_image_path,
               total_files, total_bytes, has_presupported, has_unsupported,
               has_slicer_project, formats_json, scales_json, created_at
        FROM file_archive_submodels
        WHERE file_id = ?
        ORDER BY id ASC;
    """

SELECT_FILE_ARCHIVE_MEMBERS_BY_SUBMODEL_ID = """
            SELECT id, submodel_id, internal_path, filename, size_bytes,
                   is_supported, file_extension
            FROM file_archive_members
            WHERE submodel_id = ?
            ORDER BY filename ASC;
        """

SELECT_FILE_ARCHIVE_MEMBERS_BY_FILE_ID = """
        SELECT fam.id, fam.submodel_id, fam.internal_path, fam.filename,
               fam.size_bytes, fam.is_supported, fam.file_extension,
               fas.name AS submodel_name
        FROM file_archive_members fam
        LEFT JOIN file_archive_submodels fas ON fam.submodel_id = fas.id
        WHERE fam.file_id = ?
        ORDER BY fam.internal_path ASC;
    """

PRAGMA_ROW = "PRAGMA database_list;"
