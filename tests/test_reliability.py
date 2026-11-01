"""Schedules, the health report, renewed cookies and keeping polls away from the NAS."""

import json
import logging
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival.api import server
from desync_archival.api.routes import settings as routes
from desync_archival.services import auth_service, health, jobs, scheduler
from desync_archival.services.downloader import (
    StorageManager,
    StorageUnavailableError,
    queue_models,
)


class ReliabilityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.db = self.base / "library.db"
        create_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.tasks = MagicMock(root_dir=self.base)
        self.downloads = MagicMock(active_downloads={})

    def set(self, **values):
        with self.conn:
            auth_service.set_settings(self.conn, {k: str(v) for k, v in values.items()})

    def get(self, key):
        return auth_service.get_setting(self.conn, key, "")

    def signed_in(self):
        self.set(mmf_access_token="t", mmf_token_expires_at=int(time.time()) + 7 * jobs.DAY, cookies_json='[{"name":"a","value":"b"}]')

    def task(self, status, summary=None):
        with self.conn:
            return self.conn.execute(
                "INSERT INTO pipeline_tasks (task_type, status, result_summary_json) VALUES ('x', ?, ?)",
                (status, json.dumps(summary or {})),
            ).lastrowid

    def collection(self, key):
        with self.conn:
            return self.conn.execute(
                "INSERT INTO collections (name, source, external_key, raw_backup_path) VALUES ('c', 'mmf', ?, '')", (key,),
            ).lastrowid

    def scrape_found(self, *ids):
        folder = self.base / "scrapes" / "run"
        folder.mkdir(parents=True)
        (folder / "model_ids.txt").write_text("\n".join(ids), encoding="utf-8")
        return self.task(jobs.COMPLETED, {"folder": "run"})

    def tick(self):
        scheduler.tick(self.conn, self.tasks, self.downloads)


class SyncTests(ReliabilityCase):
    def test_nothing_runs_until_turned_on(self):
        self.signed_in()
        self.tick()
        self.tasks.start_task.assert_not_called()

    def test_a_due_sync_starts_a_scrape_and_remembers_it(self):
        self.signed_in()
        self.set(sync_enabled=1)
        self.tasks.start_task.return_value = {"success": True, "task_id": 5}
        self.tick()
        self.assertEqual((self.get("sync_stage"), self.get("sync_task_id")), ("scrape", "5"))

    def test_a_recent_sync_isnt_due(self):
        self.signed_in()
        self.set(sync_enabled=1, last_sync_epoch=time.time())
        self.tick()
        self.tasks.start_task.assert_not_called()

    def test_an_expired_sign_in_waits_and_says_why(self):
        self.set(sync_enabled=1)
        self.tick()
        self.tasks.start_task.assert_not_called()
        self.assertIn("sign-in", self.get("last_sync_result"))
        self.assertEqual(self.get("last_sync_epoch"), "")

    def test_a_running_scrape_is_left_alone(self):
        self.set(sync_stage="scrape", sync_task_id=self.task("running"))
        self.tick()
        self.tasks.start_metadata_ingest.assert_not_called()
        self.assertEqual(self.get("sync_stage"), "scrape")

    def test_a_failed_scrape_ends_the_sync_and_is_reported(self):
        self.set(sync_stage="scrape", sync_task_id=self.task(jobs.FAILED))
        self.tick()
        self.assertEqual(self.get("sync_stage"), "")
        self.assertTrue(self.get("last_sync_result").startswith("Failed"))

    def test_only_a_finished_scrape_starts_the_ingest(self):
        self.collection("1")
        self.tasks.start_metadata_ingest.return_value = ({"task_id": 9}, 200)
        self.set(sync_stage="scrape", sync_task_id=self.scrape_found("1", "2"))
        self.tick()
        self.assertEqual(self.tasks.start_metadata_ingest.call_args.args[0], [1, 2])
        self.assertEqual((self.get("sync_stage"), self.get("sync_new_keys")), ("ingest", '["2"]'))

    def test_a_scrape_with_nothing_new_finishes(self):
        self.collection("1")
        self.set(sync_stage="scrape", sync_task_id=self.scrape_found("1"))
        self.tick()
        self.tasks.start_metadata_ingest.assert_not_called()
        self.assertEqual(self.get("last_sync_result"), "Completed: nothing new")

    def test_a_busy_ingest_is_tried_again_on_the_next_tick(self):
        self.tasks.start_metadata_ingest.return_value = ({"error": "busy"}, 409)
        self.set(sync_stage="scrape", sync_task_id=self.scrape_found("2"))
        self.tick()
        self.assertEqual(self.get("sync_stage"), "scrape")

    def test_new_collections_are_queued_only_when_chosen(self):
        fresh = self.collection("2")
        self.set(sync_stage="ingest", sync_task_id=self.task(jobs.COMPLETED), sync_new_keys='["2"]')
        self.tick()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM download_jobs").fetchone()[0], 0)
        self.set(sync_stage="ingest", sync_task_id=self.task(jobs.COMPLETED), sync_new_keys='["2"]', sync_auto_download=1)
        self.tick()
        self.assertEqual(self.conn.execute("SELECT model_id FROM download_jobs").fetchone()[0], fresh)
        self.assertIn("1 queued", self.get("last_sync_result"))

    def test_stage_survives_a_restart(self):
        self.set(sync_stage="ingest", sync_task_id=self.task(jobs.COMPLETED), sync_new_keys="[]")
        reopened = sqlite3.connect(self.db)
        reopened.row_factory = sqlite3.Row
        self.addCleanup(reopened.close)
        scheduler.tick(reopened, self.tasks, self.downloads)
        self.assertEqual(self.get("sync_stage"), "")


