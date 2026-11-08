"""E54's routes, as the LLM Grouping screen calls them: export, check, confirm or discard, undo,
suggestions, and the back-and-forth with an LLM when its answer needs fixing."""

import io
import json
import zipfile
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_grouping import GroupingCase

from desync_archival import settings as app_config
from desync_archival.api import server
from desync_archival.services import grouping_apply, grouping_screen
from desync_archival.services.db import get_db_connection


class RouteCase(GroupingCase):
    def setUp(self):
        super().setUp()
        app = server.create_app(manage_workers=False)
        app.state.open_database = lambda: get_db_connection(self.db, shared=True)
        self.client = TestClient(app, base_url="http://localhost")

    def check(self, answer, name="answer.json"):
        text = answer if isinstance(answer, str) else json.dumps(answer)
        response = self.client.post("/api/grouping/check", json={"text": text, "name": name})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def imports(self):
        return self.one("SELECT COUNT(*) FROM grouping_imports")[0]


class OverviewTests(RouteCase):
    def test_the_screen_gets_everything_it_shows(self):
        data = self.client.get("/api/grouping").json()
        self.assertEqual(set(data), {"waiting", "imports", "exports", "pictures", "batch_lines", "max_answer_chars"})
        self.assertEqual(data["batch_lines"], {"default": 1000, "min": 200, "max": 20000})

    def test_the_preview_counts_what_an_export_would_hold(self):
        data = self.client.get("/api/grouping/preview", params={"mode": "full", "scope": "archives"}).json()
        self.assertEqual((data["units"], data["batches"]), (1, 1))

    def test_a_bad_batch_size_is_explained(self):
        response = self.client.get("/api/grouping/preview", params={"batch_lines": "50"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("between 200 and 20000", response.json()["error"])


class ExportRouteTests(RouteCase):
    def test_the_export_downloads_as_a_zip_with_its_id(self):
        response = self.client.post("/api/grouping/exports", json={"mode": "full", "scope": "archives"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        export_key = response.headers["x-export-id"]
        self.assertIn(f'filename="{export_key}.zip"', response.headers["content-disposition"])
        names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
        self.assertEqual(sorted(names), ["README.txt", "answer-format.json", "batch-01.json", "prompt.md"])

    def test_an_unknown_scope_is_refused_before_anything_is_recorded(self):
        response = self.client.post("/api/grouping/exports", json={"scope": "everything"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.one("SELECT COUNT(*) FROM grouping_exports")[0], 0)

    def test_nothing_to_export_says_why(self):
        self.client.post("/api/grouping/exports", json={"mode": "full", "scope": "archives"})
        self.check(self.answer())
        import_id = self.one("SELECT MAX(id) FROM grouping_imports")[0]
        self.client.post(f"/api/grouping/imports/{import_id}/apply")
        response = self.client.post("/api/grouping/exports", json={"mode": "delta", "scope": "archives"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Nothing to export", response.json()["error"])


class CheckRouteTests(RouteCase):
    def test_a_careful_answer_is_kept_for_confirmation_and_changes_nothing_yet(self):
        before = grouping_apply.state(self.conn, self.key)
        report = self.check(self.answer())
        self.assertEqual(report["counts"], {"apply": 1, "suggest": 0, "refuse": 0})
        self.assertIsInstance(report["import_id"], int)
        self.assertNotIn("plans", report)
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)

    def test_confirm_applies_and_undo_puts_it_back(self):
        before = grouping_apply.state(self.conn, self.key)
        import_id = self.check(self.answer())["import_id"]
        applied = self.client.post(f"/api/grouping/imports/{import_id}/apply").json()
        self.assertEqual([a["unit"] for a in applied["applied"]], [self.key])
        undone = self.client.post(f"/api/grouping/imports/{import_id}/undo").json()
        self.assertEqual(undone["restored"], [self.key])
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)

    def test_text_that_isnt_json_gets_a_fix_request_and_isnt_kept(self):
        report = self.check("Sure! Here are the groupings you asked for: Ghul A, Ghul B, Banshee.")
        self.assertTrue(report["error"])
        self.assertIsNone(report["import_id"])
        self.assertIn("JSON", report["fix_request"])
        self.assertEqual(self.imports(), 0)

    def test_an_answer_with_nothing_usable_isnt_kept(self):
        answer = self.answer()
        answer["units"][0]["creator"] = "Someone Else"
        report = self.check(answer)
        self.assertEqual(report["counts"]["refuse"], 1)
        self.assertIsNone(report["import_id"])
        self.assertIn("creator", report["fix_request"])
        self.assertEqual(self.imports(), 0)

    def test_an_answer_too_long_to_check_says_to_send_one_batch(self):
        with patch.object(grouping_screen, "MAX_ANSWER_CHARS", 100):
            report = self.check(self.answer())
        self.assertIn("one batch's answer at a time", report["error"])
        self.assertEqual(self.imports(), 0)

    def test_a_body_past_the_request_limit_is_refused(self):
        with patch.object(app_config, "MAX_BODY_BYTES", 1000):
            response = self.client.post("/api/grouping/check", json={"text": "x" * 2000})
        self.assertEqual(response.status_code, 413)

    def test_a_missing_answer_is_refused(self):
        response = self.client.post("/api/grouping/check", json={"name": "answer.json"})
        self.assertEqual(response.status_code, 400)


class BackAndForthTests(RouteCase):
    """An answer the checks refuse, the fix request pasted back to the LLM, and its corrected answer."""

    def test_the_fix_request_names_each_problem_and_the_corrected_answer_applies(self):
        answer = self.answer()
        good_ids = list(answer["units"][0]["models"][2]["artifacts"])
        answer["units"][0]["models"][2]["artifacts"] = ["a99", *good_ids[1:]]
        first = self.check(answer)
        self.assertIsNone(first["import_id"])
        request = first["fix_request"]
        self.assertIn(self.key, request)
        self.assertIn("a99", request)
        self.assertIn(first["export_id"], request)

        answer["units"][0]["models"][2]["artifacts"] = good_ids
        second = self.check(answer, name="answer-fixed.json")
        self.assertEqual(second["counts"]["apply"], 1)
        result = self.client.post(f"/api/grouping/imports/{second['import_id']}/apply").json()
        self.assertEqual(len(result["applied"]), 1)

    def test_a_fenced_answer_with_chatter_is_still_read(self):
        text = "Here you go:\n```json\n" + json.dumps(self.answer()) + "\n```\nLet me know if you need changes."
        self.assertEqual(self.check(text)["counts"]["apply"], 1)


class ImportStepTests(RouteCase):
    def test_a_checked_import_can_be_discarded_once_and_then_not_applied(self):
        import_id = self.check(self.answer())["import_id"]
        self.assertEqual(self.client.post(f"/api/grouping/imports/{import_id}/discard").status_code, 200)
        again = self.client.post(f"/api/grouping/imports/{import_id}/discard")
        self.assertEqual(again.status_code, 409)
        self.assertIn("waiting for confirmation", again.json()["error"])
        applied = self.client.post(f"/api/grouping/imports/{import_id}/apply")
        self.assertEqual(applied.status_code, 409)
        self.assertIn("discarded", applied.json()["error"])

    def test_an_import_lists_with_its_counts(self):
        import_id = self.check(self.answer())["import_id"]
        item = self.client.get("/api/grouping").json()["imports"][0]
        self.assertEqual((item["id"], item["status"], item["counts"]["apply"], item["name"]), (import_id, "checked", 1, "answer.json"))

    def test_undo_of_an_import_never_applied_is_refused(self):
        import_id = self.check(self.answer())["import_id"]
        response = self.client.post(f"/api/grouping/imports/{import_id}/undo")
        self.assertEqual(response.status_code, 409)

    def test_suggested_tags_wait_on_the_import_until_approved(self):
        with self.conn:
            self.conn.execute("UPDATE collections SET source = 'import' WHERE id = ?", (self.collection,))
        answer = self.answer()
        answer["units"][0]["models"][2]["tags"] = ["banshee"]
        import_id = self.check(answer)["import_id"]
        self.client.post(f"/api/grouping/imports/{import_id}/apply")
        self.assertEqual(self.client.get("/api/grouping").json()["imports"][0]["tags"], ["banshee"])
        result = self.client.post(f"/api/grouping/imports/{import_id}/tags", json={"approved": ["banshee"]}).json()
        self.assertEqual((result["created"], result["added"]), (["banshee"], 1))
        self.assertEqual(self.client.get("/api/grouping").json()["imports"][0]["tags"], [])


class SuggestionRouteTests(RouteCase):
    def suggest(self):
        answer = self.answer()
        answer["units"][0]["confidence"] = "medium"
        import_id = self.check(answer)["import_id"]
        self.client.post(f"/api/grouping/imports/{import_id}/apply")
        return self.client.get("/api/grouping").json()["waiting"][0]["id"]

    def test_a_suggestion_opens_and_accepting_saves_the_persons_cards(self):
        suggestion = self.suggest()
        view = self.client.get(f"/api/grouping/suggestions/{suggestion}").json()
        self.assertEqual(view["suggestion"]["bucket"], "medium")
        cards = view["cards"]
        response = self.client.post(f"/api/grouping/suggestions/{suggestion}/accept",
                                    json={"cards": cards, "pictures": {cards[0]["paths"][0]: "i1"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/grouping").json()["waiting"], [])

    def test_rejecting_keeps_todays_grouping(self):
        before = grouping_apply.state(self.conn, self.key)
        suggestion = self.suggest()
        self.assertEqual(self.client.post(f"/api/grouping/suggestions/{suggestion}/reject").status_code, 200)
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)

    def test_a_decided_suggestion_says_so(self):
        suggestion = self.suggest()
        self.client.post(f"/api/grouping/suggestions/{suggestion}/reject")
        response = self.client.get(f"/api/grouping/suggestions/{suggestion}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("already rejected", response.json()["error"])

    def test_a_bad_card_is_refused_and_the_suggestion_still_waits(self):
        suggestion = self.suggest()
        cards = self.client.get(f"/api/grouping/suggestions/{suggestion}").json()["cards"]
        response = self.client.post(f"/api/grouping/suggestions/{suggestion}/accept", json={"cards": cards[1:]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.client.get("/api/grouping").json()["waiting"]), 1)
