import sqlite3
import tempfile
import unittest
from pathlib import Path

from db_helper import create_database


class CatalogLevelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "library.db"
        create_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.conn.close)
        self.collection = self.add_collection("Vampire Court", "1/a")

    def add_collection(self, name, key):
        with self.conn:
            return self.conn.execute(
                "INSERT INTO models (name, source, external_key, primary_category, raw_backup_path) VALUES (?, 'external', ?, 'Tabletop', '')", (name, key),
            ).lastrowid

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def test_a_new_collection_gets_a_placeholder_with_its_id(self):
        row = self.one("SELECT id, name, category, state FROM catalog_models WHERE id = ?", self.collection)
        self.assertEqual(row, (self.collection, "Vampire Court", "Tabletop", "placeholder"))
        link = self.one("SELECT collection_id FROM catalog_model_collections WHERE catalog_model_id = ?", self.collection)
        self.assertEqual(link[0], self.collection)

    def test_one_model_can_sit_in_two_collections_once_each(self):
        other = self.add_collection("Ghuls", "1/b")
        with self.conn:
            for _ in range(2):
                self.conn.execute(
                    "INSERT INTO catalog_model_collections (catalog_model_id, collection_id) VALUES (?, ?)", (self.collection, other),
                )
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_model_collections WHERE catalog_model_id = ?", self.collection)[0], 2)

    def test_a_group_name_is_unique_within_a_collection_only(self):
        other = self.add_collection("Ghuls", "1/b")
        with self.conn:
            self.conn.execute("INSERT INTO model_groups (collection_id, name) VALUES (?, 'Ghul')", (self.collection,))
            self.conn.execute("INSERT INTO model_groups (collection_id, name) VALUES (?, 'Ghul')", (other,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "duplicate model group"), self.conn:
            self.conn.execute("INSERT INTO model_groups (collection_id, name) VALUES (?, 'Ghul')", (other,))

    def test_a_repeated_artifact_is_ignored(self):
        with self.conn:
            file_id = self.conn.execute(
                "INSERT INTO model_files (model_id, filename, size_bytes, download_url, source) VALUES (?, 'a.zip', 1, '', 'external')",
                (self.collection,),
            ).lastrowid
            for _ in range(2):
                self.conn.execute(
                    "INSERT INTO model_artifacts (catalog_model_id, file_id, path) VALUES (?, ?, 'x/a.stl')", (self.collection, file_id),
                )
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_artifacts")[0], 1)

    def test_deleting_a_collection_keeps_a_model_another_collection_holds(self):
        other = self.add_collection("Ghuls", "1/b")
        with self.conn:
            self.conn.execute(
                "INSERT INTO catalog_model_collections (catalog_model_id, collection_id) VALUES (?, ?)", (self.collection, other),
            )
            self.conn.execute("DELETE FROM models WHERE id = ?", (other,))
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_models WHERE id = ?", self.collection)[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_model_collections WHERE collection_id = ?", other)[0], 0)

    def test_a_placeholder_goes_with_its_collection_and_the_id_can_return(self):
        with self.conn:
            self.conn.execute("DELETE FROM models WHERE id = ?", (self.collection,))
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_models")[0], 0)
        with self.conn:
            self.conn.execute(
                "INSERT INTO models (id, name, source, external_key, raw_backup_path) VALUES (?, 'Back', 'external', '1/a', '')",
                (self.collection,),
            )
        self.assertEqual(self.one("SELECT name FROM catalog_models WHERE id = ?", self.collection)[0], "Back")

    def test_replacing_a_row_keeps_one_placeholder(self):
        with self.conn:
            for name in ("Old", "New"):
                self.conn.execute("INSERT OR REPLACE INTO models (id, name, raw_backup_path) VALUES (50, ?, '')", (name,))
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_models WHERE id = 50")[0], 1)
