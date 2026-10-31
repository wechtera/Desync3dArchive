"""scripts/server.sh: it reports state plainly and never stops a program that isn't this server."""

import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "server.sh"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.port = free_port()
        self.env_file = self.base / "production.env"
        self.env_file.write_text(f"DESYNC_DATA_DIR={self.base}\nDESYNC_PORT={self.port}\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, *args):
        return subprocess.run(
            [str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.base), "DESYNC_PRODUCTION_ENV": str(self.env_file)},
        )

    def test_an_unknown_command_shows_usage(self):
        result = self.run_script("bounce")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage", result.stderr)

    def test_missing_settings_are_refused(self):
        self.env_file.unlink()
        result = self.run_script("status")
        self.assertEqual(result.returncode, 2)
        self.assertIn("No production settings", result.stderr)

    def test_status_reports_stopped(self):
        result = self.run_script("status")
        self.assertEqual(result.returncode, 3)
        self.assertIn("Stopped", result.stdout)

    def test_stop_when_stopped_is_fine(self):
        result = self.run_script("stop")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Already stopped", result.stdout)

    def test_another_program_on_the_port_is_left_alone(self):
        other = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(other.wait)
        self.addCleanup(other.terminate)
        for _ in range(100):
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", self.port)) == 0:
                    break
            time.sleep(0.05)

        for command in ("stop", "start", "restart"):
            result = self.run_script(command)
            self.assertEqual(result.returncode, 1, command)
            self.assertIsNone(other.poll(), command)
