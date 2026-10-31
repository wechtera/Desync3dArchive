import json
import unittest

from desync_archival.services import library_review
from desync_archival.services.library_review import ReviewError
from tests.test_library_scan import SOLID, ScanCase


class ReviewCase(ScanCase):
    """A release shaped like August 2022: a loose zip beside busts, and a model holding another."""

    def setUp(self):
        super().setUp()
        self.release = "August 2022"
        busts = self.root / self.release / "Busts"
        busts.mkdir(parents=True)
        (busts / "Bust_Bases.zip").write_bytes(b"zip")
        for name in ("Lidda_Bust_NoSupports", "Zobbin_Bust_NoSupports"):
            (busts / name).mkdir()
            (busts / name / "bust.stl").write_bytes(SOLID)
        self.add_model(self.release, "Voxshax")
        mausoleum = self.root / self.release / "Voxshax" / "GraveyardMausoleum"
        mausoleum.mkdir()
        (mausoleum / "tomb.stl").write_bytes(SOLID)
        rings = self.add_model(self.release, "Rings", "ring_a.stl")
        (rings / "ring_b.stl").write_bytes(SOLID)
        self.preview = self.indexer.preview_library(1)

    def view(self):
        conn = self.connect()
        try:
            return library_review.release_view(conn, 1, self.release)
        finally:
            conn.close()

    def save(self, cards, reviewed=True):
        conn = self.connect()
        try:
            return library_review.save(conn, 1, self.release, cards, reviewed)
        finally:
            conn.close()

    def card(self, name):
        return next(c for c in self.view()["cards"] if c["name"] == name)

    def split(self, name, member, new_name):
        """Moves one path out into a new card, the way the modal does."""
        cards = self.view()["cards"]
        for card in cards:
            if card["name"] == name and member in card["members"]:
                card["members"].remove(member)
        cards = [c for c in cards if c["members"]]
        return [*cards, {"name": new_name, "category": "Terrain", "members": [member]}]

    def add_all(self):
        conn = self.connect()
        try:
            self.indexer.approve_candidates(conn, 1, [])
        finally:
            conn.close()
        return self.indexer.add_approved(1)

    def files_of(self, name):
        return sorted(r[0] for r in self.rows(
            "SELECT mf.filename FROM model_files mf JOIN models m ON m.id = mf.model_id WHERE m.name = ?", name
        ))


class WalkTests(ReviewCase):
    def test_a_lone_zip_no_longer_hides_the_folders_below_it(self):
        names = {c["name"] for c in self.view()["cards"]}
        self.assertTrue({"Lidda Bust", "Zobbin Bust"} <= names)

    def test_the_zip_stays_with_a_card_of_its_own(self):
        self.add_all()
        self.assertEqual(self.files_of("Untitled Bust"), ["Bust_Bases.zip"])
        self.assertEqual(self.files_of("Lidda Bust"), ["bust.stl"])

    def test_the_snapshot_reaches_below_a_model(self):
        view = self.view()
        self.assertIn(f"{self.release}/Voxshax/GraveyardMausoleum", view["folders"])
        self.assertIn(f"{self.release}/Voxshax/GraveyardMausoleum/tomb.stl", view["files"])

    def test_a_swallowed_item_is_flagged_and_left_in_place(self):
        voxshax = self.card("Voxshax")
        self.assertIn("GraveyardMausoleum", " ".join(voxshax["flags"]))
        self.assertEqual(voxshax["members"], [f"{self.release}/Voxshax"])


