"""E54: applying a checked answer, and undoing it. Nothing is ever lost."""

import json
import sqlite3

from test_grouping import GroupingCase

from desync_archival import settings as app_config
from desync_archival.services import (
    archive_review,
    grouping_answer,
    grouping_apply,
    grouping_export,
    grouping_suggestions,
    picture_jobs,
)


class ApplyCase(GroupingCase):
    def counts(self):
        """Every record of a file. Applying or undoing must never change these."""
        return {table: self.one(f"SELECT COUNT(*) FROM {table}")[0]
                for table in ("model_files", "file_archive_members", "model_artifacts", "downloaded_files")}

    def checked(self, answer):
        report = grouping_answer.check(self.conn, json.dumps(answer))
        return grouping_answer.record(self.conn, report, "answer.json"), report

    def apply_answer(self, answer):
        import_id, _ = self.checked(answer)
        return import_id, grouping_apply.apply(self.conn, import_id)

    def holders(self):
        """Model name for each file in the collection, as browse would show it."""
        return {r[0]: r[1] for r in self.conn.execute("""
            SELECT a.filename, m.name FROM model_artifacts a JOIN models m ON m.id = a.model_id
            JOIN model_files f ON f.file_id = a.file_id WHERE f.model_id = ?
        """, (self.collection,))}


class ApplyTests(ApplyCase):
    def test_the_answer_becomes_the_grouping(self):
        _, result = self.apply_answer(self.answer())
        self.assertEqual([a["unit"] for a in result["applied"]], [self.key])
        holders = self.holders()
        self.assertEqual(holders["28mm_Ghul_A_Supported.stl"], "Ghul A")
        self.assertEqual(holders["32mm_Ghul_A.stl"], "Ghul A")
        self.assertEqual({holders["Banshee_Body.stl"], holders["Banshee_Head.stl"]}, {"Banshee"})
        group = self.one("SELECT g.name FROM model_groups g JOIN model_group_members gm ON gm.group_id = g.id JOIN models m ON m.id = gm.model_id WHERE m.name = 'Ghul B'")
        self.assertEqual(group[0], "Ghuls")

    def test_no_file_record_is_ever_lost(self):
        before = self.counts()
        import_id, _ = self.apply_answer(self.answer())
        self.assertEqual(self.counts(), before)
        grouping_apply.undo(self.conn, import_id)
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(self.holders()), 5, "every file still belongs to a model of its collection")

    def test_the_raw_file_list_reads_the_same(self):
        listing = "SELECT internal_path, size_bytes FROM file_archive_members ORDER BY id"
        before = self.conn.execute(listing).fetchall()
        self.apply_answer(self.answer())
        self.assertEqual(self.conn.execute(listing).fetchall(), before)

    def test_a_model_keeps_its_id_where_it_keeps_its_files(self):
        old = self.one("SELECT model_id FROM model_artifacts WHERE filename = '32mm_Ghul_B.stl'")[0]
        self.apply_answer(self.answer())
        self.assertEqual(self.one("SELECT model_id FROM model_artifacts WHERE filename = '32mm_Ghul_B.stl'")[0], old)

    def test_unplaced_files_stay_listed_on_a_skipped_card(self):
        answer = self.answer()
        unit = answer["units"][0]
        head = next(a for a in unit["models"][2]["artifacts"] if "Head" in next(p["path"] for p in self.exported["printables"] if p["id"] == a))
        unit["models"][2]["artifacts"].remove(head)
        unit["models"][2]["render_artifact"] = unit["models"][2]["artifacts"][0]
        unit["unplaced"] = [{"id": head, "reason": "A spare head"}]
        # One of five is over the 10% a high unit may leave out, so this one waits as a suggestion.
        # Accepting it runs the same apply_plan.
        import_id, report = self.checked(answer)
        self.assertEqual(report["units"][0]["outcome"], "suggest")
        plans = json.loads(self.one("SELECT answer_json FROM grouping_imports WHERE id = ?", import_id)[0])
        grouping_apply.apply_plan(self.conn, import_id, plans[self.key])
        row = self.one("SELECT m.name, m.state FROM model_artifacts a JOIN models m ON m.id = a.model_id WHERE a.filename = 'Banshee_Head.stl'")
        self.assertEqual(tuple(row), (grouping_apply.UNPLACED_CARD, "skipped"))
        cards = archive_review.view(self.conn, self.collection)["cards"]
        self.assertIn(grouping_apply.UNPLACED_CARD, [c["name"] for c in cards])

    def test_the_unit_is_marked_applied_not_reviewed(self):
        self.apply_answer(self.answer())
        self.assertEqual(self.one("SELECT status FROM grouping_units WHERE unit_key = ?", self.key)[0], "applied")
        self.assertEqual(self.one("SELECT reviewed FROM collection_reviews WHERE collection_id = ?", self.collection)[0], 0)

    def test_an_import_applies_once(self):
        import_id, _ = self.apply_answer(self.answer())
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "already applied"):
            grouping_apply.apply(self.conn, import_id)

    def test_files_changed_between_check_and_apply_refuse_that_unit(self):
        import_id, _ = self.checked(self.answer())
        with self.conn:
            self.conn.execute("UPDATE model_artifacts SET size_bytes = 99 WHERE filename = '32mm_Ghul_B.stl'")
        result = grouping_apply.apply(self.conn, import_id)
        self.assertEqual(result["applied"], [])
        self.assertIn("changed after the answer was checked", result["refused"][0]["why"])

    def test_it_writes_an_audit_row(self):
        import_id, _ = self.apply_answer(self.answer())
        detail = json.loads(self.one("SELECT detail FROM audit_log WHERE action = 'grouping_apply'")[0])
        self.assertEqual((detail["import_id"], detail["applied"]), (import_id, [self.key]))


