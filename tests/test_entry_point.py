"""Starts the real server command, which in-process tests can't exercise."""

import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from db_helper import create_database

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import npm_audit_gate


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name).resolve()
        self.port = free_port()
        self.env = {
            **os.environ, "APP_ENV": "test", "DESYNC_DATA_DIR": str(self.data), "DESYNC_PORT": str(self.port),
            "DESYNC_ALLOWED_ROOTS": str(self.data),
        }

    def tearDown(self):
        self.temp.cleanup()

    def request(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status

    def start(self):
        return subprocess.Popen(
            [sys.executable, "-m", "desync_archival.api.server"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )

    def test_command_serves_with_managers_wired_and_stops_cleanly(self):
        create_database(self.data / "library.db")
        process = self.start()
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    self.assertEqual(self.request("/"), 200)
                    break
                except ConnectionError:
                    self.assertIsNone(process.poll(), "the server exited during startup")
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.1)
            # These routes answer 500 unless the managers reached the routers.
            self.assertEqual(self.request("/api/tasks/status"), 200)
            self.assertEqual(self.request("/api/downloads/status"), 200)
        finally:
            process.terminate()
            process.wait(timeout=15)

    def test_command_refuses_an_unmigrated_database(self):
        process = self.start()
        _, stderr = process.communicate(timeout=20)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("dbtool migrate", stderr)


class AuditGateTests(unittest.TestCase):
    report = {"vulnerabilities": {"a": {"severity": "moderate"}, "b": {"severity": "low"}, "c": {"severity": "high"}}}

    def test_new_advisories_block_and_reviewed_ones_do_not(self):
        self.assertEqual(npm_audit_gate.blocked(self.report, {"a": "reviewed"}), ["c (high)"])

    def test_low_severity_does_not_block(self):
        self.assertEqual(npm_audit_gate.blocked({"vulnerabilities": {"b": {"severity": "low"}}}, {}), [])


if __name__ == "__main__":
    unittest.main()
