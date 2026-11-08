"""dbtool repair-models: B1 to B3 repaired in place, and only where those faults are."""

import io
from contextlib import redirect_stdout
from unittest.mock import patch

from test_catalog_levels import BuildCase

from desync_archival import dbtool
from desync_archival.services import catalog_levels, catalog_repair

FIRST = catalog_levels.FIRST_REAL_ID


class RepairCase(BuildCase):
    def vocabulary(self):
        _, variants, pieces = catalog_levels.vocabulary(self.conn)
        return variants, pieces

    def review(self, collection_id, reviewed=1):
        with self.conn:
            self.conn.execute("INSERT INTO collection_reviews (collection_id, reviewed) VALUES (?, ?)", (collection_id, reviewed))

    def model(self, model_id, name, collection_id, paths, state="resolved", image=None):
        """A model holding archive files, as an older build or a review left it."""
        file_id = self.archive(collection_id, [(p, 100 + i) for i, p in enumerate(paths)])
        with self.conn:
            self.conn.execute("INSERT INTO models (id, name, state, image_path) VALUES (?, ?, ?, ?)", (model_id, name, state, image))
            self.conn.execute("INSERT INTO model_collections (model_id, collection_id) VALUES (?, ?)", (model_id, collection_id))
            self.conn.execute("""
                INSERT INTO model_artifacts (model_id, file_id, archive_member_id, path, filename, size_bytes)
                SELECT ?, file_id, id, internal_path, filename, size_bytes FROM file_archive_members WHERE file_id = ?
            """, (model_id, file_id))
        return model_id

    def row(self, model_id):
        return tuple(self.one("SELECT name, state, merged_into FROM models WHERE id = ?", model_id))

    def artifacts(self, model_id):
        return self.one("SELECT COUNT(*) FROM model_artifacts WHERE model_id = ?", model_id)[0]


class SelfMergedTests(RepairCase):
    """B1, as production had it: Ghuls' placeholder holds the bundle's Ghul A, merged into itself."""

    def setUp(self):
        super().setUp()
        self.bundle = self.add_collection("Vampire Court Bundle", "1/b")
        self.single = self.add_collection("Ghuls - Vampire Court", "1/g")
        with self.conn:
            self.conn.execute("DELETE FROM model_collections WHERE model_id = ?", (self.single,))
            self.conn.execute("DELETE FROM models WHERE id = ?", (self.single,))
        self.model(self.single, "Ghuls - Vampire Court", self.bundle, ["28mm Unsupported/Ghuls/28mm_Ghul_A.stl"], state="merged")
        with self.conn:
            self.conn.execute("UPDATE models SET merged_into = id WHERE id = ?", (self.single,))

    def test_it_is_unmerged_and_named_from_its_files(self):
        self.review(self.bundle)
        variants, pieces = self.vocabulary()
        with self.conn:
            fixed = catalog_repair.fix_self_merged(self.conn, variants, pieces)
        self.assertEqual(fixed, [{"id": self.single, "was": "Ghuls - Vampire Court", "now": "Ghul A"}])
        self.assertEqual(self.row(self.single), ("Ghul A", "resolved", None))

    def test_an_unreviewed_holder_makes_it_a_proposal(self):
        variants, pieces = self.vocabulary()
        with self.conn:
            catalog_repair.fix_self_merged(self.conn, variants, pieces)
        self.assertEqual(self.row(self.single)[1], "proposed")


