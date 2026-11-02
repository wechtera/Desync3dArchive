"""One class per item from the list after E50, in the order they were done.

Each test fails on the code before its fix. Class names carry the item, so
`pytest tests/test_remaining_items.py -k E49` proves one on its own.
"""

import logging
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from db_helper import create_database

from desync_archival import backup
from desync_archival import settings as app_config
from desync_archival.services import auth_service
from desync_archival.services.reconciler import VaultReconciler
from desync_archival.services.task_manager import TaskManager


class TempDb(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.db = self.root / "library.db"
        create_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)


class LinkedAuditE49Tests(TempDb):
    def setUp(self):
        super().setUp()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.library = self.root / "loot"
        self.library.mkdir()

    def reconciler(self):
        reconciler = object.__new__(VaultReconciler)
        reconciler.db_path = self.db
        reconciler.previews_root = self.root / "previews"
        reconciler.storage = MagicMock()
        return reconciler

    def collection(self, source, folder, name):
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / "pack.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("a.stl", b"solid")
        size = archive.stat().st_size
        file_id = abs(hash(name)) % 1_000_000 + 1
        with self.conn:
            model = self.conn.execute(
                "INSERT INTO collections (name, source, vault_path, raw_backup_path) VALUES (?, ?, ?, 'm.json')",
                (name, source, str(folder)),
            ).lastrowid
            self.conn.execute(
                "INSERT INTO model_files (file_id, model_id, filename, size_bytes) VALUES (?, ?, 'pack.zip', ?)", (file_id, model, size)
            )
            self.conn.execute(
                "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,"
                " actual_bytes, vault_path) VALUES (?, ?, 'pack.zip', 'pack.zip', 'pack.zip', ?, ?)",
                (file_id, model, size, str(archive)),
            )
        return model

    def audit(self, mode="fast"):
        return self.reconciler()._execute_forward(self.vault, mode, lambda *_: None, lambda *_: None, lambda: False)

    def defects(self, report, model):
        return [
            issue["defect"] for entry in report["discrepancies"] if entry["model_id"] == model for issue in entry["issues"]
        ]

    def test_a_linked_collection_isnt_flagged_for_having_no_metadata(self):
        for source in ("external", "document"):
            model = self.collection(source, self.library / source, source)
            self.assertEqual(self.defects(self.audit(), model), [])

    def test_an_import_isnt_flagged_either(self):
        model = self.collection("import", self.vault / "ad-hoc" / "x", "import")
        self.assertEqual(self.defects(self.audit(), model), [])

    def test_an_mmf_collection_still_is(self):
        model = self.collection("mmf", self.vault / "m", "mmf")
        self.assertEqual(self.defects(self.audit(), model), ["METADATA_MISSING"])

    def test_a_deep_audit_doesnt_hash_a_linked_library(self):
        model = self.collection("external", self.library / "x", "linked")
        self.audit("deep")
        row = self.conn.execute("SELECT sha256 FROM downloaded_files WHERE model_id = ?", (model,)).fetchone()
        self.assertIsNone(row[0])

    def test_a_deep_audit_still_hashes_the_vault(self):
        model = self.collection("mmf", self.vault / "m", "mmf")
        (self.vault / "m" / "metadata.json").write_text("{}", encoding="utf-8")
        self.audit("deep")
        row = self.conn.execute("SELECT sha256 FROM downloaded_files WHERE model_id = ?", (model,)).fetchone()
        self.assertTrue(row[0])

    def test_a_missing_linked_file_is_still_reported(self):
        model = self.collection("external", self.library / "gone", "gone")
        (self.library / "gone" / "pack.zip").unlink()
        self.assertEqual(self.defects(self.audit(), model), ["FILE_NOT_FOUND"])

    def test_each_report_entry_names_its_source(self):
        self.collection("mmf", self.vault / "m", "mmf")
        self.assertEqual(self.audit()["discrepancies"][0]["source"], "mmf")

    def rows(self, model):
        return self.conn.execute("SELECT COUNT(*) FROM downloaded_files WHERE model_id = ?", (model,)).fetchone()[0]

    def path(self, model):
        return self.conn.execute("SELECT vault_path FROM collections WHERE id = ?", (model,)).fetchone()[0]

    def test_a_reset_refuses_anything_that_cant_be_downloaded_again(self):
        kept = [self.collection(source, self.library / source, source) for source in ("external", "document", "import")]
        mmf = self.collection("mmf", self.vault / "m", "mmf")
        result = self.reconciler().reset_reconciliation_models([*kept, mmf])
        self.assertEqual((result["reset_count"], result["skipped_not_mmf"]), (1, 3))
        for model in kept:
            self.assertEqual(self.rows(model), 1)
            self.assertTrue(self.path(model))
        self.assertEqual(self.rows(mmf), 0)

    def test_a_dry_run_counts_only_what_a_reset_would_touch(self):
        linked = self.collection("external", self.library / "x", "linked")
        mmf = self.collection("mmf", self.vault / "m", "mmf")
        result = self.reconciler().reset_reconciliation_models([linked, mmf], dry_run=True)
        self.assertEqual((result["would_reset"], result["file_records"], result["skipped_not_mmf"]), (1, 1, 1))

    def test_a_reset_of_only_linked_rows_changes_nothing(self):
        linked = self.collection("external", self.library / "x", "linked")
        result = self.reconciler().reset_reconciliation_models([linked])
        self.assertEqual((result["success"], result["reset_count"]), (True, 0))
        self.assertEqual(self.rows(linked), 1)


