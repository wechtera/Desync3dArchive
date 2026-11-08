"""E54, grouping by LLM: the export, and the checks every answer passes before anything changes."""

import copy
import io
import json
import struct
import unittest
import zipfile
import zlib

from test_catalog_levels import BuildCase

from desync_archival import settings as app_config
from desync_archival.services import (
    catalog_levels,
    grouping_answer,
    grouping_contract,
    grouping_export,
    grouping_units,
)
from desync_archival.services.grouping_answer import APPLY, REFUSE, SUGGEST


def png(width, height):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + chunk + struct.pack(">I", zlib.crc32(chunk))


class GroupingCase(BuildCase):
    """A peeked collection like Vampire Court: Ghul A and B, a Banshee, and their renders."""

    FILES = (
        ("28mm Supported/Ghuls/28mm_Ghul_A_Supported.stl", 10), ("32mm Unsupported/Ghuls/32mm_Ghul_A.stl", 11),
        ("32mm Unsupported/Ghuls/32mm_Ghul_B.stl", 12), ("32mm Unsupported/Banshee/Banshee_Body.stl", 13),
        ("32mm Unsupported/Banshee/Banshee_Head.stl", 14),
    )
    IMAGES = (("Renders/Ghul_A.png", 500), ("Renders/Ghul_B.png", 501), ("Renders/Ghuls_Group.png", 502))

    def setUp(self):
        super().setUp()
        with self.conn:
            self.conn.execute("UPDATE collections SET source = 'mmf', designer_name = 'Titan Forge' WHERE id = ?", (self.collection,))
        self.file_id = self.archive(self.collection, [*self.FILES, *self.IMAGES, ("Readme.pdf", 9)])
        self.build()
        for path, _ in self.IMAGES:
            preview = app_config.previews_dir() / str(self.collection) / str(self.file_id) / path.rpartition("/")[2]
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(png(1600, 1200))
            self.addCleanup(preview.unlink, missing_ok=True)
        # A peek extracts previews before it matches pictures, so match again now that they exist.
        with self.conn:
            catalog_levels.assign_images(self.conn, self.collection)
        self.key = grouping_units.archive_key(self.collection)

    def unit(self):
        return grouping_units.build(self.conn, self.key)

    def export(self, **options):
        options.setdefault("mode", "single")
        options.setdefault("scope", "archives")
        if options["mode"] == "single":
            options.setdefault("unit_key", self.key)
        export_key, blob, summary = grouping_export.build(self.conn, **options)
        archive = zipfile.ZipFile(io.BytesIO(blob))
        return export_key, archive, summary

    def ids(self, unit, word):
        return [p["id"] for p in unit["printables"] if word in p["path"]]

    def answer(self):
        """What a careful LLM returns for this unit."""
        export_key, archive, _ = self.export()
        unit = json.loads(archive.read("batch-01.json"))["units"][0]
        image = {i["path"].rpartition("/")[2]: i["id"] for i in unit["images"]}
        ghul_a, ghul_b, banshee = self.ids(unit, "Ghul_A"), self.ids(unit, "Ghul_B"), self.ids(unit, "Banshee")
        self.exported = unit
        return {"export_id": export_key, "units": [{
            "unit_id": unit["unit_id"], "fingerprint": unit["fingerprint"], "confidence": "high",
            "models": [
                {"name": "Ghul A", "group": "Ghuls", "artifacts": ghul_a, "image": image["Ghul_A.png"], "image_confidence": "high",
                 "render_artifact": ghul_a[1], "confidence": "high"},
                {"name": "Ghul B", "group": "Ghuls", "artifacts": ghul_b, "image": image["Ghul_B.png"], "image_confidence": "high",
                 "confidence": "high"},
                {"name": "Banshee", "artifacts": banshee, "image": None, "render_artifact": banshee[0], "confidence": "high"},
            ],
            "groups": [{"name": "Ghuls", "image": image["Ghuls_Group.png"], "image_confidence": "high"}],
        }]}

    def check(self, answer):
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return grouping_answer.check(self.conn, text)

    def verdict(self, answer):
        report = self.check(answer)
        self.assertEqual(report["error"], "", report)
        return report["units"][0]


