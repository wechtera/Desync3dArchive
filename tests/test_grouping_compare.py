"""Comparing LLM answers to one export: which files each put together, how the app would take each,
and how far each is from a reference. It reads only."""

import contextlib
import io
import json
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from test_grouping import GroupingCase

from desync_archival import dbtool
from desync_archival.services import grouping_compare
from desync_archival.services.grouping_compare import CompareRefused, adjusted_rand


class RandTests(unittest.TestCase):
    ITEMS = ("a1", "a2", "a3", "a4")

    def test_the_same_grouping_scores_one_whatever_the_order(self):
        self.assertEqual(adjusted_rand({"a1": 0, "a2": 0, "a3": 1, "a4": 1}, {"a1": 5, "a2": 5, "a3": 2, "a4": 2}, list(self.ITEMS)), 1.0)

    def test_all_together_against_all_apart_scores_zero(self):
        self.assertEqual(adjusted_rand(dict.fromkeys(self.ITEMS, 0), {}, list(self.ITEMS)), 0.0)

    def test_a_partial_match_scores_between(self):
        score = adjusted_rand({"a1": 0, "a2": 0, "a3": 1, "a4": 1}, {"a1": 0, "a2": 0, "a3": 0, "a4": 1}, list(self.ITEMS))
        self.assertTrue(-1 < score < 1)

    def test_one_file_is_trivially_the_same(self):
        self.assertEqual(adjusted_rand({}, {"a1": 0}, ["a1"]), 1.0)


class CompareCase(GroupingCase):
    def setUp(self):
        super().setUp()
        self.good = self.answer()

    def split(self):
        """Ghul A's two scales as two models: the over-split E54 exists to fix."""
        answer = json.loads(json.dumps(self.good))
        models = answer["units"][0]["models"]
        first, second = models[0]["artifacts"]
        models[0]["artifacts"] = [first]
        models.insert(1, {"name": "Ghul A 32mm", "artifacts": [second], "image": None, "confidence": "high"})
        return answer

    def run_compare(self, answers, reference=None, expected=None):
        return grouping_compare.compare(
            self.conn, [(label, json.dumps(a) if not isinstance(a, str) else a) for label, a in answers],
            json.dumps(reference) if reference else None, expected,
        )