class SuggestTests(ApplyCase):
    def test_a_medium_answer_changes_nothing_and_waits(self):
        answer = self.answer()
        answer["units"][0]["confidence"] = "medium"
        before = grouping_apply.state(self.conn, self.key)
        _, result = self.apply_answer(answer)
        self.assertEqual(result["suggested"], [self.key])
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)
        row = self.one("SELECT bucket, status, cards_json FROM grouping_suggestions WHERE unit_key = ?", self.key)
        self.assertEqual((row[0], row[1]), ("medium", "waiting"))
        self.assertEqual([c["name"] for c in json.loads(row[2])["cards"]], ["Ghul A", "Ghul B", "Banshee"])
        self.assertEqual(self.one("SELECT status FROM grouping_units WHERE unit_key = ?", self.key)[0], "suggested")

    def test_a_newer_suggestion_replaces_a_waiting_one(self):
        for _ in range(2):
            answer = self.answer()
            answer["units"][0]["confidence"] = "medium"
            self.apply_answer(answer)
        statuses = [r[0] for r in self.conn.execute("SELECT status FROM grouping_suggestions ORDER BY id")]
        self.assertEqual(statuses, ["replaced", "waiting"])


class ChoiceTests(ApplyCase):
    """A person holding back or leaving out units at confirm, instead of taking every verdict."""

    def medium(self):
        answer = self.answer()
        answer["units"][0]["confidence"] = "medium"
        return answer

    def test_a_unit_the_checks_passed_can_be_held_for_review(self):
        before = grouping_apply.state(self.conn, self.key)
        import_id, _ = self.checked(self.answer())
        result = grouping_apply.apply(self.conn, import_id, {self.key: "wait"})
        self.assertEqual((result["applied"], result["suggested"]), ([], [self.key]))
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)
        bucket, reasons = self.one("SELECT bucket, reasons_json FROM grouping_suggestions WHERE status = 'waiting'")
        self.assertEqual(bucket, "high", "it waits as the LLM rated it")
        self.assertIn(grouping_apply.HELD_BY_PERSON, json.loads(reasons))

    def test_a_skipped_unit_keeps_todays_grouping_and_goes_out_again(self):
        before = grouping_apply.state(self.conn, self.key)
        import_id, _ = self.checked(self.answer())
        result = grouping_apply.apply(self.conn, import_id, {self.key: "skip"})
        self.assertEqual((result["applied"], result["suggested"], result["skipped"]), ([], [], [self.key]))
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)
        self.assertEqual(self.one("SELECT COUNT(*) FROM grouping_suggestions")[0], 0)
        self.assertEqual(grouping_export.preview(self.conn, "delta", "archives", False, 1000)["units"], 1)

    def test_a_waiting_unit_can_be_skipped_too(self):
        import_id, _ = self.checked(self.medium())
        result = grouping_apply.apply(self.conn, import_id, {self.key: "skip"})
        self.assertEqual((result["suggested"], result["skipped"]), ([], [self.key]))
        self.assertEqual(self.one("SELECT COUNT(*) FROM grouping_suggestions")[0], 0)

    def test_a_waiting_unit_cant_be_pushed_straight_in(self):
        before = grouping_apply.state(self.conn, self.key)
        import_id, _ = self.checked(self.medium())
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "accept it in the review editor"):
            grouping_apply.apply(self.conn, import_id, {self.key: "apply"})
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)
        self.assertEqual(self.one("SELECT status FROM grouping_imports WHERE id = ?", import_id)[0], "checked",
                         "a refused choice leaves the import to confirm again")

    def test_a_refused_unit_can_only_be_skipped(self):
        answer = self.answer()
        answer["units"].append(json.loads(json.dumps(answer["units"][0])))
        answer["units"][1]["unit_id"] = "archive:999999"
        import_id, report = self.checked(answer)
        refused = next(u["unit"] for u in report["units"] if u["outcome"] == grouping_answer.REFUSE)
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "can only be skip"):
            grouping_apply.apply(self.conn, import_id, {refused: "wait"})

    def test_a_unit_not_in_the_answer_is_refused(self):
        import_id, _ = self.checked(self.answer())
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "isn't in that answer"):
            grouping_apply.apply(self.conn, import_id, {"archive:424242": "skip"})

    def test_units_not_named_go_as_the_checks_decided(self):
        import_id, _ = self.checked(self.answer())
        self.assertEqual([a["unit"] for a in grouping_apply.apply(self.conn, import_id, {})["applied"]], [self.key])

    def test_the_audit_row_lists_what_was_skipped(self):
        import_id, _ = self.checked(self.answer())
        grouping_apply.apply(self.conn, import_id, {self.key: "skip"})
        detail = json.loads(self.one("SELECT detail FROM audit_log WHERE action = 'grouping_apply' ORDER BY id DESC")[0])
        self.assertEqual(detail["skipped"], [self.key])


