"""One class per finding from the review's priority list, after "Fix these first".

Each test fails on the code as reviewed. Class names carry the finding ID, so
`pytest tests/test_priority_items.py -k R09` proves one finding on its own.
E48 has its own file, tests/test_connections.py.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database

from desync_archival import backup, dbtool, external, migrations
from desync_archival import settings as app_config
from desync_archival.services import data_bundle, login, secret_store
from desync_archival.services.stash_exporter import StashExporter

# Stored relative to the data folder or an archive, so a move can't change them.
NOT_MOVED_WITH_A_BUNDLE = {
    ("collections", "raw_backup_path"),
    ("downloaded_files", "local_relative_path"),
    ("file_archive_members", "internal_path"),
    ("file_archive_submodels", "preview_image_path"),
    ("file_archive_submodels", "relative_dir"),
    ("library_candidates", "folders_json"),
    ("library_candidates", "rel_path"),
    ("library_model_folders", "rel_path"),
    ("library_release_trees", "folders_json"),
    ("model_artifacts", "path"),
    ("models", "image_path"),
}


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)


class SecretsR09Tests(TempCase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "secrets.json"
        env = patch.dict(os.environ, {"DESYNC_SECRETS_FILE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)
        secret_store.write_file({"login": {"salt": "s", "hash": "h"}, "settings": {"mmf_access_token": "keep"}})
        self.before = self.path.read_bytes()

    def failing_read(self):
        return patch.object(Path, "read_text", side_effect=OSError(24, "Too many open files"))

    def test_a_failed_read_refuses_to_save(self):
        with self.failing_read(), self.assertRaises(secret_store.SecretsError):
            secret_store.save({"cookies_json": "[]"})
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_a_corrupt_file_refuses_to_save(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(secret_store.SecretsError):
            secret_store.save({"cookies_json": "[]"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_a_file_that_isnt_an_object_refuses_to_save(self):
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaises(secret_store.SecretsError):
            secret_store.save({"cookies_json": "[]"})

    def test_a_failed_read_refuses_a_password_change(self):
        with self.failing_read(), self.assertRaises(secret_store.SecretsError):
            login.set_password("a-long-enough-password")
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_a_missing_file_is_a_normal_first_run(self):
        self.path.unlink()
        secret_store.save({"cookies_json": "[]"})
        self.assertEqual(secret_store.load(), {"cookies_json": "[]"})

    def test_reads_survive_a_failed_read_and_log_it(self):
        with self.failing_read(), self.assertLogs("vault", logging.ERROR) as logs:
            self.assertEqual(secret_store.load(), {})
        self.assertIn("secrets file", logs.output[0])

    def test_a_failure_never_prints_contents(self):
        self.path.write_text('{"mmf_access_token": "hunter2', encoding="utf-8")
        with self.assertRaises(secret_store.SecretsError) as caught:
            secret_store.read_file()
        self.assertNotIn("hunter2", str(caught.exception))

    def test_a_password_change_keeps_everything_else(self):
        login.set_password("a-long-enough-password")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["settings"], {"mmf_access_token": "keep"})
        self.assertNotEqual(data["login"], {"salt": "s", "hash": "h"})

    def test_a_write_is_private_synced_and_leaves_no_temp_file(self):
        with patch("os.fsync", wraps=os.fsync) as fsync:
            secret_store.save({"cookies_json": "[]"})
        fsync.assert_called()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.suffix == ".tmp"), [])

    def test_a_failed_write_leaves_the_file_and_no_temp_file(self):
        with patch("os.fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
            secret_store.save({"cookies_json": "[]"})
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.suffix == ".tmp"), [])

    def test_two_processes_keep_each_others_writes(self):
        # dbtool runs beside the server, and a threading lock can't see across processes.
        script = (
            "import sys\n"
            "from desync_archival.services import secret_store\n"
            "for i in range(40):\n"
            "    secret_store.save({f'{sys.argv[1]}{i}': 'x'})\n"
        )
        env = {**os.environ, "DESYNC_SECRETS_FILE": str(self.path)}
        workers = [subprocess.Popen([sys.executable, "-c", script, name], env=env) for name in ("a", "b", "c")]
        for worker in workers:
            self.assertEqual(worker.wait(timeout=60), 0)
        saved = secret_store.load()
        self.assertEqual(len(saved), 121)

    def test_the_secrets_path_is_still_the_setting(self):
        self.assertEqual(app_config.secrets_file(), self.path.resolve())