class CookieExpiryTests(ReliabilityCase):
    def cookies(self, *dates):
        self.set(cookies_json=json.dumps([{"name": f"c{i}", "value": "v", **d} for i, d in enumerate(dates)]))

    def test_the_first_date_counts_in_either_format(self):
        self.cookies({"expires": 500.0}, {"expirationDate": 200.0}, {"expires": -1}, {})
        self.assertEqual(auth_service.cookie_expiry(self.conn), 200.0)

    def test_session_cookies_alone_give_no_date(self):
        self.cookies({"expires": -1}, {})
        self.assertIsNone(auth_service.cookie_expiry(self.conn))

    def test_expired_cookies_hold_the_sync_and_keep_it_due(self):
        self.signed_in()
        self.cookies({"expires": time.time() - 60})
        self.set(sync_enabled=1)
        self.tick()
        self.tasks.start_task.assert_not_called()
        self.assertIn("cookies", self.get("last_sync_result"))
        self.assertEqual(self.get("last_sync_epoch"), "")

    def check(self):
        self.downloads.storage.get_disk_metrics.return_value = {"nas": {}}
        return {c["key"]: c for c in health.report(self.conn, self.downloads)["checks"]}["cookies"]

    def test_health_warns_when_expired_or_close(self):
        self.cookies({"expires": time.time() - 60})
        self.assertIn("Expired", self.check()["detail"])
        self.cookies({"expires": time.time() + 3 * 3600})
        self.assertEqual(self.check()["state"], health.WARNING)
        self.cookies({"expires": time.time() + 30 * jobs.DAY})
        self.assertEqual(self.check()["state"], health.OK)


