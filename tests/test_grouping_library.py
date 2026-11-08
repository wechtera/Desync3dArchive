"""E54 on a linked library: the preview records each release's images, and a release goes out,
comes back and applies through the same review a person uses. Nothing is lost, and undo is exact."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_library_scan import ScanCase
from test_picture_jobs import png

from desync_archival.services import (
    catalog_review,
    grouping_answer,
    grouping_apply,
    grouping_export,
    grouping_suggestions,
    grouping_units,
    picture_jobs,
)
from desync_archival.services.external_indexer import ScanReadError
from desync_archival.services.grouping_answer import REFUSE


class LibraryCase(ScanCase):
    """March 2022: Ghoul A with its weapons in a folder of their own, Ghoul B, and one render."""

    RELEASE = "March 2022"

    def setUp(self):
        super().setUp()
        for folder, name in (("Ghoul_A", "ghoul_a.stl"), ("Ghoul_A_Weapons", "sword.stl"), ("Ghoul_B", "ghoul_b.stl")):
            self.add_model(self.RELEASE, folder, name)
        self.renders = self.root / self.RELEASE / "Renders"
        self.renders.mkdir()
        (self.renders / "Ghoul_A.png").write_bytes(png())
        (self.renders / "._Ghoul_A.png").write_bytes(b"AppleDouble")
        self.indexer.preview_library(1)
        conn = self.connect()
        self.indexer.approve_candidates(conn, 1, [])
        conn.close()
        self.indexer.add_approved(1)
        self.conn = self.connect()
        self.addCleanup(self.conn.close)
        self.key = grouping_units.library_key(1, self.RELEASE)

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def images(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT rel_path, size_bytes FROM library_images WHERE release_name = ? ORDER BY rel_path", (self.RELEASE,))]

    def answer(self, include_reviewed=False, **unit):
        export_key, _, _ = grouping_export.build(self.conn, mode="single", scope="libraries", unit_key=self.key,
                                                 include_reviewed=include_reviewed)
        exported = grouping_units.build(self.conn, self.key).payload
        short = {p["path"]: p["id"] for p in exported["printables"]}
        body = {
            "unit_id": self.key, "fingerprint": exported["fingerprint"], "confidence": "high",
            "models": [
                {"name": "Ghoul A", "artifacts": [short["Ghoul_A/ghoul_a.stl"], short["Ghoul_A_Weapons/sword.stl"]],
                 "image": "i1", "image_confidence": "high", "confidence": "high"},
                {"name": "Ghoul B", "artifacts": [short["Ghoul_B/ghoul_b.stl"]], "image": None,
                 "render_artifact": short["Ghoul_B/ghoul_b.stl"], "confidence": "high"},
            ],
            "groups": [],
        }
        body.update(unit)
        return {"export_id": export_key, "units": [body]}

    def apply_answer(self, answer):
        report = grouping_answer.check(self.conn, json.dumps(answer))
        import_id = grouping_answer.record(self.conn, report, "answer.json")
        return import_id, report, grouping_apply.apply(self.conn, import_id)

    def holder(self, filename):
        return self.one("SELECT m.name FROM model_artifacts a JOIN models m ON m.id = a.model_id WHERE a.filename = ?", filename)[0]

    def model(self, name):
        return self.one("SELECT id, state, image_path, image_source, image_locked FROM models WHERE name = ?", name)

    def raw_files(self):
        """Each folder's files as the raw list shows them. E54 must never change this."""
        return [tuple(r) for r in self.conn.execute("""
            SELECT f.model_id, f.filename, f.size_bytes, d.vault_path FROM model_files f
            JOIN downloaded_files d ON d.file_id = f.file_id ORDER BY f.file_id
        """)]

    def run_jobs(self, renderer=None):
        renderer = renderer or MagicMock(is_available=MagicMock(return_value=False))
        while picture_jobs.run_one(self.conn, renderer) == picture_jobs.WORKING:
            pass


