"""Tests for the settings module and the production guard."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desync_archival import settings
from desync_archival.api.routes.settings import (
    handle_post_external_library,
    handle_post_settings,
)
from desync_archival.api.routes.storage import handle_post_storage_config

REPO_ROOT = Path(__file__).resolve().parent.parent


class GuardBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.production = self.base / "production"
        self.safe = self.base / "safe"
        self.production.mkdir()
        self.safe.mkdir()
        self.env = patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "DESYNC_DATA_DIR": str(self.safe),
                "DESYNC_PROTECTED_PATHS": str(self.production),
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()


class ProductionGuardTests(GuardBase):
    def test_protected_path_and_children_are_refused(self):
        for path in (self.production, self.production / "a" / "b.txt"):
            with self.assertRaises(settings.ProductionPathError):
                settings.check_path(path)

    def test_default_locations_are_protected(self):
        with self.assertRaises(settings.ProductionPathError):
            settings.check_path(REPO_ROOT.parent / "scrapepage_deprecated" / "library.db")
        with self.assertRaises(settings.ProductionPathError):
            settings.check_path(Path.home() / "DESYNC_Data")

    def test_sibling_sharing_a_prefix_is_allowed(self):
        settings.check_path(Path.home() / "DESYNC_Data_dev")
        sibling = self.base / "production_dev"
        settings.check_path(sibling)

    def test_symlink_into_production_is_refused(self):
        link = self.safe / "link"
        link.symlink_to(self.production)
        with self.assertRaises(settings.ProductionPathError):
            settings.check_path(link / "file")

    def test_relative_path_resolving_into_production_is_refused(self):
        previous = Path.cwd()
        os.chdir(self.safe)
        try:
            with self.assertRaises(settings.ProductionPathError):
                settings.check_path(Path("../production/x"))
        finally:
            os.chdir(previous)

    def test_production_mode_allows_production_paths(self):
        with patch.dict(os.environ, {"APP_ENV": "production"}):
            settings.check_path(self.production)

    def test_data_dir_in_production_location_is_refused(self):
        with patch.dict(os.environ, {"DESYNC_DATA_DIR": str(self.production / "data")}), self.assertRaises(settings.ProductionPathError):
            settings.data_dir()


class RequiredSettingsTests(GuardBase):
    def test_data_dir_is_required(self):
        with patch.dict(os.environ, {"DESYNC_DATA_DIR": ""}), self.assertRaises(settings.SettingsError):
            settings.data_dir()

    def test_data_dir_must_be_absolute(self):
        with patch.dict(os.environ, {"DESYNC_DATA_DIR": "relative/path"}), self.assertRaises(settings.SettingsError):
            settings.data_dir()

    def test_app_env_is_required(self):
        with patch.dict(os.environ, {"APP_ENV": ""}), self.assertRaises(settings.SettingsError):
            settings.app_env()

    def test_development_requires_protected_paths(self):
        with patch.dict(os.environ, {"DESYNC_PROTECTED_PATHS": ""}), self.assertRaises(settings.SettingsError):
            settings.data_dir()

    def test_paths_come_from_the_data_directory(self):
        self.assertEqual(settings.db_path(), self.safe / "library.db")
        self.assertEqual(settings.previews_dir(), self.safe / "previews")
        self.assertEqual(settings.metadata_dir(), self.safe / "metadata")
        self.assertEqual(settings.logs_dir(), self.safe / "logs")

    def test_relative_stored_path_resolves_against_data_directory(self):
        self.assertEqual(settings.resolve_data_path("metadata/m.json"), self.safe / "metadata/m.json")
        self.assertEqual(settings.resolve_data_path("/abs/m.json"), Path("/abs/m.json"))


class StoredPathTests(GuardBase):
    def setUp(self):
        super().setUp()
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        self.conn.execute("CREATE TABLE external_libraries (id INTEGER PRIMARY KEY, path TEXT, is_active INTEGER NOT NULL DEFAULT 1)")

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_clean_database_passes(self):
        self.conn.execute("INSERT INTO app_settings (key, value) VALUES ('metadata_backup_path', 'metadata')")
        settings.check_stored_paths(self.conn)

    def test_setting_pointing_at_production_is_refused(self):
        self.conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('download_root_path', ?)", (str(self.production),)
        )
        with self.assertRaises(settings.ProductionPathError):
            settings.check_stored_paths(self.conn)

    def test_an_unlinked_library_does_not_block_startup(self):
        self.conn.execute("INSERT INTO external_libraries (path, is_active) VALUES (?, 0)", (str(self.production / "lib"),))
        settings.check_stored_paths(self.conn)

    def test_external_library_pointing_at_production_is_refused(self):
        self.conn.execute("INSERT INTO external_libraries (path) VALUES (?)", (str(self.production / "lib"),))
        with self.assertRaises(settings.ProductionPathError):
            settings.check_stored_paths(self.conn)


class RouteGuardTests(GuardBase):
    def setUp(self):
        super().setUp()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_post_settings_refuses_production_path(self):
        body = {"stash_download_path": str(self.production / "stash")}
        result, status = handle_post_settings(self.conn, None, body, {})
        self.assertEqual(status, 400)
        self.assertIn("Refusing production path", result["error"])

    def test_storage_config_refuses_without_creating_the_directory(self):
        target = self.production / "new_vault"
        result, status = handle_post_storage_config(self.conn, None, {"download_root_path": str(target)}, {})
        self.assertEqual(status, 400)
        self.assertIn("Refusing production path", result["error"])
        self.assertFalse(target.exists())

    def test_library_add_refuses_production_path(self):
        result, status = handle_post_external_library(self.conn, None, {"path": str(self.production)}, {})
        self.assertEqual(status, 400)
        self.assertIn("Refusing production path", result["error"])


class ProcessRefusalTests(unittest.TestCase):
    def _import_app(self, data_dir: str, app_env: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "APP_ENV": app_env, "DESYNC_DATA_DIR": data_dir, "DESYNC_PROTECTED_PATHS": "/tmp/x"}
        return subprocess.run(
            [sys.executable, "-c", "from desync_archival.api import server"],
            check=False, capture_output=True, text=True, env=env, timeout=30,
        )

    def test_app_refuses_to_import_with_production_data_dir(self):
        result = self._import_app(str(REPO_ROOT.parent / "scrapepage_deprecated"), "development")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ProductionPathError", result.stderr)


if __name__ == "__main__":
    unittest.main()
