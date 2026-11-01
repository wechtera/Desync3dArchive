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

DELETE_RELEASES = "DELETE FROM releases;"

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
