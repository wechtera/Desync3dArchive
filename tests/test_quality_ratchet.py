"""Tests the per-file comparison in scripts/quality_ratchet.py."""

import importlib.util
import sys
import unittest
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("quality_ratchet", REPO_ROOT / "scripts" / "quality_ratchet.py")
assert spec
assert spec.loader
quality_ratchet = importlib.util.module_from_spec(spec)
sys.modules["quality_ratchet"] = quality_ratchet
spec.loader.exec_module(quality_ratchet)


class RegressionTests(unittest.TestCase):
    def test_equal_or_lower_counts_pass(self):
        self.assertEqual(quality_ratchet.regressions({"a.py": 3}, Counter({"a.py": 3})), [])
        self.assertEqual(quality_ratchet.regressions({"a.py": 3}, Counter({"a.py": 1})), [])

    def test_higher_count_fails(self):
        self.assertEqual(len(quality_ratchet.regressions({"a.py": 3}, Counter({"a.py": 4}))), 1)

    def test_finding_in_a_new_file_fails(self):
        self.assertEqual(len(quality_ratchet.regressions({}, Counter({"new.py": 1}))), 1)


if __name__ == "__main__":
    unittest.main()
