"""Tests for scripts/production_manifest.py. They use temporary files only."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "production_manifest", REPO_ROOT / "scripts" / "production_manifest.py"
)
assert spec and spec.loader
production_manifest = importlib.util.module_from_spec(spec)
sys.modules["production_manifest"] = production_manifest
spec.loader.exec_module(production_manifest)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "sub").mkdir()
        self.file = self.root / "sub" / "a.txt"
        self.file.write_text("one")

    def tearDown(self):
        self.temp.cleanup()

    def _compare(self, before, roots):
        return production_manifest.compare_manifests(before, production_manifest.record_manifest(roots))

    def test_file_root_is_recorded(self):
        result = production_manifest.scan_root(self.file)
        self.assertEqual(list(result["entries"]), ["."])
        self.assertEqual(result["errors"], [])

    def test_unchanged_tree_has_no_differences(self):
        before = production_manifest.record_manifest([self.root, self.file])
        self.assertEqual(self._compare(before, [self.root, self.file]), [])

    def test_changed_file_root_is_detected(self):
        before = production_manifest.record_manifest([self.file])
        os.utime(self.file, ns=(1, 1))
        self.assertTrue(self._compare(before, [self.file]))

    def test_added_and_removed_files_are_detected(self):
        before = production_manifest.record_manifest([self.root])
        (self.root / "new.txt").write_text("x")
        self.file.unlink()
        differences = self._compare(before, [self.root])
        self.assertTrue(any("added new.txt" in line for line in differences))
        self.assertTrue(any("removed sub/a.txt" in line for line in differences))

    def test_missing_root_is_not_an_error(self):
        result = production_manifest.scan_root(self.root / "absent")
        self.assertFalse(result["exists"])


if __name__ == "__main__":
    unittest.main()
