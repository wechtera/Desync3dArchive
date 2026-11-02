"""One class per item from the list after E50, in the order they were done.

Each test fails on the code before its fix. Class names carry the item, so
`pytest tests/test_remaining_items.py -k E49` proves one on its own.
"""

import json
import logging
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from db_helper import create_database
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_fix_first import FILE_URL, DownloadCase

from desync_archival import backup, external, migrations
from desync_archival import settings as app_config
from desync_archival.api import routing, schemas, server
from desync_archival.api.routes import auth as auth_routes
from desync_archival.api.routes import catalog as catalog_routes
from desync_archival.api.routes import reconcile as reconcile_routes
from desync_archival.api.routes import settings as settings_routes
from desync_archival.services import auth_service, paths, pending_deletion
from desync_archival.services import task_manager as task_service
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.image_worker import ImageWorker
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
        self.manager._tasks = {}
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


class PurgeWaitS08Tests(TempDb):
    def setUp(self):
        super().setUp()
        self.vault = self.root / "vault"
        self.held_dir = self.vault / ".Pending-Deletion" / "1"
        self.held_dir.mkdir(parents=True)
        self.held = self.held_dir / "a.zip"
        self.held.write_bytes(b"x")
        with self.conn:
            auth_service.set_settings(self.conn, {"download_root_path": str(self.vault)})
            self.conn.execute(
                "INSERT INTO deleted_files (id, file_id, collection_id, filename, original_path, pending_path, deleted_at)"
                " VALUES (1, 1, 1, 'a.zip', '/v/a.zip', ?, datetime('now', '-1 day'))", (str(self.held),),
            )

    def age(self, days):
        with self.conn:
            self.conn.execute("UPDATE deleted_files SET deleted_at = datetime('now', ?)", (f"-{days} days",))

    def state(self):
        return self.conn.execute("SELECT purged_at FROM deleted_files").fetchone()[0]

    def test_a_file_the_nas_cant_read_still_waits(self):
        real = Path.stat

        def failing(path, *args, **kwargs):
            if path.name == "a.zip":
                raise OSError(5, "Input/output error")
            return real(path, *args, **kwargs)

        with patch.object(Path, "stat", failing):
            result = pending_deletion.purge(self.conn)
        self.assertEqual((result["waiting"], result["items"]), (1, []))

    def test_a_vanished_file_still_waits_its_turn(self):
        self.held.unlink()
        self.assertEqual(pending_deletion.purge(self.conn)["waiting"], 1)

    def test_age_comes_from_the_catalog_not_the_file(self):
        os.utime(self.held, (1_600_000_000, 1_600_000_000))
        self.assertEqual(pending_deletion.purge(self.conn)["waiting"], 1)
        self.age(15)
        os.utime(self.held)
        self.assertEqual(len(pending_deletion.purge(self.conn)["items"]), 1)

    def test_the_catalog_date_reads_as_utc(self):
        now = time.gmtime(time.time() - 3600)
        self.assertAlmostEqual(pending_deletion._age_days(time.strftime("%Y-%m-%d %H:%M:%S", now)), 1 / 24, places=3)

    def test_a_date_that_cant_be_read_never_makes_a_file_purgeable(self):
        self.assertEqual(pending_deletion._age_days("garbage"), 0.0)
        self.assertEqual(pending_deletion._age_days(None), 0.0)

    def test_the_listing_never_reads_held_files(self):
        self.age(20)
        with patch.object(pending_deletion, "_vault_root_or_none", return_value=self.vault), \
                patch.object(Path, "stat", side_effect=OSError(5, "Input/output error")):
            item = pending_deletion.listing(self.conn)["items"][0]
        self.assertEqual((item["age_days"], item["can_purge"]), (20, True))

    def test_a_read_error_at_purge_time_keeps_the_file_held(self):
        self.age(20)
        with patch.object(Path, "lstat", side_effect=OSError(5, "Input/output error")):
            result = pending_deletion.purge(self.conn, confirm=True, ids=[1])
        self.assertEqual((result["purged"], result["skipped_unreadable"]), (0, ["a.zip"]))
        self.assertTrue(self.held.exists())
        self.assertIsNone(self.state())

    def test_a_file_already_gone_is_recorded_as_purged(self):
        self.age(20)
        self.held.unlink()
        self.assertEqual(pending_deletion.purge(self.conn, confirm=True, ids=[1])["purged"], 1)
        self.assertIsNotNone(self.state())

    def test_an_old_enough_file_is_purged(self):
        self.age(20)
        self.assertEqual(pending_deletion.purge(self.conn, confirm=True, ids=[1])["purged"], 1)
        self.assertFalse(self.held.exists())



