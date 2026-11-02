"""Covers wiring that the API contract snapshots can't see."""

import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from live_server import LiveServer

from desync_archival import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "baseline"))

from record_contract import differing_fields


class ContractComparisonTests(unittest.TestCase):
    def test_comparison_detects_changes(self):
        stored = {"response": {"status": 200, "json": {"items": [1, 2], "name": "a"}}}
        self.assertEqual(differing_fields(stored, json.loads(json.dumps(stored))), [])
        for changed in (
            {"response": {"status": 500, "json": {"items": [1, 2], "name": "a"}}},
            {"response": {"status": 200, "json": {"items": [1, 3], "name": "a"}}},
            {"response": {"status": 200, "json": {"items": [1], "name": "a"}}},
            {"response": {"status": 200, "json": {"items": [1, 2]}}},
            {"response": {"status": 200, "json": {"items": [1, 2], "name": "a", "new": 1}}},
        ):
            self.assertTrue(differing_fields(stored, changed))


class StaticServingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_server = LiveServer()
        cls.port = cls.live_server.start()

    @classmethod
    def tearDownClass(cls):
        cls.live_server.stop()

    def _get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_frontend_is_served_from_web(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"DESYNC", body)
        for path in ("/static/js/app.js", "/static/js/views/models.js", "/static/css/vault.css"):
            self.assertEqual(self._get(path)[0], 200, path)

    def test_python_source_is_not_served(self):
        for path in ("/server.py", "/routes/auth.py", "/settings.py"):
            self.assertEqual(self._get(path)[0], 404, path)

    def test_preview_comes_from_data_directory(self):
        target = settings.previews_dir() / "contract_test" / "cover.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"image")
        status, body = self._get("/previews/contract_test/cover.jpg")
        self.assertEqual((status, body), (200, b"image"))

    def test_missing_preview_returns_placeholder(self):
        status, body = self._get("/previews/none/cover.jpg")
        self.assertEqual(status, 200)
        self.assertIn(b"No Preview Available", body)


@unittest.skipUnless(
    shutil.which("node") and (REPO_ROOT / "scraper" / "node_modules").is_dir(), "needs node and npm ci"
)
class ScraperArgumentTests(unittest.TestCase):
    def _run(self, *args):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(
                ["node", str(REPO_ROOT / "scraper" / "scrape.js"), *args],
                check=False, capture_output=True, text=True, timeout=30, cwd=temp,
            )
            return result, sorted(path.name for path in Path(temp).iterdir())

    def test_cookies_flag_is_required(self):
        result, _ = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("--cookies", result.stderr)

    def test_missing_cookies_file_stops_before_launching_a_browser(self):
        result, created = self._run("--cookies", "/nonexistent/cookies.json")
        self.assertIn("cookies.json not found", result.stderr)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(created, [])


class ScraperCommandTests(unittest.TestCase):
    def test_scraper_gets_an_explicit_cookies_path(self):
        source = (REPO_ROOT / "src/desync_archival/services/task_manager.py").read_text(encoding="utf-8")
        self.assertIn('"--cookies", str(cookies_dest)', source)
        self.assertTrue(settings.scraper_script().is_file())
        self.assertTrue(settings.blender_script().is_file())


if __name__ == "__main__":
    unittest.main()
