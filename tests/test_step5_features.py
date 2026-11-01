"""Scraper events, resumable downloads, scheduled backups and the pause before token expiry."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from db_helper import create_database

from desync_archival.services import auth_service, jobs
from desync_archival.services.db import get_db_connection
from desync_archival.services.downloader import DownloadManager, can_resume
from desync_archival.services.task_manager import TaskManager, parse_scrape_event

REPO_ROOT = Path(__file__).resolve().parent.parent


class TempDb(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.db = self.base / "library.db"
        create_database(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def task_manager(self):
        manager = object.__new__(TaskManager)
        manager.db_path = self.db
        manager._lock = threading.Lock()
        manager.active_task_id = None
        manager.active_process = None
        manager._cancel_requested = False
        manager._scrape_error = ""
        return manager

    def add_task(self):
        conn = sqlite3.connect(self.db)
        task_id = conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('x', 'running')").lastrowid
        conn.commit()
        conn.close()
        return task_id

    def task(self, task_id):
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT status, progress_pct, progress_label, log_output FROM pipeline_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        conn.close()
        return row


class ScrapeEventTests(TempDb):
    def test_events_parse_and_other_text_stays_plain(self):
        self.assertEqual(parse_scrape_event('{"event": "cycle", "count": 3}')["count"], 3)
        for line in ("Error: boom", "[1, 2]", '{"no": "event"}', ""):
            self.assertIsNone(parse_scrape_event(line))

    def test_cycle_updates_progress_against_the_catalog_size(self):
        manager, task_id = self.task_manager(), self.add_task()
        manager._handle_scrape_line(task_id, '{"event": "cycle", "cycle": 2, "count": 50}', 100)
        _, pct, label, log = self.task(task_id)
        self.assertEqual(pct, 50.0)
        self.assertIn("Cycle 2: 50", label)
        self.assertIn("50 unique models", log)

    def test_more_models_than_expected_stays_in_range(self):
        manager, task_id = self.task_manager(), self.add_task()
        manager._handle_scrape_line(task_id, '{"event": "cycle", "cycle": 1, "count": 900}', 100)
        self.assertLessEqual(self.task(task_id)[1], 90.0)

    def test_empty_catalog_does_not_divide_by_zero(self):
        manager, task_id = self.task_manager(), self.add_task()
        manager._handle_scrape_line(task_id, '{"event": "cycle", "cycle": 1, "count": 0}', 0)
        self.assertEqual(self.task(task_id)[1], 10.0)

    def test_plain_node_errors_reach_the_task_log(self):
        manager, task_id = self.task_manager(), self.add_task()
        manager._handle_scrape_line(task_id, "TypeError: boom", 10)
        self.assertIn("TypeError: boom", self.task(task_id)[3])

    def test_scraper_error_becomes_the_task_failure(self):
        manager, task_id = self.task_manager(), self.add_task()
        manager._handle_scrape_line(task_id, '{"event": "error", "message": "login needed"}', 10)
        self.assertEqual(manager._scrape_error, "login needed")
        self.assertIn("login needed", self.task(task_id)[3])

    def test_scraper_marks_cookies_secure_and_can_run_headless(self):
        source = (REPO_ROOT / "scraper" / "scrape.js").read_text(encoding="utf-8")
        self.assertIn("secure: true", source)
        self.assertIn("no_restriction: 'None'", source)
        self.assertEqual(source.count("      headless,\n"), 2)

    def test_scraper_source_keeps_its_safety_changes(self):
        source = (REPO_ROOT / "scraper" / "scrape.js").read_text(encoding="utf-8")
        self.assertNotIn("--no-sandbox", source)
        self.assertNotIn("4868", source)
        self.assertNotIn("response.url()}", source)
        self.assertIn(".pathname", source)


class ScrapedIdTests(unittest.TestCase):
    """Runs the scraper's real normalizeId, because the API 404s on prefixed IDs."""

    def normalize(self, value):
        source = (REPO_ROOT / "scraper" / "scrape.js").read_text(encoding="utf-8")
        function = re.search(r"function normalizeId\(raw\) \{.*?\n\}", source, re.DOTALL).group(0)
        script = f"{function}\nconsole.log(JSON.stringify(normalizeId({json.dumps(value)})));"
        return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_ids_become_plain_digits(self):
        self.assertEqual(self.normalize("object-99104"), "99104")
        self.assertEqual(self.normalize("99104"), "99104")
        self.assertEqual(self.normalize(99104), "99104")

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_anything_else_is_dropped(self):
        for value in ("", "purchase-12", "abc", "object-12x", "12 34"):
            self.assertEqual(self.normalize(value), "", value)