class ReviewNowTests(ApplyCase):
    """E57: one unit of a checked answer opened in the review editor before the rest is confirmed."""

    def medium(self):
        answer = self.answer()
        answer["units"][0]["confidence"] = "medium"
        return answer

    def test_a_waiting_unit_goes_to_review_and_the_answer_stays_unconfirmed(self):
        before = grouping_apply.state(self.conn, self.key)
        import_id, _ = self.checked(self.medium())
        result = grouping_apply.review_now(self.conn, import_id, self.key)
        self.assertEqual(self.one("SELECT status FROM grouping_suggestions WHERE id = ?", result["suggestion_id"])[0], "waiting")
        self.assertEqual(self.one("SELECT status FROM grouping_imports WHERE id = ?", import_id)[0], "checked")
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)

    def test_the_later_confirm_leaves_it_to_the_review(self):
        import_id, _ = self.checked(self.medium())
        grouping_apply.review_now(self.conn, import_id, self.key)
        result = grouping_apply.apply(self.conn, import_id, {self.key: "skip"})
        self.assertEqual((result["filed"], result["suggested"], result["skipped"]), ([self.key], [], []))
        self.assertEqual(self.one("SELECT COUNT(*) FROM grouping_suggestions WHERE status = 'waiting'")[0], 1)

    def test_a_unit_that_would_apply_is_held_and_says_why(self):
        import_id, _ = self.checked(self.answer())
        suggestion = grouping_apply.review_now(self.conn, import_id, self.key)["suggestion_id"]
        reasons = json.loads(self.one("SELECT reasons_json FROM grouping_suggestions WHERE id = ?", suggestion)[0])
        self.assertIn(grouping_apply.HELD_BY_PERSON, reasons)
        self.assertEqual(grouping_apply.apply(self.conn, import_id)["applied"], [])

    def test_accepting_before_the_confirm_counts(self):
        import_id, _ = self.checked(self.medium())
        suggestion = grouping_apply.review_now(self.conn, import_id, self.key)["suggestion_id"]
        cards = grouping_suggestions.view(self.conn, suggestion)["cards"]
        grouping_suggestions.accept(self.conn, suggestion, cards)
        self.assertEqual(self.holders()["28mm_Ghul_A_Supported.stl"], "Ghul A")
        self.assertEqual(grouping_apply.apply(self.conn, import_id)["filed"], [self.key])

    def test_it_goes_to_review_once(self):
        import_id, _ = self.checked(self.medium())
        grouping_apply.review_now(self.conn, import_id, self.key)
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "already waiting for you"):
            grouping_apply.review_now(self.conn, import_id, self.key)

    def test_a_refused_unit_has_nothing_to_review(self):
        answer = self.answer()
        answer["units"].append(json.loads(json.dumps(answer["units"][0])))
        answer["units"][1]["unit_id"] = "archive:999999"
        import_id, report = self.checked(answer)
        refused = next(u["unit"] for u in report["units"] if u["outcome"] == grouping_answer.REFUSE)
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "nothing to review"):
            grouping_apply.review_now(self.conn, import_id, refused)

    def test_only_an_answer_waiting_for_confirmation(self):
        import_id, _ = self.apply_answer(self.answer())
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "already applied"):
            grouping_apply.review_now(self.conn, import_id, self.key)


