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
from unittest.mock import MagicMock, patch

from db_helper import create_database
from pydantic import ValidationError

from desync_archival import backup, external, migrations
from desync_archival import settings as app_config
from desync_archival.api import schemas, server
from desync_archival.api.routes import reconcile as reconcile_routes
from desync_archival.api.routes import settings as settings_routes
from desync_archival.services import auth_service, pending_deletion
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
