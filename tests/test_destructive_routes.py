"""Destructive routes dry-run by default, act only on confirm, and always leave an audit row."""

import json
import os
import re
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
        conn.execute("DELETE FROM collections")
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
        conn.execute("INSERT INTO collections (id, name, url, raw_backup_path, vault_path) VALUES (9, 'M', '', '', '/v')")
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
        value = conn.execute("SELECT vault_path FROM collections WHERE id = 9").fetchone()[0]
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
        (self.old / "manifest.json").write_text("{}")
        stale = time.time() - 30 * 86400
        os.utime(self.old, (stale, stale))

    def test_both_aliases_dry_run_by_default(self):
        for path in ("/api/stashes/purge", "/api/settings/stashes/purge"):
            data = self.client.post(path, json={"days": 14}).json()
            self.assertEqual((data["dry_run"], data["would_purge"], data["reclaimable_bytes"]), (True, 1, 6))
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


# Each dry-runs without confirm, changes nothing, and leaves an audit row saying so.
DRY_RUN = {
    ("POST", "/api/reconcile/reset"): ({"model_ids": [1]}, "reconcile_reset"),
    ("POST", "/api/reconcile/delete-orphans"): ({"paths": ["/x"]}, "delete_orphans"),
    ("POST", "/api/reconcile/staging/purge"): ({"paths": []}, "staging_purge"),
    ("POST", "/api/stashes/purge"): ({"days": 14}, "stashes_purge"),
    ("POST", "/api/settings/stashes/purge"): ({"days": 14}, "stashes_purge"),
    ("POST", "/api/pending-deletion/purge"): ({}, "pending_deletion_purge"),
    ("POST", "/api/tasks/clear"): ({}, "tasks_clear"),
    ("POST", "/api/settings/logs/purge"): ({"days": 14}, "logs_purge"),
    ("DELETE", "/api/downloads/queue"): ({"purge_incomplete": True}, "queue_clear"),
    ("POST", "/api/downloads/dequeue"): ({"clear_status": "failed"}, "queue_clear"),
}
# These act at once, each for the reason given.
IMMEDIATE = {
    ("POST", "/api/reconcile/orphan-audit"): "An alias of delete-orphans",
    ("POST", "/api/reconcile/staging-purge"): "An alias of staging/purge",
    ("POST", "/api/reconcile/staging-sweep"): "An alias of staging/purge",
    ("POST", "/api/reconcile/duplicates/delete"): "Moves one file to Pending Deletion, which can be undone",
    ("DELETE", "/api/settings/libraries/{library_id}"): "Only forgets a link. It never touches the files",
    ("DELETE", "/api/settings/libraries/{library_id}/"): "The same route",
    ("DELETE", "/api/stashes/items/{item_id}"): "A stash is a list, so removing a row touches no file",
    ("DELETE", "/api/stashes/items/{item_id}/"): "The same route",
    ("DELETE", "/api/stashes/{stash_id}"): "A stash is a list, so removing one touches no file",
    ("DELETE", "/api/stashes/{stash_id}/"): "The same route",
    ("DELETE", "/api/{rest}"): "The catch-all that answers 404",
    ("DELETE", "/{rest}"): "The catch-all that answers 404",
}


class EveryDestructiveRouteTests(DestructiveCase):
    """Add a destructive route to DRY_RUN or IMMEDIATE, or the last test here fails."""

    def setUp(self):
        super().setUp()
        manager = object.__new__(TaskManager)
        manager.db_path = server.DB_PATH
        patcher = patch.object(server, "task_manager", manager)
        patcher.start()
        self.addCleanup(patcher.stop)

    def audit_rows(self, action):
        return [row for row in self.audit() if row[0] == action]

    def test_each_one_dry_runs_and_audits(self):
        for (method, path), (body, action) in DRY_RUN.items():
            with self.subTest(path=path):
                before = len(self.audit_rows(action))
                response = self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["dry_run"])
                rows = self.audit_rows(action)
                self.assertEqual(len(rows), before + 1)
                self.assertEqual(rows[-1][1], 1)

    def test_the_control_route_dry_runs_too(self):
        with patch.object(server, "download_manager", MagicMock()):
            data = self.client.post("/api/downloads/control", json={"action": "purge_incomplete"}).json()
        self.assertTrue(data["dry_run"])

    def test_no_destructive_route_goes_unlisted(self):
        word = re.compile(r"purge|clear|reset|delete|remove|prune|unlink|sweep|orphan-audit")
        app = server.create_app(manage_workers=False)
        found = set()
        for route in app.routes:
            for method in getattr(route, "methods", None) or ():
                path = re.sub(r":\w+}", "}", route.path)
                if method == "DELETE" or (method == "POST" and word.search(path)):
                    found.add((method, path))
        listed = set(DRY_RUN) | set(IMMEDIATE)
        self.assertEqual(found - listed, set())


