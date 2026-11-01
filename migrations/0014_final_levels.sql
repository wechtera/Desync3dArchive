-- The names finally match GLOSSARY.md: a row in collections is what was sold or stored as one unit,
-- and a row in models is one designed thing. SQLite rewrites triggers and foreign keys to follow.
--
-- model_id on the older tables (model_files, downloaded_files, collection_tags, stash_items and
-- friends) still points at collections. The API uses that name, so it stays.
ALTER TABLE models RENAME TO collections;
ALTER TABLE model_tags RENAME TO collection_tags;

ALTER TABLE catalog_models RENAME TO models;
ALTER TABLE catalog_model_tags RENAME TO model_tags;
ALTER TABLE catalog_model_collections RENAME TO model_collections;

ALTER TABLE model_tags RENAME COLUMN catalog_model_id TO model_id;
ALTER TABLE model_collections RENAME COLUMN catalog_model_id TO model_id;
ALTER TABLE model_artifacts RENAME COLUMN catalog_model_id TO model_id;
ALTER TABLE model_group_members RENAME COLUMN catalog_model_id TO model_id;