class ScrapeCookiesS07Tests(TempDb):
    def setUp(self):
        super().setUp()
        self.manager = object.__new__(TaskManager)
        self.manager.db_path = self.db
        self.manager.root_dir = self.root
        self.manager.scrapes_dir = self.root / "scrapes"
        self.manager.scrapes_dir.mkdir()
        self.manager._lock = threading.Lock()
        self.manager.active_task_id = None
        self.manager.active_process = None
        self.manager._cancel_events = {}
        self.manager._scrape_error = ""
        with self.conn:
            auth_service.set_settings(self.conn, {"cookies_json": '[{"name": "PHPSESSID", "value": "secret"}]'})
            self.task = self.conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('library_scrape', 'running')").lastrowid

    def left_behind(self):
        return list(self.manager.scrapes_dir.glob("*/cookies.json"))

    def test_a_missing_scraper_leaves_no_cookies(self):
        with patch.object(app_config, "scraper_script", return_value=self.root / "gone.js"):
            self.manager._run_scrape_worker(self.task)
        self.assertEqual(self.left_behind(), [])

    def test_a_scraper_that_wont_start_leaves_no_cookies(self):
        script = self.root / "scrape.js"
        script.write_text("", encoding="utf-8")
        with patch.object(app_config, "scraper_script", return_value=script), \
                patch("desync_archival.services.task_manager.subprocess.Popen", side_effect=OSError("no node")):
            self.manager._run_scrape_worker(self.task)
        self.assertEqual(self.left_behind(), [])

    def test_a_crash_past_the_workers_own_handling_leaves_no_cookies(self):
        script = self.root / "scrape.js"
        script.write_text("", encoding="utf-8")
        with patch.object(app_config, "scraper_script", return_value=script), \
                patch.object(self.manager, "_update_progress", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            self.manager._run_scrape_worker(self.task)
        self.assertEqual(self.left_behind(), [])

    def test_the_startup_sweep_clears_old_copies_and_counts_them(self):
        for name in ("a", "b"):
            folder = self.manager.scrapes_dir / name
            folder.mkdir()
            (folder / "cookies.json").write_text("secret value", encoding="utf-8")
            (folder / "model_ids.txt").write_text("1", encoding="utf-8")
        with self.assertLogs("vault", logging.WARNING) as logs:
            self.assertEqual(self.manager.remove_scrape_cookies(), 2)
        self.assertEqual(self.left_behind(), [])
        self.assertEqual(len(list(self.manager.scrapes_dir.glob("*/model_ids.txt"))), 2)
        self.assertNotIn("secret value", "".join(logs.output))

    def test_a_clean_sweep_says_nothing(self):
        with self.assertNoLogs("vault", logging.WARNING):
            self.assertEqual(self.manager.remove_scrape_cookies(), 0)


class BundleSecretsS07Tests(TempDb):
    def test_a_bundle_never_holds_secrets_openly(self):
        secrets_file = self.root / "secrets.json"
        secrets_file.write_text("{}", encoding="utf-8")
        target = self.root / "bundle" / "secrets.json"
        target.parent.mkdir()
        seen = []
        real_write = os.write

        def spy(fd, data):
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return real_write(fd, data)

        with patch("os.write", spy), patch.object(Path, "chmod", side_effect=OSError("NAS share")):
            backup.copy_private(secrets_file, target)
        self.assertEqual(seen, [0o600])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
