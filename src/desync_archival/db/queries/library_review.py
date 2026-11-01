"""Raw SQL for services/library_review.py. Names with braces take format() values."""

SELECT_LIBRARY_RELEASE_TREES_BY_LIBRARY_ID = "SELECT folders_json, files_json FROM library_release_trees WHERE library_id = ? AND release_name = ?;"

UPDATE_LIBRARY_RELEASE_TREES_RUN_ID_BY_LIBRARY_ID = "UPDATE library_release_trees SET run_id = ?, folders_json = ?, files_json = ? WHERE library_id = ? AND release_name = ?;"

INSERT_LIBRARY_RELEASE_TREES = "INSERT INTO library_release_trees (run_id, folders_json, files_json, library_id, release_name) VALUES (?, ?, ?, ?, ?);"

SELECT_LIBRARY_RELEASE_REVIEWS_BY_LIBRARY_ID = "SELECT release_name, cards_json, reviewed FROM library_release_reviews WHERE library_id = ?;"

UPDATE_LIBRARY_RELEASE_REVIEWS_CARDS_JSON_BY_LIBRARY_ID = (
    "UPDATE library_release_reviews SET cards_json = ?, reviewed = ?, updated_at = CURRENT_TIMESTAMP"
    " WHERE library_id = ? AND release_name = ?;"
)

INSERT_LIBRARY_RELEASE_REVIEWS = "INSERT INTO library_release_reviews (library_id, release_name, cards_json, reviewed) VALUES (?, ?, ?, ?);"

SELECT_COLLECTIONS_EXTERNAL_KEY_BY_LIBRARY_ID = "SELECT external_key FROM collections WHERE library_id = ? AND external_key IS NOT NULL;"

SELECT_LIBRARY_MODEL_FOLDERS_REL_PATH_BY_LIBRARY_ID = (
    "SELECT f.rel_path FROM library_model_folders f JOIN collections m ON m.id = f.model_id"
    " WHERE m.library_id = ? AND f.excluded = 0;"
)

SELECT_LIBRARY_CANDIDATES_BY_LIBRARY_ID = "SELECT rel_path, folders_json FROM library_candidates WHERE library_id = ? AND release_name = ? AND status IN (?, ?);"

SELECT_LIBRARY_CANDIDATES_BY_LIBRARY_ID_RELEASE_NAME = "SELECT rel_path, status FROM library_candidates WHERE library_id = ? AND release_name = ? AND status IN (?, ?);"

DELETE_LIBRARY_CANDIDATES_BY_LIBRARY_ID = "DELETE FROM library_candidates WHERE library_id = ? AND release_name = ? AND status IN (?, ?);"

INSERT_LIBRARY_CANDIDATES = (
    "INSERT INTO library_candidates (library_id, run_id, rel_path, release_name, name, category, folders_json,"
    " exclude_json, status, group_name, tags_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);"
)

SELECT_LIBRARY_SCAN_RUNS_BY_LIBRARY_ID = (
    "SELECT id, report_json FROM library_scan_runs WHERE library_id = ? AND mode = 'preview' AND status = 'completed'"
    " ORDER BY id DESC LIMIT 1;"
)

DELETE_LIBRARY_RELEASE_REVIEWS_BY_LIBRARY_ID = "DELETE FROM library_release_reviews WHERE library_id = ? AND release_name = ?;"

UPDATE_LIBRARY_CANDIDATES_STATUS_BY_LIBRARY_ID = "UPDATE library_candidates SET status = ? WHERE library_id = ? AND rel_path = ? AND status = ?;"

COUNT_LIBRARY_CANDIDATES_BY_LIBRARY_ID = "SELECT COUNT(*) FROM library_candidates WHERE library_id = ? AND status = ?;"

SELECT_LIBRARY_CANDIDATES_REL_PATH_BY_LIBRARY_ID = "SELECT rel_path FROM library_candidates WHERE library_id = ? AND status IN (?, ?);"
