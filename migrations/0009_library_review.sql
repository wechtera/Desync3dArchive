-- What a preview saw in each release. The review screen reads this, so moving a card doesn't mean
-- another 8 minute walk of the share.
CREATE TABLE library_release_trees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL,
    release_name TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    folders_json TEXT NOT NULL,
    files_json TEXT NOT NULL,
    FOREIGN KEY (library_id) REFERENCES external_libraries(id) ON DELETE CASCADE
);
CREATE INDEX idx_library_release_trees ON library_release_trees(library_id, release_name);
CREATE TRIGGER no_duplicate_release_tree BEFORE INSERT ON library_release_trees
WHEN EXISTS (SELECT 1 FROM library_release_trees WHERE library_id = NEW.library_id AND release_name = NEW.release_name)
BEGIN
    SELECT RAISE(ABORT, 'duplicate release tree');
END;

-- A human's cards for a release. Kept apart from previews so a new preview can't wipe them.
CREATE TABLE library_release_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL,
    release_name TEXT NOT NULL,
    cards_json TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (library_id) REFERENCES external_libraries(id) ON DELETE CASCADE
);
CREATE INDEX idx_library_release_reviews ON library_release_reviews(library_id, release_name);
CREATE TRIGGER no_duplicate_release_review BEFORE INSERT ON library_release_reviews
WHEN EXISTS (SELECT 1 FROM library_release_reviews WHERE library_id = NEW.library_id AND release_name = NEW.release_name)
BEGIN
    SELECT RAISE(ABORT, 'duplicate release review');
END;

-- A card can own a folder except for what was carved out of it for another card. Rescans have to
-- honour that, or a carved folder's files would flow back in.
ALTER TABLE library_candidates ADD COLUMN exclude_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE library_model_folders ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0;