class UnitTests(GroupingCase):
    def test_files_go_out_under_short_ids_in_path_order(self):
        unit = self.unit().payload
        self.assertEqual([p["id"] for p in unit["printables"]], ["a1", "a2", "a3", "a4", "a5"])
        self.assertEqual([i["id"] for i in unit["images"]], ["i1", "i2", "i3"])

    def test_one_archive_is_named_once_and_paths_start_inside_it(self):
        unit = self.unit()
        self.assertEqual(unit.payload["archives"], ["a.zip"])
        self.assertEqual(unit.payload["printables"][0]["path"], "28mm Supported/Ghuls/28mm_Ghul_A_Supported.stl")
        self.assertEqual(unit.payload["images"][0]["path"], "Renders/Ghul_A.png")
        self.assertEqual(unit.payload["other_files"], ["Readme.pdf"])
        # The checks and the review use the ID map, which keeps the full path.
        self.assertEqual(unit.ids["a1"]["path"], "a.zip/28mm Supported/Ghuls/28mm_Ghul_A_Supported.stl")

    def test_several_archives_keep_their_names_on_each_path(self):
        with self.conn:
            file_id = self.conn.execute(
                "INSERT INTO model_files (model_id, filename, size_bytes, download_url, source) VALUES (?, 'b.zip', 1, '', 'mmf')",
                (self.collection,)).lastrowid
            self.conn.execute("INSERT INTO file_archive_members (file_id, internal_path, filename, size_bytes) VALUES (?, 'Wraith/Wraith.stl', 'Wraith.stl', 40)",
                              (file_id,))
        self.build()
        payload = self.unit().payload
        self.assertEqual(payload["archives"], ["a.zip", "b.zip"])
        self.assertIn("b.zip/Wraith/Wraith.stl", [p["path"] for p in payload["printables"]])
        self.assertTrue(all(p["path"].startswith(("a.zip/", "b.zip/")) for p in payload["printables"]))

    def test_images_say_their_size_whether_theyre_here_and_what_they_seem_to_show(self):
        group = next(i for i in self.unit().payload["images"] if "Group" in i["path"])
        self.assertEqual((group["pixels"], group["available"], group["roles"]), ("1600x1200", True, ["group"]))

    def test_identity_comes_from_the_catalog(self):
        unit = self.unit().payload
        self.assertEqual((unit["creator"], unit["source"], unit["tags_allowed"]), ("Titan Forge", "mmf", False))

    def test_todays_grouping_goes_out_as_a_hint(self):
        current = self.unit().payload["current"]
        self.assertIn("may be wrong", current["note"])
        self.assertEqual(sorted(a for m in current["models"] for a in m["artifacts"]), ["a1", "a2", "a3", "a4", "a5"])
        ghul_a = next(m for m in current["models"] if m["name"] == "Ghul A")
        # IDs follow path order, and Banshee's folder sorts before Ghul A's unsupported file.
        self.assertEqual((ghul_a["artifacts"], ghul_a["image"]), (["a1", "a4"], "i1"))

    def test_the_fingerprint_follows_files_not_names(self):
        before = self.unit().fingerprint
        with self.conn:
            self.conn.execute("UPDATE models SET name = 'Renamed'")
        self.assertEqual(self.unit().fingerprint, before)
        with self.conn:
            self.conn.execute("UPDATE model_artifacts SET size_bytes = 99 WHERE filename = '32mm_Ghul_B.stl'")
        self.assertNotEqual(self.unit().fingerprint, before)

    def test_a_release_name_with_wildcard_characters_matches_only_itself(self):
        # LIKE would read _ as any character, and 2020_09 would take 2020-09's files.
        self.assertEqual(grouping_units.parse_key("library:3:2020_09 - Pack"), ("library", 3, "2020_09 - Pack"))
        with self.assertRaises(grouping_units.UnitError):
            grouping_units.parse_key("archive:abc")


