-- Unlinking a library flags its contents instead of deleting them, so stash items keep a valid
-- model and relinking the same path brings the same rows back with the same IDs.
ALTER TABLE models ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE external_libraries ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE releases ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;
