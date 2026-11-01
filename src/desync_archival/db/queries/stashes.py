"""Raw SQL for the stashes API routes. Names with braces take format() values, never user input."""

SELECT_STASHES_BY_ID = "SELECT id, name, description FROM stashes WHERE id = ?;"

POST_EXPORT_STASH_INACTIVE = "SELECT 1 FROM collections WHERE id = ? AND is_active = 0;"

SELECT_STASH_ITEMS_BY_STASH_ID = "SELECT * FROM stash_items WHERE stash_id = ?;"