class UntrackedIngestS06Tests(TempDb):
    def setUp(self):
        super().setUp()
        self.vault = self.root / "vault"
        self.model_dir = self.vault / "Maker" / "2024-01" / "5_Model"
        (self.model_dir / "files").mkdir(parents=True)
        with self.conn:
            self.conn.execute(
                "INSERT INTO collections (id, name, source, external_key, raw_backup_path, designer_name, release_month)"
                " VALUES (5, 'Model', 'mmf', '5', 'm.json', 'Maker', '2024-01')"
            )
        self.reconciler = object.__new__(VaultReconciler)
        self.reconciler.db_path = self.db
        self.reconciler.storage = MagicMock()
        self.reconciler.storage.get_root_path.return_value = self.vault

    def ingest(self, path):
        return self.reconciler.ingest_untracked_files([{"model_id": 5, "file_path": str(path)}])

    def files(self):
        return self.conn.execute("SELECT COUNT(*) FROM downloaded_files WHERE model_id = 5").fetchone()[0]

    def vault_path(self):
        return self.conn.execute("SELECT vault_path FROM collections WHERE id = 5").fetchone()[0]

    def test_a_file_outside_the_vault_is_refused(self):
        outside = self.root / "secrets.txt"
        outside.write_text("x", encoding="utf-8")
        result = self.ingest(outside)
        self.assertEqual(result["ingested_count"], 0)
        self.assertIn("outside the vault", result["errors"][0])
        self.assertEqual(self.files(), 0)

    def test_a_link_leading_out_of_the_vault_is_refused(self):
        outside = self.root / "secrets.txt"
        outside.write_text("x", encoding="utf-8")
        link = self.model_dir / "files" / "link.stl"
        link.symlink_to(outside)
        self.assertEqual(self.ingest(link)["ingested_count"], 0)

    def test_a_file_in_the_vault_is_added(self):
        target = self.model_dir / "files" / "a.stl"
        target.write_bytes(b"solid")
        self.assertEqual(self.ingest(target)["ingested_count"], 1)
        self.assertEqual(self.files(), 1)
        self.assertEqual(self.vault_path(), str(self.model_dir))

    def test_an_existing_vault_path_is_left_alone(self):
        with self.conn:
            self.conn.execute("UPDATE collections SET vault_path = '/already/here' WHERE id = 5")
        target = self.model_dir / "files" / "a.stl"
        target.write_bytes(b"solid")
        self.ingest(target)
        self.assertEqual(self.vault_path(), "/already/here")

    def test_a_file_near_the_top_cant_make_the_vault_root_a_model_folder(self):
        folder = self.vault / "loose"
        folder.mkdir()
        target = folder / "a.stl"
        target.write_bytes(b"solid")
        self.assertEqual(self.ingest(target)["ingested_count"], 1)
        self.assertIsNone(self.vault_path())


class UntrackedRouteS06Tests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        migrations.apply_pending(self.conn)
        self.tasks = MagicMock()
        self.tasks.start_task.return_value = {"success": True, "task_id": 4}
        self.manager = MagicMock(active_downloads={}, active_offloads={})
        patcher = patch("desync_archival.api.routes.reconcile._get_managers", return_value=(MagicMock(), self.tasks))
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self):
        return reconcile_routes.handle_post_reconcile_ingest_untracked(
            self.conn, self.manager, {"items": [{"model_id": 5, "file_path": "/v/a.stl"}]}, {},
        )

    def test_it_runs_as_a_job_and_is_audited(self):
        body, status = self.post()
        self.assertEqual((status, body["task_id"]), (200, 4))
        self.assertEqual(self.tasks.start_task.call_args.args[0], "untracked_ingest")
        self.assertEqual(self.conn.execute("SELECT action FROM audit_log").fetchone()[0], "ingest_untracked")

    def test_it_waits_for_running_work(self):
        self.manager.active_downloads = {1: {}}
        self.assertEqual(self.post()[1], 409)
        self.tasks.start_task.assert_not_called()

    def test_a_busy_job_slot_is_reported(self):
        self.tasks.start_task.return_value = {"success": False, "error": "busy"}
        self.assertEqual(self.post()[1], 409)


