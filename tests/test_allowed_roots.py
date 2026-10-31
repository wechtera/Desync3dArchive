"""V6: the API can only browse or set paths inside DESYNC_ALLOWED_ROOTS."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival import settings
from desync_archival.api import server


class AllowedRootsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "allowed"
        self.other = self.base / "outside"
        (self.root / "inner").mkdir(parents=True)
        self.other.mkdir()
        (self.root / "link_out").symlink_to(self.other)
        create_database(server.DB_PATH)
        self.env = patch.dict(os.environ, {"DESYNC_ALLOWED_ROOTS": str(self.root)})
        self.env.start()
        self.client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def bad_paths(self):
        return [str(self.other), str(self.root / "link_out"), str(self.root / ".." / "outside"), "/etc"]


class RuleTests(AllowedRootsCase):
    def test_inside_is_allowed_and_a_shared_prefix_is_not(self):
        self.assertTrue(settings.is_allowed(self.root / "inner"))
        self.assertFalse(settings.is_allowed(self.base / "allowed_more"))

    def test_default_root_is_the_data_directory(self):
        with patch.dict(os.environ, {"DESYNC_ALLOWED_ROOTS": ""}):
            self.assertEqual(settings.allowed_roots(), [settings.data_dir()])

    def test_roots_can_not_be_set_over_the_network(self):
        self.client.post("/api/settings", json={"DESYNC_ALLOWED_ROOTS": "/", "allowed_roots": "/"})
        self.assertEqual(settings.allowed_roots(), [self.root])


class BrowseTests(AllowedRootsCase):
    def test_outside_folders_are_refused(self):
        for path in self.bad_paths():
            self.assertEqual(self.client.get("/api/system/browse", params={"path": path}).status_code, 403, path)

    def test_default_starts_at_the_root_with_no_parent(self):
        data = self.client.get("/api/system/browse").json()
        self.assertEqual(data["current_path"], str(self.root))
        self.assertIsNone(data["parent_path"])

    def test_inside_folder_lists_and_has_a_parent(self):
        data = self.client.get("/api/system/browse", params={"folder": str(self.root / "inner")}).json()
        self.assertEqual(data["parent_path"], str(self.root))


class SettingPathTests(AllowedRootsCase):
    def test_settings_refuse_outside_paths(self):
        for key in ("download_root_path", "download_work_path", "stash_download_path", "metadata_backup_path"):
            for path in self.bad_paths():
                self.assertEqual(self.client.post("/api/settings", json={key: path}).status_code, 400, f"{key} {path}")

    def test_relative_path_escaping_the_data_directory_is_refused(self):
        response = self.client.post("/api/settings", json={"metadata_backup_path": "../../outside"})
        self.assertEqual(response.status_code, 400)

    def test_storage_config_refuses_without_creating_anything(self):
        target = self.other / "new_vault"
        response = self.client.post("/api/storage/config", json={"download_root_path": str(target)})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(target.exists())
        self.assertEqual(self.client.post("/api/storage/config", json={"download_work_path": "/etc"}).status_code, 400)

    def test_library_add_refuses_outside_paths(self):
        for path in self.bad_paths():
            self.assertEqual(self.client.post("/api/settings/libraries", json={"path": path}).status_code, 400, path)

    def test_paths_inside_the_roots_still_save(self):
        inner = str(self.root / "inner")
        self.assertEqual(self.client.post("/api/settings", json={"stash_download_path": inner}).status_code, 200)
        self.assertEqual(self.client.post("/api/storage/config", json={"download_root_path": inner}).status_code, 200)
