"""Tests the app lifespan, threadpool use, request logging and response building."""

import http.client
import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from db_helper import create_database
from fastapi.testclient import TestClient
from live_server import LiveServer

from desync_archival.api import server
from desync_archival.migrations import MigrationError
from desync_archival.services import logger as log


class LifespanTests(unittest.TestCase):
    def setUp(self):
        create_database(server.DB_PATH)

    def test_app_refuses_to_start_on_an_old_schema(self):
        conn = sqlite3.connect(server.DB_PATH)
        conn.execute("PRAGMA user_version = 1")
        conn.close()
        with self.assertRaises(MigrationError), TestClient(server.create_app(), base_url="http://localhost"):
            pass
        create_database(server.DB_PATH)

    def test_managers_start_and_stop_with_the_app(self):
        manager = MagicMock()
        with patch.object(server, "DownloadManager", return_value=manager), patch.object(server, "TaskManager") as tasks, \
                TestClient(server.create_app(), base_url="http://localhost") as client:
            self.assertEqual(client.get("/api/does-not-exist").status_code, 404)
            manager.start.assert_called_once()
            tasks.assert_called_once()
            manager.stop.assert_not_called()
        manager.stop.assert_called_once()

    def test_storage_problem_does_not_stop_startup(self):
        manager = MagicMock()
        manager.start.side_effect = server.StorageUnavailableError("no storage")
        with patch.object(server, "DownloadManager", return_value=manager), patch.object(server, "TaskManager"), \
                TestClient(server.create_app(), base_url="http://localhost") as client:
            self.assertEqual(client.get("/").status_code, 200)

    def test_workers_stay_off_when_the_caller_owns_them(self):
        with patch.object(server, "DownloadManager") as manager, TestClient(server.create_app(manage_workers=False)):
            manager.assert_not_called()


class ThreadpoolTests(unittest.TestCase):
    def test_slow_route_does_not_stall_others(self):
        release = threading.Event()
        started = threading.Event()
        original = server.dispatch_route

        def slow(method, path, **kwargs):
            if path == "/api/slow-test":
                started.set()
                release.wait(timeout=10)
                return {"slow": True}, 200
            return original(method=method, path=path, **kwargs)

        live = LiveServer()
        port = live.start()
        results = {}

        def call_slow():
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            connection.request("GET", "/api/slow-test")
            results["slow"] = connection.getresponse().status
            connection.close()

        with patch.object(server, "dispatch_route", side_effect=slow):
            thread = threading.Thread(target=call_slow)
            thread.start()
            self.assertTrue(started.wait(timeout=5))
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            begun = time.monotonic()
            connection.request("GET", "/api/does-not-exist")
            self.assertEqual(connection.getresponse().status, 404)
            self.assertLess(time.monotonic() - begun, 2)
            connection.close()
            release.set()
            thread.join(timeout=10)
        live.stop()
        self.assertEqual(results["slow"], 200)


class RequestLoggingTests(unittest.TestCase):
    def entries(self):
        for handler in log.logger.handlers:
            handler.flush()
        return [json.loads(line) for line in log.APP_LOG_PATH.read_text(encoding="utf-8").splitlines()]

    def test_request_is_logged_with_id_and_outcome(self):
        with TestClient(server.create_app(manage_workers=False), base_url="http://localhost") as client:
            client.get("/api/logging-check")
        entry = [e for e in self.entries() if "/api/logging-check" in e["message"]][-1]
        self.assertIn("404", entry["message"])
        self.assertTrue(entry["request_id"])

    def test_query_string_is_left_out(self):
        with TestClient(server.create_app(manage_workers=False), base_url="http://localhost") as client:
            client.get("/api/query-check?code=abc123query")
        self.assertNotIn("abc123query", log.APP_LOG_PATH.read_text(encoding="utf-8"))

    def test_each_request_gets_its_own_id(self):
        with TestClient(server.create_app(manage_workers=False), base_url="http://localhost") as client:
            client.get("/api/id-check")
            client.get("/api/id-check")
        ids = [e["request_id"] for e in self.entries() if "/api/id-check" in e["message"]]
        self.assertEqual(len(set(ids)), len(ids))


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.create_app(manage_workers=False), follow_redirects=False, base_url="http://localhost")

    def respond_with(self, value):
        target = "desync_archival.api.server.dispatch_route"
        return patch(target, return_value=value)

    def test_two_tuple_json(self):
        with self.respond_with(({"a": 1}, 201)):
            response = self.client.get("/api/x")
        self.assertEqual((response.status_code, response.json()), (201, {"a": 1}))

    def test_three_tuple_bytes_with_headers(self):
        with self.respond_with((b"\xff\xd8", 200, {"Content-Type": "image/jpeg", "X-Test": "1"})):
            response = self.client.get("/api/x")
        self.assertEqual(response.content, b"\xff\xd8")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.headers["x-test"], "1")

    def test_plain_string_is_html(self):
        with self.respond_with("<p>hi</p>"):
            response = self.client.get("/api/x")
        self.assertIn("text/html", response.headers["content-type"])
        self.assertEqual(response.text, "<p>hi</p>")

    def test_plain_bytes_are_octet_stream(self):
        with self.respond_with(b"raw"):
            response = self.client.get("/api/x")
        self.assertEqual(response.headers["content-type"], "application/octet-stream")

    def test_redirect_location_comes_from_html_and_dict(self):
        with self.respond_with(("<meta http-equiv='refresh' content='0;url=https://a.example/b'>", 302)):
            self.assertEqual(self.client.get("/api/x").headers["location"], "https://a.example/b")
        with self.respond_with(({"auth_url": "https://a.example/c"}, 302)):
            self.assertEqual(self.client.get("/api/x").headers["location"], "https://a.example/c")

    def test_invalid_json_body_becomes_empty_dict(self):
        with self.respond_with(({}, 200)) as dispatch:
            self.client.post("/api/x", content=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(dispatch.call_args.kwargs["body_data"], {})

    def test_route_exception_returns_generic_500(self):
        with patch("desync_archival.api.server.dispatch_route", side_effect=ValueError("boom")):
            response = self.client.get("/api/x")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"error": "Internal server error"})

    def test_static_writes_are_refused(self):
        self.assertEqual(self.client.options("/anything").status_code, 405)
        self.assertEqual(self.client.post("/anything").status_code, 405)
        self.assertEqual(self.client.get("/anything").status_code, 404)

    def test_preview_cache_headers_match_the_old_server(self):
        target = server.PREVIEWS_DIR / "cache_test" / "cover.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"image")
        response = self.client.get("/previews/cache_test/cover.jpg")
        self.assertEqual(response.headers["cache-control"], "public, max-age=604800")
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(self.client.get("/previews/none.jpg").headers["cache-control"], "no-cache")

    def test_preview_traversal_gets_the_placeholder(self):
        response = self.client.get("/previews/..%2f..%2flibrary.db")
        self.assertIn("image/svg+xml", response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
