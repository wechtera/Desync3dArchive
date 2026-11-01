-- A review can point a renamed folder at the inactive model it used to be.
ALTER TABLE library_candidates ADD COLUMN relink_id INTEGER;

-- The list of tags a person can choose from. blocked means deleted on purpose, so an import that
-- brings the same word again adds nothing.
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_tags_name ON tags(name);
CREATE TRIGGER no_duplicate_tag_name BEFORE INSERT ON tags
WHEN EXISTS (SELECT 1 FROM tags WHERE name = NEW.name)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- One tag taken off one collection. Scans add tags, so without this a rescan would put it back.
CREATE TABLE removed_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE
);
CREATE INDEX idx_removed_tags ON removed_tags(collection_id, tag);
CREATE TRIGGER no_duplicate_removed_tag BEFORE INSERT ON removed_tags
WHEN EXISTS (SELECT 1 FROM removed_tags WHERE collection_id = NEW.collection_id AND tag = NEW.tag)
BEGIN
    SELECT RAISE(IGNORE);
END;

INSERT INTO tags (name)
SELECT DISTINCT lower(replace(trim(tag), ' ', '-')) FROM collection_tags WHERE trim(tag) != ''
UNION SELECT DISTINCT lower(replace(trim(tag), ' ', '-')) FROM model_tags WHERE trim(tag) != '';

-- In triggers, so every importer follows the rules without having to remember them.
CREATE TRIGGER collection_tag_respects_removal BEFORE INSERT ON collection_tags
WHEN EXISTS (SELECT 1 FROM removed_tags WHERE collection_id = NEW.model_id AND tag = NEW.tag)
  OR EXISTS (SELECT 1 FROM tags WHERE name = NEW.tag AND blocked = 1)
BEGIN
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER model_tag_respects_block BEFORE INSERT ON model_tags
WHEN EXISTS (SELECT 1 FROM tags WHERE name = NEW.tag AND blocked = 1)
BEGIN
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER collection_tag_registers AFTER INSERT ON collection_tags
BEGIN
    INSERT INTO tags (name) VALUES (NEW.tag);
END;
CREATE TRIGGER model_tag_registers AFTER INSERT ON model_tags
BEGIN
    INSERT INTO tags (name) VALUES (NEW.tag);
END;