class SecondProcessR19Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"DESYNC_DATA_DIR": temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(server.release_instance_lock)

    def test_a_second_copy_stops_before_it_touches_any_job(self):
        server.take_instance_lock()
        first = server._instance_lock
        server._instance_lock = None
        with self.assertRaises(app_config.SettingsError) as caught:
            server.take_instance_lock()
        server._instance_lock = first
        self.assertIn("already running", str(caught.exception))

    def test_the_lock_comes_back_when_the_first_copy_ends(self):
        server.take_instance_lock()
        server.release_instance_lock()
        server.take_instance_lock()

    def test_it_runs_before_anything_else_in_main(self):
        with patch.object(server, "take_instance_lock", side_effect=app_config.SettingsError("stop")), \
                patch.object(server, "check_network_ready") as ready, patch.object(server.uvicorn, "run") as run, \
                self.assertRaises(app_config.SettingsError):
            server.main()
        ready.assert_not_called()
        run.assert_not_called()


class AppEnvS14Tests(unittest.TestCase):
    def test_only_the_three_known_values_pass(self):
        for value in ("production", "development", "test"):
            with patch.dict(os.environ, {"APP_ENV": value}):
                self.assertEqual(app_config.app_env(), value)

    def test_a_near_miss_is_refused(self):
        for value in ("dev", "prod", "Development", "staging"):
            with patch.dict(os.environ, {"APP_ENV": value}), self.assertRaises(app_config.SettingsError) as caught:
                app_config.app_env()
            self.assertIn("development", str(caught.exception))


class SettingsKeysWS3Tests(TempDb):
    def test_keys_only_the_app_writes_are_refused(self):
        for key in ("last_backup_epoch", "circuit_breaker_tripped", "sync_stage", "mmf_token_expires_at", "last_anything"):
            with self.assertRaises(ValidationError):
                schemas.SettingsBody.model_validate({key: "1"})

    def test_ordinary_settings_still_pass(self):
        body = schemas.SettingsBody.model_validate({"worker_concurrency": 2, "scraper_headless": "1", "custom": "x"})
        self.assertEqual(body.model_dump(exclude_unset=True)["custom"], "x")

    def test_a_new_secret_cant_leak_through_the_settings_route(self):
        with self.conn:
            self.conn.execute("INSERT INTO app_settings (key, value) VALUES ('brand_new_api_secret', 'hunter2')")
        data, status = settings_routes.handle_get_settings(self.conn)
        self.assertEqual(status, 200)
        self.assertNotIn("brand_new_api_secret", data)
        self.assertNotIn("hunter2", json.dumps(data))

    def test_listed_settings_and_secret_flags_still_come_back(self):
        with self.conn:
            auth_service.set_settings(self.conn, {"worker_concurrency": "3", "mmf_client_id": "abc"})
        data, _ = settings_routes.handle_get_settings(self.conn)
        self.assertEqual((data["worker_concurrency"], data["mmf_client_id"]), ("3", "abc"))
        self.assertIn("mmf_access_token", data["secrets_configured"])
        self.assertNotIn("mmf_access_token", data)


class ToolLookupWS3Tests(unittest.TestCase):
    def test_an_explicit_setting_beats_path(self):
        with tempfile.TemporaryDirectory() as folder:
            chosen = Path(folder) / "my-unrar"
            chosen.write_text("#!/bin/sh\n", encoding="utf-8")
            chosen.chmod(0o755)
            with patch.dict(os.environ, {"DESYNC_UNRAR_BIN": str(chosen)}), \
                    patch("desync_archival.external.shutil.which", return_value="/usr/bin/true"):
                self.assertEqual(external.find_unrar(), str(chosen))

    def test_a_bad_setting_falls_back(self):
        with patch.dict(os.environ, {"DESYNC_UNRAR_BIN": "/nowhere/unrar"}):
            self.assertNotEqual(external.find_unrar(), "/nowhere/unrar")


class BackupPathsS11Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.allowed = self.root / "allowed"
        self.allowed.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        env = patch.dict(os.environ, {"DESYNC_DATA_DIR": str(self.data), "DESYNC_ALLOWED_ROOTS": str(self.allowed)})
        env.start()
        self.addCleanup(env.stop)

    def test_a_folder_outside_the_allowed_roots_is_refused(self):
        body, status = settings_routes._save_backup_paths(str(self.outside / "b"), "")
        self.assertEqual(status, 403)
        self.assertIn("allowed", body["error"])
        self.assertFalse(app_config.backup_paths_file().exists())

    def test_the_second_folder_is_checked_too(self):
        _, status = settings_routes._save_backup_paths(str(self.allowed / "b"), str(self.outside / "m"))
        self.assertEqual(status, 403)

    def test_allowed_folders_save(self):
        self.assertEqual(settings_routes._save_backup_paths(str(self.allowed / "b"), "")[1], 200)
        self.assertEqual(json.loads(app_config.backup_paths_file().read_text(encoding="utf-8"))["backup_dir"], str(self.allowed / "b"))

    def test_a_crash_while_saving_leaves_the_old_file(self):
        app_config.save_backup_paths("/old", "")
        with patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            app_config.save_backup_paths("/new", "")
        self.assertEqual(json.loads(app_config.backup_paths_file().read_text(encoding="utf-8"))["backup_dir"], "/old")
        self.assertEqual([p.name for p in self.data.iterdir()], ["backup_paths.json"])


