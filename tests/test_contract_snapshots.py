"""Fails if a recorded API contract snapshot holds a secret or a signed URL."""

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "baseline"))

from contract_tools import REDACTED, find_leaks, normalize

SNAPSHOT_DIR = REPO_ROOT / "tests" / "contract" / "snapshots"


class ScannerTests(unittest.TestCase):
    def test_finds_value_under_secret_key(self):
        self.assertTrue(find_leaks({"mmf_client_secret": "abc"}))
        self.assertTrue(find_leaks({"settings": {"mmf_access_token": "abc"}}))

    def test_finds_inline_secrets(self):
        self.assertTrue(find_leaks({"log": "cookie cf_clearance=abcdefgh12345"}))
        self.assertTrue(find_leaks({"log": "Authorization: Bearer abcdefgh1234567890"}))

    def test_finds_signed_urls(self):
        self.assertTrue(find_leaks({"url": "https://cdn.example.com/a.zip?X-Amz-Signature=abc"}))
        self.assertTrue(find_leaks({"url": "https://cdn.example.com/a.zip?token=abc"}))

    def test_ignores_clean_values(self):
        clean = {
            "mmf_client_secret": "",
            "mmf_access_token": REDACTED,
            "token_configured": True,
            "has_client_secret": True,
            "url": "https://www.myminifactory.com/download/1?archive_id=2",
        }
        self.assertEqual(find_leaks(clean), [])

    def test_normalize_removes_what_the_scanner_finds(self):
        dirty = {
            "mmf_client_secret": "abc",
            "url": "https://cdn.example.com/a.zip?X-Amz-Signature=abc&id=1",
            "log": "PHPSESSID=abcdefgh12345 at /dev/path",
        }
        clean = normalize(dirty, [("/dev/path", "<DEV>")])
        self.assertEqual(find_leaks(clean), [])
        self.assertEqual(clean["log"], f"PHPSESSID={REDACTED} at <DEV>")
        self.assertIn("id=1", clean["url"])


class SnapshotTests(unittest.TestCase):
    def test_snapshots_exist(self):
        self.assertTrue(list(SNAPSHOT_DIR.glob("*.json")))

    def test_snapshots_hold_no_secrets(self):
        for path in sorted(SNAPSHOT_DIR.glob("*.json")):
            with self.subTest(snapshot=path.name):
                snapshot = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(find_leaks(snapshot["response"]), [])


if __name__ == "__main__":
    unittest.main()