class ExportTests(GroupingCase):
    def test_the_zip_holds_the_contract_and_the_batches(self):
        _, archive, summary = self.export()
        self.assertEqual(sorted(archive.namelist()), ["README.txt", "answer-format.json", "batch-01.json", "prompt.md"])
        self.assertEqual(json.loads(archive.read("answer-format.json")), grouping_contract.answer_schema())
        self.assertEqual(summary["units"], 1)

    def test_the_prompt_settles_what_llms_read_two_ways(self):
        # The 2026-11-08 LLM test: Opus made a base pack one model where Sonnet split it, and both
        # held most units for review only because models shared a picture.
        _, archive, _ = self.export()
        prompt = " ".join(archive.read("prompt.md").decode().split())
        self.assertIn("Each base sold on its own is its own model,** in a group called Bases, even when many come in one pack", prompt)
        self.assertIn("A shared group shot, a scale sheet or no picture at all never lowers a model's or the unit's confidence", prompt)

    def test_a_batch_holds_one_unit_per_line_without_indents(self):
        with self.conn:
            other = self.add_collection("Banshees", "1/c")
        self.archive(other, [("Banshee_C.stl", 20)])
        self.build()
        _, archive, _ = self.export(mode="full", include_reviewed=True)
        text = archive.read("batch-01.json").decode()
        body = json.loads(text)
        self.assertEqual(len(body["units"]), 2)
        self.assertEqual(len(text.splitlines()), 4)
        self.assertNotIn("\n ", text)

    def test_tags_go_out_only_where_a_unit_takes_them(self):
        with self.conn:
            self.conn.execute("UPDATE collections SET source = 'import' WHERE id = ?", (self.collection,))
            self.conn.execute("INSERT INTO tags (name) VALUES ('undead')")
        _, archive, _ = self.export()
        self.assertIn("undead", archive.read("tags.txt").decode())

    def test_the_export_keeps_what_each_id_meant(self):
        export_key, _, _ = self.export()
        row = self.conn.execute("""
            SELECT u.ids_json FROM grouping_export_units u JOIN grouping_exports e ON e.id = u.export_id WHERE e.export_key = ?
        """, (export_key,)).fetchone()
        ids = json.loads(row[0])
        artifact = self.one("SELECT id FROM model_artifacts WHERE filename = '28mm_Ghul_A_Supported.stl'")[0]
        self.assertEqual((ids["a1"]["kind"], ids["a1"]["id"]), ("artifact", artifact))
        self.assertEqual(ids["i1"]["kind"], "image")

    def test_delta_leaves_out_a_unit_whose_answer_is_settled_until_its_files_change(self):
        self.export()
        self.assertEqual(grouping_export.preview(self.conn, "delta", "archives", False, 2000)["units"], 1,
                         "exported but never answered still goes out")
        with self.conn:
            self.conn.execute("UPDATE grouping_units SET status = 'applied', decided_fingerprint = exported_fingerprint")
        self.assertEqual(grouping_export.preview(self.conn, "delta", "archives", False, 2000)["units"], 0)
        with self.conn:
            self.conn.execute("UPDATE model_artifacts SET size_bytes = 99 WHERE filename = '32mm_Ghul_B.stl'")
        self.assertEqual(grouping_export.preview(self.conn, "delta", "archives", False, 2000)["units"], 1)

    def test_a_reviewed_unit_stays_out_unless_asked_for(self):
        with self.conn:
            self.conn.execute("INSERT INTO collection_reviews (collection_id, reviewed) VALUES (?, 1)", (self.collection,))
        self.assertEqual(grouping_export.preview(self.conn, "full", "archives", False, 2000)["units"], 0)
        self.assertEqual(grouping_export.preview(self.conn, "full", "archives", True, 2000)["units"], 1)

    def test_batches_hold_whole_units_and_a_big_one_goes_alone(self):
        unit = self.unit()
        small = [unit, unit, unit]
        self.assertEqual([len(b) for b in grouping_export.batches(small, unit.lines * 2)], [2, 1])
        self.assertEqual([len(b) for b in grouping_export.batches(small, 1)], [1, 1, 1])

    def test_settings_out_of_range_are_refused(self):
        for options in ({"mode": "sideways"}, {"scope": "everything"}, {"batch_lines": 5}):
            with self.subTest(options), self.assertRaises(grouping_export.ExportRefused):
                grouping_export.build(self.conn, **{"mode": "full", "scope": "archives", **options})

    def test_nothing_to_export_says_so(self):
        self.export()
        with self.conn:
            self.conn.execute("UPDATE grouping_units SET decided_fingerprint = exported_fingerprint")
        with self.assertRaisesRegex(grouping_export.ExportRefused, "Nothing to export"):
            grouping_export.build(self.conn, mode="delta", scope="archives")


