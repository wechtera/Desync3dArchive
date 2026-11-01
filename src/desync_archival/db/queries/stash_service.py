"""Raw SQL for services/stash_service.py. Names with braces take format() values."""

SELECT_SQLITE_MASTER_NAME_BY_TYPE = "SELECT name FROM sqlite_master WHERE type='table';"

SELECT_COLLECTIONS_BY_ID = "SELECT id, name, vault_path FROM collections WHERE id = ? AND is_active = 1;"

SELECT_DOWNLOADED_FILES_BY_FILE_ID = """
            SELECT file_id, vault_path, staging_path, storage_tier
            FROM downloaded_files
            WHERE file_id = ?;
        """

SELECT_FILE_ARCHIVE_SUBMODELS_FILE_ID_BY_ID = "SELECT file_id FROM file_archive_submodels WHERE id = ?;"

SELECT_FILE_ARCHIVE_MEMBERS_FILE_ID_BY_ID = "SELECT file_id FROM file_archive_members WHERE id = ?;"

SELECT_COLLECTIONS_VAULT_PATH_BY_ID = "SELECT vault_path FROM collections WHERE id = ?;"

SELECT_DOWNLOADED_FILES_BY_FILE_ID_V2 = "SELECT vault_path, staging_path, storage_tier FROM downloaded_files WHERE file_id = ?;"

UPDATE_STASHES_IS_ACTIVE_BY_IS_ACTIVE = "UPDATE stashes SET is_active = 0 WHERE is_active = 1;"

INSERT_STASHES = """
                    INSERT INTO stashes (name, description, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                    """

SELECT_STASHES_BY_ID = "SELECT * FROM stashes WHERE id = ?;"

SELECT_STASHES_ID_BY_ID = "SELECT id FROM stashes WHERE id = ?;"

UPDATE_STASHES_IS_ACTIVE_BY_ID = "UPDATE stashes SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?;"

DELETE_STASHES_BY_ID = "DELETE FROM stashes WHERE id = ?;"

SELECT_STASHES_BY_ID_V2 = "SELECT id, name FROM stashes WHERE id = ?;"

SELECT_COLLECTIONS_ID_BY_ID = "SELECT id FROM collections WHERE id = ? AND is_active = 1;"

SELECT_MODEL_FILES_FILE_ID_BY_FILE_ID = "SELECT file_id FROM model_files WHERE file_id = ?;"

SELECT_FILE_ARCHIVE_SUBMODELS_ID_BY_ID = "SELECT id FROM file_archive_submodels WHERE id = ?;"

SELECT_FILE_ARCHIVE_MEMBERS_ID_BY_ID = "SELECT id FROM file_archive_members WHERE id = ?;"

SELECT_STASH_ITEMS_ID_BY_STASH_ID = """
                SELECT id FROM stash_items
                WHERE stash_id = ?
                  AND item_type = ?
                  AND model_id = ?
                  AND COALESCE(file_id, 0) = ?
                  AND COALESCE(submodel_id, 0) = ?
                  AND COALESCE(archive_member_id, 0) = ?;
                """

INSERT_STASH_ITEMS = """
                    INSERT INTO stash_items (
                        stash_id, item_type, model_id, file_id, submodel_id,
                        archive_member_id, notes, added_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                    """

UPDATE_STASHES_UPDATED_AT_BY_ID = "UPDATE stashes SET updated_at = CURRENT_TIMESTAMP WHERE id = ?;"

SELECT_STASH_ITEMS_BY_ID = "SELECT * FROM stash_items WHERE id = ?;"

SELECT_STASH_ITEMS_STASH_ID_BY_ID = "SELECT stash_id FROM stash_items WHERE id = ?;"

DELETE_STASH_ITEMS_BY_ID = "DELETE FROM stash_items WHERE id = ?;"

INSERT_STASH_ITEMS_V2 = """
                        INSERT INTO stash_items (stash_id, item_type, model_id, added_at)
                        VALUES (?, 'model', ?, CURRENT_TIMESTAMP);
                        """
