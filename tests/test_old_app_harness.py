"""Tests the safety guards in scripts/baseline/old_app_server.py."""

import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "baseline"))

import old_app_server

SCRIPT = REPO_ROOT / "scripts" / "baseline" / "old_app_server.py"


class GuardTests(unittest.TestCase):
    def test_loopback_detection(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertTrue(old_app_server.is_loopback(host))
        for host in ("8.8.8.8", "www.myminifactory.com", "192.168.1.10"):
            self.assertFalse(old_app_server.is_loopback(host))

    def test_outside_connection_is_refused(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with self.assertRaises(old_app_server.NetworkBlockedError):
                old_app_server.guarded_connect(sock, ("8.8.8.8", 443))
            with self.assertRaises(old_app_server.NetworkBlockedError):
                old_app_server.guarded_connect_ex(sock, ("8.8.8.8", 443))

    def test_outside_lookup_is_refused(self):
        with self.assertRaises(socket.gaierror):
            old_app_server.guarded_getaddrinfo("www.myminifactory.com", 443)

    def test_node_is_refused_and_other_programs_run(self):
        with self.assertRaises(OSError):
            old_app_server.GuardedPopen(["/opt/homebrew/bin/node", "scrape.js"])
        process = old_app_server.GuardedPopen(["/usr/bin/true"])
        self.assertEqual(process.wait(), 0)

    def _run(self, app_root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--app-root", str(app_root)],
            check=False, capture_output=True, text=True, timeout=20,
        )

    def test_refuses_to_run_from_a_real_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "scrapepage"
            root.mkdir()
            result = self._run(root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a throwaway app", result.stderr)

    def test_refuses_old_app_without_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "old_app"
            root.mkdir()
            result = self._run(root)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