class IntegrityTests(ReliabilityCase):
    def test_a_due_check_uses_the_chosen_mode(self):
        self.set(integrity_enabled=1, integrity_mode="deep")
        self.tasks.start_task.return_value = {"success": True, "task_id": 3}
        self.tick()
        self.tasks.start_task.assert_called_once_with("vault_reconcile", {"scan_mode": "deep"})

    def test_an_offline_vault_waits(self):
        self.set(integrity_enabled=1)
        self.downloads.storage.get_root_path.side_effect = StorageUnavailableError("gone")
        self.tick()
        self.tasks.start_task.assert_not_called()
        self.assertIn("offline", self.get("last_integrity_result"))
        self.assertEqual(self.get("last_integrity_epoch"), "")

    def test_downloads_in_flight_hold_it_back(self):
        self.set(integrity_enabled=1)
        self.downloads.active_downloads = {1: {}}
        self.tick()
        self.tasks.start_task.assert_not_called()


class ScheduleFormTests(ReliabilityCase):
    def test_saving_is_audited(self):
        body, status = routes.handle_post_schedules(self.conn, body={
            "sync_enabled": True, "sync_interval_days": 3, "integrity_interval_days": 30, "integrity_mode": "deep",
        })
        self.assertEqual((status, body["schedules"]["sync_interval_days"]), (200, "3"))
        self.assertEqual(self.conn.execute("SELECT action FROM audit_log").fetchone()[0], "schedules_changed")

    def test_a_bad_interval_is_refused(self):
        for days in (0, 366, "x"):
            _, status = routes.handle_post_schedules(self.conn, body={"sync_interval_days": days, "integrity_interval_days": 30})
            self.assertEqual(status, 400)

    def test_a_bad_mode_is_refused(self):
        _, status = routes.handle_post_schedules(self.conn, body={
            "sync_interval_days": 7, "integrity_interval_days": 30, "integrity_mode": "rm",
        })
        self.assertEqual(status, 400)


class HealthTests(ReliabilityCase):
    def report(self):
        self.downloads.storage.get_disk_metrics.return_value = {"nas": {"mounted": True, "total_bytes": 100, "free_bytes": 50}}
        result = health.report(self.conn, self.downloads)
        return result, {c["key"]: c for c in result["checks"]}

    def test_a_token_near_expiry_warns(self):
        self.set(mmf_access_token="t", mmf_token_expires_at=int(time.time()) + 3600)
        self.assertEqual(self.report()[1]["sign_in"]["state"], health.WARNING)

    def test_an_expired_token_warns(self):
        self.assertIn("Expired", self.report()[1]["sign_in"]["detail"])

    def test_a_failed_backup_is_a_problem(self):
        self.set(last_backup_epoch=time.time(), last_backup_result="Failed: OSError")
        result, checks = self.report()
        self.assertEqual((checks["backup"]["state"], result["status"]), (health.PROBLEM, health.PROBLEM))

    def test_an_offline_vault_is_a_warning_not_a_problem(self):
        self.downloads.storage.get_disk_metrics.return_value = {"nas": {"mounted": False}}
        self.assertEqual({c["key"]: c for c in health.report(self.conn, self.downloads)["checks"]}["vault"]["state"], health.WARNING)

    def test_a_job_running_over_a_day_is_a_problem(self):
        with self.conn:
            self.conn.execute("INSERT INTO pipeline_tasks (task_type, status, started_at) VALUES ('x', 'running', datetime('now', '-2 days'))")
        self.assertEqual(self.report()[1]["tasks"]["state"], health.PROBLEM)

    def test_job_metrics_count_failures(self):
        with self.conn:
            for status in ("completed", "failed"):
                self.conn.execute(
                    "INSERT INTO pipeline_tasks (task_type, status, started_at, completed_at) "
                    "VALUES ('scan', ?, datetime('now', '-1 hour'), datetime('now'))", (status,),
                )
        row = self.report()[0]["jobs"][0]
        self.assertEqual((row["task_type"], row["runs"], row["failed"], row["average_seconds"]), ("scan", 2, 1, 3600))

    def test_the_report_only_reads_cached_disk_numbers(self):
        with patch.object(Path, "rglob") as walk:
            self.report()
        walk.assert_not_called()

    def test_the_public_probe_says_nothing_else(self):
        app = server.create_app(manage_workers=False)
        with patch("desync_archival.settings.login_required", return_value=True):
            response = TestClient(app, base_url="http://localhost").get("/healthz")
        self.assertEqual((response.status_code, response.json()), (200, {"status": "ok"}))