class PictureTests(ApplyCase):
    def picture(self, name):
        return tuple(self.one("SELECT image_path, image_source FROM models WHERE name = ?", name))

    def clear_pictures(self):
        with self.conn:
            self.conn.execute("UPDATE models SET image_path = NULL, image_source = NULL")

    def test_a_model_with_no_picture_gets_the_one_chosen(self):
        self.clear_pictures()
        self.apply_answer(self.answer())
        path, source = self.picture("Ghul B")
        self.assertTrue(path.endswith("/Ghul_B.png"))
        self.assertEqual(source, "llm")

    def test_a_model_without_a_picture_gets_a_render_queued(self):
        self.apply_answer(self.answer())
        banshee = self.one("SELECT id FROM models WHERE name = 'Banshee'")[0]
        job = self.one("SELECT kind, source_json, status FROM picture_jobs WHERE model_id = ?", banshee)
        self.assertEqual((job[0], job[2]), ("render", "pending"))
        artifact = json.loads(job[1])["artifact"]
        self.assertEqual(self.one("SELECT model_id FROM model_artifacts WHERE id = ?", artifact)[0], banshee)

    def test_a_group_gets_its_group_shot(self):
        self.apply_answer(self.answer())
        path, source = tuple(self.one("SELECT image_path, image_source FROM model_groups WHERE name = 'Ghuls'"))
        self.assertTrue(path.endswith("/Ghuls_Group.png"))
        self.assertEqual(source, "llm")

    def test_a_picture_the_archive_matched_is_only_replaced_by_a_person(self):
        answer = self.answer()
        models = answer["units"][0]["models"]
        models[0]["image"], models[1]["image"] = models[1]["image"], models[0]["image"]
        before = self.one("SELECT image_path FROM models WHERE id = (SELECT model_id FROM model_artifacts WHERE filename = '32mm_Ghul_B.stl')")[0]
        _, result = self.apply_answer(answer)
        self.assertEqual(self.picture("Ghul B")[0], before, "the matcher's own picture stays")
        self.assertIn("Ghul B", [p["name"] for p in result["pictures_held_back"]])
        waiting = json.loads(self.one("SELECT cards_json FROM grouping_suggestions WHERE status = 'waiting'")[0])
        self.assertTrue(waiting["pictures"])

    def test_a_locked_picture_is_never_touched(self):
        with self.conn:
            self.conn.execute("UPDATE models SET image_locked = 1, image_source = 'owner' WHERE id = (SELECT model_id FROM model_artifacts WHERE filename = '32mm_Ghul_B.stl')")
        before = self.picture(self.one("SELECT name FROM models WHERE image_locked = 1")[0])
        self.apply_answer(self.answer())
        self.assertEqual(self.picture("Ghul B"), before)

    def test_an_unsure_picture_waits_while_the_grouping_applies(self):
        answer = self.answer()
        answer["units"][0]["models"][1]["image_confidence"] = "medium"
        self.clear_pictures()
        _, result = self.apply_answer(answer)
        self.assertEqual([a["unit"] for a in result["applied"]], [self.key])
        self.assertIn("Ghul B", [p["name"] for p in result["pictures_held_back"]])