class ImageRecordTests(LibraryCase):
    def test_the_preview_records_each_image_with_its_size_and_skips_hidden_files(self):
        self.assertEqual(self.images(), [(f"{self.RELEASE}/Renders/Ghoul_A.png", len(png()))])

    def test_the_release_goes_out_with_its_images(self):
        payload = grouping_units.build(self.conn, self.key).payload
        self.assertEqual([(i["id"], i["path"], i["available"]) for i in payload["images"]], [("i1", "Renders/Ghoul_A.png", False)])

    def test_a_new_preview_replaces_the_releases_images(self):
        (self.renders / "Ghoul_A.png").unlink()
        (self.renders / "Ghoul_B.jpg").write_bytes(b"jpeg")
        self.indexer.preview_library(1)
        self.assertEqual(self.images(), [(f"{self.RELEASE}/Renders/Ghoul_B.jpg", 4)])

    def test_a_failed_read_keeps_the_images_it_had(self):
        (self.renders / "Ghoul_A.png").unlink()
        with patch.object(self.indexer, "_read_release", side_effect=ScanReadError("Loot: Host is down")):
            self.assertFalse(self.indexer.preview_library(1)["success"])
        self.assertEqual(len(self.images()), 1)

    def test_a_stopped_preview_keeps_the_images_it_had(self):
        (self.renders / "Ghoul_A.png").unlink()
        self.assertTrue(self.indexer.preview_library(1, cancel_cb=lambda: True)["stopped"])
        self.assertEqual(len(self.images()), 1)


class LibraryApplyTests(LibraryCase):
    def test_the_answer_regroups_the_release_through_its_review(self):
        _, report, _ = self.apply_answer(self.answer())
        self.assertEqual(report["units"][0]["outcome"], grouping_answer.APPLY, report["units"][0])
        self.assertEqual((self.holder("ghoul_a.stl"), self.holder("sword.stl"), self.holder("ghoul_b.stl")),
                         ("Ghoul A", "Ghoul A", "Ghoul B"))
        self.assertEqual(self.model("Ghoul A Weapons")["state"], "merged")
        self.assertEqual(self.one("SELECT status FROM grouping_units WHERE unit_key = ?", self.key)[0], "applied")

    def test_every_raw_file_list_reads_the_same(self):
        before = self.raw_files()
        self.apply_answer(self.answer())
        self.assertEqual(self.raw_files(), before)

    def test_the_chosen_library_image_is_copied_here_and_shown(self):
        self.apply_answer(self.answer())
        kind, source = self.one("SELECT kind, source_json FROM picture_jobs WHERE status = 'pending' AND kind = 'copy'")
        origin = self.root / self.RELEASE / "Renders" / "Ghoul_A.png"
        self.assertEqual((kind, json.loads(source)["origin"], json.loads(source)["source"]), ("copy", str(origin), "llm"))
        self.run_jobs()
        _, _, path, source_kind, locked = self.model("Ghoul A")
        name = f"{hashlib.sha256(str(origin).encode()).hexdigest()[:12]}.png"
        self.assertEqual((path, source_kind, locked), (f"previews/library/{name}", "llm", 0))
        self.assertEqual((self.previews / "library" / name).read_bytes(), png())

    def test_an_offline_library_leaves_the_copy_waiting(self):
        self.apply_answer(self.answer())
        self.root.rename(self.root.with_name("Loot offline"))
        self.run_jobs()
        self.assertEqual(self.one("SELECT status FROM picture_jobs WHERE kind = 'copy'")[0], "pending")
        self.assertIsNone(self.model("Ghoul A")["image_path"])

    def test_undo_puts_the_release_back_exactly(self):
        before = grouping_apply.state(self.conn, self.key)
        files = self.raw_files()
        import_id, _, _ = self.apply_answer(self.answer())
        grouping_apply.undo(self.conn, import_id)
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)
        self.assertEqual(self.raw_files(), files)
        self.assertEqual(self.model("Ghoul A Weapons")["state"], "resolved")
        self.assertEqual({r[0] for r in self.conn.execute("SELECT status FROM picture_jobs")}, {"skipped"})

    def test_switching_the_creator_is_refused_and_changes_nothing(self):
        before = grouping_apply.state(self.conn, self.key)
        _, report, _ = self.apply_answer(self.answer(creator="Someone Else"))
        self.assertEqual(report["units"][0]["outcome"], REFUSE)
        self.assertIn("creator", " ".join(report["units"][0]["problems"]))
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)

    def test_a_release_path_is_matched_exactly_not_as_a_pattern(self):
        # "March_2022" would match "March 2022" under LIKE, where _ is a wildcard.
        self.add_model("March_2022", "Wraith", "wraith.stl")
        self.indexer.preview_library(1)
        conn = self.connect()
        self.indexer.approve_candidates(conn, 1, [])
        conn.close()
        self.indexer.add_approved(1)
        paths = [p["path"] for p in grouping_units.build(self.conn, self.key).payload["printables"]]
        self.assertNotIn("Wraith/wraith.stl", paths)


