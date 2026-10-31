-- The levels from GLOSSARY.md, built beside the old tables so the app keeps working throughout.
-- Until the final rename, a `models` row IS a collection, and `catalog_models` holds real models.

CREATE TABLE catalog_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT,
    -- placeholder: nothing is known about the collection's contents yet.
    state TEXT NOT NULL DEFAULT 'placeholder',
    -- Set when two models turn out to be one. Old IDs keep resolving, so stashes hold.
    merged_into INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (merged_into) REFERENCES catalog_models(id)
);
CREATE INDEX idx_catalog_models_state ON catalog_models(state);
CREATE INDEX idx_catalog_models_merged ON catalog_models(merged_into);

-- A bundle and the same item sold alone share a model, so this is many to many.
CREATE TABLE catalog_model_collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_model_id INTEGER NOT NULL,
    collection_id INTEGER NOT NULL,
    FOREIGN KEY (catalog_model_id) REFERENCES catalog_models(id) ON DELETE CASCADE,
    FOREIGN KEY (collection_id) REFERENCES models(id) ON DELETE CASCADE
);
CREATE INDEX idx_model_collections_model ON catalog_model_collections(catalog_model_id);
CREATE INDEX idx_model_collections_collection ON catalog_model_collections(collection_id);
CREATE TRIGGER no_duplicate_model_collection BEFORE INSERT ON catalog_model_collections
WHEN EXISTS (
    SELECT 1 FROM catalog_model_collections
    WHERE catalog_model_id = NEW.catalog_model_id AND collection_id = NEW.collection_id
)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- Each group has one scope, so it can't span collections by accident: a collection, or a linked
-- library's release, which is what a collection is there until the final rename. A model shared
-- by two collections can sit in a group in each.
CREATE TABLE model_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER,
    library_id INTEGER,
    release_name TEXT,
    name TEXT NOT NULL,
    FOREIGN KEY (collection_id) REFERENCES models(id) ON DELETE CASCADE,
    FOREIGN KEY (library_id) REFERENCES external_libraries(id) ON DELETE CASCADE
);
CREATE INDEX idx_model_groups_collection ON model_groups(collection_id);
CREATE INDEX idx_model_groups_release ON model_groups(library_id, release_name);
CREATE TRIGGER no_duplicate_model_group BEFORE INSERT ON model_groups
WHEN EXISTS (
    SELECT 1 FROM model_groups WHERE name = NEW.name
        AND COALESCE(collection_id, -1) = COALESCE(NEW.collection_id, -1)
        AND COALESCE(library_id, -1) = COALESCE(NEW.library_id, -1)
        AND COALESCE(release_name, '') = COALESCE(NEW.release_name, '')
)
BEGIN
    SELECT RAISE(ABORT, 'duplicate model group');
END;

CREATE TABLE model_group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    catalog_model_id INTEGER NOT NULL,
    FOREIGN KEY (group_id) REFERENCES model_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (catalog_model_id) REFERENCES catalog_models(id) ON DELETE CASCADE
);
CREATE INDEX idx_group_members_group ON model_group_members(group_id);
CREATE INDEX idx_group_members_model ON model_group_members(catalog_model_id);
CREATE TRIGGER no_duplicate_group_member BEFORE INSERT ON model_group_members
WHEN EXISTS (
    SELECT 1 FROM model_group_members WHERE group_id = NEW.group_id AND catalog_model_id = NEW.catalog_model_id
)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- Printable objects. Loose ones have only file_id. One inside an archive has archive_member_id too,
-- because it isn't a file on disk. Scale and support live here: same shape, same model.
CREATE TABLE model_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_model_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    archive_member_id INTEGER,
    path TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes INTEGER,
    scale TEXT,
    supported INTEGER,
    hollow INTEGER,
    FOREIGN KEY (catalog_model_id) REFERENCES catalog_models(id) ON DELETE CASCADE,
    FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE,
    FOREIGN KEY (archive_member_id) REFERENCES file_archive_members(id) ON DELETE CASCADE
);
CREATE INDEX idx_model_artifacts_model ON model_artifacts(catalog_model_id);
CREATE INDEX idx_model_artifacts_file ON model_artifacts(file_id);
CREATE TRIGGER no_duplicate_artifact BEFORE INSERT ON model_artifacts
WHEN EXISTS (
    SELECT 1 FROM model_artifacts
    WHERE catalog_model_id = NEW.catalog_model_id AND file_id = NEW.file_id AND path = NEW.path
)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- Tags on a model, apart from model_tags, which still belongs to collections until the final rename.
CREATE TABLE catalog_model_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_model_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    FOREIGN KEY (catalog_model_id) REFERENCES catalog_models(id) ON DELETE CASCADE
);
CREATE INDEX idx_catalog_model_tags_model ON catalog_model_tags(catalog_model_id);
CREATE INDEX idx_catalog_model_tags_tag ON catalog_model_tags(tag);
CREATE TRIGGER no_duplicate_catalog_model_tag BEFORE INSERT ON catalog_model_tags
WHEN EXISTS (SELECT 1 FROM catalog_model_tags WHERE catalog_model_id = NEW.catalog_model_id AND tag = NEW.tag)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- Which collections a human has been through. A new peek reopens one.
CREATE TABLE collection_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (collection_id) REFERENCES models(id) ON DELETE CASCADE
);
CREATE INDEX idx_collection_reviews ON collection_reviews(collection_id);
CREATE TRIGGER no_duplicate_collection_review BEFORE INSERT ON collection_reviews
WHEN EXISTS (SELECT 1 FROM collection_reviews WHERE collection_id = NEW.collection_id)
BEGIN
    SELECT RAISE(ABORT, 'duplicate collection review');
END;

ALTER TABLE library_candidates ADD COLUMN group_name TEXT NOT NULL DEFAULT '';
ALTER TABLE library_candidates ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';

-- Every collection starts with one placeholder under the same ID, so browsing models shows all of
-- them from day one, and every ID in a stash or preview path already means the right thing.
INSERT INTO catalog_models (id, name, category)
SELECT id, name, primary_category FROM models;

INSERT INTO catalog_model_collections (catalog_model_id, collection_id)
SELECT id, id FROM models;

-- A placeholder shares its collection's ID, and models hands out its own IDs. Real models start far
-- above that range, or one would take the ID the next collection needs and swallow its placeholder.
INSERT INTO sqlite_sequence (name, seq)
SELECT 'catalog_models', 1000000000000 WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'catalog_models');
UPDATE sqlite_sequence SET seq = 1000000000000 WHERE name = 'catalog_models';

-- New collections arrive through old code that knows nothing of this. Runs until the final rename.
-- OR IGNORE because INSERT OR REPLACE on models removes a row without firing its delete trigger.
CREATE TRIGGER catalog_placeholder_for_new_collection AFTER INSERT ON models
BEGIN
    INSERT OR IGNORE INTO catalog_models (id, name, category) VALUES (NEW.id, NEW.name, NEW.primary_category);
    INSERT INTO catalog_model_collections (catalog_model_id, collection_id) VALUES (NEW.id, NEW.id);
END;

-- A placeholder only stands in for its collection, so it goes too, or a reused ID would collide.
-- A real model stays: it may belong to another collection, and it's worth a human's look.
CREATE TRIGGER catalog_placeholder_gone_with_collection AFTER DELETE ON models
BEGIN
    DELETE FROM catalog_models WHERE id = OLD.id AND state = 'placeholder' AND NOT EXISTS (
        SELECT 1 FROM catalog_model_collections
        WHERE catalog_model_id = OLD.id AND collection_id != OLD.id
    );
END;