class QueueClearS09Tests(DestructiveCase):
    def job(self, status):
        conn = get_db_connection(server.DB_PATH)
        with conn:
            model = conn.execute(
                "INSERT INTO collections (name, source, raw_backup_path) VALUES (?, 'mmf', 'm.json')", (status,)
            ).lastrowid
            conn.execute("INSERT INTO download_jobs (model_id, status) VALUES (?, ?)", (model, status))
        conn.close()
        return model

    def jobs(self):
        conn = get_db_connection(server.DB_PATH)
        rows = sorted(row[0] for row in conn.execute("SELECT status FROM download_jobs"))
        conn.close()
        return rows

    def delete(self, body):
        return self.client.request("DELETE", "/api/downloads/queue", json=body)

    def test_purging_lists_first_and_changes_nothing(self):
        self.job("pending")
        self.job("failed")
        data = self.delete({"purge_incomplete": True}).json()
        self.assertEqual((data["dry_run"], data["would_remove"]), (True, 2))
        self.assertEqual(len(self.jobs()), 2)

    def test_purging_with_confirm_removes_and_audits(self):
        self.job("pending")
        self.job("downloading")
        self.assertFalse(self.delete({"purge_incomplete": True, "confirm": True}).json().get("dry_run"))
        self.assertEqual(self.jobs(), ["downloading"])
        self.assertEqual(self.audit()[-1][:3], ("queue_clear", 0, "ok"))

    def test_clearing_a_status_lists_first(self):
        self.job("failed")
        self.assertEqual(self.delete({"clear_status": "failed"}).json()["would_remove"], 1)
        self.assertEqual(self.jobs(), ["failed"])
        self.delete({"clear_status": "failed", "confirm": True})
        self.assertEqual(self.jobs(), [])

    def test_an_unknown_status_is_refused(self):
        self.assertEqual(self.delete({"clear_status": "nonsense", "confirm": True}).status_code, 400)

    def test_removing_chosen_models_acts_at_once_and_is_audited(self):
        model = self.job("pending")
        self.assertEqual(self.delete({"model_ids": [model]}).json()["removed"], 1)
        self.assertEqual(self.audit()[-1][:3], ("queue_remove", 0, "ok"))

    def test_the_control_purge_needs_confirm(self):
        self.job("pending")
        with patch.object(server, "download_manager", MagicMock()):
            self.client.post("/api/downloads/control", json={"action": "purge_incomplete"})
            self.assertEqual(self.jobs(), ["pending"])
            self.client.post("/api/downloads/control", json={"action": "purge_incomplete", "confirm": True})
        self.assertEqual(self.jobs(), [])


class LogPurgeS10Tests(DestructiveCase):
    def setUp(self):
        super().setUp()
        self.logs = self.base / "logs"
        self.logs.mkdir()
        self.old = self.logs / "vault.log.1"
        self.new = self.logs / "vault.log.2"
        self.old.write_bytes(b"12345")
        self.new.write_bytes(b"123")
        stale = time.time() - 30 * 86400
        os.utime(self.old, (stale, stale))
        patcher = patch("desync_archival.api.routes.settings.LOGS_DIR", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def purge(self, **body):
        return self.client.post("/api/settings/logs/purge", json=body)

    def test_the_default_only_reports(self):
        data = self.purge(days=14).json()
        self.assertEqual((data["dry_run"], data["would_delete_files"], data["reclaimable_bytes"]), (True, 1, 5))
        self.assertTrue(self.old.exists())

    def test_only_logs_older_than_days_go(self):
        data = self.purge(days=14, confirm=True).json()
        self.assertEqual((data["purged_files_count"], data["reclaimed_disk_bytes"]), (1, 5))
        self.assertFalse(self.old.exists())
        self.assertTrue(self.new.exists())

    def test_the_audit_row_is_written_before_anything_goes(self):
        self.purge(days=14, confirm=True)
        row = [r for r in self.audit() if r[0] == "logs_purge"][-1]
        self.assertEqual((row[1], row[2]), (0, "ok"))
        self.assertEqual(json.loads(row[3])["files"], 1)

    def test_a_file_that_wont_delete_is_reported(self):
        with patch.object(Path, "unlink", side_effect=OSError(13, "Permission denied")):
            response = self.purge(days=14, confirm=True)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.json()["success"])
        self.assertIn("vault.log.1", response.json()["error"])

    def test_a_days_value_below_one_is_refused(self):
        self.assertEqual(self.purge(days=0, confirm=True).status_code, 400)