class ReadingTests(unittest.TestCase):
    """What LLMs wrap their JSON in, and what the app reads past."""

    def read(self, text):
        return grouping_answer.read_text(text)

    def test_plain_json(self):
        self.assertEqual(self.read('{"a": 1}'), ({"a": 1}, [], ""))

    def test_a_code_fence_with_prose_around_it(self):
        data, notes, error = self.read('Here is the answer:\n```json\n{"a": 1}\n```\nLet me know!')
        self.assertEqual((data, error), ({"a": 1}, ""))
        self.assertIn("Read the JSON inside a code fence", notes)

    def test_trailing_commas(self):
        data, notes, _ = self.read('{"a": [1, 2,], }')
        self.assertEqual(data, {"a": [1, 2]})
        self.assertIn("Removed commas before a closing bracket", notes)

    def test_a_byte_order_mark(self):
        self.assertEqual(self.read('﻿{"a": 1}')[0], {"a": 1})

    def test_broken_json_says_where(self):
        _, _, error = self.read('{"a": 1,\n "b": }')
        self.assertIn("line 2", error)
        self.assertIn("isn't valid JSON", error)

    def test_empty_and_huge(self):
        self.assertIn("empty", self.read("   ")[2])
        self.assertIn("MB", self.read("x" * (grouping_answer.MAX_ANSWER_BYTES + 1))[2])


class NameTests(unittest.TestCase):
    def test_names_are_tidied(self):
        self.assertEqual(grouping_answer.clean_name("  Ghul   A "), ("Ghul A", ""))

    def test_names_that_could_mislead_a_person_are_refused(self):
        for bad in ("Ghul\u202eA", "Ghul\x00A", "<b>Ghul</b>", "see https://example.com", "", "   ", "!!!", "x" * 121, 7):
            with self.subTest(repr(bad)):
                self.assertTrue(grouping_answer.clean_name(bad)[1])

    def test_quoted_text_shows_hidden_characters_as_codes(self):
        self.assertEqual(grouping_answer.visible("a\u202eb"), "a\\u202eb")


class RenderChoiceTests(GroupingCase):
    """Only an STL can be rendered, so a render_artifact that isn't one falls back to the model's STL."""

    FILES = (*GroupingCase.FILES, ("32mm Unsupported/Banshee/Banshee_Body.lys", 15))

    def artifact_id(self, name):
        return self.one("SELECT id FROM model_artifacts WHERE filename = ?", name)[0]

    def test_a_lychee_file_picked_to_render_becomes_the_models_stl(self):
        answer = self.answer()
        banshee = answer["units"][0]["models"][2]
        lychee = next(s for s in banshee["artifacts"] if self.exported_path(s).endswith(".lys"))
        banshee["render_artifact"] = lychee
        report = self.check(answer)
        self.assertIn("isn't an STL", " ".join(report["units"][0]["warnings"]))
        self.assertEqual(report["units"][0]["outcome"], APPLY)
        plan = report["plans"][self.key]["models"][2]
        self.assertEqual(plan["render_artifact"], self.artifact_id("Banshee_Body.stl"))

    def test_a_model_with_no_stl_gets_no_render(self):
        answer = self.answer()
        models = answer["units"][0]["models"]
        lychee = next(s for s in models[2]["artifacts"] if self.exported_path(s).endswith(".lys"))
        models[2]["artifacts"].remove(lychee)
        models[2]["render_artifact"] = models[2]["artifacts"][0]
        models.append({"name": "Banshee Lychee", "artifacts": [lychee], "image": None, "render_artifact": lychee, "confidence": "high"})
        report = self.check(answer)
        self.assertIn("no render will be made", " ".join(report["units"][0]["warnings"]))
        self.assertIsNone(report["plans"][self.key]["models"][3]["render_artifact"])

    def exported_path(self, short):
        return next(p["path"] for p in self.exported["printables"] if p["id"] == short)