class UnsafeFilenamesS05Tests(DownloadCase):
    def name_it(self, filename):
        with self.conn:
            self.conn.execute("UPDATE model_files SET filename = ? WHERE file_id = 9", (filename,))

    def staged_names(self):
        return sorted(p.name for p in (self.work / "staged" / "5").iterdir())

    def test_a_climbing_name_stays_in_its_folder(self):
        self.name_it("../../../etc/evil.stl")
        self.stage()
        self.assertEqual(self.staged_names(), ["evil.stl"])

    def test_an_absolute_name_stays_in_its_folder(self):
        self.name_it("/etc/evil.stl")
        self.stage()
        self.assertEqual(self.staged_names(), ["evil.stl"])

    def test_a_windows_path_is_reduced_too(self):
        self.name_it("..\\..\\evil.stl")
        self.stage()
        self.assertEqual(self.staged_names(), ["evil.stl"])

    def test_a_name_with_nothing_left_is_refused(self):
        self.name_it("../..")
        with self.assertRaises(ValueError):
            self.stage()

    def test_two_files_cleaning_to_one_name_both_survive(self):
        self.name_it("12_a.stl")
        with self.conn:
            self.conn.execute(
                "INSERT INTO model_files (file_id, model_id, filename, size_bytes, download_url, source, external_key)"
                " VALUES (10, 5, '34_a.stl', 1000, ?, 'mmf', '10')", (FILE_URL,),
            )
        self.stage()
        self.assertEqual(self.staged_names(), ["a.stl", "a_10.stl"])
        rows = self.conn.execute("SELECT cleaned_filename FROM downloaded_files ORDER BY file_id").fetchall()
        self.assertEqual([r[0] for r in rows], ["a.stl", "a_10.stl"])

    def test_names_differing_only_in_case_count_as_the_same(self):
        self.assertEqual(paths.distinct_filename("A.stl", 2, {"a.stl"}), "A_2.stl")

    def test_a_suffixed_name_can_not_land_on_a_third_file(self):
        self.assertEqual(paths.distinct_filename("a.stl", 2, {"a.stl", "a_2.stl"}), "a_2_2.stl")

    def test_a_name_that_is_only_a_number_keeps_its_name(self):
        self.name_it("12_.stl")
        self.stage()
        self.assertEqual(self.staged_names(), ["12_.stl"])


