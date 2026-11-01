"""Jobs survive a killed app, crashes stay contained, and time limits apply."""

import http.client
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database

from desync_archival.services import jobs
from desync_archival.services.db import get_db_connection
from desync_archival.services.task_manager import TaskManager, parse_cookies


def seed(db_path):
    conn = get_db_connection(db_path)
    conn.executescript("""
        INSERT INTO collections (id, name, url, raw_backup_path) VALUES (1, 'A', '', ''), (2, 'B', '', ''), (3, 'C', '', '');
        INSERT INTO pipeline_tasks (task_type, status) VALUES ('library_scrape', 'running'), ('vault_reconcile', 'completed');
        INSERT INTO download_jobs (model_id, status) VALUES (1, 'downloading'), (2, 'offloading'), (3, 'failed');
    """)
    conn.commit()
    conn.close()


def states(db_path):
    conn = sqlite3.connect(db_path)
    result = {
        "tasks": [row[0] for row in conn.execute("SELECT status FROM pipeline_tasks ORDER BY id")],
        "jobs": [row[0] for row in conn.execute("SELECT status FROM download_jobs ORDER BY model_id")],
    }
    conn.close()
    return result


class TempDb(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.db = self.base / "library.db"
        create_database(self.db)
        seed(self.db)

    def tearDown(self):
        self.temp.cleanup()


class RecoveryTests(TempDb):
    def test_in_progress_work_becomes_recoverable(self):
        conn = get_db_connection(self.db)
        result = jobs.recover_interrupted(conn)
        conn.close()
        self.assertEqual(result, {"tasks_interrupted": 1, "downloads_requeued": 1, "offloads_requeued": 1})
        self.assertEqual(states(self.db), {
            "tasks": ["interrupted", "completed"], "jobs": ["pending", "staging_complete", "failed"],
        })

    def test_recovery_is_idempotent(self):
        conn = get_db_connection(self.db)
        jobs.recover_interrupted(conn)
        self.assertEqual(sum(jobs.recover_interrupted(conn).values()), 0)
        conn.close()


class KilledAppTests(TempDb):
    """Kills a real server process with SIGKILL and restarts it on the same data."""

    def env(self, port):
        return {
            **os.environ, "APP_ENV": "test", "DESYNC_DATA_DIR": str(self.base), "DESYNC_PORT": str(port),
            "DESYNC_ALLOWED_ROOTS": str(self.base),
        }

    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-m", "desync_archival.api.server"],
            env=self.env(port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                return process, port, self.get(port, "/api/tasks/status")
            except ConnectionError:
                time.sleep(0.1)
        process.kill()
        self.fail("The server did not start")

    @staticmethod
    def get(port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path)
        body = json.loads(connection.getresponse().read())
        connection.close()
        return body

    def test_kill_mid_job_then_restart_recovers(self):
        process, _, _ = self.start()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE pipeline_tasks SET status = 'running'")
        conn.execute("UPDATE download_jobs SET status = 'downloading' WHERE model_id = 3")
        conn.commit()
        conn.close()
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
        self.assertIn("running", states(self.db)["tasks"])

        process, _, status = self.start()
        try:
            self.assertIsNone(status["active_task"])
            after = states(self.db)
            self.assertNotIn("running", after["tasks"])
            self.assertNotIn("downloading", after["jobs"])
        finally:
            process.terminate()
            process.wait(timeout=15)


class SpawnTests(TempDb):
    def manager(self):
        manager = object.__new__(TaskManager)
        manager.db_path = self.db
        manager._lock = threading.Lock()
        manager.active_task_id = 1
        manager.active_process = None
        manager._cancel_events = {1: threading.Event()}
        return manager

    def test_a_crashing_worker_fails_its_task_and_frees_the_slot(self):
        manager = self.manager()
        finished = threading.Event()

        def crash():
            try:
                raise RuntimeError("private detail")
            finally:
                finished.set()

        manager._spawn(1, "vault_reconcile", crash, ())
        finished.wait(5)
        deadline = time.monotonic() + 5
        while manager.active_task_id is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(manager.active_task_id)
        conn = sqlite3.connect(self.db)
        status, label = conn.execute("SELECT status, progress_label FROM pipeline_tasks WHERE id = 1").fetchone()
        conn.close()
        self.assertEqual(status, "failed")
        self.assertNotIn("private detail", label)

    def test_time_limit_cancels_the_active_task(self):
        manager = self.manager()
        with patch.object(jobs, "time_limit", return_value=0.05):
            release = threading.Event()
            cancel = manager._cancel_events[1]
            manager._spawn(1, "library_scrape", lambda: release.wait(2), ())
            cancel.wait(3)
            release.set()
        self.assertTrue(cancel.is_set())
        deadline = time.monotonic() + 5
        while manager.active_task_id is not None and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_time_limit_ignores_a_finished_task(self):
        manager = self.manager()
        manager.active_task_id = 99
        manager._on_time_limit(1, "library_scrape")
        self.assertFalse(manager._cancelled(1))


class ReleaseRebuildTests(TempDb):
    def test_ingest_rebuild_adds_new_releases(self):
        conn = sqlite3.connect(self.db)
        conn.execute("""
            UPDATE collections SET designer_name = 'Maker', designer_id = 9, published_at = '2026-10-01' WHERE id = 1
        """)
        conn.commit()
        conn.close()
        manager = object.__new__(TaskManager)
        manager.db_path = self.db
        manager._rebuild_releases(1)
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT designer_name, release_month FROM releases").fetchall()
        conn.close()
        self.assertIn(("Maker", "2026-10"), rows)

    def test_a_failed_rebuild_does_not_raise(self):
        manager = object.__new__(TaskManager)
        manager.db_path = self.db
        with patch("desync_archival.services.task_manager.run_startup_analytics", side_effect=RuntimeError("x")):
            manager._rebuild_releases(1)


class CookieParsingTests(unittest.TestCase):
    def test_list_and_dict_forms(self):
        self.assertEqual(parse_cookies('[{"name": "a", "value": 1}, {"bad": 2}]'), {"a": "1"})
        self.assertEqual(parse_cookies('{"a": "1", "b": 2}'), {"a": "1", "b": "2"})

    def test_bad_input_gives_nothing(self):
        for text in ("", "not json", "5", '"text"'):
            self.assertEqual(parse_cookies(text), {})


if __name__ == "__main__":
    unittest.main()