class SavingTests(ReviewCase):
    def test_a_carved_folder_becomes_its_own_model(self):
        member = f"{self.release}/Voxshax/GraveyardMausoleum"
        self.save(self.split("x", member, "Mausoleum"))
        self.add_all()
        self.assertEqual(self.files_of("Mausoleum"), ["tomb.stl"])
        self.assertEqual(self.files_of("Voxshax"), ["body.stl"])

    def test_a_carved_folder_stays_out_on_a_rescan(self):
        self.save(self.split("x", f"{self.release}/Voxshax/GraveyardMausoleum", "Mausoleum"))
        self.add_all()
        self.indexer.rescan_known(1)
        self.assertEqual(self.files_of("Voxshax"), ["body.stl"])

    def test_single_files_can_be_cards(self):
        ring = f"{self.release}/Rings/ring_a.stl"
        self.save(self.split("x", ring, "Ring A"))
        self.add_all()
        self.assertEqual(self.files_of("Ring A"), ["ring_a.stl"])
        self.assertEqual(self.files_of("Rings"), ["ring_b.stl"])
        self.indexer.rescan_known(1)
        self.assertEqual(self.files_of("Ring A"), ["ring_a.stl"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE name = 'Ring A' AND is_active = 1"), 1)

    def test_a_skipped_card_is_not_added_and_its_files_stay_with_nobody(self):
        cards = self.split("x", f"{self.release}/Voxshax/GraveyardMausoleum", "Mausoleum")
        cards[-1]["skip"] = True
        self.save(cards)
        self.add_all()
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE name = 'Mausoleum'"), 0)
        self.assertEqual(self.files_of("Voxshax"), ["body.stl"])

    def test_a_rename_and_category_reach_the_catalog(self):
        cards = self.view()["cards"]
        for card in cards:
            if card["name"] == "Rings":
                card["name"], card["category"] = "Magic Rings", "Props"
        self.save(cards)
        self.add_all()
        self.assertEqual(self.rows("SELECT primary_category FROM models WHERE name = 'Magic Rings'")[0][0], "Props")

    def test_a_member_in_two_cards_is_refused(self):
        cards = self.view()["cards"]
        cards[1]["members"].append(cards[0]["members"][0])
        with self.assertRaisesRegex(ReviewError, "two cards"):
            self.save(cards)

    def test_a_path_outside_the_snapshot_is_refused(self):
        with self.assertRaisesRegex(ReviewError, "snapshot"):
            self.save([{"name": "Escape", "members": ["../../etc"]}])

    def test_a_nameless_card_is_refused(self):
        with self.assertRaisesRegex(ReviewError, "name"):
            self.save([{"name": "  ", "members": [f"{self.release}/Rings"]}])

    def test_a_refused_save_changes_nothing(self):
        before = self.rows("SELECT rel_path, status FROM library_candidates ORDER BY id")
        with self.assertRaises(ReviewError):
            self.save([{"name": "Escape", "members": ["nope"]}])
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in self.rows("SELECT rel_path, status FROM library_candidates ORDER BY id")])

    def test_a_folder_of_a_known_model_cannot_be_claimed(self):
        self.indexer.add_approved(1)
        self.add_all()
        with self.assertRaises(ReviewError):
            self.save([{"name": "Thief", "members": [f"{self.release}/Rings"]}])

    def test_an_approved_card_leaves_the_review_and_its_paths_lock(self):
        conn = self.connect()
        self.indexer.approve_candidates(conn, 1, [])
        conn.close()
        self.assertEqual(self.view()["cards"], [])
        with self.assertRaisesRegex(ReviewError, "already belongs"):
            self.save([{"name": "Thief", "members": [f"{self.release}/Rings"]}])

    def test_reset_returns_the_code_guess(self):
        self.save(self.split("x", f"{self.release}/Voxshax/GraveyardMausoleum", "Mausoleum"))
        conn = self.connect()
        library_review.reset(conn, 1, self.release)
        conn.close()
        names = {c["name"] for c in self.view()["cards"]}
        self.assertNotIn("Mausoleum", names)
        self.assertFalse(self.view()["changed"])


