"""Raw SQL for services/mesh_renderer.py. Names with braces take format() values."""

SELECT_FILE_ARCHIVE_MEMBERS_BY_ID = "SELECT id, file_id, internal_path, filename FROM file_archive_members WHERE id = ?;"

SELECT_DOWNLOADED_FILES_BY_FILE_ID = "SELECT vault_path, staging_path FROM downloaded_files WHERE file_id = ?;"

SELECT_DOWNLOADED_FILES_BY_FILE_ID_V2 = """
                        SELECT df.file_id, df.model_id, df.vault_path, df.staging_path, df.cleaned_filename, df.file_extension
                        FROM downloaded_files df
                        WHERE df.file_id = ?;
                    """

SELECT_MODEL_FILES_BY_FILE_ID = """
                            SELECT mf.file_id, mf.model_id, df.vault_path, df.staging_path, mf.filename AS cleaned_filename, NULL AS file_extension
                            FROM model_files mf
                            LEFT JOIN downloaded_files df ON mf.file_id = df.file_id
                            WHERE mf.file_id = ?;
                        """

SELECT_FILE_ARCHIVE_MEMBERS_BY_FILE_ID = """
            SELECT id, internal_path, filename, size_bytes, is_supported
            FROM file_archive_members
            WHERE file_id = ?
              AND (LOWER(file_extension) = '.stl' OR LOWER(filename) LIKE '%.stl');
        """

SELECT_DOWNLOADED_FILES_MODEL_ID_BY_FILE_ID = "SELECT model_id FROM downloaded_files WHERE file_id = ?;"

SELECT_FILE_ARCHIVE_MEMBERS_BY_FILE_ID_LOWER = """
                    SELECT id, internal_path, filename, size_bytes, is_supported
                    FROM file_archive_members
                    WHERE file_id = ?
                      AND (LOWER(file_extension) = '.stl' OR LOWER(filename) LIKE '%.stl');
                """

SELECT_COLLECTIONS_VAULT_PATH_BY_ID = "SELECT vault_path FROM collections WHERE id = ?;"

SELECT_DOWNLOADED_FILES_BY_MODEL_ID = """
            SELECT df.vault_path, df.staging_path
            FROM downloaded_files df
            WHERE df.model_id = ?
              AND (LOWER(df.file_extension) = '.stl' OR LOWER(df.cleaned_filename) LIKE '%.stl');
        """

SELECT_DOWNLOADED_FILES_BY_MODEL_ID_LOWER = """
            SELECT df.file_id, df.vault_path, df.staging_path
            FROM downloaded_files df
            WHERE df.model_id = ?
              AND (LOWER(df.file_extension) IN ('.zip', '.rar', '.7z')
                   OR LOWER(df.cleaned_filename) LIKE '%.zip'
                   OR LOWER(df.cleaned_filename) LIKE '%.rar'
                   OR LOWER(df.cleaned_filename) LIKE '%.7z');
        """