class JobClaimTests(TempDb):
    """Many workers race for jobs on a real database file, as the download workers do."""

    def race(self, status, claim_name):
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA foreign_keys = OFF")
        for model_id in range(1, 31):
            conn.execute("INSERT INTO models (id, name, url, raw_backup_path) VALUES (?, 'M', '', '')", (model_id,))
            conn.execute("INSERT INTO download_jobs (model_id, status) VALUES (?, ?)", (model_id, status))
        conn.commit()
        conn.close()

        manager = object.__new__(DownloadManager)
        manager.db_path = self.db
        claimed = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait(timeout=10)
            misses = 0
            while misses < 20:
                job = getattr(manager, claim_name)()
                if job is None:
                    misses += 1
                    continue
                misses = 0
                with lock:
                    claimed.append(job["model_id"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return claimed

    def test_each_download_job_goes_to_exactly_one_worker(self):
        claimed = self.race("pending", "_claim_next_download_job")
        self.assertEqual(sorted(claimed), list(range(1, 31)))

    def test_each_offload_job_goes_to_exactly_one_worker(self):
        claimed = self.race("staging_complete", "_claim_next_offload_job")
        self.assertEqual(sorted(claimed), list(range(1, 31)))


class CanResumeTests(unittest.TestCase):
    def test_only_a_matching_206_resumes(self):
        self.assertTrue(can_resume(206, "bytes 100-199/200", 100))
        for status, header, offset in (
            (200, None, 100), (206, "bytes 0-199/200", 100), (206, None, 100), (206, "nonsense", 100), (206, "bytes 0-9/9", 0),
        ):
            self.assertFalse(can_resume(status, header, offset), (status, header, offset))


class ResumeOverHttpTests(TempDb):
    """Runs the streaming helpers over a real HTTP transport with a fake file host."""

    content = bytes(range(256)) * 40

    def host(self, honor_range=True, bad_start=False):
        seen = []

        def handler(request):
            seen.append(request.headers.get("Range"))
            header = request.headers.get("Range")
            if header and honor_range:
                start = int(header.split("=")[1].rstrip("-"))
                reported = 0 if bad_start else start
                return httpx.Response(
                    206, content=self.content[start:],
                    headers={"Content-Range": f"bytes {reported}-{len(self.content) - 1}/{len(self.content)}"},
                )
            return httpx.Response(200, content=self.content)

        return httpx.Client(transport=httpx.MockTransport(handler)), seen

    def fetch(self, client, part, resume_from):
        """The same steps as the download worker."""
        headers = {"Range": f"bytes={resume_from}-"} if resume_from else None
        hasher = hashlib.sha256()
        with client.stream("GET", "https://files.example/a.zip", headers=headers) as resp:
            resuming = can_resume(resp.status_code, resp.headers.get("Content-Range"), resume_from)
            if resuming:
                hasher.update(part.read_bytes())
            with open(part, "ab" if resuming else "wb") as out:
                for chunk in resp.iter_bytes():
                    out.write(chunk)
                    hasher.update(chunk)
        return resuming, hasher.hexdigest()

    def test_partial_file_resumes_to_a_complete_correct_file(self):
        part = self.base / "a.part"
        part.write_bytes(self.content[:1000])
        client, seen = self.host()
        resumed, digest = self.fetch(client, part, 1000)
        self.assertTrue(resumed)
        self.assertEqual(seen, ["bytes=1000-"])
        self.assertEqual(part.read_bytes(), self.content)
        self.assertEqual(digest, hashlib.sha256(self.content).hexdigest())

    def test_host_without_range_support_restarts_cleanly(self):
        part = self.base / "a.part"
        part.write_bytes(b"stale" * 100)
        client, _ = self.host(honor_range=False)
        resumed, digest = self.fetch(client, part, part.stat().st_size)
        self.assertFalse(resumed)
        self.assertEqual(part.read_bytes(), self.content)
        self.assertEqual(digest, hashlib.sha256(self.content).hexdigest())

    def test_range_from_the_wrong_offset_is_not_appended(self):
        part = self.base / "a.part"
        part.write_bytes(self.content[:1000])
        client, _ = self.host(bad_start=True)
        resumed, _ = self.fetch(client, part, 1000)
        self.assertFalse(resumed)
        self.assertEqual(part.stat().st_size, len(self.content) - 1000)

    def test_no_range_header_on_a_first_attempt(self):
        client, seen = self.host()
        self.fetch(client, self.base / "new.part", 0)
        self.assertEqual(seen, [None])


class ReconnectTests(unittest.TestCase):
    def manager(self, token="t", expires_in=3600):
        manager = object.__new__(DownloadManager)
        manager.access_token = token
        manager.token_expires_at = int(time.time()) + expires_in
        return manager

    def test_token_with_time_left_does_not_pause(self):
        self.assertFalse(self.manager().needs_reconnect())

    def test_token_near_expiry_pauses(self):
        self.assertTrue(self.manager(expires_in=300).needs_reconnect())
        self.assertTrue(self.manager(expires_in=-300).needs_reconnect())

    def test_cookie_mode_does_not_pause(self):
        self.assertFalse(self.manager(token="", expires_in=-5).needs_reconnect())

    def test_unknown_expiry_does_not_pause(self):
        manager = self.manager()
        manager.token_expires_at = 0
        self.assertFalse(manager.needs_reconnect())

    def test_signing_in_clears_the_pause(self):
        temp = tempfile.TemporaryDirectory()
        db = Path(temp.name) / "library.db"
        create_database(db)
        manager = self.manager(expires_in=10)
        manager.db_path = db
        manager.storage = MagicMock()
        conn = get_db_connection(db)
        auth_service.set_settings(conn, {
            "mmf_access_token": "new", "mmf_token_expires_at": str(int(time.time()) + 86400),
        })
        conn.close()
        with patch.object(DownloadManager, "_get_db", lambda self: get_db_connection(db)):
            manager._load_settings()
        self.assertFalse(manager.needs_reconnect())
        temp.cleanup()


class BackupScheduleTests(TempDb):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {
            "DESYNC_BACKUP_DIR": str(self.base / "backups"), "DESYNC_DATA_DIR": str(self.base / "data"),
        })
        self.env.start()
        (self.base / "data").mkdir()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def setting(self, key):
        conn = get_db_connection(self.db)
        try:
            return auth_service.get_setting(conn, key)
        finally:
            conn.close()

    def test_due_backup_runs_through_the_job_runner_and_is_recorded(self):
        manager = self.task_manager()
        self.assertTrue(manager.run_scheduled_backup())
        deadline = time.monotonic() + 10
        while not self.setting("last_backup_at") and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.setting("last_backup_result").startswith("OK:"))
        backups = list((self.base / "backups").glob("library-*.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_recent_backup_is_not_repeated(self):
        conn = get_db_connection(self.db)
        auth_service.set_settings(conn, {"last_backup_epoch": str(time.time())})
        conn.close()
        self.assertFalse(self.task_manager().run_scheduled_backup())

    def test_no_backup_folder_means_no_backup(self):
        with patch.dict(os.environ, {"DESYNC_BACKUP_DIR": ""}):
            self.assertFalse(self.task_manager().run_scheduled_backup())

    def test_a_running_pipeline_task_delays_the_backup(self):
        self.add_task()
        self.assertFalse(self.task_manager().run_scheduled_backup())

    def test_failed_backup_is_recorded_and_finishes_the_task(self):
        manager = self.task_manager()
        task_id = self.add_task()
        with patch.dict(os.environ, {"DESYNC_BACKUP_DIR": str(self.base / "data" / "inside")}):
            manager._run_backup_worker(task_id, {})
        self.assertTrue(self.setting("last_backup_result").startswith("Failed"))
        self.assertEqual(self.task(task_id)[0], jobs.FAILED)


if __name__ == "__main__":
    unittest.main()


class BackupIntervalTests(unittest.TestCase):
    def test_backups_are_monthly(self):
        self.assertEqual(jobs.BACKUP_INTERVAL, 30 * 24 * 3600)