class CookieTests(ReliabilityCase):
    def stored(self):
        return {c["name"]: c["value"] for c in json.loads(self.get("cookies_json"))}

    def test_renewed_cookies_replace_old_ones_and_the_rest_stay(self):
        self.set(cookies_json=json.dumps([{"name": "cf_clearance", "value": "old"}, {"name": "PHPSESSID", "value": "s"}]))
        count = auth_service.keep_fresh_cookies(self.conn, json.dumps([{"name": "cf_clearance", "value": "new"}]))
        self.assertEqual((count, self.stored()), (1, {"cf_clearance": "new", "PHPSESSID": "s"}))
        self.assertTrue(self.get("cookies_saved_at"))

    def test_garbage_changes_nothing(self):
        self.set(cookies_json='[{"name":"a","value":"b"}]')
        for text in ("not json", "{}", "[]", '[{"name":"a","value":""}]'):
            self.assertEqual(auth_service.keep_fresh_cookies(self.conn, text), 0)
        self.assertEqual(self.stored(), {"a": "b"})


class NasAccessTests(ReliabilityCase):
    def test_disk_numbers_are_cached_between_polls(self):
        storage = StorageManager(self.db)
        with patch.object(storage, "_read_disk_metrics", return_value={"nas": {}}) as read:
            for _ in range(5):
                storage.get_disk_metrics()
        self.assertEqual(read.call_count, 1)

    def test_disk_numbers_refresh_after_the_cache_expires(self):
        storage = StorageManager(self.db)
        with patch.object(storage, "_read_disk_metrics", return_value={"nas": {}}) as read:
            storage.get_disk_metrics()
            storage._metrics = (time.monotonic() - storage.METRICS_SECONDS - 1, {})
            storage.get_disk_metrics()
        self.assertEqual(read.call_count, 2)

    def test_settings_gets_vault_totals_from_the_catalog(self):
        vault = self.base / "vault"
        vault.mkdir()
        with self.conn:
            self.conn.execute("DELETE FROM app_settings WHERE key = 'download_root_path'")
            self.conn.execute("INSERT INTO app_settings (key, value) VALUES ('download_root_path', ?)", (str(vault),))
        model = self.collection("9")
        with self.conn:
            file_id = self.conn.execute("INSERT INTO model_files (model_id, filename) VALUES (?, 'a.stl')", (model,)).lastrowid
            self.conn.execute(
                "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, actual_bytes, vault_path) "
                "VALUES (?, ?, 'a.stl', 'a.stl', 'a.stl', 42, ?)", (file_id, model, f"{vault}/a.stl"),
            )
        real = routes._fast_dir_stats
        with patch.object(routes, "_fast_dir_stats", side_effect=lambda p: (_ for _ in ()).throw(AssertionError(p)) if p == vault else real(p)):
            body, _ = routes.handle_get_directory_usage(self.conn)
        nas = next(d for d in body["directories"] if d["key"] == "nas")
        self.assertEqual((nas["file_count"], nas["size_bytes"]), (1, 42))


class PollLoggingTests(unittest.TestCase):
    def test_polls_log_quietly_and_failures_stay_visible(self):
        client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        with patch.object(server.logger, "log") as log:
            client.get("/healthz")
            client.get("/api/does-not-exist")
        levels = [call.args[0] for call in log.call_args_list]
        self.assertEqual(levels, [logging.DEBUG, logging.INFO])


class QueueTests(ReliabilityCase):
    def test_queueing_twice_keeps_one_job(self):
        model = self.collection("3")
        queue_models(self.conn, None, [model])
        queue_models(self.conn, None, [model])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM download_jobs").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
