"""E50: everything but the file download itself goes to MMF as a plain API client.

Tested against the live API on 2026-11-01: /user, /objects/{id}, /objects/{id}/files and /files/{id}
all answer a plain client with a token. Files sit on the website, behind a Cloudflare challenge.
"""

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from db_helper import create_database
from test_fix_first import FILE_URL, DownloadCase

from desync_archival.services import auth_service
from desync_archival.services.task_manager import TaskManager

REAL_ASYNC_CLIENT = httpx.AsyncClient
LIVE_URL = "https://www.myminifactory.com/download/9?downloadfile=new.zip"


def api_reply(url=FILE_URL):
    return httpx.Response(200, json={"files": {"items": [{"id": 9, "download_url": url, "size": 1000}]}})


class Requests:
    """A fake MMF that records what it was sent."""

    def __init__(self, content):
        self.content = content
        self.seen = []
        self.dead = set()

    def __call__(self, request):
        self.seen.append(request)
        if "/api/v2/" in str(request.url):
            return self.api(request)
        if str(request.url) in self.dead:
            return httpx.Response(404, text="gone")
        return httpx.Response(200, content=self.content)

    def api(self, request):
        return api_reply()

    def api_calls(self):
        return [r for r in self.seen if "/api/v2/" in str(r.url)]

    def file_calls(self):
        return [r for r in self.seen if "/api/v2/" not in str(r.url)]


class LinkRefreshTests(DownloadCase):
    def setUp(self):
        super().setUp()
        self.dm.access_token = "token"
        self.server = Requests(self.CONTENT)

    def set_link(self, url):
        with self.conn:
            self.conn.execute("UPDATE model_files SET download_url = ? WHERE file_id = 9", (url,))

    def link(self):
        return self.conn.execute("SELECT download_url FROM model_files WHERE file_id = 9").fetchone()[0]

    def test_a_working_link_makes_no_api_call(self):
        self.stage(self.server)
        self.assertEqual(self.server.api_calls(), [])
        self.assertEqual(len(self.server.file_calls()), 1)

    def test_a_missing_link_is_looked_up_first(self):
        self.set_link("")
        self.server.api = lambda request: api_reply(LIVE_URL)
        self.stage(self.server)
        self.assertEqual(len(self.server.api_calls()), 1)
        self.assertEqual(self.link(), LIVE_URL)

    def test_a_dead_link_is_checked_replaced_and_retried(self):
        self.server.dead.add(FILE_URL)
        self.server.api = lambda request: api_reply(LIVE_URL)
        self.stage(self.server)
        self.assertEqual(len(self.server.api_calls()), 1)
        self.assertEqual([str(r.url) for r in self.server.file_calls()], [FILE_URL, LIVE_URL])
        self.assertEqual(self.link(), LIVE_URL)

    def test_a_dead_link_the_api_confirms_fails_with_no_second_try(self):
        self.server.dead.add(FILE_URL)
        with self.assertRaises(RuntimeError) as caught:
            self.stage(self.server)
        self.assertIn("404", str(caught.exception))
        self.assertEqual(len(self.server.file_calls()), 1)

    def test_a_retried_link_only_gets_one_more_try(self):
        self.server.dead.update({FILE_URL, LIVE_URL})
        self.server.api = lambda request: api_reply(LIVE_URL)
        with self.assertRaises(RuntimeError):
            self.stage(self.server)
        self.assertEqual(len(self.server.file_calls()), 2)

    def test_the_link_check_is_a_plain_api_client(self):
        self.set_link("")
        self.server.api = lambda request: api_reply(LIVE_URL)
        self.stage(self.server)
        request = self.server.api_calls()[0]
        self.assertTrue(request.headers["user-agent"].startswith("python-httpx/"))
        self.assertEqual(request.headers["authorization"], "Bearer token")
        for name in ("referer", "cookie"):
            self.assertNotIn(name, request.headers)

    def test_with_no_sign_in_a_dead_link_just_fails(self):
        self.dm.access_token = ""
        self.server.dead.add(FILE_URL)
        with self.assertRaises(RuntimeError):
            self.stage(self.server)
        self.assertEqual(self.server.api_calls(), [])

    def test_an_expired_token_trips_the_breaker(self):
        self.set_link("")
        self.server.api = lambda request: httpx.Response(401)
        with patch.object(self.dm, "trip_circuit_breaker") as trip, self.assertRaises(RuntimeError):
            self.stage(self.server)
        trip.assert_called_once()

    def test_a_network_failure_on_the_check_doesnt_hide_the_real_error(self):
        self.server.dead.add(FILE_URL)
        def boom(request):
            raise httpx.ConnectError("no route")
        self.server.api = boom
        with self.assertRaises(RuntimeError) as caught:
            self.stage(self.server)
        self.assertIn("404", str(caught.exception))

    def test_a_cloudflare_challenge_isnt_treated_as_a_bad_link(self):
        def challenged(request):
            return httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="Just a moment...")

        reasons = []
        with patch.object(self.dm, "trip_circuit_breaker", side_effect=reasons.append), self.assertRaises(RuntimeError):
            self.stage(challenged)
        self.assertIn("Cloudflare", reasons[0])
        self.assertEqual(self.link(), FILE_URL)

    def test_a_plain_403_still_says_the_sign_in_expired(self):
        reasons = []
        with patch.object(self.dm, "trip_circuit_breaker", side_effect=reasons.append), self.assertRaises(RuntimeError):
            self.stage(lambda request: httpx.Response(403))
        self.assertIn("expired", reasons[0])


class IngestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "library.db"
        create_database(self.db)
        self.manager = object.__new__(TaskManager)
        self.manager.db_path = self.db
        self.manager.root_dir = self.root
        self.manager._lock = threading.Lock()
        self.manager.active_task_id = None
        self.manager._cancel_events = {}
        conn = sqlite3.connect(self.db)
        with conn:
            self.task = conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('metadata_ingest', 'running')").lastrowid
        conn.close()
        self.metadata = self.root / "metadata"
        self.seen = []
        self.status = 200

    def run_ingest(self, token="token", cookies='[{"name": "PHPSESSID", "value": "s"}]'):
        conn = sqlite3.connect(self.db)
        with conn:
            auth_service.set_settings(conn, {"mmf_access_token": token, "cookies_json": cookies})
        conn.close()

        def answer(request):
            self.seen.append(request)
            if self.status != 200:
                return httpx.Response(self.status)
            return httpx.Response(200, json={"id": 77, "name": "Hero", "files": {"items": []}, "designer": {"name": "M", "username": "m"}})

        def build(**kwargs):
            return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(answer), **kwargs)

        with patch("desync_archival.services.task_manager.httpx.AsyncClient", side_effect=build):
            asyncio.run(self.manager._async_ingest(self.task, [77], 0, "test", 1))

    def result(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT status, result_summary_json, log_output FROM pipeline_tasks WHERE id = ?", (self.task,)).fetchone()
            return row["status"], json.loads(row["result_summary_json"] or "{}"), row["log_output"] or ""
        finally:
            conn.close()

    def test_a_request_is_a_plain_api_client(self):
        self.run_ingest()
        request = self.seen[0]
        self.assertTrue(request.headers["user-agent"].startswith("python-httpx/"))
        self.assertEqual(request.headers["authorization"], "Bearer token")
        self.assertEqual(request.headers["accept"], "application/json")
        for name in ("referer", "cookie"):
            self.assertNotIn(name, request.headers)
        self.assertEqual(self.result()[1]["ingested"], 1)

    def test_with_no_sign_in_it_asks_nothing_of_mmf(self):
        self.run_ingest(token="")
        self.assertEqual(self.seen, [])
        _, summary, log = self.result()
        self.assertEqual(summary["failed"], 1)
        self.assertIn("Sign in", log)

    def test_saved_metadata_still_works_with_no_sign_in(self):
        self.metadata.mkdir()
        saved = {"id": 77, "name": "Hero", "files": {"items": []}, "designer": {"name": "M", "username": "m"}}
        (self.metadata / "model_77.json").write_text(json.dumps(saved), encoding="utf-8")
        self.run_ingest(token="")
        self.assertEqual(self.seen, [])
        self.assertEqual(self.result()[1]["ingested"], 1)

    def test_an_expired_token_stops_asking_and_never_falls_back_to_cookies(self):
        self.status = 401
        self.run_ingest()
        self.assertEqual(len(self.seen), 1)
        self.assertNotIn("cookie", self.seen[0].headers)
        _, summary, log = self.result()
        self.assertEqual(summary["failed"], 1)
        self.assertIn("Sign in", log)


class BrowserSignalsTests(unittest.TestCase):
    def test_only_the_file_download_still_presents_as_a_browser(self):
        services = Path(__file__).resolve().parents[1] / "src/desync_archival/services"
        counts = {path.name: path.read_text(encoding="utf-8").count("Mozilla/") for path in services.glob("*.py")}
        self.assertEqual({name: count for name, count in counts.items() if count}, {"downloader.py": 1})
