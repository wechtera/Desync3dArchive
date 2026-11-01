"""Raw SQL for services/analytics.py. Names with braces take format() values."""

UPDATE_COLLECTIONS_RELEASE_MONTH = """
            UPDATE collections 
            SET release_month = substr(published_at, 1, 7)
            WHERE (release_month IS NULL OR release_month = '') 
              AND published_at IS NOT NULL;
        """

UPDATE_COLLECTIONS_IS_BONUS_BY_LOWER = """
            UPDATE collections
            SET is_bonus = 1
            WHERE LOWER(name) LIKE '%reward%'
               OR LOWER(name) LIKE '%bonus%'
               OR id IN (
                   SELECT model_id FROM collection_tags 
                   WHERE LOWER(tag) LIKE '%reward%' OR LOWER(tag) LIKE '%bonus%'
               );
        """

# external_indexer owns the external_ rows, and nothing here can rebuild them.
DELETE_RELEASES = "DELETE FROM releases WHERE release_key NOT LIKE 'external\\_%' ESCAPE '\\';"

# The one definition of a release key. A designer with no ID falls back to their name, because a
# shared 0_ prefix would give two such designers one key and the duplicate trigger aborts the rebuild.
RELEASE_KEY = "COALESCE(m.designer_id, 'name:' || COALESCE(m.designer_name, 'Unknown')) || '_' || m.release_month"
RELEASE_FILTER = "m.is_active = 1 AND m.source = 'mmf' AND m.release_month IS NOT NULL AND m.release_month != ''"

# Files and tags are separate on purpose. Joined together, each file counts once per tag.
SELECT_RELEASE_CLUSTERS = f"""
            SELECT
                {RELEASE_KEY} AS release_id,
                MAX(m.designer_name) AS designer_name,
                MAX(m.designer_id) AS designer_id,
                m.release_month,
                SUM(CASE WHEN m.is_bonus = 0 THEN 1 ELSE 0 END) AS regular_model_count,
                SUM(CASE WHEN m.is_bonus = 1 THEN 1 ELSE 0 END) AS bonus_model_count,
                COALESCE(SUM(f.file_count), 0) AS total_files,
                COALESCE(SUM(f.total_bytes), 0) AS total_size_bytes,
                MAX(m.primary_image_url) AS sample_image_url,
                GROUP_CONCAT(m.description) AS all_descriptions
            FROM collections m
            LEFT JOIN (
                SELECT model_id, COUNT(*) AS file_count, SUM(size_bytes) AS total_bytes
                FROM model_files GROUP BY model_id
            ) f ON f.model_id = m.id
            WHERE {RELEASE_FILTER}
            GROUP BY release_id;
        """

SELECT_RELEASE_TAGS = f"""
            SELECT {RELEASE_KEY} AS release_id, GROUP_CONCAT(DISTINCT t.tag) AS all_tags
            FROM collections m JOIN collection_tags t ON t.model_id = m.id
            WHERE {RELEASE_FILTER}
            GROUP BY release_id;
        """

INSERT_RELEASES = """
            INSERT INTO releases (
                release_key, designer_name, designer_id, release_month,
                model_count, bonus_count, total_files, total_size_bytes,
                is_likely_bundle, sample_image_url, common_tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

SELECT_RELEASES_BY_DESIGNER_NAME = """
            SELECT release_month, model_count, bonus_count 
            FROM releases 
            WHERE designer_name = ? 
            ORDER BY release_month ASC;
        """

COUNT_COLLECTIONS_BY_DESIGNER_NAME = "SELECT COUNT(*) FROM collections WHERE designer_name = ? AND is_bonus = 1 AND is_active = 1;"

SELECT_RELEASES = """
            SELECT release_month, SUM(model_count) AS model_count, SUM(bonus_count) AS bonus_count
            FROM releases 
            GROUP BY release_month
            ORDER BY release_month ASC;
        """

COUNT_COLLECTIONS_BY_IS_BONUS = "SELECT COUNT(*) FROM collections WHERE is_bonus = 1 AND is_active = 1;"
