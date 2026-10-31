"""Secrets: the file wins, writes go to the file, and the database fallback lasts the transition."""

import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival import backup, settings
from desync_archival.api import server
from desync_archival.services import auth_service, login, secret_store
from desync_archival.services.downloader import DownloadManager
from desync_archival.services.task_manager import TaskManager


class SecretCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "library.db"
        create_database(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.stored("mmf_access_token", "db-token")
        self.stored("cookie_phpsessid", "db-cookie")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def stored(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def database_value(self, key):
        return self.conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()[0]


class ReadTests(SecretCase):
    def test_a_key_not_in_the_file_falls_back_to_the_database(self):
        self.assertEqual(auth_service.get_setting(self.conn, "mmf_access_token"), "db-token")

    def test_the_file_wins(self):
        secret_store.save({"mmf_access_token": "file-token"})
        self.assertEqual(auth_service.get_setting(self.conn, "mmf_access_token"), "file-token")

    def test_a_cleared_value_in_the_file_is_respected(self):
        secret_store.save({"mmf_access_token": ""})
        self.assertEqual(auth_service.get_setting(self.conn, "mmf_access_token"), "")

    def test_other_settings_ignore_the_file(self):
        self.stored("batch_size", "3")
        self.assertEqual(auth_service.get_setting(self.conn, "batch_size"), "3")

    def test_background_services_read_the_file(self):
        secret_store.save({"mmf_access_token": "file-token"})
        self.assertEqual(TaskManager(self.path, Path(self.temp.name))._get_setting("mmf_access_token"), "file-token")
        manager = DownloadManager(self.path)
        manager._load_settings()
        self.assertEqual(manager.access_token, "file-token")


class WriteTests(SecretCase):
    def test_a_new_sign_in_goes_to_the_file_only(self):
        auth_service.set_settings(self.conn, {"mmf_access_token": "fresh", "mmf_username": "me"})
        self.assertEqual(auth_service.get_setting(self.conn, "mmf_access_token"), "fresh")
        self.assertEqual(self.database_value("mmf_access_token"), "db-token")
        self.assertEqual(self.database_value("mmf_username"), "me")

    def test_the_file_is_private(self):
        secret_store.save({"mmf_access_token": "x"})
        self.assertEqual(stat.S_IMODE(settings.secrets_file().stat().st_mode), 0o600)

    def test_it_keeps_the_login_password(self):
        login.set_password("a good long password")
        secret_store.save({"mmf_access_token": "x"})
        login.set_password("another long password")
        self.assertEqual(secret_store.load(), {"mmf_access_token": "x"})
        self.assertTrue(login.verify_password("another long password"))

    def test_the_settings_route_saves_cookies_to_the_file(self):
        create_database(server.DB_PATH)
        client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        response = client.post("/api/settings", json={"cookie_phpsessid": "typed"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(secret_store.load()["cookie_phpsessid"], "typed")
        self.assertTrue(client.get("/api/settings").json()["secrets_configured"]["cookie_phpsessid"])
        self.assertNotIn("typed", client.get("/api/settings").text)


class ExportAndBlankTests(SecretCase):
    def test_export_copies_only_what_has_a_value(self):
        self.stored("mmf_refresh_token", "")
        exported = secret_store.export_from_database(self.conn)
        self.assertIn("mmf_access_token", exported)
        self.assertNotIn("mmf_refresh_token", exported)
        self.assertEqual(secret_store.load()["mmf_access_token"], "db-token")

    def test_export_keeps_a_newer_value_in_the_file(self):
        secret_store.save({"mmf_access_token": "newer"})
        secret_store.export_from_database(self.conn)
        self.assertEqual(secret_store.load()["mmf_access_token"], "newer")

    def test_export_leaves_the_database_alone(self):
        secret_store.export_from_database(self.conn)
        self.assertEqual(self.database_value("mmf_access_token"), "db-token")

    def test_blank_refuses_until_the_file_holds_everything(self):
        secret_store.save({"mmf_access_token": "x"})
        with self.assertRaises(secret_store.SecretsError):
            secret_store.blank_database(self.conn)
        self.assertEqual(self.database_value("cookie_phpsessid"), "db-cookie")

    def test_blank_clears_the_database_and_the_app_still_reads_them(self):
        secret_store.export_from_database(self.conn)
        secret_store.blank_database(self.conn)
        self.assertEqual(self.database_value("mmf_access_token"), "")
        self.assertEqual(auth_service.get_setting(self.conn, "mmf_access_token"), "db-token")

    def test_the_commands_never_print_values(self):
        env = {**os.environ, "DESYNC_DATA_DIR": str(settings.data_dir())}
        create_database(settings.db_path())
        conn = sqlite3.connect(settings.db_path())
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('mmf_access_token', 'very-private-value')")
        conn.commit()
        conn.close()
        for args in (["export-secrets"], ["blank-secrets"], ["blank-secrets", "--confirm"]):
            result = subprocess.run(
                [sys.executable, "-m", "desync_archival.dbtool", *args], env=env, capture_output=True, text=True, check=True,
            )
            self.assertNotIn("very-private-value", result.stdout + result.stderr)
            self.assertIn("mmf_access_token", result.stdout)


class BackupTests(SecretCase):
    def setUp(self):
        super().setUp()
        self.backups = Path(self.temp.name) / "backups"
        self.env = patch.dict(os.environ, {"DESYNC_BACKUP_DIR": str(self.backups)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def test_each_backup_keeps_a_private_copy_of_the_secrets(self):
        secret_store.save({"mmf_access_token": "x"})
        target = backup.create_backup(self.path)
        copy = backup.secrets_copy(target)
        self.assertEqual(copy.read_bytes(), settings.secrets_file().read_bytes())
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)

    def test_a_backup_works_with_no_secrets_file(self):
        target = backup.create_backup(self.path)
        self.assertFalse(backup.secrets_copy(target).exists())

    def test_pruning_removes_the_secrets_copy_too(self):
        secret_store.save({"mmf_access_token": "x"})
        first = backup.create_backup(self.path)
        backup.create_backup(self.path, keep=1)
        self.assertFalse(first.exists())
        self.assertFalse(backup.secrets_copy(first).exists())


class StoredPathTests(unittest.TestCase):
    def test_paths_inside_the_data_folder_are_relative(self):
        self.assertEqual(settings.stored_data_path(settings.metadata_dir() / "model_1.json"), "metadata/model_1.json")

    def test_paths_outside_it_stay_absolute(self):
        self.assertEqual(settings.stored_data_path(Path("/elsewhere/a.json")), "/elsewhere/a.json")


if __name__ == "__main__":
    unittest.main()
