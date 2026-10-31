"""Raw SQL for the stashes API routes. Names with braces take format() values, never user input."""

POST_EXPORT_STASH_1 = "SELECT id, name, description FROM stashes WHERE id = ?;"

POST_EXPORT_STASH_INACTIVE = "SELECT 1 FROM models WHERE id = ? AND is_active = 0;"

POST_EXPORT_STASH_2 = "SELECT * FROM stash_items WHERE stash_id = ?;"