class RarListingFallbackTests(unittest.TestCase):
    UNRAR_LISTING = """
UNRAR 7.11 freeware

Archive: /work/a.rar
Details: RAR 5

        Name: Hero/Supported/hero.stl
        Type: File
        Size: 2048

        Name: Hero/Supported
        Type: Directory
        Size: 0
"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tool = Path(self.temp.name) / "unrar"
        self.tool.touch()
        self.archive = Path(self.temp.name) / "a.rar"
        self.archive.touch()
        self.peeker = ArchivePeeker.__new__(ArchivePeeker)
        self.peeker._bin_unrar = str(self.tool)
        self.peeker._bin_7z = None

    def test_unrar_alone_lists_files_and_skips_folders(self):
        answer = SimpleNamespace(returncode=0, stdout=self.UNRAR_LISTING, stderr="")
        with patch.object(external, "run", return_value=answer):
            members = self.peeker._inspect_rar(self.archive)
        self.assertEqual([(m.internal_path, m.size_bytes) for m in members], [("Hero/Supported/hero.stl", 2048)])
        self.assertEqual(members[0].file_extension, ".stl")

    def test_a_tool_that_fails_is_a_read_error_and_not_an_empty_archive(self):
        answer = SimpleNamespace(returncode=3, stdout="", stderr="")
        with patch.object(external, "run", return_value=answer), self.assertRaises(external.ExtractionRefused):
            self.peeker._inspect_rar(self.archive)

    @unittest.skipUnless(external.find_7zip(), "needs 7-Zip")
    def test_real_seven_zip_lists_only_the_members(self):
        folder = Path(self.temp.name) / "src"
        folder.mkdir()
        (folder / "hero.stl").write_bytes(b"x" * 100)
        packed = Path(self.temp.name) / "sample.7z"
        external.run([external.find_7zip(), "a", str(packed), str(folder / "hero.stl")], 30)
        self.peeker._bin_unrar = None
        self.peeker._bin_7z = external.find_7zip()
        self.assertEqual([(m.internal_path, m.size_bytes) for m in self.peeker._inspect_rar(packed)], [("hero.stl", 100)])


class SupportedRuleTests(unittest.TestCase):
    # Each row is a spelling found in the library or one the three old rules read differently.
    TABLE = (
        ("28mm Supported/Ghuls.stl", True),
        ("Hero (Supported).stl", True),
        ("hero_supported.stl", True),
        ("Presupported/hero.stl", True),
        ("Pre-Supported/hero.stl", True),
        ("presuported_stl/hero.stl", True),
        ("Fatalis Soulified Support STLs/arm.stl", True),
        ("hero_with_supports.stl", True),
        ("BrazenBull_32mm_LYCHEE/a.stl", True),
        ("ReadyToSlice/a.stl", True),
        ("hero.lys", True),
        ("HERO.CHITUBOX", True),
        ("32mm Unsupported/Ghuls.stl", False),
        ("BrazenBull_32mm_UnSupported/a.stl", False),
        ("HeroUnsupported.stl", False),
        ("Hero/NoSupports/a.stl", False),
        ("Hero/No Supports/a.stl", False),
        ("Hero/not_supported/a.stl", False),
        ("Hero/hero.stl", False),
        ("Support Pillar.stl", False),
    )

    def test_the_table(self):
        for text, expected in self.TABLE:
            with self.subTest(text):
                self.assertEqual(paths.is_supported(text), expected)

    def test_a_supported_file_inside_an_unsupported_folder_counts(self):
        self.assertTrue(paths.is_supported("Unsupported/hero_supported.stl"))

    def test_only_negated_words_read_as_unsupported(self):
        self.assertTrue(paths.says_unsupported("32mm NoSupports/a.stl"))
        self.assertTrue(paths.says_unsupported("32mm Unsupported/a.stl"))
        self.assertFalse(paths.says_unsupported("32mm Supported/a.stl"))


class OnePassExtractionP05Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.folder = self.base / "out"
        self.folder.mkdir()
        self.seven_zip = external.find_7zip()
        self.tree = {"a/Head.stl": b"head a", "b/Head.stl": b"head b!", "c[1].stl": b"bracket", "other.stl": b"unasked"}
        self.source = self.base / "source"
        for name, body in self.tree.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)

    def packed(self, name="sample.7z"):
        archive = self.base / name
        external.run([self.seven_zip, "a", str(archive), *[str(p) for p in self.source.iterdir()]], 30, check=True)
        return archive

    def zipped(self):
        archive = self.base / "sample.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for name, body in self.tree.items():
                handle.writestr(name, body)
        return archive

    def members(self, *names):
        return [external.ArchiveMember(name, len(self.tree[name])) for name in names]

    def extract(self, archive, members):
        return external.extract_members(archive, members, self.folder, None, self.seven_zip)

    @unittest.skipUnless(external.find_7zip(), "needs 7-Zip")
    def test_one_process_unpacks_every_member(self):
        archive = self.packed()
        with patch.object(external, "run", wraps=external.run) as run:
            found = self.extract(archive, self.members("a/Head.stl", "b/Head.stl", "c[1].stl"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual({k: v.read_bytes() for k, v in found.items()}, {
            "a/Head.stl": b"head a", "b/Head.stl": b"head b!", "c[1].stl": b"bracket",
        })

    @unittest.skipUnless(external.find_7zip(), "needs 7-Zip")
    def test_only_the_listed_members_are_unpacked(self):
        self.extract(self.packed(), self.members("a/Head.stl", "c[1].stl"))
        self.assertEqual(
            sorted(p.relative_to(self.folder).as_posix() for p in external.regular_files(self.folder)),
            ["a/Head.stl", "c[1].stl"],
        )

    @unittest.skipUnless(external.find_7zip(), "needs 7-Zip")
    def test_a_member_the_pass_missed_is_left_out_for_the_slow_path(self):
        members = self.members("a/Head.stl", "b/Head.stl") + [external.ArchiveMember("elsewhere/Head.stl", 6)]
        self.assertEqual(sorted(self.extract(self.packed(), members)), ["a/Head.stl", "b/Head.stl"])

    @unittest.skipUnless(external.find_7zip(), "needs 7-Zip")
    def test_a_wrong_size_is_left_for_extract_member_to_refuse(self):
        members = [external.ArchiveMember("a/Head.stl", 999), *self.members("b/Head.stl")]
        self.assertEqual(sorted(self.extract(self.packed(), members)), ["b/Head.stl"])

    @unittest.skipUnless(external.find_7zip(), "needs 7-Zip")
    def test_the_list_file_is_removed(self):
        self.extract(self.packed(), self.members("a/Head.stl", "b/Head.stl"))
        self.assertEqual(list(self.base.glob("*.list")), [])

    def test_a_zip_opens_once_and_keeps_same_named_files_apart(self):
        archive = self.zipped()
        with patch.object(external.zipfile, "ZipFile", wraps=zipfile.ZipFile) as opened:
            found = self.extract(archive, self.members("a/Head.stl", "b/Head.stl"))
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(found["b/Head.stl"].read_bytes(), b"head b!")
        self.assertEqual(found["a/Head.stl"].read_bytes(), b"head a")

    def test_a_zip_member_written_with_backslashes_is_found(self):
        members = [external.ArchiveMember("a\\Head.stl", 6), external.ArchiveMember("b\\Head.stl", 7)]
        self.assertEqual(sorted(self.extract(self.zipped(), members)), ["a\\Head.stl", "b\\Head.stl"])

    def test_a_masked_name_never_reaches_a_list_file(self):
        members = [external.ArchiveMember("*.stl", 1), *self.members("a/Head.stl", "b/Head.stl")]
        written = []
        archive = self.base / "sample.7z"
        archive.touch()

        def fake_run(cmd, _timeout, **_kwargs):
            written.append((self.folder.with_name("out.list")).read_text(encoding="utf-8"))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(external, "run", side_effect=fake_run):
            self.extract(archive, members)
        self.assertEqual(written, ["a/Head.stl\nb/Head.stl\n"])

    def test_a_climbing_name_never_reaches_a_list_file(self):
        members = [external.ArchiveMember("../evil.stl", 1), *self.members("a/Head.stl")]
        self.assertEqual(self.extract(self.zipped(), members), {})

    def test_one_member_takes_the_normal_path(self):
        with patch.object(external, "run") as run:
            self.assertEqual(self.extract(self.base / "a.7z", self.members("a/Head.stl")), {})
        run.assert_not_called()

    def test_a_pass_bigger_than_the_limit_takes_the_normal_path(self):
        huge = [external.ArchiveMember("a/Head.stl", 10**15), external.ArchiveMember("b/Head.stl", 10**15)]
        with patch.object(external, "run") as run:
            self.assertEqual(self.extract(self.base / "a.7z", huge), {})
        run.assert_not_called()

    def test_running_past_the_limit_clears_the_folder_and_refuses(self):
        archive = self.base / "a.7z"
        archive.touch()
        (self.folder / "half.stl").write_bytes(b"x")
        with patch.object(external, "run", side_effect=external.ExtractionRefused("too big")), \
                self.assertRaises(external.ExtractionRefused):
            self.extract(archive, self.members("a/Head.stl", "b/Head.stl"))
        self.assertEqual(external.regular_files(self.folder), [])

    def test_a_rar_goes_to_unrar_by_list_file_and_stops_at_the_first_tool_with_results(self):
        archive = self.base / "a.rar"
        archive.touch()
        unrar = self.base / "unrar"
        seven = self.base / "7zz"
        unrar.touch()
        seven.touch()
        calls = []

        def fake_run(cmd, _timeout, **_kwargs):
            calls.append(cmd)
            for name, body in (("a/Head.stl", b"head a"), ("b/Head.stl", b"head b!")):
                (self.folder / name).parent.mkdir(exist_ok=True)
                (self.folder / name).write_bytes(body)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(external, "run", side_effect=fake_run):
            found = external.extract_members(
                archive, self.members("a/Head.stl", "b/Head.stl"), self.folder, str(unrar), str(seven),
            )
        self.assertEqual(sorted(found), ["a/Head.stl", "b/Head.stl"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:5], [str(unrar), "x", "-y", "-scfl", "--"])
        self.assertTrue(calls[0][6].startswith("@"))

    @unittest.skipUnless(external.find_7zip(), "needs 7-Zip")
    def test_the_peeker_reads_a_folder_of_images_in_one_process(self):
        (self.source / "a" / "one.jpg").write_bytes(b"one")
        (self.source / "b" / "two.jpg").write_bytes(b"two")
        archive = self.packed()
        peeker = ArchivePeeker.__new__(ArchivePeeker)
        peeker._bin_unrar = None
        peeker._bin_7z = self.seven_zip
        dest = self.base / "previews"
        with patch.object(external, "run", wraps=external.run) as run:
            found = peeker._extract_images(archive, ["a/one.jpg", "b/two.jpg"], dest, "7z")
        self.assertEqual(run.call_count, 1)
        self.assertEqual({p.name: p.read_bytes() for p in found.values()}, {"one.jpg": b"one", "two.jpg": b"two"})
        self.assertEqual([p.name for p in dest.iterdir() if p.name.startswith(".")], [])


class SingletonsA03Tests(TempDb):
    def other_db(self):
        other = self.root / "other.db"
        create_database(other)
        return other

    def test_each_reconciler_keeps_its_own_database(self):
        first, second = VaultReconciler(self.db), VaultReconciler(self.other_db())
        self.assertIsNot(first, second)
        self.assertNotEqual(first.db_path, second.db_path)

    def test_each_task_manager_keeps_its_own_database_and_slot(self):
        first, second = TaskManager(self.db), TaskManager(self.other_db())
        self.assertNotEqual(first.db_path, second.db_path)
        first._begin(1)
        self.assertEqual(first.active_task_id, 1)
        self.assertIsNone(second.active_task_id)

    def test_a_later_instance_does_not_repoint_an_earlier_one(self):
        first = TaskManager(self.db)
        path = first.db_path
        TaskManager(self.other_db())
        self.assertEqual(first.db_path, path)

    def test_routes_use_the_manager_the_server_made(self):
        made = TaskManager(self.db)
        with patch.object(task_service, "current", made):
            _, manager = reconcile_routes._get_managers(self.conn)
        self.assertIs(manager, made)

    def test_a_task_owns_its_stop_flag(self):
        manager = TaskManager(self.db)
        manager._begin(1)
        first = manager._task(1)
        first.stop.set()
        manager._release(1)
        manager._begin(2)
        self.assertFalse(manager._cancelled(2))
        self.assertFalse(manager._cancelled(1))
        self.assertIsNot(manager._task(2), first)

    def test_releasing_an_old_task_leaves_the_new_one_running(self):
        manager = TaskManager(self.db)
        manager._begin(2)
        manager._release(1)
        self.assertEqual(manager.active_task_id, 2)

    def test_a_task_stops_only_its_own_tool(self):
        manager = TaskManager(self.db)
        manager._begin(1)
        manager._begin(2)
        first, second = MagicMock(), MagicMock()
        first.poll.return_value = second.poll.return_value = None
        manager._task(1).process = first
        manager._task(2).process = second
        with manager._lock:
            manager._ask_to_stop(1, force=True, note="x")
        first.kill.assert_called_once()
        second.kill.assert_not_called()

    def test_stopping_reaches_only_the_active_tasks_tool(self):
        manager = TaskManager(self.db)
        manager._begin(1)
        process = MagicMock()
        process.poll.return_value = None
        manager._task(1).process = process
        with manager._lock:
            self.assertFalse(manager._ask_to_stop(99, force=True, note="x"))
        process.kill.assert_not_called()


class ResponseTypesS12Tests(unittest.TestCase):
    def test_a_plain_string_is_never_html(self):
        response = server.build_response("<script>alert(1)</script>")
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")

    def test_only_a_marked_page_is_html(self):
        response = server.build_response(schemas.HtmlPage("<html></html>"))
        self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")

    def test_a_handler_can_still_name_its_own_type(self):
        response = server.build_response("{}", 200, {"Content-Type": "application/json"})
        self.assertEqual(response.headers["content-type"], "application/json")

    def test_the_callback_page_is_marked(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with patch("desync_archival.api.routes.auth.auth_service.get_setting", return_value=""):
            body = auth_routes.handle_get_auth_callback(conn, None, {}, {})[0]
        self.assertIsInstance(body, schemas.HtmlPage)


class ReadsWriteNothingS16Tests(TempDb):
    def manager(self):
        return MagicMock()

    def call(self, handler, params=None):
        reconciler = MagicMock()
        reconciler.scan_staging_garbage.return_value = {"success": True, "items": []}
        reconciler.scan_path_drift.return_value = {"success": True, "items": []}
        manager = self.manager()
        with patch.object(reconcile_routes, "_get_managers", return_value=(reconciler, manager)):
            handler(self.conn, None, {}, params or {})
        return manager

    def test_checks_read_by_get_record_nothing(self):
        for handler in (reconcile_routes.handle_get_reconcile_staging_garbage,
                        reconcile_routes.handle_get_reconcile_path_drift,
                        reconcile_routes.handle_get_reconcile_duplicates):
            with self.subTest(handler.__name__):
                self.call(handler).record_finished.assert_not_called()

    def test_checks_posted_are_recorded(self):
        for handler in (reconcile_routes.handle_post_reconcile_staging_check,
                        reconcile_routes.handle_post_reconcile_path_drift,
                        reconcile_routes.handle_post_reconcile_duplicates):
            with self.subTest(handler.__name__):
                self.call(handler).record_finished.assert_called_once()


class RenderSlotsS16Tests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        patcher = patch.object(catalog_routes, "RENDER_WAIT_SECONDS", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)
        renderer = patch.object(catalog_routes, "MeshRenderer")
        self.renderer = renderer.start()
        self.addCleanup(renderer.stop)
        self.renderer.return_value.is_available.return_value = True
        self.renderer.return_value.render_ephemeral.return_value = b"jpeg"

    def render(self):
        return catalog_routes.handle_post_render_stl(self.conn, None, {"id": 1})

    def test_a_third_render_is_turned_away_and_told_when_to_return(self):
        for _ in range(2):
            self.assertTrue(catalog_routes.RENDER_SLOTS.acquire(blocking=False))
            self.addCleanup(catalog_routes.RENDER_SLOTS.release)
        body, status, headers = self.render()
        self.assertEqual(status, 429)
        self.assertIn("busy", body["error"])
        self.assertEqual(headers["Retry-After"], "5")
        self.renderer.return_value.render_ephemeral.assert_not_called()

    def test_a_finished_render_gives_its_slot_back(self):
        for _ in range(3):
            self.assertEqual(self.render()[1], 200)

    def test_a_failed_render_gives_its_slot_back(self):
        self.renderer.return_value.render_ephemeral.side_effect = RuntimeError("blender crashed")
        for _ in range(3):
            self.assertEqual(self.render()[1], 500)


class MalformedBodyWS7Tests(unittest.TestCase):
    def test_broken_json_is_refused_and_not_run_with_defaults(self):
        client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        with patch.object(reconcile_routes, "_get_managers") as managers:
            response = client.post(
                "/api/reconcile/scan", content=b'{"scan_mode": "deep"', headers={"Content-Type": "application/json"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("valid JSON", response.json()["error"])
        managers.assert_not_called()

    def test_bytes_that_are_not_text_are_refused(self):
        with self.assertRaises(ValueError):
            server.parse_body(b"\xff\xfe")

    def test_an_empty_body_still_means_no_options(self):
        self.assertEqual(server.parse_body(b""), {})


class BadPagingWS7Tests(unittest.TestCase):
    def test_a_word_for_limit_answers_400_on_every_paged_route(self):
        create_database(server.DB_PATH)
        client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        for path in ("/api/releases", "/api/models", "/api/downloads/history"):
            for query in ("limit=abc", "offset=abc"):
                with self.subTest(path=path, query=query):
                    response = client.get(f"{path}?{query}")
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("whole numbers", response.json()["error"])

    def test_numbers_are_held_to_range(self):
        self.assertEqual(routing.paging({"limit": ["9999"], "offset": ["-5"]}, 24, 100), (100, 0))
        self.assertEqual(routing.paging({"limit": ["0"]}, 24, 100), (1, 0))
        self.assertEqual(routing.paging({}, 24, 100), (24, 0))


class CoverCopyWS7Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.art = self.root / "art.jpg"
        self.art.write_bytes(b"full picture")
        self.previews = self.root / "previews"
        patcher = patch.object(app_config, "previews_dir", return_value=self.previews)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.worker = object.__new__(ImageWorker)
        self.worker.indexer = MagicMock()
        self.worker.indexer._find_model_artwork.return_value = self.art
        self.worker._still_mounted = MagicMock(return_value=True)
        self.worker._fail = MagicMock(return_value="failed")
        self.job = {
            "id": 1, "model_id": 5, "vault_path": str(self.root / "model"), "library_path": str(self.root),
            "external_key": "1/Pack/model",
        }
        self.cover = self.previews / "5" / "cover.jpg"

    def test_a_copy_that_dies_midway_leaves_no_cover_to_mistake_for_done(self):
        def half_copy(_source, target):
            Path(target).write_bytes(b"half")
            raise OSError("share dropped")

        with patch("desync_archival.services.image_worker.shutil.copy2", side_effect=half_copy):
            self.assertEqual(self.worker._cover(MagicMock(), self.job), "failed")
        self.assertEqual(list(self.cover.parent.iterdir()), [])

    def test_a_whole_copy_lands_under_the_real_name(self):
        self.worker._cover(MagicMock(), self.job)
        self.assertEqual(self.cover.read_bytes(), b"full picture")
        self.assertEqual([p.name for p in self.cover.parent.iterdir()], ["cover.jpg"])
