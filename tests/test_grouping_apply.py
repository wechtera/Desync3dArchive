"""E54: applying a checked answer, and undoing it. Nothing is ever lost."""

import json

from test_grouping import GroupingCase

from desync_archival.services import archive_review, grouping_answer, grouping_apply


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