class UndoTests(ApplyCase):
    def test_undo_puts_back_exactly_what_was_there(self):
        before = grouping_apply.state(self.conn, self.key)
        import_id, _ = self.apply_answer(self.answer())
        self.assertNotEqual(grouping_apply.state(self.conn, self.key), before)
        result = grouping_apply.undo(self.conn, import_id)
        self.assertEqual(result["restored"], [self.key])
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)

    def test_a_model_the_apply_made_redirects_after_undo(self):
        import_id, _ = self.apply_answer(self.answer())
        made = {r[0] for r in self.conn.execute("SELECT id FROM models WHERE name IN ('Ghul A', 'Banshee')")}
        grouping_apply.undo(self.conn, import_id)
        for model_id in made:
            state, merged = self.one("SELECT state, merged_into FROM models WHERE id = ?", model_id)
            if state == "merged":
                self.assertIsNotNone(merged)

    def test_a_unit_a_person_edited_since_is_left_alone(self):
        import_id, _ = self.apply_answer(self.answer())
        with self.conn:
            self.conn.execute("UPDATE models SET name = 'Ghul A, renamed by hand' WHERE name = 'Ghul A'")
        result = grouping_apply.undo(self.conn, import_id)
        self.assertEqual(result["restored"], [])
        self.assertIn("Changed since", result["left"][0]["why"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM models WHERE name = 'Ghul A, renamed by hand'")[0], 1)

    def finish_picture_jobs(self):
        """What the image worker does once the NAS answers: each queued picture lands."""
        self.conn.row_factory = sqlite3.Row
        for job in self.conn.execute("SELECT * FROM picture_jobs WHERE status = 'pending'").fetchall():
            picture_jobs._set(self.conn, job, f"renders/{job['id']}.png", "render")
            picture_jobs._finish(self.conn, job["id"], "done")

    def test_undo_still_restores_once_its_own_pictures_arrive(self):
        # Seen on production 2026-11-08: Ogre Boss's renders landed seconds after the apply, and
        # undo took them for a person's edit and left the unit applied.
        before = grouping_apply.state(self.conn, self.key)
        import_id, _ = self.apply_answer(self.answer())
        self.finish_picture_jobs()
        self.assertTrue(self.one("SELECT COUNT(*) FROM models WHERE image_source = 'render'")[0])
        result = grouping_apply.undo(self.conn, import_id)
        self.assertEqual((result["restored"], result["left"]), ([self.key], []))
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)

    def test_a_person_edit_after_the_pictures_still_stops_undo(self):
        import_id, _ = self.apply_answer(self.answer())
        self.finish_picture_jobs()
        with self.conn:
            self.conn.execute("UPDATE models SET name = 'Ghul the First' WHERE name = 'Ghul A'")
        self.assertEqual([u["unit"] for u in grouping_apply.undo(self.conn, import_id)["left"]], [self.key])

    def test_a_picture_landing_after_a_person_edit_doesnt_hide_the_edit(self):
        import_id, _ = self.apply_answer(self.answer())
        with self.conn:
            self.conn.execute("UPDATE models SET name = 'Ghul the First' WHERE name = 'Ghul A'")
        self.finish_picture_jobs()
        self.assertEqual([u["unit"] for u in grouping_apply.undo(self.conn, import_id)["left"]], [self.key])

    def test_undo_cancels_picture_jobs_it_queued(self):
        import_id, _ = self.apply_answer(self.answer())
        grouping_apply.undo(self.conn, import_id)
        self.assertEqual(self.one("SELECT COUNT(*) FROM picture_jobs WHERE status = 'pending'")[0], 0)

    def test_an_undone_unit_goes_out_in_the_next_delta(self):
        import_id, _ = self.apply_answer(self.answer())
        grouping_apply.undo(self.conn, import_id)
        self.assertEqual(tuple(self.one("SELECT status, decided_fingerprint FROM grouping_units WHERE unit_key = ?", self.key)), ("undone", None))

    def test_only_an_applied_import_can_be_undone(self):
        import_id, _ = self.checked(self.answer())
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "Only an applied import"):
            grouping_apply.undo(self.conn, import_id)


class RedirectTests(ApplyCase):
    def test_an_emptied_model_redirects_to_where_its_files_went(self):
        # Archive Review's save sent every emptied model to the first card, so an old link to
        # Voragheel's rock opened Slaughtersteel Golem. Here the Ghuls card comes second.
        old_b = self.one("SELECT model_id FROM model_artifacts WHERE filename = '32mm_Ghul_B.stl'")[0]
        answer = self.answer()
        models = answer["units"][0]["models"]
        ghuls = {"name": "Ghuls", "artifacts": models[0]["artifacts"] + models[1]["artifacts"], "confidence": "high"}
        answer["units"][0]["models"] = [models[2], ghuls]
        answer["units"][0]["groups"] = []
        import_id, _ = self.checked(answer)
        plans = json.loads(self.one("SELECT answer_json FROM grouping_imports WHERE id = ?", import_id)[0])
        grouping_apply.apply_plan(self.conn, import_id, plans[self.key])
        ghuls_id = self.one("SELECT model_id FROM model_artifacts WHERE filename = '32mm_Ghul_B.stl'")[0]
        self.assertNotEqual(ghuls_id, old_b, "Ghul A's model has more files, so it keeps its ID")
        self.assertEqual(tuple(self.one("SELECT state, merged_into FROM models WHERE id = ?", old_b)), ("merged", ghuls_id))


