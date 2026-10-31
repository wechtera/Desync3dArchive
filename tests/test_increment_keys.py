"""The schema rule: an increment column is the key of every table, and nothing else is unique."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from db_helper import create_database

from desync_archival import migrations

EXEMPT = {"app_settings"}


def user_tables(conn):
    rows = conn.execute("""
        SELECT name, sql FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'catalog_search%'
    """).fetchall()
    return [(name, sql) for name, sql in rows if name not in EXEMPT]


class SchemaRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "library.db"
        create_database(path)
        cls.conn = sqlite3.connect(path)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def test_every_table_has_one_increment_key(self):
        for table, sql in user_tables(self.conn):
            keys = [row[1] for row in self.conn.execute(f'PRAGMA table_info("{table}")') if row[5]]
            self.assertEqual(len(keys), 1, table)
            self.assertIn("AUTOINCREMENT", sql, table)

    def test_nothing_else_is_unique(self):
        for table, sql in user_tables(self.conn):
            self.assertNotIn("UNIQUE", sql.upper(), table)
            for row in self.conn.execute(f'PRAGMA index_list("{table}")'):
                self.assertEqual(row[2], 0, f"{table}: {row[1]}")


class RulesCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "library.db"
        create_database(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "INSERT INTO models (id, name, source, external_key, raw_backup_path) VALUES (10, 'A', 'mmf', '500', '')"
        )
        self.conn.execute(
            "INSERT INTO models (id, name, source, vault_path, raw_backup_path) VALUES (11, 'B', 'external', '/nas/B', '')"
        )
        self.conn.execute("INSERT INTO model_files (file_id, model_id, source, external_key) VALUES (20, 10, 'mmf', '900')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def refused(self, sql, params=()):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(sql, params)
        self.conn.rollback()


class DuplicateTriggerTests(RulesCase):
    def test_same_key_and_source_is_refused(self):
        self.refused("INSERT INTO models (name, source, external_key, raw_backup_path) VALUES ('C', 'mmf', '500', '')")

    def test_same_key_from_another_source_is_allowed(self):
        self.conn.execute("INSERT INTO models (name, source, external_key, raw_backup_path) VALUES ('C', 'other', '500', '')")

    def test_many_models_may_have_no_key(self):
        for name in ("C", "D"):
            self.conn.execute(
                "INSERT INTO models (name, source, vault_path, raw_backup_path) VALUES (?, 'external', ?, '')", (name, f"/nas/{name}")
            )

    def test_changing_a_key_onto_another_row_is_refused(self):
        self.conn.execute("INSERT INTO models (id, name, source, external_key, raw_backup_path) VALUES (12, 'C', 'mmf', '501', '')")
        self.refused("UPDATE models SET external_key = '500' WHERE id = 12")

    def test_a_row_may_keep_its_own_key_on_update(self):
        self.conn.execute("UPDATE models SET external_key = '500', name = 'New' WHERE id = 10")

    def test_same_library_path_is_refused(self):
        self.refused("INSERT INTO models (name, source, vault_path, raw_backup_path) VALUES ('C', 'external', '/nas/B', '')")

    def test_mmf_models_may_share_a_path_rule_free(self):
        for key in ("1", "2"):
            self.conn.execute(
                "INSERT INTO models (name, source, external_key, vault_path, raw_backup_path) VALUES ('C', 'mmf', ?, '/v', '')", (key,)
            )

    def test_same_file_key_is_refused(self):
        self.refused("INSERT INTO model_files (model_id, source, external_key) VALUES (10, 'mmf', '900')")

    def test_a_file_can_only_be_downloaded_once(self):
        insert = """INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename,
                    local_relative_path, actual_bytes) VALUES (20, 10, 'a', 'a', 'a', 1)"""
        self.conn.execute(insert)
        self.refused(insert)

    def test_a_model_has_one_job(self):
        insert = "INSERT INTO download_jobs (model_id, status) VALUES (10, 'pending')"
        self.conn.execute(insert)
        self.refused(insert)

    def test_a_path_has_one_library(self):
        insert = "INSERT INTO external_libraries (name, path) VALUES ('L', '/nas')"
        self.conn.execute(insert)
        self.refused(insert)

    def test_a_release_key_is_used_once(self):
        insert = """INSERT INTO releases (release_key, designer_name, release_month, model_count, total_files,
                    total_size_bytes) VALUES ('x_1', 'x', '1', 1, 0, 0)"""
        self.conn.execute(insert)
        self.refused(insert)

    def test_a_stash_holds_an_item_once(self):
        self.conn.execute("INSERT INTO stashes (id, name) VALUES (1, 'S')")
        insert = "INSERT INTO stash_items (stash_id, item_type, model_id) VALUES (1, 'model', 10)"
        self.conn.execute(insert)
        self.refused(insert)

    def test_a_repeated_tag_is_skipped_quietly(self):
        for _ in range(2):
            self.conn.execute("INSERT OR IGNORE INTO model_tags (model_id, tag) VALUES (10, 'x')")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM model_tags").fetchone()[0], 1)


class KeyTests(RulesCase):
    def test_a_deleted_key_never_comes_back(self):
        self.conn.execute("DELETE FROM models WHERE id = 11")
        cursor = self.conn.execute("INSERT INTO models (name, raw_backup_path) VALUES ('New', '')")
        self.assertGreater(cursor.lastrowid, 11)

    def test_deleting_a_file_removes_its_download_record(self):
        self.conn.execute(
            "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename,"
            " local_relative_path, actual_bytes) VALUES (20, 10, 'a', 'a', 'a', 1)"
        )
        self.conn.execute("DELETE FROM model_files WHERE file_id = 20")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM downloaded_files").fetchone()[0], 0)

    def test_search_index_follows_the_rebuilt_tables(self):
        self.conn.execute("UPDATE models SET name = 'Wyvern' WHERE id = 10")
        self.conn.execute("INSERT INTO model_tags (model_id, tag) VALUES (10, 'beast')")
        self.assertEqual(self.conn.execute("SELECT rowid FROM catalog_search WHERE name LIKE '%wyv%'").fetchone()[0], 10)
        self.assertEqual(self.conn.execute("SELECT rowid FROM catalog_search WHERE tags LIKE '%beast%'").fetchone()[0], 10)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "library.db"
        self.conn = sqlite3.connect(self.path)
        steps = migrations.load_migrations()
        self.last = steps[-1]
        version_seven = dict(steps)[7]
        self.seven = version_seven
        for number, file in steps:
            if number < 7:
                migrations._apply_file(self.conn, number, file)
        self.conn.execute(
            "INSERT INTO models (id, name, source, external_key, raw_backup_path) VALUES (863982, 'A', 'mmf', '863982', '')"
        )
        self.conn.execute(
            "INSERT INTO models (id, name, source, raw_backup_path) VALUES (2000000024, 'B', 'external', '')"
        )
        self.conn.execute("INSERT INTO model_files (file_id, model_id, source, external_key) VALUES (563, 863982, 'mmf', '563')")
        self.conn.execute("INSERT INTO model_tags (model_id, tag) VALUES (863982, 'dragon')")
        self.conn.execute(
            "INSERT INTO releases (id, designer_name, designer_id, release_month, model_count, total_files, total_size_bytes)"
            " VALUES ('362414_2021-05', 'D', 362414, '2021-05', 1, 0, 0)"
        )
        self.conn.execute("INSERT INTO download_jobs (id, model_id, status) VALUES (55, 863982, 'completed')")
        self.conn.execute("INSERT INTO stashes (id, name) VALUES (1, 'S')")
        self.conn.execute("INSERT INTO stash_items (stash_id, item_type, model_id) VALUES (1, 'model', 863982)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_values_survive_and_new_keys_continue_above_them(self):
        migrations._apply_file(self.conn, 7, self.seven)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(self.conn.execute("SELECT external_key FROM models WHERE id = 863982").fetchone()[0], "863982")
        self.assertEqual(self.conn.execute("SELECT release_key FROM releases").fetchone()[0], "362414_2021-05")
        self.assertEqual(self.conn.execute("SELECT id FROM download_jobs").fetchone()[0], 55)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.conn.execute("SELECT rowid FROM catalog_search WHERE tags LIKE '%dragon%'").fetchone()[0], 863982)
        cursor = self.conn.execute("INSERT INTO models (name, raw_backup_path) VALUES ('New', '')")
        self.assertEqual(cursor.lastrowid, 2000000025)

    def test_the_stash_keeps_its_item(self):
        migrations._apply_file(self.conn, 7, self.seven)
        self.assertEqual(self.conn.execute("SELECT model_id FROM stash_items").fetchone()[0], 863982)

    def test_a_failed_rebuild_changes_nothing(self):
        broken = Path(self.temp.name) / "0007_increment_keys.sql"
        text = self.seven.read_text().replace("PRAGMA user_version = 7;\nCOMMIT;", "SELECT * FROM no_such_table;\nCOMMIT;")
        broken.write_text(text)
        with self.assertRaises(sqlite3.Error):
            migrations._apply_file(self.conn, 7, broken)
        self.assertEqual(migrations.current_version(self.conn), 6)
        sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'models'").fetchone()[0]
        self.assertNotIn("AUTOINCREMENT", sql)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM models").fetchone()[0], 2)

    def test_real_migration_refuses_broken_references(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("INSERT INTO model_tags (model_id, tag) VALUES (999, 'orphan')")
        self.conn.commit()
        self.conn.close()
        with self.assertRaises(migrations.MigrationError):
            migrations.migrate_database(self.path)
        self.conn = sqlite3.connect(self.path)

    def test_full_migration_with_backup_keeps_row_counts(self):
        self.conn.close()
        result = migrations.migrate_database(self.path)
        self.assertEqual(result["to"], migrations.latest_version())
        self.conn = sqlite3.connect(self.path)
        shutil.copy(self.path, self.path.with_suffix(".copy"))


if __name__ == "__main__":
    unittest.main()