class CheckTests(GroupingCase):
    def test_a_careful_answer_applies(self):
        verdict = self.verdict(self.answer())
        self.assertEqual((verdict["outcome"], verdict["bucket"]), (APPLY, "high"))
        self.assertEqual(verdict["after"]["models"], 3)
        self.assertIn("models become 3", verdict["summary"])

    def test_a_medium_rating_waits_for_a_person_with_its_reason(self):
        answer = self.answer()
        answer["units"][0]["models"][2].update(confidence="medium", reason="Might be two figures")
        verdict = self.verdict(answer)
        self.assertEqual((verdict["outcome"], verdict["bucket"]), (SUGGEST, "medium"))
        self.assertIn("Might be two figures", " ".join(verdict["reasons"]))

    def test_a_low_unit_is_low(self):
        answer = self.answer()
        answer["units"][0]["confidence"] = "low"
        self.assertEqual(self.verdict(answer)["bucket"], "low")

    def test_a_reviewed_unit_never_applies_on_its_own(self):
        answer = self.answer()
        with self.conn:
            self.conn.execute("INSERT INTO collection_reviews (collection_id, reviewed) VALUES (?, 1)", (self.collection,))
        verdict = self.verdict(answer)
        self.assertEqual(verdict["outcome"], SUGGEST)
        self.assertIn("A person reviewed this unit", " ".join(verdict["reasons"]))

    def test_too_much_left_unplaced_waits_for_a_person(self):
        answer = self.answer()
        unit = answer["units"][0]
        banshee = unit["models"].pop()
        unit["unplaced"] = [{"id": a, "reason": "unsure"} for a in banshee["artifacts"]]
        verdict = self.verdict(answer)
        self.assertEqual(verdict["outcome"], SUGGEST)
        self.assertIn("left out of every model", " ".join(verdict["reasons"]))

    def test_it_tolerates_wrapping_and_reports_it(self):
        report = self.check("Sure!\n```json\n" + json.dumps(self.answer()) + "\n```")
        self.assertEqual(report["units"][0]["outcome"], APPLY)
        self.assertTrue(report["notes"])


