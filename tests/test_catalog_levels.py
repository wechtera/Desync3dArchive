import os
import re
import socket
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from db_helper import create_database

from desync_archival import dbtool
from desync_archival import settings as app_config
from desync_archival.api.routes import settings as routes
from desync_archival.migrations import MigrationError
from desync_archival.services import (
    archive_review,
    catalog_browse,
    catalog_levels,
    file_import,
    jobs,
    locations,
    tags,
)
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.library_review import ReviewError
from desync_archival.services.reconciler import VaultReconciler
from desync_archival.services.stash_service import StashService
from desync_archival.services.task_manager import TaskManager


class LevelsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "library.db"
        create_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.conn.close)
        self.collection = self.add_collection("Vampire Court", "1/a")

    def add_collection(self, name, key):
        with self.conn:
            return self.conn.execute(
                "INSERT INTO collections (name, source, external_key, primary_category, raw_backup_path) VALUES (?, 'external', ?, 'Tabletop', '')", (name, key),
            ).lastrowid

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()


class CatalogLevelTests(LevelsCase):
    def test_a_new_collection_gets_a_placeholder_with_its_id(self):
        row = self.one("SELECT id, name, category, state FROM models WHERE id = ?", self.collection)
        self.assertEqual(row, (self.collection, "Vampire Court", "Tabletop", "placeholder"))
        link = self.one("SELECT collection_id FROM model_collections WHERE model_id = ?", self.collection)
        self.assertEqual(link[0], self.collection)

    def test_one_model_can_sit_in_two_collections_once_each(self):
        other = self.add_collection("Ghuls", "1/b")
        with self.conn:
            for _ in range(2):
                self.conn.execute(
                    "INSERT INTO model_collections (model_id, collection_id) VALUES (?, ?)", (self.collection, other),
                )
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_collections WHERE model_id = ?", self.collection)[0], 2)

    def test_a_group_name_is_unique_within_a_collection_only(self):
        other = self.add_collection("Ghuls", "1/b")
        with self.conn:
            self.conn.execute("INSERT INTO model_groups (collection_id, name) VALUES (?, 'Ghul')", (self.collection,))
            self.conn.execute("INSERT INTO model_groups (collection_id, name) VALUES (?, 'Ghul')", (other,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "duplicate model group"), self.conn:
            self.conn.execute("INSERT INTO model_groups (collection_id, name) VALUES (?, 'Ghul')", (other,))

    def test_a_repeated_artifact_is_ignored(self):
        with self.conn:
            file_id = self.conn.execute(
                "INSERT INTO model_files (model_id, filename, size_bytes, download_url, source) VALUES (?, 'a.zip', 1, '', 'external')",
                (self.collection,),
            ).lastrowid
            for _ in range(2):
                self.conn.execute(
                    "INSERT INTO model_artifacts (model_id, file_id, path, filename) VALUES (?, ?, 'x/a.stl', 'a.stl')", (self.collection, file_id),
                )
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_artifacts")[0], 1)

    def test_deleting_a_collection_keeps_a_model_another_collection_holds(self):
        other = self.add_collection("Ghuls", "1/b")
        with self.conn:
            self.conn.execute(
                "INSERT INTO model_collections (model_id, collection_id) VALUES (?, ?)", (self.collection, other),
            )
            self.conn.execute("DELETE FROM collections WHERE id = ?", (other,))
        self.assertEqual(self.one("SELECT COUNT(*) FROM models WHERE id = ?", self.collection)[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_collections WHERE collection_id = ?", other)[0], 0)

    def test_a_placeholder_goes_with_its_collection_and_the_id_can_return(self):
        with self.conn:
            self.conn.execute("DELETE FROM collections WHERE id = ?", (self.collection,))
        self.assertEqual(self.one("SELECT COUNT(*) FROM models")[0], 0)
        with self.conn:
            self.conn.execute(
                "INSERT INTO collections (id, name, source, external_key, raw_backup_path) VALUES (?, 'Back', 'external', '1/a', '')",
                (self.collection,),
            )
        self.assertEqual(self.one("SELECT name FROM models WHERE id = ?", self.collection)[0], "Back")

    def test_replacing_a_row_keeps_one_placeholder(self):
        with self.conn:
            for name in ("Old", "New"):
                self.conn.execute("INSERT OR REPLACE INTO collections (id, name, raw_backup_path) VALUES (50, ?, '')", (name,))
        self.assertEqual(self.one("SELECT COUNT(*) FROM models WHERE id = 50")[0], 1)


class BuildCase(LevelsCase):
    def archive(self, collection_id, entries):
        with self.conn:
            file_id = self.conn.execute(
                "INSERT INTO model_files (model_id, filename, size_bytes, download_url, source) VALUES (?, 'a.zip', 1, '', 'mmf')",
                (collection_id,),
            ).lastrowid
            for path, size in entries:
                self.conn.execute(
                    "INSERT INTO file_archive_members (file_id, internal_path, filename, size_bytes) VALUES (?, ?, ?, ?)",
                    (file_id, path, path.rpartition("/")[2], size),
                )
        return file_id

    def build(self):
        return catalog_levels.build_archive_models(self.conn)

    GHOULS = (
        ("32mm Unsupported/Ghouls/32mm_Ghoul_A.stl", 10), ("32mm Unsupported/Ghouls/32mm_Ghoul_B.stl", 11),
        ("32mm Supported/Ghouls/32mm_Ghoul_A_Supported.stl", 12), ("Bases/Round_25.stl", 13), ("Bases/Round_32.stl", 14),
    )


class BuildTests(BuildCase):
    def test_poses_become_grouped_models_and_the_first_keeps_the_id(self):
        self.archive(self.collection, self.GHOULS)
        self.build()
        names = dict(self.conn.execute(
            "SELECT id, name FROM models WHERE state = 'proposed' ORDER BY id"
        ).fetchall())
        self.assertIn(self.collection, names)
        self.assertIn("Ghoul A", names.values())
        members = self.one("SELECT COUNT(*) FROM model_group_members m JOIN model_groups g ON g.id = m.group_id WHERE g.name = 'Ghoul'")
        self.assertEqual(members[0], 2)

    def test_bases_are_models_in_a_group_with_a_tag(self):
        self.archive(self.collection, self.GHOULS)
        self.build()
        self.assertEqual(self.one("SELECT COUNT(*) FROM models WHERE name IN ('Round 25', 'Round 32')")[0], 2)
        members = self.one(
            "SELECT COUNT(*) FROM model_group_members m JOIN model_groups g ON g.id = m.group_id WHERE g.name = 'Bases'"
        )
        self.assertEqual(members[0], 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_tags WHERE tag = 'base'")[0], 2)

    def test_a_tag_with_spaces_becomes_one_dashed_word(self):
        self.assertEqual(catalog_levels.clean_tag("  Loot   Studios "), "loot-studios")

    def test_size_and_support_are_attributes_of_one_model(self):
        self.archive(self.collection, self.GHOULS)
        self.build()
        rows = self.conn.execute("""
            SELECT a.scale, a.supported FROM model_artifacts a JOIN models m ON m.id = a.model_id
            WHERE m.name = 'Ghoul A' ORDER BY a.supported
        """).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("32mm", 0), ("32mm", 1)])

    def test_a_bundle_and_a_single_share_one_model(self):
        bundle = self.add_collection("Bundle", "1/c")
        single = self.add_collection("Single", "1/d")
        self.archive(bundle, [("Oak/Oak.stl", 50), ("Elm/Elm.stl", 60)])
        self.archive(single, [("Oak/Oak.stl", 50)])
        self.assertEqual(self.build()["shared"], 1)
        model = self.one("SELECT model_id FROM model_artifacts WHERE filename = 'Oak.stl'")[0]
        self.assertEqual(self.one("SELECT COUNT(DISTINCT model_id) FROM model_artifacts WHERE filename = 'Oak.stl'")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_collections WHERE model_id = ?", model)[0], 2)
        self.assertEqual(tuple(self.one("SELECT state, merged_into FROM models WHERE id = ?", single)), ("merged", model))

    def test_every_printable_file_becomes_an_artifact(self):
        bundle = self.add_collection("Bundle", "1/c")
        single = self.add_collection("Single", "1/d")
        self.archive(bundle, [("Oak/Oak.stl", 50), ("Oak/readme.txt", 5)])
        self.archive(single, [("Oak/Oak.stl", 50)])
        self.build()
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_artifacts")[0], 2)
        self.assertEqual(self.one("SELECT COUNT(DISTINCT file_id) FROM model_artifacts")[0], 2)

    def test_a_different_size_is_never_joined(self):
        bundle = self.add_collection("Bundle", "1/c")
        single = self.add_collection("Single", "1/d")
        self.archive(bundle, [("Oak/Oak.stl", 50)])
        self.archive(single, [("Oak/Oak.stl", 51)])
        self.assertEqual(self.build()["shared"], 0)

    def test_old_peek_rows_are_untouched_and_a_rerun_adds_nothing(self):
        self.archive(self.collection, self.GHOULS)
        before = self.one("SELECT COUNT(*) FROM file_archive_members")[0]
        self.build()
        artifacts = self.one("SELECT COUNT(*) FROM model_artifacts")[0]
        self.assertEqual(self.build()["collections"], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_artifacts")[0], artifacts)
        self.assertEqual(self.one("SELECT COUNT(*) FROM file_archive_members")[0], before)

    def search(self, table, text):
        return self.one(f"SELECT COUNT(*) FROM {table} WHERE name LIKE ?", f"%{text}%")[0]

    def test_all_three_levels_are_searchable(self):
        self.archive(self.collection, self.GHOULS)
        self.build()
        self.assertGreaterEqual(self.search("catalog_search_models", "Ghoul A"), 1)
        self.assertEqual(self.search("catalog_search_groups", "Ghoul"), 1)
        self.assertGreaterEqual(self.search("catalog_search_artifacts", "32mm_Ghoul_B"), 1)


class OrphanLinkTests(BuildCase):
    def test_links_to_missing_rows_are_cleared_and_real_ones_stay(self):
        self.archive(self.collection, self.GHOULS)
        self.build()
        keep = self.one("SELECT COUNT(*) FROM model_group_members")[0]
        self.conn.execute("PRAGMA foreign_keys = OFF")
        with self.conn:
            self.conn.execute("INSERT INTO model_group_members (group_id, model_id) VALUES (9999, 1)")
            self.conn.execute("INSERT INTO model_tags (model_id, tag) VALUES (9999, 'x')")
        self.assertEqual(catalog_levels.clean_orphan_links(self.conn), 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_group_members")[0], keep)


class StashSelectionTests(BuildCase):
    def setUp(self):
        super().setUp()
        self.conn.row_factory = sqlite3.Row
        self.archive(self.collection, self.GHOULS)
        self.build()
        with self.conn:
            self.conn.execute("INSERT INTO stashes (name, is_active) VALUES ('s', 1)")
        self.model = catalog_browse.list_models(self.conn, "Ghoul A")["models"][0]["id"]

    def stash(self, **picked):
        with patch.object(StashService, "verify_on_storage"), \
                patch.object(StashService, "_verify_archive_member_downloaded"):
            return catalog_browse.stash_selection(self.conn, self.model, **picked)

    def test_only_the_chosen_support_type_goes_in(self):
        self.assertEqual(self.stash(scale="32mm", support="unsupported")["matched"], 1)

    def test_no_choice_means_every_artifact(self):
        self.assertEqual(self.stash()["matched"], 2)

    def test_a_size_with_nothing_matches_nothing(self):
        self.assertEqual(self.stash(scale="75mm")["added"], 0)

    def test_no_active_stash_is_refused(self):
        with self.conn:
            self.conn.execute("UPDATE stashes SET is_active = 0")
        with self.assertRaisesRegex(ValueError, "active stash"):
            catalog_browse.stash_selection(self.conn, self.model)


class PeekTests(unittest.TestCase):
    """A real peek of a real zip, because the danger is in how it rewrites its rows."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / "library.db"
        self.zip = root / "pack.zip"
        create_database(self.db)
        patcher = patch("desync_archival.services.archive_peeker.app_config.previews_dir", return_value=root / "previews")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.conn.close)
        with self.conn:
            self.collection = self.conn.execute(
                "INSERT INTO collections (name, source, raw_backup_path) VALUES ('Pack', 'mmf', '')"
            ).lastrowid
            self.file_id = self.conn.execute(
                "INSERT INTO model_files (model_id, filename, size_bytes, download_url, source) VALUES (?, 'pack.zip', 1, '', 'mmf')",
                (self.collection,),
            ).lastrowid
            self.conn.execute("INSERT INTO stashes (name) VALUES ('s')")

    def peek(self, names):
        with zipfile.ZipFile(self.zip, "w") as archive:
            for name in names:
                archive.writestr(name, b"x" * (len(name) * 10))
        result = ArchivePeeker(self.db).inspect_and_record(self.collection, self.file_id, self.zip)
        self.assertTrue(result["success"], result)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def artifact(self, filename):
        return self.conn.execute("SELECT id FROM model_artifacts WHERE filename = ?", (filename,)).fetchone()

    def test_a_peek_fills_the_levels_on_its_own(self):
        self.peek(["Ghouls/Ghoul_A.stl", "Ghouls/Ghoul_B.stl", "Bases/Round_25.stl", "readme.txt"])
        self.assertEqual(self.count("model_artifacts"), 3)
        self.assertEqual(self.count("model_groups"), 2)
        self.assertEqual(self.conn.execute("SELECT state FROM models WHERE id = ?", (self.collection,)).fetchone()[0], "proposed")

    def test_a_repeat_peek_keeps_ids_and_adds_nothing(self):
        names = ["Ghouls/Ghoul_A.stl", "Ghouls/Ghoul_B.stl"]
        self.peek(names)
        before = self.conn.execute("SELECT id, archive_member_id FROM model_artifacts ORDER BY id").fetchall()
        models = self.count("models")
        self.peek(names)
        self.assertEqual(before, self.conn.execute("SELECT id, archive_member_id FROM model_artifacts ORDER BY id").fetchall())
        self.assertEqual(self.count("models"), models)

    def test_a_repeat_peek_keeps_stash_items(self):
        names = ["Ghouls/Ghoul_A.stl", "Ghouls/Ghoul_B.stl"]
        self.peek(names)
        member, submodel = self.conn.execute(
            "SELECT id, submodel_id FROM file_archive_members WHERE submodel_id IS NOT NULL LIMIT 1"
        ).fetchone()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stash_items (stash_id, item_type, model_id, file_id, archive_member_id) VALUES (1, 'archive_member', ?, ?, ?)",
                (self.collection, self.file_id, member),
            )
            self.conn.execute(
                "INSERT INTO stash_items (stash_id, item_type, model_id, file_id, submodel_id) VALUES (1, 'submodel', ?, ?, ?)",
                (self.collection, self.file_id, submodel),
            )
        self.peek(names)
        self.assertEqual(self.count("stash_items"), 2)

    def test_a_new_file_in_a_repeat_peek_becomes_an_artifact(self):
        self.peek(["Ghouls/Ghoul_A.stl", "Ghouls/Ghoul_B.stl"])
        self.peek(["Ghouls/Ghoul_A.stl", "Ghouls/Ghoul_B.stl", "Ghouls/Ghoul_C.stl"])
        self.assertIsNotNone(self.artifact("Ghoul_C.stl"))

    def test_a_file_gone_from_the_archive_takes_its_artifact_with_it(self):
        self.peek(["Ghouls/Ghoul_A.stl", "Ghouls/Ghoul_B.stl"])
        self.peek(["Ghouls/Ghoul_A.stl"])
        self.assertIsNone(self.artifact("Ghoul_B.stl"))
        self.assertIsNotNone(self.artifact("Ghoul_A.stl"))

    def test_each_model_gets_the_picture_beside_its_files(self):
        self.peek(["Piotr/Piotr_Body.stl", "Piotr/Piotr.jpg", "Reyna/Reyna_Body.stl", "Reyna/Reyna.jpg"])
        rows = dict(self.conn.execute("SELECT name, image_path FROM models WHERE state = 'proposed'").fetchall())
        self.assertTrue(rows["Piotr"].endswith("/Piotr.jpg"))
        self.assertTrue(rows["Reyna"].endswith("/Reyna.jpg"))

    def test_an_empty_image_is_never_used(self):
        self.peek(["Piotr/Piotr_Body.stl", "Piotr/Piotr.jpg"])
        path = self.conn.execute("SELECT image_path FROM models WHERE state = 'proposed'").fetchone()[0]
        with self.conn:
            self.conn.execute("UPDATE models SET image_path = NULL")
        (Path(self.temp.name) / path).write_bytes(b"")
        catalog_levels.assign_images(self.conn, self.collection)
        self.assertIsNone(self.conn.execute("SELECT image_path FROM models WHERE state = 'proposed'").fetchone()[0])

    def test_a_reviewed_collection_gets_its_picture_when_one_is_repaired(self):
        self.peek(["Piotr/Piotr_Body.stl", "Piotr/Piotr.jpg"])
        path = self.conn.execute("SELECT image_path FROM models WHERE state = 'proposed'").fetchone()[0]
        self.conn.row_factory = sqlite3.Row
        cards = archive_review.view(self.conn, self.collection)["cards"]
        cards[0]["name"] = "Piotr Renamed"
        archive_review.save(self.conn, self.collection, cards, True)
        with self.conn:
            self.conn.execute("UPDATE models SET image_path = NULL")
        self.assertEqual(catalog_levels.rebuild_unreviewed(self.conn)["reset"], 0)
        row = self.conn.execute("SELECT name, image_path FROM models WHERE name = 'Piotr Renamed'").fetchone()
        self.assertEqual(tuple(row), ("Piotr Renamed", path))

    def test_a_peek_records_when_it_ran(self):
        self.peek(["Piotr/Piotr_Body.stl"])
        self.assertIsNotNone(self.conn.execute("SELECT peeked_at FROM model_files WHERE file_id = ?", (self.file_id,)).fetchone()[0])


class ImageExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name)

    def extract(self, tries):
        peeker = ArchivePeeker.__new__(ArchivePeeker)
        peeker._bin_unrar = peeker._bin_7z = "tool"
        commands = [[write] for write in tries]

        def fake_run(cmd, _timeout):
            cmd[0](next(self.dest.glob(".extract_*")))

        with patch("desync_archival.services.archive_peeker.external.member_commands", return_value=commands), \
                patch("desync_archival.services.archive_peeker.external.run", side_effect=fake_run):
            return peeker._extract_images(self.dest / "a.rar", ["x/pic.jpg"], self.dest, "rar")

    def test_an_empty_file_falls_through_to_the_next_tool(self):
        found = self.extract([lambda d: (d / "pic.jpg").write_bytes(b""), lambda d: (d / "pic.jpg").write_bytes(b"real")])
        self.assertEqual(found["x/pic.jpg"].read_bytes(), b"real")

    def test_only_empty_results_extract_nothing(self):
        self.assertEqual(self.extract([lambda d: (d / "pic.jpg").write_bytes(b"")]), {})
        self.assertEqual(list(self.dest.glob(".extract_*")), [])


class CollectionMergeTests(BuildCase):
    PLAIN = (("Pack/Piotr Kvade - Warrior of the Hollow Moon/Piotr_Body.stl", 10), ("Pack/Piotr Kvade - Warrior of the Hollow Moon/Piotr_Base.stl", 11))
    PRE = (
        ("Pack - Presupported/Piotr Kvade - Warrior of the Hollow Moon - Presupported/Piotr_Body_Supported.stl", 20),
        ("Pack - Presupported/Piotr Kvade - Warrior of the Hollow Moon - Presupported/Piotr_Base_Supported.stl", 21),
    )

    def test_two_archives_of_one_collection_make_one_model(self):
        self.archive(self.collection, self.PLAIN)
        self.archive(self.collection, self.PRE)
        self.build()
        self.assertEqual(self.one("SELECT COUNT(*) FROM models WHERE state = 'proposed'")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_artifacts")[0], 4)

    def test_archives_peeked_one_at_a_time_still_make_one_model(self):
        self.archive(self.collection, self.PLAIN)
        self.build()
        self.archive(self.collection, self.PRE)
        self.build()
        self.assertEqual(self.one("SELECT COUNT(*) FROM models WHERE state = 'proposed'")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_artifacts")[0], 4)

    def test_the_model_takes_the_fuller_folder_name(self):
        self.archive(self.collection, self.PLAIN)
        self.build()
        self.assertEqual(self.one("SELECT name FROM models WHERE state = 'proposed'")[0], "Piotr Kvade Warrior of the Hollow Moon")

    def test_a_rebuild_redoes_proposals_and_keeps_reviewed_work(self):
        self.archive(self.collection, self.PLAIN)
        kept = self.add_collection("Kept", "1/k")
        self.archive(kept, [("Oak/Oak.stl", 5)])
        self.build()
        self.conn.row_factory = sqlite3.Row
        cards = archive_review.view(self.conn, kept)["cards"]
        cards[0]["name"] = "Renamed Oak"
        archive_review.save(self.conn, kept, cards, True)
        with self.conn:
            self.conn.execute("UPDATE models SET name = 'Stale' WHERE id = ?", (self.collection,))
        catalog_levels.rebuild_unreviewed(self.conn)
        self.assertEqual(self.one("SELECT name FROM models WHERE id = ?", self.collection)[0], "Piotr Kvade Warrior of the Hollow Moon")
        self.assertEqual(self.one("SELECT COUNT(*) FROM models WHERE name = 'Renamed Oak'")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_artifacts")[0], 3)


class ArchiveReviewTests(BuildCase):
    def setUp(self):
        super().setUp()
        self.conn.row_factory = sqlite3.Row
        self.archive(self.collection, self.GHOULS)
        self.build()

    def view(self):
        return archive_review.view(self.conn, self.collection)

    def card(self, cards, name):
        return next(c for c in cards if c["name"] == name)

    def test_the_view_shows_cards_with_their_groups(self):
        cards = self.view()["cards"]
        self.assertEqual(self.card(cards, "Ghoul A")["group"], "Ghoul")

    def test_a_rename_and_a_new_group_are_saved(self):
        cards = self.view()["cards"]
        self.card(cards, "Ghoul A")["name"] = "Crouching Ghoul"
        self.card(cards, "Crouching Ghoul")["group"] = "Undead"
        archive_review.save(self.conn, self.collection, cards, True)
        self.assertEqual(self.card(self.view()["cards"], "Crouching Ghoul")["group"], "Undead")
        self.assertTrue(self.view()["reviewed"])

    def test_two_cards_can_merge_and_the_old_id_redirects(self):
        cards = self.view()["cards"]
        a, b = self.card(cards, "Ghoul A"), self.card(cards, "Ghoul B")
        a["paths"] += b["paths"]
        archive_review.save(self.conn, self.collection, [c for c in cards if c is not b], True)
        self.assertEqual(len(self.card(self.view()["cards"], "Ghoul A")["paths"]), 3)
        row = self.one("SELECT state, merged_into FROM models WHERE id = ?", int(b["key"]))
        self.assertEqual(tuple(row), ("merged", int(a["key"])))

    def test_a_file_can_split_into_a_new_model(self):
        cards = self.view()["cards"]
        a = self.card(cards, "Ghoul A")
        moved = a["paths"].pop()
        archive_review.save(self.conn, self.collection, [*cards, {"name": "Split", "paths": [moved]}], True)
        self.assertEqual(self.card(self.view()["cards"], "Split")["paths"], [moved])

    def test_a_folder_claim_takes_everything_below_it(self):
        view = self.view()
        folder = next(f for f in view["folders"] if f.endswith("/Bases"))
        cards = [c for c in view["cards"] if not any(m.startswith(folder) for m in c["paths"])]
        archive_review.save(self.conn, self.collection, [*cards, {"name": "All Bases", "paths": [folder]}], True)
        self.assertEqual(len(self.card(self.view()["cards"], "All Bases")["paths"]), 2)

    def test_every_artifact_needs_a_card(self):
        cards = self.view()["cards"]
        with self.assertRaisesRegex(ReviewError, "no card"):
            archive_review.save(self.conn, self.collection, cards[1:], True)

    def test_a_skipped_card_keeps_its_artifacts_searchable(self):
        cards = self.view()["cards"]
        self.card(cards, "Ghoul A")["skip"] = True
        archive_review.save(self.conn, self.collection, cards, True)
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_search_artifacts WHERE name LIKE '%Ghoul_A%'")[0], 2)
        self.assertTrue(self.card(self.view()["cards"], "Ghoul A")["skip"])

    def test_new_contents_reopen_a_reviewed_collection(self):
        archive_review.save(self.conn, self.collection, self.view()["cards"], True)
        self.archive(self.collection, [("Ghouls/Ghoul_C.stl", 77)])
        self.build()
        self.assertFalse(self.view()["reviewed"])

    def test_a_piece_that_became_a_model_is_flagged(self):
        other = self.add_collection("Beast", "1/b")
        self.archive(other, [("Beast/Beast.stl", 5), ("Pieces/Beast LT.stl", 6)])
        self.build()
        cards = archive_review.view(self.conn, other)["cards"]
        flags = {c["name"]: c["flags"] for c in cards}
        self.assertEqual(flags.get("Beast LT"), ["Looks like a part of Beast"])
        self.assertEqual(flags["Beast"], [])

    def test_a_typo_pair_is_flagged_as_in_a_release(self):
        other = self.add_collection("Hags", "1/h")
        self.archive(other, [("Monsters/Green Hag.stl", 5), ("Monsters/Gree Hag.stl", 6)])
        self.build()
        flags = [f for c in archive_review.view(self.conn, other)["cards"] for f in c["flags"]]
        self.assertIn("Very close to Gree Hag, possibly the same thing", flags)

    def test_the_queue_puts_unreviewed_first(self):
        other = self.add_collection("Other", "1/z")
        self.archive(other, [("Oak/Oak.stl", 5)])
        self.build()
        archive_review.save(self.conn, self.collection, self.view()["cards"], True)
        self.assertEqual(archive_review.queue(self.conn)[0]["name"], "Other")


class BrowseTests(BuildCase):
    def setUp(self):
        super().setUp()
        self.conn.row_factory = sqlite3.Row
        self.archive(self.collection, self.GHOULS)
        self.build()

    def names(self, search=""):
        return [m["name"] for m in catalog_browse.list_models(self.conn, search)["models"]]

    def test_models_are_what_gets_listed(self):
        self.assertIn("Ghoul B", self.names())

    def test_a_placeholder_can_still_be_browsed(self):
        self.add_collection("Unpeeked", "1/u")
        self.assertIn("Unpeeked", self.names())

    def test_a_real_model_never_takes_the_next_collection_id(self):
        new = self.add_collection("Next", "1/n")
        self.assertEqual(self.one("SELECT name FROM models WHERE id = ?", new)[0], "Next")
        self.assertGreater(self.one("SELECT MAX(id) FROM models")[0], 10**12)

    def test_search_reaches_a_model_a_group_and_an_artifact(self):
        self.assertEqual(self.names("Ghoul B"), ["Ghoul B"])
        self.assertEqual(sorted(self.names("Bases")), ["Round 25", "Round 32"])
        self.assertEqual(self.names("32mm_Ghoul_B"), ["Ghoul B"])

    def test_a_skipped_or_merged_model_is_hidden_but_its_old_id_works(self):
        cards = archive_review.view(self.conn, self.collection)["cards"]
        a = next(c for c in cards if c["name"] == "Ghoul A")
        b = next(c for c in cards if c["name"] == "Ghoul B")
        a["paths"] += b["paths"]
        archive_review.save(self.conn, self.collection, [c for c in cards if c is not b], True)
        self.assertNotIn("Ghoul B", self.names())
        self.assertEqual(catalog_browse.model_detail(self.conn, int(b["key"]))["id"], int(a["key"]))

    def test_a_model_leads_to_its_group_and_the_group_lists_the_variants(self):
        model = next(m for m in catalog_browse.list_models(self.conn, "Ghoul A")["models"])
        detail = catalog_browse.model_detail(self.conn, model["id"])
        self.assertEqual(detail["groups"][0]["name"], "Ghoul")
        self.assertEqual(len(detail["artifacts"]), 2)
        group = catalog_browse.group_detail(self.conn, detail["groups"][0]["id"])
        self.assertEqual([m["name"] for m in group["models"]], ["Ghoul A", "Ghoul B"])

    def test_a_model_shows_its_creator_and_inherits_collection_tags(self):
        with self.conn:
            self.conn.execute("UPDATE collections SET designer_name = 'Maker', release_month = '2026-10' WHERE id = ?", (self.collection,))
            self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, 'undead')", (self.collection,))
        model = catalog_browse.list_models(self.conn, "Ghoul A")["models"][0]
        detail = catalog_browse.model_detail(self.conn, model["id"])
        self.assertEqual((detail["designer_name"], detail["release_month"]), ("Maker", "2026-10"))
        self.assertIn("undead", detail["tags"])
        self.assertTrue(detail["artifacts"][0]["archive_member_id"])
        self.assertEqual(detail["artifacts"][0]["collection_id"], self.collection)

    def test_a_shared_model_lists_each_artifact_once(self):
        other = self.add_collection("Single", "1/s")
        self.archive(other, [("Ghouls/Unique.stl", 99)])
        self.archive(self.add_collection("Twin", "1/t"), [("Ghouls/Unique.stl", 99)])
        self.build()
        model = catalog_browse.list_models(self.conn, "Unique")["models"][0]
        self.assertEqual(len(catalog_browse.model_detail(self.conn, model["id"])["artifacts"]), 1)

    def test_a_creator_filters_models_and_lists_every_creator(self):
        other = self.add_collection("Other", "1/o")
        with self.conn:
            self.conn.execute("UPDATE collections SET designer_name = 'Maker B' WHERE id = ?", (other,))
            self.conn.execute("UPDATE collections SET designer_name = 'Maker A' WHERE id = ?", (self.collection,))
        everyone = catalog_browse.list_models(self.conn)
        self.assertEqual(everyone["creators"], ["Maker A", "Maker B"])
        narrowed = catalog_browse.list_models(self.conn, creator="Maker A")
        self.assertGreater(narrowed["total"], 0)
        self.assertLess(narrowed["total"], everyone["total"])
        self.assertEqual(catalog_browse.list_models(self.conn, "Ghoul", creator="Maker B")["total"], 0)
        self.assertEqual(catalog_browse.list_models(self.conn, creator="Maker A")["creators"], everyone["creators"])

    def test_a_tag_filters_models_including_inherited_tags(self):
        with self.conn:
            self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, 'undead')", (self.collection,))
        other = self.add_collection("Other", "1/o")
        shown = catalog_browse.list_models(self.conn, tag="undead")
        self.assertGreater(shown["total"], 0)
        self.assertNotIn(other, [m["id"] for m in shown["models"]])
        self.assertEqual(catalog_browse.list_models(self.conn, "Bases", tag="undead")["total"], 2)

    def test_an_inactive_collection_hides_its_only_model(self):
        with self.conn:
            self.conn.execute("UPDATE collections SET is_active = 0 WHERE id = ?", (self.collection,))
        self.assertEqual(self.names(), [])


class BusyGuardTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE pipeline_tasks (id INTEGER PRIMARY KEY, task_type TEXT, status TEXT)")

    def test_idle_means_go(self):
        self.assertIsNone(jobs.busy_reason(self.conn, MagicMock(active_downloads={})))

    def test_a_running_task_is_named(self):
        self.conn.execute("INSERT INTO pipeline_tasks VALUES (7, 'vault_reconcile', 'running')")
        self.assertIn("#7", jobs.busy_reason(self.conn))

    def test_running_downloads_block(self):
        self.assertIn("Downloads", jobs.busy_reason(self.conn, MagicMock(active_downloads={1: {}})))

    def test_a_finished_task_blocks_nothing(self):
        self.conn.execute("INSERT INTO pipeline_tasks VALUES (7, 'vault_reconcile', 'completed')")
        self.assertIsNone(jobs.busy_reason(self.conn))


class RunningAppGuardTests(unittest.TestCase):
    def test_a_listening_port_refuses_and_a_closed_one_passes(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            with patch("desync_archival.settings.host", return_value="127.0.0.1"), \
                    patch("desync_archival.settings.port", return_value=port):
                with self.assertRaisesRegex(MigrationError, "running"):
                    dbtool.refuse_if_app_running()
                server.close()
                dbtool.refuse_if_app_running()


class TaskLogTests(unittest.TestCase):
    def test_a_secret_never_reaches_the_task_log(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "library.db"
        create_database(path)
        manager = TaskManager(path)
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        with conn:
            task = conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('t', 'running')").lastrowid
        manager._append_log(task, 'failed with Authorization: Bearer abcdefghijklmnop1234')
        logged = conn.execute("SELECT log_output FROM pipeline_tasks WHERE id = ?", (task,)).fetchone()[0]
        self.assertNotIn("abcdefghijklmnop1234", logged)
        self.assertIn("failed with", logged)


class ImportTests(BuildCase):
    def setUp(self):
        super().setUp()
        self.conn.row_factory = sqlite3.Row
        root = Path(self.temp.name)
        self.vault = root / "vault"
        self.vault.mkdir()
        self.incoming = root / "incoming"
        self.incoming.mkdir()
        with self.conn:
            self.conn.execute("UPDATE app_settings SET value = ? WHERE key = 'download_root_path'", (str(self.vault),))
            self.conn.execute(
                "INSERT INTO app_settings (key, value) SELECT 'download_root_path', ? WHERE changes() = 0", (str(self.vault),),
            )
        patcher = patch("desync_archival.settings.is_allowed", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, rel, body=b"solid"):
        path = self.incoming / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def bring(self, *paths, **extra):
        return file_import.import_paths(self.conn, self.db, file_import.check_paths([str(p) for p in paths]), **extra)

    def test_a_loose_stl_becomes_a_findable_model(self):
        self.bring(self.write("dragon.stl"), creator="Maker")
        row = self.one("SELECT state, name FROM models WHERE name = 'dragon'")
        self.assertEqual(tuple(row), ("resolved", "dragon"))
        self.assertEqual(self.one("SELECT designer_name FROM collections WHERE source = 'import'")[0], "Maker")
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_search_artifacts WHERE name LIKE '%dragon.stl%'")[0], 1)

    def test_imports_stay_apart_from_creator_folders(self):
        self.bring(self.write("dragon.stl"))
        found = list(self.vault.glob("ad-hoc/*/dragon.stl"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].relative_to(self.vault).parts[0], "ad-hoc")

    def test_several_files_become_separate_collections(self):
        totals = self.bring(self.write("a.stl", b"one"), self.write("b.stl", b"two"))
        self.assertEqual(totals["collections"], 2)

    def test_a_folder_is_one_collection_and_keeps_its_structure(self):
        self.write("Pack/Ghouls/Ghoul_A.stl", b"a")
        self.write("Pack/Ghouls/Ghoul_B.stl", b"b")
        self.write("Pack/.hidden.stl", b"h")
        self.write("Pack/notes.txt", b"n")
        totals = self.bring(self.incoming / "Pack")
        self.assertEqual((totals["collections"], totals["imported"]), (1, 2))
        self.assertTrue(list(self.vault.glob("ad-hoc/Pack-*/Ghouls/Ghoul_A.stl")))
        self.assertEqual(self.one("SELECT COUNT(*) FROM models WHERE name LIKE 'Ghoul %'")[0], 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_groups WHERE name = 'Ghoul'")[0], 1)
        self.assertEqual(catalog_browse.list_models(self.conn, "Ghoul")["total"], 2)

    def test_a_repeat_is_skipped_and_new_files_beside_it_still_land(self):
        self.bring(self.write("dragon.stl", b"same"))
        totals = self.bring(self.write("Mixed/renamed.stl", b"same").parent)
        self.assertEqual((totals["imported"], totals["repeats"]), (0, 1))
        self.write("Mixed/new.stl", b"different")
        totals = self.bring(self.incoming / "Mixed")
        self.assertEqual((totals["imported"], totals["repeats"]), (1, 1))

    def zipped(self, rel, *members):
        path = self.incoming / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for member in members:
                archive.writestr(member, b"solid " + member.encode())
        return path

    def test_a_zip_is_peeked_into_models_and_waits_for_review(self):
        totals = self.bring(self.zipped("Pack.zip", "Ghouls/Ghoul_A.stl", "Ghouls/Ghoul_B.stl"))
        self.assertEqual(totals["archives"], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_artifacts WHERE archive_member_id IS NOT NULL")[0], 2)
        self.assertEqual(catalog_browse.list_models(self.conn, "Ghoul")["total"], 2)
        self.assertEqual([i["name"] for i in archive_review.queue(self.conn)], ["Pack"])

    def test_a_folder_of_raw_files_and_zips_keeps_both(self):
        self.write("Mixed/Dragon.stl", b"raw")
        self.zipped("Mixed/Ghouls.zip", "Ghoul_A.stl", "Ghoul_B.stl")
        totals = self.bring(self.incoming / "Mixed")
        self.assertEqual((totals["collections"], totals["imported"], totals["archives"]), (1, 2, 1))
        collection = self.one("SELECT id FROM collections WHERE source = 'import'")[0]
        shown = {m["name"] for m in catalog_browse.list_models(self.conn)["models"]}
        self.assertTrue({"Dragon", "Ghoul A", "Ghoul B"} <= shown, shown)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM model_artifacts a JOIN model_files f ON f.file_id = a.file_id WHERE f.model_id = ?",
            collection,
        )[0], 3)

    def test_a_cancelled_job_is_marked_cancelled(self):
        self.conn.commit()
        manager = object.__new__(TaskManager)
        manager.db_path = self.db
        manager._lock = threading.Lock()
        manager.active_task_id = 1
        manager._cancel_requested = True
        task_id = self.conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('file_import', 'running')").lastrowid
        self.conn.commit()
        manager._run_file_import_worker(task_id, {"paths": [str(self.write("dragon.stl"))]})
        self.assertEqual(self.one("SELECT status FROM pipeline_tasks WHERE id = ?", task_id)[0], "cancelled")
        self.assertIsNone(manager.active_task_id)

    def test_a_zip_a_raw_file_and_a_folder_picked_together(self):
        self.write("Set/part.stl", b"p")
        totals = self.bring(self.zipped("a.zip", "x.stl"), self.write("b.stl", b"b"), self.incoming / "Set")
        self.assertEqual((totals["collections"], totals["imported"]), (3, 3))

    def test_an_unreadable_zip_is_kept_and_reported(self):
        said = []
        path = self.write("broken.zip", b"not a zip at all")
        totals = self.bring(path, log=said.append)
        self.assertEqual((totals["imported"], totals["archives"]), (1, 0))
        self.assertTrue(any("couldn't be read" in line for line in said))
        self.assertTrue(list(self.vault.glob("ad-hoc/*/broken.zip")))

    def test_progress_moves_during_one_large_file(self):
        seen = []
        path = self.write("big.stl", b"x" * (file_import.CHUNK * 3))
        with patch("desync_archival.services.file_import.time.monotonic", side_effect=range(10, 1000, 5)):
            self.bring(path, progress=lambda pct, label: seen.append((pct, label)))
        self.assertGreaterEqual(len(seen), 4)
        self.assertEqual([p for p, _ in seen], sorted(p for p, _ in seen))
        self.assertTrue(any(label.startswith("Copying") for _, label in seen))

    def test_cancelling_leaves_no_half_file_and_no_row(self):
        checks = iter([False, False, True])
        path = self.write("big.stl", b"x" * (file_import.CHUNK * 2))
        with self.assertRaises(file_import.ImportCancelled):
            self.bring(path, cancelled=lambda: next(checks, True))
        self.assertEqual([p for p in self.vault.rglob("*") if p.is_file()], [])
        self.assertEqual(self.one("SELECT COUNT(*) FROM collections WHERE source = 'import'")[0], 0)

    def test_too_little_space_refuses_before_copying(self):
        with patch("desync_archival.services.file_import.shutil.disk_usage") as usage:
            usage.return_value.free = 1
            with self.assertRaisesRegex(file_import.ImportRefused, "free"):
                self.bring(self.write("dragon.stl"))
        self.assertEqual(list(self.vault.rglob("*.stl")), [])

    def test_the_hash_is_stored(self):
        self.bring(self.write("dragon.stl"))
        self.assertEqual(len(self.one("SELECT sha256 FROM downloaded_files WHERE original_filename = 'dragon.stl'")[0]), 64)

    def test_an_unknown_type_is_refused_before_anything_runs(self):
        with self.assertRaisesRegex(file_import.ImportRefused, "printable"):
            file_import.check_paths([str(self.write("notes.txt"))])

    def test_a_missing_path_is_refused(self):
        with self.assertRaisesRegex(file_import.ImportRefused, "doesn't exist"):
            file_import.check_paths([str(self.incoming / "gone.stl")])

    def test_an_empty_choice_is_refused(self):
        with self.assertRaisesRegex(file_import.ImportRefused, "at least one"):
            file_import.check_paths([])

    def test_an_offline_vault_refuses_and_copies_nothing(self):
        with self.conn:
            self.conn.execute("UPDATE app_settings SET value = '/nowhere/at/all' WHERE key = 'download_root_path'")
        with self.assertRaisesRegex(file_import.ImportRefused, "isn't available"):
            self.bring(self.write("dragon.stl"))

    def test_the_orphan_audit_never_looks_inside_imports(self):
        self.bring(self.write("Pack/123_numbered/part.stl", b"p").parents[1])
        # Skips the constructor: the class is a singleton, and a test database would stick to it.
        reconciler = object.__new__(VaultReconciler)
        with patch.object(VaultReconciler, "_get_db", side_effect=self._reader), \
                patch.object(VaultReconciler, "_save_run_snapshot"):
            result = reconciler._execute_reverse(self.vault, lambda *_: None, lambda *_: None, lambda: False)
        self.assertEqual(result["orphan_directories"], [])

    def test_the_job_imports_and_finishes(self):
        self.conn.commit()
        manager = object.__new__(TaskManager)
        manager.db_path = self.db
        manager._lock = threading.Lock()
        manager.active_task_id = None
        manager._cancel_requested = False
        task_id = self.conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('file_import', 'running')").lastrowid
        self.conn.commit()
        manager._run_file_import_worker(task_id, {"paths": [str(self.write("dragon.stl")), str(self.write("wyrm.stl", b"w"))]})
        self.assertEqual(self.one("SELECT status FROM pipeline_tasks WHERE id = ?", task_id)[0], "completed")
        self.assertEqual(self.one("SELECT COUNT(*) FROM collections WHERE source = 'import'")[0], 2)

    def test_the_job_fails_cleanly_when_the_vault_is_gone(self):
        with self.conn:
            self.conn.execute("UPDATE app_settings SET value = '/nowhere/at/all' WHERE key = 'download_root_path'")
        manager = object.__new__(TaskManager)
        manager.db_path = self.db
        manager._lock = threading.Lock()
        manager.active_task_id = 7
        manager._cancel_requested = False
        task_id = self.conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('file_import', 'running')").lastrowid
        self.conn.commit()
        manager._run_file_import_worker(task_id, {"paths": [str(self.write("dragon.stl"))]})
        self.assertEqual(self.one("SELECT status FROM pipeline_tasks WHERE id = ?", task_id)[0], "failed")
        self.assertIsNone(manager.active_task_id)

    def test_the_route_refuses_a_bad_path_without_starting_a_job(self):
        manager = MagicMock()
        _, status = routes.handle_post_import_file(self.conn, body={"paths": ["/gone/x.stl"]}, task_manager=manager)
        self.assertEqual(status, 400)
        manager.start_task.assert_not_called()

    def test_the_route_reports_a_busy_job_as_a_conflict(self):
        manager = MagicMock()
        manager.start_task.return_value = {"success": False, "error": "busy"}
        _, status = routes.handle_post_import_file(self.conn, body={"paths": [str(self.write("a.stl"))]}, task_manager=manager)
        self.assertEqual(status, 409)
        self.assertEqual(manager.start_task.call_args.args[0], "file_import")

    def test_browse_lists_printable_files_only_when_asked(self):
        self.write("dragon.stl")
        self.write("notes.txt")
        folder = {"path": [str(self.incoming)]}
        plain, _ = routes.handle_get_system_browse(self.conn, params=folder)
        self.assertNotIn("files", plain)
        picked, _ = routes.handle_get_system_browse(self.conn, params={**folder, "files": ["1"]})
        self.assertEqual([f["name"] for f in picked["files"]], ["dragon.stl"])
        self.assertTrue(picked["roots"])

    def _reader(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

class LocationTests(BuildCase):
    def listed(self):
        return {e["name"]: e for e in locations.listing(self.conn)}

    def setUp(self):
        super().setUp()
        self.conn.row_factory = None
        self.vault = Path(self.temp.name) / "vault"
        self.vault.mkdir()
        with self.conn:
            self.conn.execute("DELETE FROM app_settings WHERE key = 'download_root_path'")
            self.conn.execute("INSERT INTO app_settings (key, value) VALUES ('download_root_path', ?)", (str(self.vault),))

    def test_ad_hoc_always_follows_the_vault_root(self):
        entry = self.listed()["Ad-hoc imports"]
        self.assertEqual(entry["path"], str(self.vault / "ad-hoc"))
        self.assertIn("Fixed", entry["change"])

    def test_every_entry_explains_itself(self):
        for entry in self.listed().values():
            self.assertTrue(entry["purpose"] and entry["change"] and entry["group"], entry)

    def test_every_data_folder_is_listed(self):
        shown = {e["path"] for e in self.listed().values()}
        for folder in (app_config.previews_dir, app_config.metadata_dir, app_config.logs_dir, app_config.scrapes_dir):
            self.assertIn(str(folder()), shown)

    def test_a_linked_library_is_listed(self):
        with self.conn:
            self.conn.execute("INSERT INTO external_libraries (name, path, is_active) VALUES ('Loot', '/nas/loot', 1)")
        self.assertEqual(self.listed()["Linked library: Loot"]["path"], "/nas/loot")

    def test_an_offline_location_is_reported_and_not_an_error(self):
        with self.conn:
            self.conn.execute("UPDATE app_settings SET value = '/nowhere/at/all' WHERE key = 'download_root_path'")
        self.assertEqual(self.listed()["Vault"]["state"], "not found")

    def test_an_unset_location_says_so(self):
        with self.conn:
            self.conn.execute("DELETE FROM app_settings WHERE key = 'download_root_path'")
        listed = self.listed()
        self.assertEqual((listed["Vault"]["state"], listed["Ad-hoc imports"]["path"]), ("not set", ""))

    def test_production_reports_its_own_settings_file(self):
        with patch.dict(os.environ, {"APP_ENV": "production", "DESYNC_PRODUCTION_ENV": "/etc/x.env"}):
            self.assertEqual(app_config.settings_file(), Path("/etc/x.env"))
        with patch.dict(os.environ, {"APP_ENV": "development"}):
            self.assertEqual(app_config.settings_file().name, ".env")

    def test_every_server_setting_is_documented(self):
        repo = Path(__file__).resolve().parents[1]
        used = set()
        for path in [*(repo / "src").rglob("*.py"), *(repo / "scripts").glob("*.sh")]:
            used |= set(re.findall(r"\b(?:DESYNC_[A-Z_]+|APP_ENV)\b", path.read_text(encoding="utf-8")))
        used = {name for name in used if not name.endswith("_")}
        for page in ("web/index.html", "README.md"):
            text = (repo / page).read_text(encoding="utf-8")
            self.assertEqual({name for name in used if name not in text}, set(), page)


class AuditCoverageTests(BuildCase):
    def actions(self):
        return [r[0] for r in self.conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]

    def test_tag_changes_are_recorded_and_refusals_are_not(self):
        tags.create(self.conn, "undead")
        routes.handle_post_tags(self.conn, body={"action": "rename", "name": "undead", "new_name": "ghoul"})
        routes.handle_post_tags(self.conn, body={"action": "delete", "name": "ghoul"})
        self.assertEqual(self.actions(), ["tag_rename", "tag_delete"])
        routes.handle_post_tags(self.conn, body={"action": "rename", "name": "nope", "new_name": ""})
        self.assertEqual(len(self.actions()), 2)

    def test_unlinking_a_missing_library_records_nothing(self):
        with patch("desync_archival.services.jobs.busy_reason", return_value=""):
            _, status = routes.handle_delete_external_library(self.conn, 999)
        self.assertEqual((status, self.actions()), (404, []))


class TagTests(BuildCase):
    def setUp(self):
        super().setUp()
        self.conn.row_factory = sqlite3.Row
        self.archive(self.collection, self.GHOULS)
        self.build()
        self.model = catalog_browse.list_models(self.conn, "Ghoul A")["models"][0]["id"]
        with self.conn:
            self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, 'undead')", (self.collection,))

    def tags_of(self):
        return catalog_browse.model_detail(self.conn, self.model)["tags"]

    def test_an_import_registers_and_rolls_into_an_existing_tag(self):
        self.assertEqual(self.one("SELECT COUNT(*) FROM tags WHERE name = 'undead'")[0], 1)
        other = self.add_collection("Other", "1/o")
        with self.conn:
            self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, 'undead')", (other,))
        self.assertEqual(self.one("SELECT COUNT(*) FROM tags WHERE name = 'undead'")[0], 1)

    def test_only_an_existing_tag_can_be_added(self):
        with self.assertRaisesRegex(tags.TagRefused, "isn't a tag"):
            tags.add_to_model(self.conn, self.model, "brand-new")

    def test_a_created_tag_can_be_added_and_is_one_word(self):
        self.assertEqual(tags.create(self.conn, "  Big Boss "), "big-boss")
        tags.add_to_model(self.conn, self.model, "big boss")
        self.assertIn("big-boss", self.tags_of())

    def test_a_removed_tag_stays_removed_after_a_rescan(self):
        tags.remove_from_model(self.conn, self.model, "undead")
        self.assertNotIn("undead", self.tags_of())
        with self.conn:
            self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, 'undead')", (self.collection,))
        self.assertNotIn("undead", self.tags_of())

    def test_adding_a_removed_tag_back_on_purpose_works(self):
        tags.remove_from_model(self.conn, self.model, "undead")
        tags.add_to_model(self.conn, self.model, "undead")
        self.assertIn("undead", self.tags_of())

    def test_a_deleted_tag_never_returns(self):
        tags.delete(self.conn, "undead")
        with self.conn:
            self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, 'undead')", (self.collection,))
        self.assertNotIn("undead", self.tags_of())

    def test_a_rename_merges_and_blocks_the_old_spelling(self):
        tags.create(self.conn, "ghoulish")
        tags.rename(self.conn, "undead", "ghoulish")
        self.assertIn("ghoulish", self.tags_of())
        self.assertNotIn("undead", [t["name"] for t in tags.listing(self.conn)["tags"]])

    def test_a_tag_reaches_every_model_in_a_group(self):
        tags.create(self.conn, "pack")
        group = self.one("SELECT id FROM model_groups WHERE name = 'Ghoul'")[0]
        self.assertEqual(tags.add_to_group(self.conn, group, "pack"), 2)

    def test_dashes_collapse_and_symbols_count_as_junk(self):
        self.assertEqual(catalog_levels.clean_tag("01 - Orconspiracy"), "01-orconspiracy")
        with self.conn:
            self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, '&')", (self.collection,))
            self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, '01---old')", (self.collection,))
        tags.clean_up(self.conn)
        self.assertEqual(self.one("SELECT COUNT(*) FROM collection_tags WHERE tag = '&'")[0], 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM collection_tags WHERE tag = '01-old'")[0], 1)

    def test_clean_up_clears_stale_names_from_the_list(self):
        with self.conn:
            self.conn.execute("INSERT INTO tags (name) VALUES ('01---old')")
        tags.clean_up(self.conn)
        self.assertEqual(self.one("SELECT COUNT(*) FROM tags WHERE name = '01---old'")[0], 0)

    def test_clean_up_removes_junk_and_respells(self):
        with self.conn:
            self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, 'of')", (self.collection,))
            self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, 'loot studios')", (self.collection,))
        result = tags.clean_up(self.conn)
        self.assertEqual(result["junk_removed"], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM collection_tags WHERE tag = 'loot-studios'")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM collection_tags WHERE tag = 'of'")[0], 0)