class SuggestionReviewTests(ApplyCase):
    """A waiting answer, opened in the review editor and decided by a person."""

    def suggest(self):
        answer = self.answer()
        answer["units"][0]["confidence"] = "medium"
        answer["units"][0]["reason"] = "Not sure the Banshee is one figure"
        self.apply_answer(answer)
        return self.one("SELECT id FROM grouping_suggestions WHERE status = 'waiting'")[0]

    def test_it_opens_with_the_llms_cards_and_reasons(self):
        view = grouping_suggestions.view(self.conn, self.suggest())
        self.assertEqual([c["name"] for c in view["cards"]], ["Ghul A", "Ghul B", "Banshee"])
        self.assertIn("Not sure the Banshee", " ".join(view["suggestion"]["reasons"]))
        self.assertTrue(all("Suggested" in c["flags"][0] for c in view["cards"]))
        self.assertEqual(view["cards"][1]["picture"], "i2")
        self.assertTrue(all(i["url"].startswith("/previews/") for i in view["suggestion"]["images"]))

    def test_the_waiting_list_says_what_each_is(self):
        self.suggest()
        item = grouping_suggestions.waiting(self.conn)[0]
        self.assertEqual((item["unit"], item["bucket"], item["kind"], item["models"]), (self.key, "medium", "grouping", 3))

    def test_accepting_saves_it_as_a_persons_review_with_their_pictures_locked(self):
        suggestion = self.suggest()
        cards = grouping_suggestions.view(self.conn, suggestion)["cards"]
        cards[2]["name"] = "Banshee, edited"
        grouping_suggestions.accept(self.conn, suggestion, cards, {cards[0]["paths"][0]: "i3"})
        self.assertIn("Banshee, edited", self.holders().values())
        self.assertEqual(self.one("SELECT reviewed FROM collection_reviews WHERE collection_id = ?", self.collection)[0], 1)
        path, source, locked = self.one("SELECT image_path, image_source, image_locked FROM models WHERE name = 'Ghul A'")
        self.assertTrue(path.endswith("Ghuls_Group.png"))
        self.assertEqual((source, locked), ("owner", 1))
        self.assertEqual(self.one("SELECT status FROM grouping_suggestions WHERE id = ?", suggestion)[0], "accepted")

    def test_rejecting_keeps_todays_grouping_and_the_unit_out_of_deltas(self):
        before = grouping_apply.state(self.conn, self.key)
        grouping_suggestions.reject(self.conn, self.suggest())
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)
        self.assertEqual(self.one("SELECT status FROM grouping_units WHERE unit_key = ?", self.key)[0], "rejected")
        self.assertEqual(grouping_export.preview(self.conn, "delta", "archives", False, 2000)["units"], 0)

    def test_a_suggestion_is_decided_once(self):
        suggestion = self.suggest()
        grouping_suggestions.reject(self.conn, suggestion)
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "already rejected"):
            grouping_suggestions.accept(self.conn, suggestion, None)

    def test_a_suggestion_for_files_that_changed_is_refused(self):
        suggestion = self.suggest()
        with self.conn:
            self.conn.execute("UPDATE model_artifacts SET size_bytes = 99 WHERE filename = '32mm_Ghul_B.stl'")
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "no longer fits"):
            grouping_suggestions.view(self.conn, suggestion)

    def test_a_picture_must_be_one_of_this_units(self):
        suggestion = self.suggest()
        cards = grouping_suggestions.view(self.conn, suggestion)["cards"]
        before = grouping_apply.state(self.conn, self.key)
        with self.assertRaisesRegex(grouping_apply.ApplyRefused, "isn't a picture of this unit"):
            grouping_suggestions.accept(self.conn, suggestion, cards, {cards[0]["paths"][0]: "a1"})
        # Nothing was saved, and the suggestion still waits.
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)
        self.assertEqual(self.one("SELECT status FROM grouping_suggestions WHERE id = ?", suggestion)[0], "waiting")

    def test_a_picture_not_held_here_is_fetched_for_the_person_not_refused(self):
        suggestion = self.suggest()
        cards = grouping_suggestions.view(self.conn, suggestion)["cards"]
        (app_config.previews_dir() / str(self.collection) / str(self.file_id) / "Ghuls_Group.png").unlink()
        before = self.one("SELECT image_path FROM models WHERE name = 'Ghul A'")[0]
        result = grouping_suggestions.accept(self.conn, suggestion, cards, {cards[0]["paths"][0]: "i3"})
        self.assertEqual((result["pictures"], result["pictures_waiting"]), (0, 1))
        kind, source = tuple(self.one("SELECT kind, source_json FROM picture_jobs WHERE status = 'pending'"))
        self.assertEqual((kind, json.loads(source)["source"]), ("extract", "owner"))
        self.assertTrue(json.loads(source)["preview"].endswith("Ghuls_Group.png"))
        # It keeps today's picture until the job lands, rather than showing nothing.
        self.assertEqual(self.one("SELECT image_path FROM models WHERE name = 'Ghul A'")[0], before)

    def test_a_persons_choice_cancels_a_job_still_waiting_for_that_model(self):
        suggestion = self.suggest()
        cards = grouping_suggestions.view(self.conn, suggestion)["cards"]
        ghul_a = self.one("SELECT id FROM models WHERE name = 'Ghul A'")[0]
        with self.conn:
            waiting = self.conn.execute("INSERT INTO picture_jobs (kind, model_id, source_json) VALUES ('render', ?, '{}')",
                                        (ghul_a,)).lastrowid
        grouping_suggestions.accept(self.conn, suggestion, cards, {cards[0]["paths"][0]: "i2"})
        self.assertEqual(tuple(self.one("SELECT status, detail FROM picture_jobs WHERE id = ?", waiting)),
                         ("skipped", "Replaced by a later choice"))

    def test_a_pictures_only_suggestion_sets_just_the_pictures(self):
        answer = self.answer()
        answer["units"][0]["models"][1]["image_confidence"] = "medium"
        with self.conn:
            self.conn.execute("UPDATE models SET image_path = NULL, image_source = NULL")
        self.apply_answer(answer)
        suggestion = self.one("SELECT id FROM grouping_suggestions WHERE status = 'waiting'")[0]
        view = grouping_suggestions.view(self.conn, suggestion)
        self.assertEqual(view["suggestion"]["kind"], "pictures")
        item = view["suggestion"]["pictures"][0]
        grouping_suggestions.accept(self.conn, suggestion, None, {str(item["model_id"]): item["image"]})
        self.assertEqual(self.one("SELECT image_source FROM models WHERE id = ?", item["model_id"])[0], "owner")