class CompareTests(CompareCase):
    def test_answers_are_scored_against_the_reference(self):
        result = self.run_compare([("claude", self.good), ("other", self.split())], reference=self.good)
        self.assertEqual(result["summary"]["claude"]["vs_reference"], 1.0)
        self.assertLess(result["summary"]["other"]["vs_reference"], 1.0)
        unit = result["units"][0]
        self.assertEqual((unit["answers"]["claude"]["models"], unit["answers"]["other"]["models"]), (3, 4))
        self.assertLess(unit["pairs"]["claude / other"], 1.0)

    def test_pictures_are_matched_to_the_reference(self):
        wrong = json.loads(json.dumps(self.good))
        wrong["units"][0]["models"][0]["image"] = wrong["units"][0]["models"][1]["image"]
        result = self.run_compare([("right", self.good), ("wrong", wrong)], reference=self.good)
        self.assertEqual(result["summary"]["right"]["pictures"], [2, 2])
        self.assertEqual(result["summary"]["wrong"]["pictures"], [1, 2])

    def test_a_refused_answer_still_shows_how_it_grouped(self):
        refused = json.loads(json.dumps(self.good))
        refused["units"][0]["creator"] = "Someone Else"
        result = self.run_compare([("poisoned", refused)], reference=self.good)
        entry = result["units"][0]["answers"]["poisoned"]
        self.assertEqual(entry["outcome"], "refuse")
        self.assertIn("creator", " ".join(entry["problems"]))
        self.assertEqual(entry["vs_reference"], 1.0)

    def test_expected_counts_by_collection_name(self):
        result = self.run_compare([("other", self.split())], expected={"Vampire Court": 3})
        self.assertEqual(result["units"][0]["expected"], 3)
        self.assertEqual(result["summary"]["other"]["expected_off"], 1.0)

    def test_an_unreadable_answer_is_listed_not_fatal(self):
        result = self.run_compare([("good", self.good), ("chatty", "I grouped them as Ghul A, Ghul B and the Banshee.")])
        self.assertFalse(result["summary"]["chatty"]["readable"])
        self.assertEqual(result["summary"]["chatty"]["units_missing"], 1)
        self.assertIsNone(result["units"][0]["answers"]["chatty"])

    def test_a_left_out_file_is_counted(self):
        partial = json.loads(json.dumps(self.good))
        partial["units"][0]["models"][2]["artifacts"] = partial["units"][0]["models"][2]["artifacts"][:1]
        self.assertEqual(self.run_compare([("partial", partial)])["units"][0]["answers"]["partial"]["unplaced"], 1)

    def test_an_answer_in_batch_files_is_read_as_one(self):
        empty = {"export_id": self.good["export_id"], "units": []}
        result = grouping_compare.compare(self.conn, [("claude", [json.dumps(empty), json.dumps(self.good)])], json.dumps(self.good))
        self.assertTrue(result["summary"]["claude"]["readable"])
        self.assertEqual(result["summary"]["claude"]["vs_reference"], 1.0)

    def test_a_unit_sent_in_two_batches_is_caught(self):
        result = grouping_compare.compare(self.conn, [("twice", [json.dumps(self.good), json.dumps(self.good)])])
        self.assertIn("in the answer twice", " ".join(result["units"][0]["answers"]["twice"]["problems"]))
        self.assertEqual(result["summary"]["twice"]["counts"]["refuse"], 1)

    def test_cleanup_an_answer_needed_is_reported(self):
        fenced = "Here it is:\n```json\n" + json.dumps(self.good) + "\n```"
        result = self.run_compare([("fenced", fenced)])
        self.assertTrue(result["summary"]["fenced"]["notes"])
        self.assertIn("needed cleaning up", grouping_compare.describe(result))

    def test_it_writes_nothing(self):
        before = self.conn.total_changes
        counts = [self.one(f"SELECT COUNT(*) FROM {t}")[0] for t in ("grouping_imports", "grouping_suggestions", "models")]
        self.run_compare([("good", self.good), ("other", self.split())], reference=self.good)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual([self.one(f"SELECT COUNT(*) FROM {t}")[0] for t in ("grouping_imports", "grouping_suggestions", "models")], counts)

    def test_answers_to_different_exports_are_refused(self):
        other = self.answer()
        with self.assertRaisesRegex(CompareRefused, "different exports"):
            self.run_compare([("one", self.good), ("two", other)])

    def test_an_export_from_elsewhere_is_refused(self):
        stranger = json.loads(json.dumps(self.good))
        stranger["export_id"] = "e-19990101-000000-dead"
        with self.assertRaisesRegex(CompareRefused, "isn't in this database"):
            self.run_compare([("stranger", stranger)])

    def test_the_report_reads_as_markdown(self):
        report = grouping_compare.describe(self.run_compare([("claude", self.good), ("other", self.split())], reference=self.good))
        self.assertIn("| claude | yes | 1 | 0 | 0 |", report)
        self.assertIn("### Vampire Court (archive:", report)
        self.assertIn("**Agreement between answers:** claude / other", report)


class CommandTests(CompareCase):
    def dbtool(self, *args):
        out = io.StringIO()
        with patch("sys.argv", ["dbtool", *args]), patch("desync_archival.dbtool.settings.db_path", return_value=self.db), \
                contextlib.redirect_stdout(out):
            code = dbtool.main()
        return code, out.getvalue()

    def test_the_kit_export_writes_a_zip_of_the_chosen_collections(self):
        destination = Path(self.temp.name) / "kit.zip"
        code, out = self.dbtool("grouping-export", str(destination), "--collection", str(self.collection))
        self.assertEqual(code, 0, out)
        self.assertIn("1 units in 1 batches", out)
        unit = json.loads(zipfile.ZipFile(destination).read("batch-01.json"))["units"][0]
        self.assertEqual(unit["unit_id"], self.key)

    def test_the_comparison_writes_its_report(self):
        folder = Path(self.temp.name)
        (folder / "claude.json").write_text(json.dumps(self.good))
        (folder / "other.json").write_text(json.dumps(self.split()))
        code, out = self.dbtool("grouping-compare", str(folder / "claude.json"), str(folder / "other.json"),
                                "--reference", str(folder / "claude.json"), "--out", str(folder / "report.md"))
        self.assertEqual(code, 0, out)
        self.assertIn("| other | yes |", (folder / "report.md").read_text())

    def test_a_folder_holds_one_llms_batch_answers(self):
        folder = Path(self.temp.name) / "claude"
        folder.mkdir()
        (folder / "batch-01.json").write_text(json.dumps(self.good))
        code, out = self.dbtool("grouping-compare", str(folder))
        self.assertEqual(code, 0, out)
        self.assertIn("| claude | yes | 1 |", out)

    def test_a_comparison_that_cant_run_says_why(self):
        missing = Path(self.temp.name) / "nope.json"
        code, _ = self.dbtool("grouping-compare", str(missing))
        self.assertEqual(code, 1)