class SameNameTests(RepairCase):
    """B2: one figure twice in a reviewed collection, once from its Pre-Supported folder."""

    def setUp(self):
        super().setUp()
        self.set = self.add_collection("Insect Invasion", "2/s")
        self.review(self.set)
        self.kept = self.model(FIRST + 1, "Ant A", self.set, ["Ants/Pre-Supported/LYS/LYS_Ant_A_Supported.lys"])
        self.gone = self.model(FIRST + 2, "Ant A", self.set, ["Ants/STL/Ant_A.stl"], image="previews/1/2/ant.jpg")

    def merge(self):
        variants, pieces = self.vocabulary()
        with self.conn:
            return catalog_repair.merge_same_name(self.conn, variants, pieces)

    def test_the_newer_row_merges_into_the_older(self):
        merged, kept_apart = self.merge()
        self.assertEqual(merged, [{"kept": self.kept, "gone": self.gone, "name": "Ant A"}])
        self.assertEqual(kept_apart, [])
        self.assertEqual(self.artifacts(self.kept), 2)
        self.assertEqual(self.artifacts(self.gone), 0)
        self.assertEqual(self.row(self.gone), ("Ant A", "merged", self.kept))

    def test_links_groups_tags_and_a_picture_follow(self):
        single = self.add_collection("Winged Ant Warriors", "2/w")
        with self.conn:
            self.conn.execute("INSERT INTO model_collections (model_id, collection_id) VALUES (?, ?)", (self.gone, single))
            group = self.conn.execute("INSERT INTO model_groups (collection_id, name) VALUES (?, 'Ants')", (self.set,)).lastrowid
            self.conn.execute("INSERT INTO model_group_members (group_id, model_id) VALUES (?, ?)", (group, self.gone))
            self.conn.execute("INSERT INTO model_tags (model_id, tag) VALUES (?, 'insect')", (self.gone,))
        self.merge()
        self.assertEqual({r[0] for r in self.conn.execute("SELECT collection_id FROM model_collections WHERE model_id = ?", (self.kept,))},
                         {self.set, single})
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_collections WHERE model_id = ?", self.gone)[0], 0)
        self.assertEqual(self.one("SELECT model_id FROM model_group_members WHERE group_id = ?", group)[0], self.kept)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT model_id FROM model_tags WHERE tag = 'insect'")], [self.kept])
        self.assertEqual(self.one("SELECT image_path FROM models WHERE id = ?", self.kept)[0], "previews/1/2/ant.jpg")

    def test_an_older_redirect_moves_to_the_kept_row(self):
        with self.conn:
            self.conn.execute("INSERT INTO models (id, name, state, merged_into) VALUES (?, 'Old Ant', 'merged', ?)", (FIRST + 9, self.gone))
        self.merge()
        self.assertEqual(self.row(FIRST + 9)[2], self.kept)

    def test_a_pair_seen_in_a_bundle_and_its_single_merges_once(self):
        single = self.add_collection("Winged Ant Warriors", "2/w")
        with self.conn:
            for model_id in (self.kept, self.gone):
                self.conn.execute("INSERT INTO model_collections (model_id, collection_id) VALUES (?, ?)", (model_id, single))
        merged, _ = self.merge()
        self.assertEqual(len(merged), 1)

    def test_the_same_name_on_different_figures_is_left_alone(self):
        # Blighted Fields' second "Frog Riders Pose 01" holds the Sisters Wraith.
        frogs = self.model(FIRST + 3, "Frog Riders Pose 01", self.set, ["Pose 01/Frog_Rider_Body_Pose01.lys"])
        wraith = self.model(FIRST + 4, "Frog Riders Pose 01", self.set, ["Sister and Sister/Sisters_Wraith_Body_Pose01.lys"])
        merged, kept_apart = self.merge()
        self.assertEqual([m["gone"] for m in merged], [self.gone])
        self.assertEqual(kept_apart, [{"collection": self.set, "name": "frog riders pose 01", "ids": [frogs, wraith]}])
        self.assertIsNone(self.row(wraith)[2])

    def test_bases_alone_still_match_by_their_pieces(self):
        # Snow Effigies: 25mm Base.stl and LYS_25mm Base_Supported.lys hold nothing but a base.
        a = self.model(FIRST + 5, "Snow Effigies", self.set, ["Snow Effigies x5 - 25mm Base/Original STL/25mm Base.stl"])
        b = self.model(FIRST + 6, "Snow Effigies", self.set, ["Snow Effigies x5 - 25mm Base/Presupported/LYS_25mm Base_Supported.lys"])
        merged, _ = self.merge()
        self.assertIn({"kept": a, "gone": b, "name": "Snow Effigies"}, merged)

    def test_a_model_without_archive_files_is_never_merged(self):
        # A linked library's loose model can share a name, and nothing here should move it.
        with self.conn:
            self.conn.execute("INSERT INTO models (id, name, state) VALUES (?, 'Ant A', 'resolved')", (FIRST + 7,))
            self.conn.execute("INSERT INTO model_collections (model_id, collection_id) VALUES (?, ?)", (FIRST + 7, self.set))
        merged, _ = self.merge()
        self.assertNotIn(FIRST + 7, {m["gone"] for m in merged} | {m["kept"] for m in merged})


