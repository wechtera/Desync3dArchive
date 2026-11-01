"""The duplicate report: the hash decides, names only suggest, and nothing is changed."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival.api import server
from desync_archival.services.duplicates import find_duplicates, normalize_name


class DuplicateCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "library.db"
        create_database(self.path)
        self.conn = sqlite3.connect(self.path)
        for table in ("downloaded_files", "model_files", "models"):
            self.conn.execute(f"DELETE FROM {table}")
        for model_id, name, source in ((1, "One", "mmf"), (2, "Two", "mmf"), (3, "Three", "external")):
            self.conn.execute(
                "INSERT INTO collections (id, name, source, raw_backup_path) VALUES (?, ?, ?, '')", (model_id, name, source)
            )
        self.next_file = 100

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def add(self, model_id, filename, size, sha=None):
        self.next_file += 1
        self.conn.execute(
            "INSERT INTO model_files (file_id, model_id, filename, size_bytes) VALUES (?, ?, ?, ?)",
            (self.next_file, model_id, filename, size),
        )
        if sha:
            self.conn.execute(
                "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,"
                " actual_bytes, sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.next_file, model_id, filename, filename, filename, size, sha),
            )

    def kinds(self, **kwargs):
        return [group["kind"] for group in find_duplicates(self.conn, **kwargs)["files"]]


class FileTests(DuplicateCase):
    def test_same_hash_is_exact_whatever_the_name(self):
        self.add(1, "golem.zip", 10, "abc")
        self.add(2, "renamed.zip", 10, "abc")
        self.assertEqual(self.kinds(), ["exact"])

    def test_same_name_and_size_with_different_hashes_is_not_a_duplicate(self):
        self.add(1, "golem.zip", 10, "abc")
        self.add(2, "golem.zip", 10, "def")
        self.assertEqual(self.kinds(), [])

    def test_a_linked_library_file_can_only_be_a_candidate(self):
        self.add(1, "golem.stl", 10, "abc")
        self.add(3, "Golem.STL", 10)
        self.assertEqual(self.kinds(), ["candidate"])

    def test_same_name_with_a_different_size_is_nothing(self):
        self.add(1, "golem.zip", 10)
        self.add(2, "golem.zip", 11)
        self.assertEqual(self.kinds(), [])

    def test_files_with_no_size_are_ignored(self):
        self.add(1, "golem.zip", 0)
        self.add(2, "golem.zip", 0)
        self.assertEqual(self.kinds(), [])

    def test_a_repeat_inside_one_model_is_not_a_duplicate(self):
        self.add(1, "golem.zip", 10)
        self.add(1, "golem.zip", 10)
        self.assertEqual(self.kinds(), [])

    def test_shared_bases_are_left_out_unless_asked_for(self):
        self.add(1, "75mm Base.zip", 10, "abc")
        self.add(2, "75mm Base.zip", 10, "abc")
        self.assertEqual(self.kinds(), [])
        self.assertEqual(self.kinds(include_shared=True), ["exact"])

    def test_the_ignored_names_are_a_setting(self):
        self.conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('duplicate_ignore_names', 'golem')")
        self.add(1, "golem.zip", 10, "abc")
        self.add(2, "golem.zip", 10, "abc")
        self.add(1, "75mm Base.zip", 11, "def")
        self.add(2, "75mm Base.zip", 11, "def")
        self.assertEqual([g["filename"] for g in find_duplicates(self.conn)["files"]], ["75mm Base.zip"])

    def test_inactive_models_are_left_out(self):
        self.add(1, "golem.zip", 10, "abc")
        self.add(2, "golem.zip", 10, "abc")
        self.conn.execute("UPDATE collections SET is_active = 0 WHERE id = 2")
        self.assertEqual(self.kinds(), [])


class ModelTests(DuplicateCase):
    def test_all_files_shared_is_a_duplicate_model(self):
        self.add(1, "a.zip", 10, "aaa")
        self.add(2, "a.zip", 10, "aaa")
        groups = find_duplicates(self.conn)["models"]
        self.assertEqual([g["kind"] for g in groups], ["duplicate"])

    def test_some_files_shared_is_an_overlap(self):
        self.add(1, "a.zip", 10, "aaa")
        self.add(1, "b.zip", 20, "bbb")
        self.add(2, "a.zip", 10, "aaa")
        groups = find_duplicates(self.conn)["models"]
        self.assertEqual([(g["kind"], g["shared_files"]) for g in groups], [("overlap", 1)])


class SafetyTests(DuplicateCase):
    def test_nothing_is_changed(self):
        self.add(1, "a.zip", 10, "aaa")
        self.add(2, "a.zip", 10, "aaa")
        self.conn.commit()
        before = self.conn.total_changes
        find_duplicates(self.conn)
        self.assertEqual(self.conn.total_changes, before)

    def test_normalizing_names(self):
        self.assertEqual(normalize_name("[12.5 MB] Golem_Pose-1.ZIP"), "golem pose 1.zip")
        self.assertEqual(normalize_name("839284_Golem.zip"), "golem.zip")


class RouteTests(unittest.TestCase):
    def test_route_reports_without_touching_storage(self):
        create_database(server.DB_PATH)
        client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        response = client.get("/api/reconcile/duplicates", params={"include_shared": "1"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["summary"]["ignored_names"], [])


if __name__ == "__main__":
    unittest.main()
