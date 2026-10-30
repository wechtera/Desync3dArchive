"""Tests for scripts/build_dev_data.py. They use a generated source, never production data."""

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_dev_data", REPO_ROOT / "scripts" / "build_dev_data.py")
assert spec and spec.loader
build_dev_data = importlib.util.module_from_spec(spec)
sys.modules["build_dev_data"] = build_dev_data
spec.loader.exec_module(build_dev_data)

ACCESS_TOKEN = "live-access-token-value-0123456789"
CLIENT_SECRET = "live-client-secret-value-0123456789"
COOKIE = "live-cookie-value-0123456789"


class BuildDevDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.source = base / "scrapepage"
        self.dest = base / "dev"
        self.repo = base / "repo"
        self.nas = base / "nas_mount"
        self.repo.mkdir()
        self.forbidden = [str(self.source), str(self.nas)]
        self._make_source()

    def tearDown(self):
        self.temp.cleanup()

    def _make_source(self):
        (self.source / "metadata").mkdir(parents=True)
        (self.source / "previews" / "1").mkdir(parents=True)
        (self.source / "previews" / "1" / "cover.jpg").write_bytes(b"jpg")
        (self.source / "metadata" / "model_1.json").write_text("{}")
        for item in ("services", "ui", "scripts"):
            (self.source / item).mkdir()
            (self.source / item / "code.py").write_text("x = 1\n")
        (self.source / "ui" / "cookies.json").write_text(COOKIE)
        (self.source / "cookies.json").write_text(COOKIE)

        conn = sqlite3.connect(self.source / "library.db")
        conn.executescript(
            """
            CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE models (id INTEGER PRIMARY KEY, name TEXT, vault_path TEXT, source TEXT,
                                 raw_backup_path TEXT);
            CREATE TABLE external_libraries (id INTEGER PRIMARY KEY, name TEXT, path TEXT);
            CREATE TABLE pipeline_tasks (id INTEGER PRIMARY KEY, log_output TEXT);
            """
        )
        settings = {
            "mmf_access_token": ACCESS_TOKEN,
            "mmf_client_secret": CLIENT_SECRET,
            "cookie_phpsessid": COOKIE,
            "mmf_username": "kept",
            "download_root_path": f"{self.nas}/vault",
            "download_work_path": f"{self.nas}/work",
            "stash_download_path": f"{self.nas}/stash",
            "worker_concurrency": "2",
        }
        conn.executemany("INSERT INTO app_settings VALUES (?, ?)", settings.items())
        conn.execute(
            "INSERT INTO models VALUES (1, 'Model', ?, 'mmf', ?)",
            (f"{self.nas}/vault/a", f"{self.source}/metadata/model_1.json"),
        )
        conn.execute("INSERT INTO external_libraries VALUES (1, 'Loot', ?)", (f"{self.nas}/loot",))
        conn.execute(
            "INSERT INTO pipeline_tasks VALUES (1, ?)",
            (f"used {ACCESS_TOKEN} on {self.nas}/vault/a",),
        )
        conn.commit()
        conn.close()

    def _build(self):
        build_dev_data.build(self.source, self.dest, [], self.repo, self.forbidden)

    def _settings(self):
        conn = sqlite3.connect(self.dest / "library.db")
        try:
            return dict(conn.execute("SELECT key, value FROM app_settings"))
        finally:
            conn.close()

    def test_secrets_are_blanked_and_absent_from_file_bytes(self):
        self._build()
        settings = self._settings()
        self.assertEqual(settings["mmf_access_token"], "")
        self.assertEqual(settings["mmf_client_secret"], "")
        raw = (self.dest / "library.db").read_bytes()
        for secret in (ACCESS_TOKEN, CLIENT_SECRET, COOKIE):
            self.assertNotIn(secret.encode(), raw)

    def test_settings_that_are_not_secret_are_kept(self):
        self._build()
        settings = self._settings()
        self.assertEqual(settings["mmf_username"], "kept")
        self.assertEqual(settings["worker_concurrency"], "2")

    def test_paths_are_rewritten_into_destination(self):
        self._build()
        settings = self._settings()
        self.assertEqual(settings["download_root_path"], str(self.dest / "nas/vault"))
        self.assertEqual(settings["download_work_path"], str(self.dest / "staging"))
        self.assertEqual(settings["stash_download_path"], str(self.dest / "stash"))

        conn = sqlite3.connect(self.dest / "library.db")
        vault_path, raw_path = conn.execute("SELECT vault_path, raw_backup_path FROM models").fetchone()
        library_path = conn.execute("SELECT path FROM external_libraries").fetchone()[0]
        log_output = conn.execute("SELECT log_output FROM pipeline_tasks").fetchone()[0]
        conn.close()
        self.assertEqual(vault_path, str(self.dest / "nas/vault/a"))
        self.assertEqual(raw_path, str(self.dest / "metadata/model_1.json"))
        self.assertEqual(library_path, str(self.dest / "nas/Loot"))
        self.assertNotIn(str(self.nas), log_output)
        self.assertNotIn(ACCESS_TOKEN, log_output)

    def test_source_database_is_not_changed(self):
        before = (self.source / "library.db").read_bytes()
        self._build()
        self.assertEqual((self.source / "library.db").read_bytes(), before)

    def test_unhandled_production_path_fails_and_removes_the_copy(self):
        conn = sqlite3.connect(self.source / "library.db")
        conn.execute("UPDATE pipeline_tasks SET log_output = '/Volumes/Other/path'")
        conn.commit()
        conn.close()
        self.forbidden.append("/Volumes/")
        with self.assertRaises(build_dev_data.SanitizeError):
            self._build()
        self.assertFalse((self.dest / "library.db").exists())
        self.assertFalse((self.dest / "library.db.building").exists())

    def test_old_app_has_code_and_sanitized_database_only(self):
        self._build()
        old_app = self.dest / "old_app"
        self.assertTrue((old_app / "services" / "code.py").exists())
        self.assertTrue((old_app / "library.db").exists())
        self.assertFalse((old_app / "ui" / "cookies.json").exists())
        self.assertFalse((old_app / "cookies.json").exists())
        self.assertTrue((old_app / "previews" / "1" / "cover.jpg").exists())

    def test_destination_inside_source_is_refused(self):
        self.dest = self.source / "dev"
        with self.assertRaises(build_dev_data.SanitizeError):
            self._build()

    def test_destination_inside_repository_is_refused(self):
        self.dest = self.repo / "dev"
        with self.assertRaises(build_dev_data.SanitizeError):
            self._build()

    def test_destination_overlapping_protected_path_is_refused(self):
        with self.assertRaises(build_dev_data.SanitizeError):
            build_dev_data.build(self.source, self.dest, [self.dest / "x"], self.repo, self.forbidden)

    def test_unmarked_destination_with_content_is_left_alone(self):
        self.dest.mkdir()
        keep = self.dest / "important.txt"
        keep.write_text("keep")
        with self.assertRaises(build_dev_data.SanitizeError):
            self._build()
        self.assertEqual(keep.read_text(), "keep")

    def test_rebuild_keeps_baseline_directory(self):
        (self.dest / "baseline").mkdir(parents=True)
        (self.dest / "baseline" / "manifest.json").write_text("{}")
        self._build()
        self._build()
        self.assertTrue((self.dest / "baseline" / "manifest.json").exists())
        self.assertTrue((self.dest / "library.db").exists())

    def test_sibling_with_same_prefix_is_not_within_parent(self):
        self.assertFalse(build_dev_data.is_within(Path("/a/Data_dev"), Path("/a/Data")))
        self.assertTrue(build_dev_data.is_within(Path("/a/Data/x"), Path("/a/Data")))


if __name__ == "__main__":
    unittest.main()