class PreviewAgainTests(ReviewCase):
    def test_a_new_preview_keeps_the_human_cards(self):
        self.save(self.split("x", f"{self.release}/Voxshax/GraveyardMausoleum", "Mausoleum"))
        self.indexer.preview_library(1)
        self.assertEqual(self.card("Mausoleum")["members"], [f"{self.release}/Voxshax/GraveyardMausoleum"])
        self.assertTrue(self.view()["reviewed"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_candidates WHERE name = 'Mausoleum'"), 1)

    def test_a_new_folder_reopens_a_reviewed_release(self):
        self.save(self.view()["cards"])
        self.add_model(self.release, "Newcomer")
        self.indexer.preview_library(1)
        view = self.view()
        self.assertFalse(view["reviewed"])
        self.assertEqual(self.card("Newcomer")["flags"], [library_review.NEW_SINCE_REVIEW])

    def test_a_new_file_in_a_card_folder_joins_quietly(self):
        self.save(self.view()["cards"])
        (self.root / self.release / "Rings" / "ring_c.stl").write_bytes(SOLID)
        self.indexer.preview_library(1)
        self.assertTrue(self.view()["reviewed"])
        self.add_all()
        self.assertIn("ring_c.stl", self.files_of("Rings"))

    def test_a_vanished_member_is_dropped_and_reopens_the_release(self):
        ring = f"{self.release}/Rings/ring_a.stl"
        self.save(self.split("x", ring, "Ring A"))
        (self.root / ring).unlink()
        self.indexer.preview_library(1)
        self.assertNotIn("Ring A", {c["name"] for c in self.view()["cards"]})
        self.assertFalse(self.view()["reviewed"])

    def test_the_report_shows_saved_cards_not_the_guess(self):
        self.save(self.split("x", f"{self.release}/Voxshax/GraveyardMausoleum", "Mausoleum"))
        conn = self.connect()
        report = json.loads(conn.execute("SELECT report_json FROM library_scan_runs ORDER BY id DESC").fetchone()[0])
        report = library_review.with_reviews(conn, 1, report)
        conn.close()
        entry = next(r for r in report["releases"] if r["name"] == self.release)
        self.assertTrue(entry["reviewed"])
        self.assertIn("Mausoleum", [m["name"] for m in entry["new"]])


class ApproveReviewedTests(ReviewCase):
    def ready(self, hold=()):
        conn = self.connect()
        try:
            result = library_review.approve_ready(conn, 1, list(hold))
        finally:
            conn.close()
        self.indexer.add_approved(1)
        return result

    def added(self):
        return {r[0] for r in self.rows("SELECT name FROM library_candidates WHERE status = 'added'")}

    def proposed(self):
        return {r[0] for r in self.rows("SELECT name FROM library_candidates WHERE status = 'proposed'")}

    def test_unflagged_cards_go_and_flagged_ones_wait(self):
        self.ready()
        self.assertIn("Rings", self.added())
        self.assertIn("Voxshax", self.proposed())
        self.assertIn("Untitled Bust", self.proposed())

    def test_a_reviewed_release_goes_whole_even_with_flags(self):
        self.save(self.view()["cards"])
        self.ready()
        self.assertIn("Voxshax", self.added())
        self.assertEqual(self.proposed(), set())

    def test_a_held_release_stays_proposed_not_skipped(self):
        result = self.ready([self.release])
        self.assertEqual(result["approved"], self.count(
            "SELECT COUNT(*) FROM library_candidates WHERE status = 'added' AND release_name != ?", self.release
        ))
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_candidates WHERE status = 'skipped'"), 0)
        self.assertIn("Rings", self.proposed())

    def test_what_waits_can_still_be_reviewed_and_added_later(self):
        self.ready()
        view = self.view()
        self.assertNotIn("Rings", [c["name"] for c in view["cards"]])
        self.assertEqual(len(view["settled"]), self.count(
            "SELECT COUNT(*) FROM library_candidates WHERE status = 'added' AND release_name = ?", self.release
        ))
        cards = view["cards"]
        next(c for c in cards if c["name"] == "Voxshax")["name"] = "Voxshax Reviewed"
        self.save(cards)
        self.ready()
        self.assertIn("Voxshax Reviewed", self.added())
        self.assertIn("Rings", self.added())
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE name = 'Rings'"), 1)

    def test_the_report_shows_only_what_is_left(self):
        self.ready()
        conn = self.connect()
        report = json.loads(conn.execute("SELECT report_json FROM library_scan_runs WHERE mode = 'preview' ORDER BY id DESC").fetchone()[0])
        report = library_review.with_reviews(conn, 1, report)
        conn.close()
        entry = next(r for r in report["releases"] if r["name"] == self.release)
        self.assertNotIn("Rings", [m["name"] for m in entry["new"]])
        self.assertGreater(entry["added"], 0)
        self.assertEqual(report["totals"]["ready"], 0)

    def test_a_new_preview_keeps_what_waits(self):
        self.ready()
        self.indexer.preview_library(1)
        self.assertIn("Voxshax", self.proposed())
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE name = 'Rings'"), 1)


class LevelsTests(ReviewCase):
    def test_a_bust_joins_its_character_and_a_card_group_reaches_the_catalog(self):
        self.add_model(self.release, "Lidda_32mm")
        self.indexer.preview_library(1)
        cards = self.view()["cards"]
        self.assertEqual(next(c for c in cards if c["name"] == "Lidda")["group"], "Lidda")
        self.assertEqual(next(c for c in cards if c["name"] == "Lidda Bust")["group"], "Lidda")
        self.add_all()
        self.assertEqual(self.count("""
            SELECT COUNT(*) FROM model_group_members m JOIN model_groups g ON g.id = m.group_id
            WHERE g.name = 'Lidda' AND g.release_name = ?
        """, self.release), 2)

    def test_a_loose_file_becomes_a_findable_artifact_and_follows_a_rescan(self):
        self.add_all()
        self.assertEqual(self.count("SELECT COUNT(*) FROM catalog_search_artifacts WHERE name LIKE '%ring_a%'"), 1)
        self.assertEqual(self.rows("SELECT state FROM catalog_models m JOIN models x ON x.id = m.id WHERE x.name = 'Rings'")[0][0], "resolved")
        (self.root / self.release / "Rings" / "ring_a.stl").unlink()
        self.indexer.rescan_known(1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM model_artifacts WHERE filename = 'ring_a.stl'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM model_artifacts WHERE filename = 'ring_b.stl'"), 1)


class CarveTests(unittest.TestCase):
    def test_the_deepest_claim_wins(self):
        carved = library_review.carve({"a": ["x"], "b": ["x/y"], "c": ["x/y/z"]})
        self.assertEqual(carved, {"a": ["x/y", "x/y/z"], "b": ["x/y/z"], "c": []})

    def test_a_name_prefix_is_not_a_parent(self):
        self.assertEqual(library_review.carve({"a": ["x"], "b": ["xy"]}), {"a": [], "b": []})
