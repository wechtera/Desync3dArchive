"""Destructive routes dry-run by default, act only on confirm, and always leave an audit row."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival.api import server
from desync_archival.services import auth_service
from desync_archival.services.db import get_db_connection
from desync_archival.services.task_manager import TaskManager


class DestructiveCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.vault = self.base / "vault"
        self.work = self.base / "work"
        self.stash = self.base / "stash"
        for folder in (self.vault, self.work / "in_flight", self.stash):
            folder.mkdir(parents=True)
        create_database(server.DB_PATH)
        conn = get_db_connection(server.DB_PATH)
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM pipeline_tasks")
        conn.execute("DELETE FROM download_jobs")
        conn.execute("DELETE FROM downloaded_files")
        conn.execute("DELETE FROM models")
        conn.commit()
        auth_service.set_settings(conn, {
            "download_root_path": str(self.vault), "download_work_path": str(self.work),
            "stash_download_path": str(self.stash),
        })
        conn.close()
        self.client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")

    def tearDown(self):
        conn = get_db_connection(server.DB_PATH)
        auth_service.set_settings(conn, {"download_root_path": "", "download_work_path": "", "stash_download_path": ""})
        conn.close()
        self.temp.cleanup()

    def audit(self):
        conn = sqlite3.connect(server.DB_PATH)
        rows = conn.execute("SELECT action, dry_run, outcome, detail, request_id FROM audit_log ORDER BY id").fetchall()
        conn.close()
        return rows


class OrphanTests(DestructiveCase):
    def setUp(self):
        super().setUp()
        self.orphan = self.vault / "Creator" / "orphan"
        self.orphan.mkdir(parents=True)
        (self.orphan / "file.stl").write_bytes(b"12345")

    def test_default_is_a_dry_run_that_deletes_nothing(self):
        data = self.client.post("/api/reconcile/delete-orphans", json={"paths": [str(self.orphan)]}).json()
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["would_delete"], [str(self.orphan)])
        self.assertEqual(data["reclaimable_bytes"], 5)
        self.assertTrue(self.orphan.exists())
        self.assertEqual(self.audit()[0][:3], ("delete_orphans", 1, "ok"))

    def test_confirm_deletes_and_is_audited(self):
        body = {"paths": [str(self.orphan)], "confirm": True}
        data = self.client.post("/api/reconcile/orphan-audit", json=body).json()
        self.assertEqual(data["deleted_count"], 1)
        self.assertFalse(self.orphan.exists())
        action, dry_run, _, detail, request_id = self.audit()[0]
        self.assertEqual((action, dry_run), ("delete_orphans", 0))
        self.assertIn(str(self.orphan), detail)
        self.assertTrue(request_id)

    def test_outside_the_vault_is_refused_even_with_confirm(self):
        outside = self.base / "keep"
        outside.mkdir()
        data = self.client.post("/api/reconcile/delete-orphans", json={"paths": [str(outside)], "confirm": True}).json()
        self.assertEqual(data["deleted_count"], 0)
        self.assertTrue(outside.exists())

    def test_string_confirm_is_refused(self):
        response = self.client.post("/api/reconcile/delete-orphans", json={"paths": ["x"], "confirm": "maybe"})
        self.assertEqual(response.status_code, 400)


class StagingTests(DestructiveCase):
    def setUp(self):
        super().setUp()
        self.garbage = self.work / "in_flight" / "stale.part"
        self.garbage.write_bytes(b"123")

    def test_dry_run_reports_and_keeps_the_file(self):
        body = {"paths": [str(self.garbage)]}
        data = self.client.post("/api/reconcile/staging/purge", json=body).json()
        self.assertEqual((data["dry_run"], data["would_purge"], data["reclaimable_bytes"]), (True, 1, 3))
        self.assertTrue(self.garbage.exists())
        self.assertEqual(self.audit()[0][:2], ("staging_purge", 1))

    def test_confirm_purges(self):
        body = {"paths": [str(self.garbage)], "confirm": True}
        data = self.client.post("/api/reconcile/staging-purge", json=body).json()
        self.assertEqual(data["purged_count"], 1)
        self.assertFalse(self.garbage.exists())
        self.assertEqual(self.audit()[0][:2], ("staging_purge", 0))


class ResetTests(DestructiveCase):
    def setUp(self):
        super().setUp()
        conn = get_db_connection(server.DB_PATH)
        conn.execute("INSERT INTO models (id, name, url, raw_backup_path, vault_path) VALUES (9, 'M', '', '', '/v')")
        conn.execute("INSERT INTO model_files (file_id, model_id, filename) VALUES (1, 9, 'a')")
        conn.execute("""
            INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename,
                                          local_relative_path, actual_bytes)
            VALUES (1, 9, 'a', 'a', 'a', 1)
        """)
        conn.commit()
        conn.close()

    def vault_path(self):
        conn = sqlite3.connect(server.DB_PATH)
        value = conn.execute("SELECT vault_path FROM models WHERE id = 9").fetchone()[0]
        conn.close()
        return value

    def test_dry_run_changes_nothing(self):
        data = self.client.post("/api/reconcile/reset", json={"model_ids": [9]}).json()
        self.assertEqual((data["dry_run"], data["would_reset"], data["file_records"]), (True, 1, 1))
        self.assertEqual(self.vault_path(), "/v")

    def test_confirm_resets(self):
        self.client.post("/api/reconcile/reset", json={"model_ids": [9], "confirm": True})
        self.assertIsNone(self.vault_path())
        self.assertEqual(self.audit()[0][:2], ("reconcile_reset", 0))


class TaskClearTests(DestructiveCase):
    def setUp(self):
        super().setUp()
        manager = object.__new__(TaskManager)
        manager.db_path = server.DB_PATH
        self.patcher = patch.object(server, "task_manager", manager)
        self.patcher.start()
        conn = get_db_connection(server.DB_PATH)
        conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('t', 'completed'), ('t', 'running')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.patcher.stop()
        super().tearDown()

    def count(self):
        conn = sqlite3.connect(server.DB_PATH)
        value = conn.execute("SELECT count(*) FROM pipeline_tasks").fetchone()[0]
        conn.close()
        return value

    def test_dry_run_counts_only_finished_tasks(self):
        data = self.client.post("/api/tasks/clear", json={}).json()
        self.assertEqual(data["would_delete"], 1)
        self.assertEqual(self.count(), 2)

    def test_confirm_clears_and_keeps_running_tasks(self):
        self.assertEqual(self.client.post("/api/tasks/clear", json={"confirm": True}).json()["deleted_count"], 1)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.audit()[0][:2], ("tasks_clear", 0))

    def test_empty_post_never_clears(self):
        self.client.post("/api/tasks/clear")
        self.assertEqual(self.count(), 2)


class StashPurgeTests(DestructiveCase):
    def setUp(self):
        super().setUp()
        self.old = self.stash / "old_batch"
        self.old.mkdir()
        (self.old / "a.stl").write_bytes(b"1234")
        stale = time.time() - 30 * 86400
        os.utime(self.old, (stale, stale))

    def test_both_aliases_dry_run_by_default(self):
        for path in ("/api/stashes/purge", "/api/settings/stashes/purge"):
            data = self.client.post(path, json={"days": 14}).json()
            self.assertEqual((data["dry_run"], data["would_purge"], data["reclaimable_bytes"]), (True, 1, 4))
        self.assertTrue(self.old.exists())
        self.assertEqual([row[:2] for row in self.audit()], [("stashes_purge", 1), ("stashes_purge", 1)])

    def test_confirm_purges(self):
        data = self.client.post("/api/stashes/purge", json={"days": 14, "confirm": True}).json()
        self.assertEqual(data["purged_count"], 1)
        self.assertFalse(self.old.exists())
        self.assertEqual(self.audit()[0][:2], ("stashes_purge", 0))
        self.assertEqual(json.loads(self.audit()[0][3])["purged_count"], 1)


class NotFoundTests(DestructiveCase):
    def test_unknown_route_and_wrong_method_are_404(self):
        for method, path in (("GET", "/api/nope"), ("POST", "/api/metrics"), ("DELETE", "/api/settings")):
            response = self.client.request(method, path)
            self.assertEqual(response.status_code, 404, path)
            self.assertEqual(response.json(), {"error": f"Route not found: {method} {path}"})

    def test_the_old_dispatcher_is_gone(self):
        self.assertFalse(hasattr(server, "dispatch_route"))
        self.assertFalse(hasattr(MagicMock, "dispatch_route"))