class SlugGroupTests(RepairCase):
    """B3: a reviewed collection's group named by its folder's slug."""

    def setUp(self):
        super().setUp()
        self.court = self.add_collection("Court Knights", "3/c")
        self.review(self.court)

    def group(self, name, collection_id, models):
        with self.conn:
            group_id = self.conn.execute("INSERT INTO model_groups (collection_id, name) VALUES (?, ?)", (collection_id, name)).lastrowid
            for model_id in models:
                self.conn.execute("INSERT INTO model_group_members (group_id, model_id) VALUES (?, ?)", (group_id, model_id))
        return group_id

    def rename(self):
        variants, _ = self.vocabulary()
        with self.conn:
            return catalog_repair.rename_slug_groups(self.conn, variants)

    def name_of(self, group_id):
        row = self.one("SELECT name FROM model_groups WHERE id = ?", group_id)
        return row[0] if row else None

    def test_a_slug_takes_its_folders_words(self):
        knight = self.model(FIRST + 1, "Knight A", self.court, ["28mm Supported/Court Knights/Knight_A.stl"])
        group = self.group("courtknights", self.court, [knight])
        renamed, _ = self.rename()
        self.assertEqual(renamed, [{"collection": self.court, "was": "courtknights", "now": "Court Knights", "joined": False}])
        self.assertEqual(self.name_of(group), "Court Knights")

    def test_a_kit_slug_takes_the_figure_above_it(self):
        hunter = self.model(FIRST + 1, "Alfar Spear", self.court, ["Frost Alfar Hunter A/Kit I/Forstalfar_Spear_R.stl"])
        group = self.group("kiti", self.court, [hunter])
        self.rename()
        self.assertEqual(self.name_of(group), "Frost Alfar Hunter A")

    def test_a_kit_slug_over_several_figures_is_left_for_a_person(self):
        a = self.model(FIRST + 1, "Spear", self.court, ["Frost Alfar Hunter A/Kit I/Spear.stl"])
        b = self.model(FIRST + 2, "Axe", self.court, ["Frost Alfar Hunter B/Kit I/Axe.stl"])
        group = self.group("kiti", self.court, [a, b])
        _, mixed = self.rename()
        self.assertEqual(mixed, [{"collection": self.court, "name": "kiti", "titles": ["Frost Alfar Hunter A", "Frost Alfar Hunter B"]}])
        self.assertEqual(self.name_of(group), "kiti")

    def test_it_joins_a_group_that_already_has_the_name(self):
        knight = self.model(FIRST + 1, "Knight A", self.court, ["Court Knights/Knight_A.stl"])
        other = self.model(FIRST + 2, "Knight B", self.court, ["Court Knights/Knight_B.stl"])
        named = self.group("Court Knights", self.court, [other])
        slug = self.group("courtknights", self.court, [knight])
        renamed, _ = self.rename()
        self.assertTrue(renamed[0]["joined"])
        self.assertIsNone(self.name_of(slug))
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_group_members WHERE group_id = ?", named)[0], 2)

    def test_names_a_person_could_have_chosen_are_never_touched(self):
        knight = self.model(FIRST + 1, "Knight A", self.court, ["Court Knights/Knight_A.stl"])
        typed = self.group("Knights", self.court, [knight])
        # Lowercase, but from the files: Cast n Play's circle bases.
        base = self.model(FIRST + 2, "Circle A", self.court, ["35mm Base/circle_35mma.stl"])
        lowercase = self.group("circle", self.court, [base])
        self.rename()
        self.assertEqual((self.name_of(typed), self.name_of(lowercase)), ("Knights", "circle"))

    def test_an_unreviewed_collection_is_left_to_the_rebuild(self):
        open_collection = self.add_collection("Other", "3/o")
        knight = self.model(FIRST + 1, "Knight A", open_collection, ["Court Knights/Knight_A.stl"])
        group = self.group("courtknights", open_collection, [knight])
        self.rename()
        self.assertEqual(self.name_of(group), "courtknights")


class RepairCommandTests(RepairCase):
    def setUp(self):
        super().setUp()
        self.set = self.add_collection("Insect Invasion", "2/s")
        self.review(self.set)
        self.model(FIRST + 1, "Ant A", self.set, ["Ants/Pre-Supported/LYS/LYS_Ant_A_Supported.lys"])
        self.model(FIRST + 2, "Ant A", self.set, ["Ants/STL/Ant_A.stl"])

    def run_command(self, *flags, production=False):
        out = io.StringIO()
        with patch("sys.argv", ["dbtool", "repair-models", *flags]), \
                patch("desync_archival.dbtool.settings.db_path", return_value=self.db), \
                patch("desync_archival.dbtool.settings.is_production", return_value=production), \
                patch("desync_archival.dbtool.refuse_if_app_running") as running, \
                patch("desync_archival.dbtool.backup.create_backup", return_value="the-backup") as backup, \
                redirect_stdout(out):
            code = dbtool.main()
        return code, out.getvalue(), running, backup

    def merged(self):
        return self.one("SELECT COUNT(*) FROM models WHERE state = 'merged' AND merged_into IS NOT NULL")[0]

    def test_a_dry_run_reports_and_changes_nothing(self):
        code, out, running, backup = self.run_command()
        self.assertEqual(code, 0)
        self.assertIn("Dry run on a copy. Nothing changed.", out)
        self.assertIn("B2, duplicate models merged: 1", out)
        self.assertEqual(self.merged(), 0)
        running.assert_not_called()
        backup.assert_not_called()

    def test_apply_backs_up_then_repairs(self):
        code, out, running, backup = self.run_command("--apply")
        self.assertEqual(code, 0)
        self.assertIn("backup written: the-backup", out)
        self.assertEqual(self.merged(), 1)
        running.assert_called_once()
        backup.assert_called_once()
        self.assertEqual(self.one("SELECT action FROM audit_log ORDER BY id DESC")[0], "catalog_repair")

    def test_production_needs_its_confirmation(self):
        code, _, _, backup = self.run_command("--apply", production=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.merged(), 0)
        backup.assert_not_called()
        self.assertEqual(self.run_command("--apply", "--confirm-production", production=True)[0], 0)
        self.assertEqual(self.merged(), 1)

    def test_a_second_run_finds_nothing_left(self):
        self.run_command("--apply")
        _, out, _, _ = self.run_command("--apply")
        for line in ("B1, models merged into themselves: 0", "B2, duplicate models merged: 0", "B3, groups renamed: 0"):
            self.assertIn(line, out)