class PersonReviewTests(LibraryCase):
    """A person's save in the catalog editor marks the release theirs. E54 then only suggests."""

    def edit(self):
        cards = catalog_review.view(self.conn, 1, self.RELEASE)["cards"]
        next(c for c in cards if c["name"] == "Ghoul B")["name"] = "Ghoul B, renamed"
        catalog_review.save(self.conn, 1, self.RELEASE, cards)

    def test_an_editor_save_marks_the_release_reviewed(self):
        self.assertFalse(grouping_units.build(self.conn, self.key).reviewed)
        self.edit()
        self.assertTrue(grouping_units.build(self.conn, self.key).reviewed)
        self.assertTrue(catalog_review.view(self.conn, 1, self.RELEASE)["reviewed"])

    def test_saving_again_keeps_one_mark(self):
        self.edit()
        catalog_review.save(self.conn, 1, self.RELEASE, catalog_review.view(self.conn, 1, self.RELEASE)["cards"])
        self.assertEqual(self.one("SELECT COUNT(*) FROM library_catalog_reviews")[0], 1)

    def test_an_automatic_apply_never_marks_it(self):
        self.apply_answer(self.answer())
        self.assertFalse(grouping_units.build(self.conn, self.key).reviewed)

    def test_a_high_answer_for_a_reviewed_release_waits_for_the_person(self):
        self.edit()
        before = grouping_apply.state(self.conn, self.key)
        self.apply_answer(self.answer(include_reviewed=True))
        self.assertEqual(grouping_apply.state(self.conn, self.key), before)
        self.assertEqual(self.one("SELECT bucket FROM grouping_suggestions WHERE status = 'waiting'")[0], "high")

    def test_undo_leaves_a_release_the_person_saved_since(self):
        import_id, _, _ = self.apply_answer(self.answer())
        cards = catalog_review.view(self.conn, 1, self.RELEASE)["cards"]
        catalog_review.save(self.conn, 1, self.RELEASE, cards)  # Saved as it is: the person agreed.
        after = grouping_apply.state(self.conn, self.key)
        result = grouping_apply.undo(self.conn, import_id)
        self.assertEqual(grouping_apply.state(self.conn, self.key), after)
        self.assertIn(self.key, json.dumps(result))

    def test_the_editor_matches_its_release_exactly(self):
        self.add_model("March_2022", "Wraith", "wraith.stl")
        self.indexer.preview_library(1)
        conn = self.connect()
        self.indexer.approve_candidates(conn, 1, [])
        conn.close()
        self.indexer.add_approved(1)
        files = catalog_review.view(self.conn, 1, "March_2022")["files"]
        self.assertEqual(files, ["March_2022/Wraith/wraith.stl"])


