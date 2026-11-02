"""A missing mount limits what needs the files. It must never invalidate what the catalog knows."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival.api import server
from desync_archival.services.reconciler import VaultReconciler
from desync_archival.services.stash_service import StashService


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.offline = self.root / "unmounted" / "Lib"
        self.model_path = self.offline / "Golem"

        create_database(server.DB_PATH)
        self.conn = sqlite3.connect(server.DB_PATH)
        self.conn.row_factory = sqlite3.Row
        for table in ("stash_items", "stashes", "file_archive_members", "file_archive_submodels", "downloaded_files",
                      "model_files", "collections", "external_libraries"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.execute(
            "INSERT INTO collections (id, name, designer_name, source, vault_path, raw_backup_path)"
            " VALUES (100, 'Golem', 'Forge', 'external', ?, '')", (str(self.model_path),)
        )
        self.conn.execute("INSERT INTO model_files (file_id, model_id, filename, size_bytes) VALUES (9, 100, 'g.zip', 5)")
        self.conn.execute(
            "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,"
            " expected_bytes, actual_bytes, storage_tier, vault_path) VALUES (9, 100, 'g.zip', 'g.zip', 'g.zip', 5, 5, 'nas', ?)",
            (str(self.model_path / "g.zip"),),
        )
        self.conn.execute("INSERT INTO file_archive_submodels (id, file_id, model_id, name) VALUES (3, 9, 100, 'Arm')")
        self.conn.execute(
            "INSERT INTO file_archive_members (id, submodel_id, file_id, internal_path, filename, size_bytes)"
            " VALUES (4, 3, 9, 'a/arm.stl', 'arm.stl', 5)"
        )
        self.conn.execute("INSERT INTO external_libraries (id, name, path) VALUES (1, 'Lib', ?)", (str(self.offline),))
        self.conn.execute("UPDATE collections SET library_id = 1 WHERE id = 100")
        self.conn.execute("INSERT INTO stashes (id, name) VALUES (7, 'Print soon')")
        self.conn.commit()
        self.client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        self.stash = StashService(server.DB_PATH)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

class StashTests(OfflineCase):
    def test_every_item_type_can_be_added(self):
        self.stash.add_to_stash(7, "model", 100)
        self.stash.add_to_stash(7, "file", 100, file_id=9)
        self.stash.add_to_stash(7, "submodel", 100, submodel_id=3)
        self.stash.add_to_stash(7, "archive_member", 100, archive_member_id=4)
        self.assertEqual(len(self.stash.get_stash_detail(7)["items"]), 4)

    def test_bulk_add_works(self):
        response = self.client.post("/api/stashes/7/bulk-models", json={"model_ids": [100]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.stash.get_stash_detail(7)["items"]), 1)

    def test_a_model_never_archived_is_still_refused(self):
        self.conn.execute("UPDATE collections SET vault_path = NULL WHERE id = 100")
        self.conn.commit()
        with self.assertRaises(ValueError):
            self.stash.add_to_stash(7, "model", 100)

class ExportTests(OfflineCase):
    def export(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('stash_download_path', ?)", (str(self.root / "export"),)
        )
        self.conn.commit()
        return self.client.post("/api/stashes/7/export", json={"sync": True, "filter": "all"})

    def test_export_of_a_model_says_its_folder_is_offline(self):
        self.stash.add_to_stash(7, "model", 100)
        response = self.export()
        self.assertEqual(response.status_code, 400)
        self.assertIn("vault directory not found on storage", response.json()["error"])

    def test_export_of_each_file_item_says_it_is_offline(self):
        for item_type, extra in (("file", {"file_id": 9}), ("submodel", {"submodel_id": 3}), ("archive_member", {"archive_member_id": 4})):
            self.conn.execute("DELETE FROM stash_items")
            self.conn.commit()
            self.stash.add_to_stash(7, item_type, 100, **extra)
            response = self.export()
            self.assertEqual(response.status_code, 400, item_type)
            self.assertIn("File #9 not found on storage", response.json()["error"])

    def test_export_skips_an_unlinked_item_through_the_route(self):
        self.stash.add_to_stash(7, "model", 100)
        self.assertEqual(self.client.delete("/api/settings/libraries/1").status_code, 200)
        response = self.export()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total_files"], 0)


class MetadataTests(OfflineCase):
    def test_catalog_and_metadata_views_work(self):
        self.assertEqual(self.client.get("/api/models/100").status_code, 200)
        self.assertIn(100, [m["id"] for m in self.client.get("/api/models", params={"search": "Golem"}).json()["models"]])
        self.assertEqual(self.client.get("/api/models/100/files").status_code, 200)
        self.assertEqual(self.client.get("/api/settings/libraries").status_code, 200)

class ReconcileTests(OfflineCase):
    def audit(self):
        reconciler = VaultReconciler(server.DB_PATH)
        reconciler.storage.get_root_path = lambda: self.root
        return reconciler._execute_forward(self.root, "fast", lambda *_: None, lambda *_: None, lambda: False)

    def test_files_on_an_offline_library_are_not_reported_missing(self):
        report = self.audit()
        self.assertEqual(report["discrepancies"], [])
        self.assertEqual(report["summary"]["skipped_offline"], 1)

    def test_a_mounted_library_is_still_audited(self):
        self.offline.mkdir(parents=True)
        report = self.audit()
        self.assertEqual(report["summary"]["skipped_offline"], 0)
        self.assertEqual(report["discrepancies"][0]["issues"][0]["defect"], "DIRECTORY_NOT_FOUND")

    def test_a_path_that_only_shares_a_prefix_is_audited(self):
        self.conn.execute("UPDATE collections SET vault_path = ? WHERE id = 100", (f"{self.offline}2/Golem",))
        self.conn.commit()
        self.assertEqual(self.audit()["summary"]["skipped_offline"], 0)

if __name__ == "__main__":
    unittest.main()
