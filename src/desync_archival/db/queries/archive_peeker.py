"""Raw SQL for services/archive_peeker.py. Names with braces take format() values."""

SELECT_COLLECTIONS_PRIMARY_IMAGE_URL_BY_ID = "SELECT primary_image_url FROM collections WHERE id = ?;"

SELECT_COLLECTIONS_EXTERNAL_KEY_BY_ID = "SELECT external_key FROM collections WHERE id = ?;"

SELECT_FILE_ARCHIVE_SUBMODELS_BY_FILE_ID = "SELECT id, name, relative_dir FROM file_archive_submodels WHERE file_id = ?;"

SELECT_FILE_ARCHIVE_MEMBERS_BY_FILE_ID = "SELECT internal_path, id FROM file_archive_members WHERE file_id = ?;"

UPDATE_FILE_ARCHIVE_SUBMODELS_PREVIEW_IMAGE_PATH_BY_ID = """
                            UPDATE file_archive_submodels SET preview_image_path = ?, total_files = ?, total_bytes = ?,
                                has_presupported = ?, has_unsupported = ?, has_slicer_project = ?, formats_json = ?,
                                scales_json = ?
                            WHERE id = ?;
                        """

INSERT_FILE_ARCHIVE_SUBMODELS = """
                            INSERT INTO file_archive_submodels (
                                file_id, model_id, name, relative_dir, preview_image_path,
                                total_files, total_bytes, has_presupported, has_unsupported,
                                has_slicer_project, formats_json, scales_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                        """

UPDATE_FILE_ARCHIVE_MEMBERS_SUBMODEL_ID_BY_ID = """
                            UPDATE file_archive_members
                            SET submodel_id = ?, filename = ?, size_bytes = ?, is_supported = ?, file_extension = ?
                            WHERE id = ?;
                        """

INSERT_FILE_ARCHIVE_MEMBERS = """
                            INSERT INTO file_archive_members (
                                submodel_id, filename, size_bytes, is_supported, file_extension, file_id, internal_path
                            ) VALUES (?, ?, ?, ?, ?, ?, ?);
                        """

DELETE_FILE_ARCHIVE_MEMBERS_BY_ID = "DELETE FROM file_archive_members WHERE id = ?;"

DELETE_FILE_ARCHIVE_SUBMODELS_BY_ID = "DELETE FROM file_archive_submodels WHERE id = ?;"

UPDATE_MODEL_FILES_PEEKED_AT_BY_FILE_ID = "UPDATE model_files SET peeked_at = CURRENT_TIMESTAMP WHERE file_id = ?;"