class PoisonTests(GroupingCase):
    """An answer can't rewrite who made a unit, reach into another unit, or invent files."""

    def problems(self, answer):
        verdict = self.verdict(answer)
        self.assertEqual(verdict["outcome"], REFUSE)
        return " ".join(verdict["problems"])

    def test_switching_the_creator_is_refused(self):
        answer = self.answer()
        answer["units"][0]["creator"] = "Someone Else"
        self.assertIn("It changes creator", self.problems(answer))

    def test_repeating_the_creator_unchanged_is_fine(self):
        answer = self.answer()
        answer["units"][0]["creator"] = "Titan Forge"
        verdict = self.verdict(answer)
        self.assertEqual(verdict["outcome"], APPLY)
        self.assertIn("Repeated creator unchanged, which is fine", verdict["warnings"])

    def test_setting_any_other_identity_field_is_refused(self):
        for level, key in (("unit", "designer"), ("unit", "collection_id"), ("model", "path"), ("model", "model_id")):
            with self.subTest(key):
                answer = self.answer()
                target = answer["units"][0] if level == "unit" else answer["units"][0]["models"][0]
                target[key] = "anything"
                self.assertIn(key, self.problems(answer))

    def test_an_id_from_nowhere_is_refused(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["artifacts"].append("a99")
        self.assertIn("a99, which is not in this unit", self.problems(answer))

    def test_an_image_can_not_be_placed_as_a_printable_or_the_reverse(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["artifacts"].append("i1")
        self.assertIn("an image, not a printable", self.problems(answer))
        answer = self.answer()
        answer["units"][0]["models"][0]["image"] = "a3"
        self.assertIn("a printable, not an image", self.problems(answer))

    def test_a_file_placed_twice_or_not_at_all_is_refused(self):
        answer = self.answer()
        unit = answer["units"][0]
        unit["models"][1]["artifacts"].append(unit["models"][0]["artifacts"][0])
        unit["models"][2]["artifacts"].pop()
        problems = self.problems(answer)
        self.assertIn("placed 2 times", problems)
        self.assertIn("missing", problems)

    def test_another_units_answer_is_refused(self):
        answer = self.answer()
        other = self.add_collection("Other", "9/o")
        answer["units"][0]["unit_id"] = grouping_units.archive_key(other)
        self.assertIn("wasn't in export", self.problems(answer))

    def test_an_answer_for_an_export_the_app_never_made_is_refused_whole(self):
        answer = self.answer()
        answer["export_id"] = "e-forged"
        report = self.check(answer)
        self.assertIn("isn't one this app made", report["error"])
        self.assertEqual(report["units"], [])

    def test_a_changed_fingerprint_is_refused(self):
        answer = self.answer()
        answer["units"][0]["fingerprint"] = "sha256:0"
        self.assertIn("fingerprint isn't the one exported", self.problems(answer))

    def test_files_changed_since_the_export_make_it_stale(self):
        answer = self.answer()
        with self.conn:
            self.conn.execute("UPDATE model_artifacts SET size_bytes = 99 WHERE filename = '32mm_Ghul_B.stl'")
        report = self.check(answer)
        verdict = report["units"][0]
        self.assertEqual((verdict["outcome"], verdict["stale"]), (REFUSE, True))
        self.assertIn("Export it again", verdict["summary"])
        self.assertEqual(report["fix_request"], "", "a stale unit is the owner's to re-export, not the LLM's to fix")

    def test_the_same_unit_twice_is_refused_the_second_time(self):
        answer = self.answer()
        answer["units"].append(copy.deepcopy(answer["units"][0]))
        report = self.check(answer)
        self.assertEqual([u["outcome"] for u in report["units"]], [APPLY, REFUSE])

    def test_a_picture_shown_for_two_models_must_say_so(self):
        answer = self.answer()
        models = answer["units"][0]["models"]
        models[1]["image"] = models[0]["image"]
        self.assertIn("Set image_shared to true", self.problems(answer))
        for model in models[:2]:
            model["image_shared"] = True
        self.assertEqual(self.verdict(answer)["outcome"], APPLY)

    def test_a_render_must_be_of_the_models_own_file(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["render_artifact"] = answer["units"][0]["models"][2]["artifacts"][0]
        self.assertIn("isn't one of its own artifacts", self.problems(answer))

    def test_two_models_with_one_name_are_refused(self):
        answer = self.answer()
        answer["units"][0]["models"][1]["name"] = "Ghul A"
        self.assertIn("Two models are both called Ghul A", self.problems(answer))

    def test_a_bad_name_is_reported_once_without_its_files_seeming_missing(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["name"] = "Ghul\u202eA"
        problems = self.problems(answer)
        self.assertIn("\\u202e", problems)
        self.assertNotIn("missing", problems)

    def test_tags_never_reach_an_mmf_unit_and_unknown_ones_only_wait(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["tags"] = ["undead"]
        verdict = self.verdict(answer)
        self.assertIn("this unit takes no tags", " ".join(verdict["warnings"]))

    def test_suggested_tags_are_cleaned_and_markup_dropped(self):
        with self.conn:
            self.conn.execute("UPDATE collections SET source = 'import' WHERE id = ?", (self.collection,))
        answer = self.answer()
        answer["units"][0]["suggested_tags"] = ["Vampire Court", "<script>", "ok\u202e"]
        report = self.check(answer)
        self.assertEqual(report["plans"][self.key]["suggested_tags"], ["vampire-court"])

    def test_an_mmf_unit_suggests_no_tags(self):
        answer = self.answer()
        answer["units"][0]["suggested_tags"] = ["vampire-court"]
        self.assertEqual(self.check(answer)["plans"][self.key]["suggested_tags"], [])

    def test_a_misnamed_field_is_named_with_the_likely_one(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["alternates"] = ["i3"]
        verdict = self.verdict(answer)
        self.assertEqual(verdict["outcome"], APPLY)
        self.assertIn("Unknown field alternates in model Ghul A was ignored. Did you mean image_alternates?", verdict["warnings"])

    def test_a_refused_unit_tells_the_llm_about_ignored_fields_too(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["alternates"] = ["i3"]
        answer["units"][0]["models"][1]["artifacts"] = ["a99"]
        request = self.check(answer)["fix_request"]
        self.assertIn("a99", request)
        self.assertIn("Did you mean image_alternates?", request)

    def test_a_broken_structure_is_explained_per_field(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["confidence"] = "certain"
        self.assertIn("models.0.confidence", self.problems(answer))

    def test_one_bad_unit_never_sinks_the_rest(self):
        answer = self.answer()
        bad = copy.deepcopy(answer["units"][0])
        bad["unit_id"] = "archive:999999"
        answer["units"].insert(0, bad)
        self.assertEqual([u["outcome"] for u in self.check(answer)["units"]], [REFUSE, APPLY])


class SenseTests(GroupingCase):
    """Answers that pass every rule but don't make sense to a person wait for one."""

    def test_files_that_share_nothing_with_their_model_wait(self):
        answer = self.answer()
        models = answer["units"][0]["models"]
        models[0]["artifacts"] += models[2]["artifacts"]
        models[0]["render_artifact"] = None
        del models[2]
        verdict = self.verdict(answer)
        self.assertEqual(verdict["outcome"], SUGGEST)
        self.assertIn("share no name or folder", " ".join(verdict["reasons"]))

    def test_joining_models_that_each_had_their_own_picture_waits(self):
        answer = self.answer()
        unit = answer["units"][0]
        hints = self.exported["current"]["models"]
        pictured = [h for h in hints if h["image"]]
        self.assertGreaterEqual(len(pictured), 2, "the build gave Ghul A and Ghul B their renders")
        ghuls = unit["models"][0]["artifacts"] + unit["models"][1]["artifacts"]
        unit["models"] = [{"name": "Ghuls", "artifacts": ghuls, "confidence": "high"}, unit["models"][2]]
        verdict = self.verdict(answer)
        self.assertEqual(verdict["outcome"], SUGGEST)
        self.assertIn("which each had their own picture", " ".join(verdict["reasons"]))


class FixRequestTests(GroupingCase):
    """What the owner pastes back to the LLM."""

    def test_it_names_each_problem_with_the_unit_and_export(self):
        answer = self.answer()
        answer["units"][0]["models"][0]["artifacts"].append("a99")
        report = self.check(answer)
        request = report["fix_request"]
        self.assertIn(answer["export_id"], request)
        self.assertIn(f"Unit {self.key}", request)
        self.assertIn("a99", request)
        self.assertIn("send just these units again", request)
        # One paragraph to paste into a chat: no break lands mid-sentence.
        self.assertNotIn("\n", request.split("\n\n")[0].rstrip("\n"))

    def test_unreadable_text_gets_a_request_too(self):
        report = self.check("I couldn't do this one, sorry.")
        self.assertTrue(report["error"])
        self.assertIn("one JSON object", report["fix_request"])

    def test_a_clean_answer_needs_none(self):
        self.assertEqual(self.check(self.answer())["fix_request"], "")

    def test_a_checked_answer_is_kept_for_apply(self):
        report = self.check(self.answer())
        import_id = grouping_answer.record(self.conn, report, "answer-01.json")
        row = self.one("SELECT source_name, status, answer_json FROM grouping_imports WHERE id = ?", import_id)
        self.assertEqual((row[0], row[1]), ("answer-01.json", "checked"))
        self.assertIn(self.key, json.loads(row[2]))
