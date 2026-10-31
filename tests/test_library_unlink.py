"""Unlinking a library flags its contents inactive, and relinking brings the same rows back."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival.api import server
from desync_archival.services.external_indexer import (
    ExternalLibraryIndexer,
    deactivate_library,
)
from desync_archival.services.stash_exporter import StashExporter
from desync_archival.services.stash_service import StashService


class UnlinkCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.library = self.root / "Lib"
        self.model_dir = self.library / "2024-01 - Drop" / "Golem"
        self.model_dir.mkdir(parents=True)
        (self.model_dir / "golem.stl").write_bytes(b"solid")

        create_database(server.DB_PATH)
        self.conn = sqlite3.connect(server.DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        for table in ("stash_items", "stashes", "models", "external_libraries", "releases"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.execute(
            "INSERT INTO models (id, name, designer_name, source, vault_path, raw_backup_path, release_month)"
            " VALUES (100, 'Golem', 'Forge', 'external', ?, '', '2024-01')", (str(self.model_dir),)
        )
        self.conn.execute(
            "INSERT INTO models (id, name, designer_name, source, vault_path, raw_backup_path, release_month)"
            " VALUES (101, 'Sibling', 'Forge', 'external', ?, '', '2024-02')", (str(self.root / "Lib2" / "Sibling"),)
        )
        self.conn.execute(
            "INSERT INTO models (id, name, designer_name, raw_backup_path) VALUES (50, 'MMF Golem', 'Other', '')"
        )
        self.conn.execute("INSERT INTO external_libraries (id, name, path) VALUES (1, 'Lib', ?)", (str(self.library),))
        self.conn.execute(
            "INSERT INTO releases (id, designer_name, designer_id, release_month, model_count, total_files, total_size_bytes)"
            " VALUES ('external_Forge_2024-01', 'Forge', 0, '2024-01', 1, 0, 0)"
        )
        self.conn.execute("INSERT INTO stashes (id, name) VALUES (7, 'Print soon')")
        self.conn.execute("INSERT INTO stash_items (stash_id, item_type, model_id) VALUES (7, 'model', 100)")
        self.conn.commit()
        self.client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def unlink(self):
        response = self.client.delete("/api/settings/libraries/1")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def active(self, model_id):
        return self.conn.execute("SELECT is_active FROM models WHERE id = ?", (model_id,)).fetchone()[0]

    def catalog_ids(self):
        return {model["id"] for model in self.client.get("/api/models", params={"limit": 150}).json()["models"]}


class UnlinkTests(UnlinkCase):
    def test_contents_are_flagged_and_nothing_is_deleted(self):
        self.assertEqual(self.unlink()["path"], str(self.library))
        self.assertEqual(self.active(100), 0)
        self.assertEqual(self.conn.execute("SELECT is_active FROM external_libraries").fetchone()[0], 0)

    def test_only_that_library_is_flagged(self):
        self.unlink()
        self.assertEqual(self.active(50), 1)
        self.assertEqual(self.active(101), 1)

    def test_a_path_that_only_shares_a_prefix_is_not_flagged(self):
        self.conn.execute("UPDATE models SET vault_path = ? WHERE id = 101", (f"{self.library}2/Sibling",))
        self.conn.commit()
        self.unlink()
        self.assertEqual(self.active(101), 1)

    def test_files_on_disk_are_untouched(self):
        self.unlink()
        self.assertTrue((self.model_dir / "golem.stl").is_file())

    def test_inactive_models_leave_the_catalog_and_search(self):
        self.assertIn(100, self.catalog_ids())
        self.unlink()
        self.assertNotIn(100, self.catalog_ids())
        found = self.client.get("/api/models", params={"search": "Golem"}).json()["models"]
        self.assertEqual([model["id"] for model in found], [50])

    def test_an_inactive_model_still_opens_by_id_and_says_so(self):
        self.unlink()
        response = self.client.get("/api/models/100")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["is_active"], 0)
        self.assertEqual(self.client.get("/api/models/100/files").status_code, 200)

    def test_metrics_stop_counting_them(self):
        before = self.client.get("/api/metrics").json()["total_models"]
        self.unlink()
        self.assertEqual(self.client.get("/api/metrics").json()["total_models"], before - 1)

    def test_the_library_leaves_the_list_and_can_not_be_scanned(self):
        self.unlink()
        self.assertEqual(self.client.get("/api/settings/libraries").json()["libraries"], [])
        self.assertEqual(self.client.delete("/api/settings/libraries/1").status_code, 404)
        self.assertEqual(self.client.post("/api/settings/libraries/1/scan").status_code, 404)

    def test_a_release_with_no_active_models_is_hidden(self):
        self.unlink()
        self.assertEqual(self.conn.execute("SELECT is_active FROM releases").fetchone()[0], 0)

    def test_a_release_shared_with_an_active_model_stays(self):
        self.conn.execute("UPDATE models SET release_month = '2024-01' WHERE id = 101")
        self.conn.commit()
        self.unlink()
        self.assertEqual(self.conn.execute("SELECT is_active FROM releases").fetchone()[0], 1)


class StashTests(UnlinkCase):
    def test_stash_items_stay_and_are_marked_inactive(self):
        service = StashService(server.DB_PATH)
        self.assertFalse(service.get_stash_detail(7)["items"][0]["is_inactive"])
        self.unlink()
        items = service.get_stash_detail(7)["items"]
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["is_inactive"])

    def test_inactive_models_can_not_be_added(self):
        self.unlink()
        with self.assertRaises(ValueError):
            StashService(server.DB_PATH).add_to_stash(7, "model", 100)

    def test_export_skips_inactive_items_and_says_so(self):
        self.unlink()
        messages = []
        export_root = self.root / "export"
        exporter = StashExporter(server.DB_PATH, export_root, self.root / "work", self.root / "previews")
        exporter.export_stash(7, log_cb=messages.append)

        manifest = json.loads(next(export_root.glob("*/manifest.json")).read_text())
        self.assertEqual(manifest["skipped_inactive_items"], 1)
        self.assertEqual(manifest["items"], [])
        self.assertTrue(any("library is unlinked" in message for message in messages))


class RelinkTests(UnlinkCase):
    def test_relinking_keeps_the_same_ids_and_stash_items(self):
        self.unlink()
        response = self.client.post(
            "/api/settings/libraries", json={"name": "Lib", "path": str(self.library), "default_creator": "Forge"}
        )
        self.assertLess(response.status_code, 300, response.text)
        self.assertEqual(self.conn.execute("SELECT is_active FROM external_libraries").fetchone()[0], 1)

        indexer = ExternalLibraryIndexer(server.DB_PATH)
        indexer.previews_root = self.root / "previews"
        indexer.scan_and_index_library(1)

        self.assertEqual(self.active(100), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM models WHERE source = 'external'").fetchone()[0], 2)
        self.assertFalse(StashService(server.DB_PATH).get_stash_detail(7)["items"][0]["is_inactive"])


class IndexerTests(UnlinkCase):
    def test_indexer_connections_enforce_foreign_keys(self):
        conn = ExternalLibraryIndexer(server.DB_PATH)._get_db()
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        conn.close()

    def test_the_old_method_only_flags_too(self):
        result = ExternalLibraryIndexer(server.DB_PATH).unlink_library(1)
        self.assertTrue(result["success"])
        self.assertEqual(self.active(100), 0)

    def test_deactivate_reports_the_models_it_flagged(self):
        self.assertEqual(deactivate_library(self.conn, 1, str(self.library)), [100])


if __name__ == "__main__":
    unittest.main()