class CoverTests(LibraryCase):
    """E54-9: in a release with no artwork, a library cover was a size-picked render, so the render
    of the artifact the LLM picked replaces it."""

    def without_artwork(self):
        for image in self.renders.iterdir():
            image.unlink()
        self.indexer.preview_library(1)

    def render_job(self):
        return json.loads(self.one("SELECT source_json FROM picture_jobs WHERE kind = 'render'")[0])

    def collection(self, folder):
        return self.one("SELECT id FROM collections WHERE external_key = ?", f"1/{self.RELEASE}/{folder}")[0]

    def test_a_release_with_artwork_keeps_its_covers(self):
        self.apply_answer(self.answer())
        self.assertEqual(self.render_job()["covers"], [])

    def test_a_release_without_artwork_gets_the_chosen_render_as_cover(self):
        self.without_artwork()
        answer = self.answer()
        answer["units"][0]["models"][0].update(image=None, image_confidence="low")
        self.apply_answer(answer)
        self.assertEqual(self.render_job()["covers"], [self.collection("ghoulb")])
        renderer = MagicMock()
        renderer.is_available.return_value = True
        renderer.render_primary_thumbnail.side_effect = lambda mesh, out: Path(out).write_bytes(png(9, 9)) or True
        self.run_jobs(renderer)
        self.assertEqual(Path(renderer.render_primary_thumbnail.call_args.args[0]).name, "ghoul_b.stl")
        self.assertEqual((self.previews / str(self.collection("ghoulb")) / "cover.jpg").read_bytes(), png(9, 9))

    def test_only_a_folder_the_model_holds_most_of_is_covered(self):
        self.without_artwork()
        (self.root / self.RELEASE / "Ghoul_B" / "ghoul_b_base.stl").write_bytes(b"solid b\nendsolid b\n")
        (self.root / self.RELEASE / "Ghoul_B" / "ghoul_b_arm.stl").write_bytes(b"solid c\nendsolid c\n")
        self.indexer.rescan_known(1)
        base = self.one("SELECT id FROM model_artifacts WHERE filename = 'ghoul_b_base.stl'")[0]
        body = self.one("SELECT id FROM model_artifacts WHERE filename = 'ghoul_b.stl'")[0]
        arm = self.one("SELECT id FROM model_artifacts WHERE filename = 'ghoul_b_arm.stl'")[0]
        self.assertEqual(grouping_apply._covers(self.conn, self.key, [base]), [])
        self.assertEqual(grouping_apply._covers(self.conn, self.key, [body, arm]), [self.collection("ghoulb")])


class LibrarySuggestionTests(LibraryCase):
    def suggest(self):
        self.apply_answer(self.answer(confidence="medium", reason="Not sure the weapons are Ghoul A's"))
        return self.one("SELECT id FROM grouping_suggestions WHERE status = 'waiting'")[0]

    def test_it_opens_in_the_release_review_with_the_llms_cards(self):
        view = grouping_suggestions.view(self.conn, self.suggest())
        self.assertEqual([c["name"] for c in view["cards"]], ["Ghoul A", "Ghoul B"])
        self.assertEqual(view["suggestion"]["images"][0]["name"], "Ghoul_A.png")
        # Library images sit on the NAS, so the picker names them rather than showing them.
        self.assertEqual(view["suggestion"]["images"][0]["url"], "")

    def test_accepting_with_a_library_picture_fetches_it_and_locks_it(self):
        suggestion = self.suggest()
        cards = grouping_suggestions.view(self.conn, suggestion)["cards"]
        result = grouping_suggestions.accept(self.conn, suggestion, cards, {cards[0]["paths"][0]: "i1"})
        self.assertEqual(result["pictures_waiting"], 1)
        self.assertTrue(grouping_units.build(self.conn, self.key).reviewed)
        self.run_jobs()
        _, _, path, source, locked = self.model("Ghoul A")
        self.assertTrue(path.startswith("previews/library/"))
        self.assertEqual((source, locked), ("owner", 1))