class TagTests(ApplyCase):
    def setUp(self):
        super().setUp()
        with self.conn:
            self.conn.execute("UPDATE collections SET source = 'import' WHERE id = ?", (self.collection,))
            self.conn.execute("INSERT INTO tags (name) VALUES ('undead')")

    def tagged(self, model="Ghul A"):
        return [r[0] for r in self.conn.execute(
            "SELECT t.tag FROM model_tags t JOIN models m ON m.id = t.model_id WHERE m.name = ? ORDER BY t.tag", (model,))]

    def test_an_existing_tag_is_added(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["tags"] = ["undead"]
        self.apply_answer(answer)
        self.assertEqual(self.tagged(), ["undead"])

    def test_a_tag_a_person_removed_stays_off(self):
        with self.conn:
            self.conn.execute("INSERT INTO removed_tags (collection_id, tag) VALUES (?, 'undead')", (self.collection,))
        answer = self.answer()
        answer["units"][0]["models"][0]["tags"] = ["undead"]
        self.apply_answer(answer)
        self.assertEqual(self.tagged(), [])

    def test_a_new_tag_waits_until_a_person_approves_it(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["tags"] = ["ghoulish"]
        import_id, _ = self.apply_answer(answer)
        self.assertEqual(self.tagged(), [])
        grouping_suggestions.approve_tags(self.conn, import_id, ["ghoulish"])
        self.assertEqual(self.tagged(), ["ghoulish"])
