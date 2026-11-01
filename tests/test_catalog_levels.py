import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database

from desync_archival.services import archive_review, catalog_browse, catalog_levels
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.library_review import ReviewError


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
                "INSERT INTO models (name, source, external_key, primary_category, raw_backup_path) VALUES (?, 'external', ?, 'Tabletop', '')", (name, key),
            ).lastrowid

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()


class CatalogLevelTests(LevelsCase):
    def test_a_new_collection_gets_a_placeholder_with_its_id(self):
        row = self.one("SELECT id, name, category, state FROM catalog_models WHERE id = ?", self.collection)
        self.assertEqual(row, (self.collection, "Vampire Court", "Tabletop", "placeholder"))
        link = self.one("SELECT collection_id FROM catalog_model_collections WHERE catalog_model_id = ?", self.collection)
        self.assertEqual(link[0], self.collection)

    def test_one_model_can_sit_in_two_collections_once_each(self):
        other = self.add_collection("Ghuls", "1/b")
        with self.conn:
            for _ in range(2):
                self.conn.execute(
                    "INSERT INTO catalog_model_collections (catalog_model_id, collection_id) VALUES (?, ?)", (self.collection, other),
                )
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_model_collections WHERE catalog_model_id = ?", self.collection)[0], 2)

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
                    "INSERT INTO model_artifacts (catalog_model_id, file_id, path, filename) VALUES (?, ?, 'x/a.stl', 'a.stl')", (self.collection, file_id),
                )
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_artifacts")[0], 1)

    def test_deleting_a_collection_keeps_a_model_another_collection_holds(self):
        other = self.add_collection("Ghuls", "1/b")
        with self.conn:
            self.conn.execute(
                "INSERT INTO catalog_model_collections (catalog_model_id, collection_id) VALUES (?, ?)", (self.collection, other),
            )
            self.conn.execute("DELETE FROM models WHERE id = ?", (other,))
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_models WHERE id = ?", self.collection)[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_model_collections WHERE collection_id = ?", other)[0], 0)

    def test_a_placeholder_goes_with_its_collection_and_the_id_can_return(self):
        with self.conn:
            self.conn.execute("DELETE FROM models WHERE id = ?", (self.collection,))
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_models")[0], 0)
        with self.conn:
            self.conn.execute(
                "INSERT INTO models (id, name, source, external_key, raw_backup_path) VALUES (?, 'Back', 'external', '1/a', '')",
                (self.collection,),
            )
        self.assertEqual(self.one("SELECT name FROM catalog_models WHERE id = ?", self.collection)[0], "Back")

    def test_replacing_a_row_keeps_one_placeholder(self):
        with self.conn:
            for name in ("Old", "New"):
                self.conn.execute("INSERT OR REPLACE INTO models (id, name, raw_backup_path) VALUES (50, ?, '')", (name,))
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_models WHERE id = 50")[0], 1)


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
            "SELECT id, name FROM catalog_models WHERE state = 'proposed' ORDER BY id"
        ).fetchall())
        self.assertIn(self.collection, names)
        self.assertIn("Ghoul A", names.values())
        members = self.one("SELECT COUNT(*) FROM model_group_members m JOIN model_groups g ON g.id = m.group_id WHERE g.name = 'Ghoul'")
        self.assertEqual(members[0], 2)

    def test_bases_are_models_in_a_group_with_a_tag(self):
        self.archive(self.collection, self.GHOULS)
        self.build()
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_models WHERE name IN ('Round 25', 'Round 32')")[0], 2)
        members = self.one(
            "SELECT COUNT(*) FROM model_group_members m JOIN model_groups g ON g.id = m.group_id WHERE g.name = 'Bases'"
        )
        self.assertEqual(members[0], 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_model_tags WHERE tag = 'base'")[0], 2)

    def test_a_tag_with_spaces_becomes_one_dashed_word(self):
        self.assertEqual(catalog_levels.clean_tag("  Loot   Studios "), "loot-studios")

    def test_size_and_support_are_attributes_of_one_model(self):
        self.archive(self.collection, self.GHOULS)
        self.build()
        rows = self.conn.execute("""
            SELECT a.scale, a.supported FROM model_artifacts a JOIN catalog_models m ON m.id = a.catalog_model_id
            WHERE m.name = 'Ghoul A' ORDER BY a.supported
        """).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("32mm", 0), ("32mm", 1)])

    def test_a_bundle_and_a_single_share_one_model(self):
        bundle = self.add_collection("Bundle", "1/c")
        single = self.add_collection("Single", "1/d")
        self.archive(bundle, [("Oak/Oak.stl", 50), ("Elm/Elm.stl", 60)])
        self.archive(single, [("Oak/Oak.stl", 50)])
        self.assertEqual(self.build()["shared"], 1)
        model = self.one("SELECT catalog_model_id FROM model_artifacts WHERE filename = 'Oak.stl'")[0]
        self.assertEqual(self.one("SELECT COUNT(DISTINCT catalog_model_id) FROM model_artifacts WHERE filename = 'Oak.stl'")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_model_collections WHERE catalog_model_id = ?", model)[0], 2)
        self.assertEqual(tuple(self.one("SELECT state, merged_into FROM catalog_models WHERE id = ?", single)), ("merged", model))

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
            self.conn.execute("INSERT INTO model_group_members (group_id, catalog_model_id) VALUES (9999, 1)")
            self.conn.execute("INSERT INTO catalog_model_tags (catalog_model_id, tag) VALUES (9999, 'x')")
        self.assertEqual(catalog_levels.clean_orphan_links(self.conn), 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_group_members")[0], keep)


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
                "INSERT INTO models (name, source, raw_backup_path) VALUES ('Pack', 'mmf', '')"
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
        self.assertEqual(self.conn.execute("SELECT state FROM catalog_models WHERE id = ?", (self.collection,)).fetchone()[0], "proposed")

    def test_a_repeat_peek_keeps_ids_and_adds_nothing(self):
        names = ["Ghouls/Ghoul_A.stl", "Ghouls/Ghoul_B.stl"]
        self.peek(names)
        before = self.conn.execute("SELECT id, archive_member_id FROM model_artifacts ORDER BY id").fetchall()
        models = self.count("catalog_models")
        self.peek(names)
        self.assertEqual(before, self.conn.execute("SELECT id, archive_member_id FROM model_artifacts ORDER BY id").fetchall())
        self.assertEqual(self.count("catalog_models"), models)

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
        rows = dict(self.conn.execute("SELECT name, image_path FROM catalog_models WHERE state = 'proposed'").fetchall())
        self.assertTrue(rows["Piotr"].endswith("/Piotr.jpg"))
        self.assertTrue(rows["Reyna"].endswith("/Reyna.jpg"))

    def test_an_empty_image_is_never_used(self):
        self.peek(["Piotr/Piotr_Body.stl", "Piotr/Piotr.jpg"])
        path = self.conn.execute("SELECT image_path FROM catalog_models WHERE state = 'proposed'").fetchone()[0]
        with self.conn:
            self.conn.execute("UPDATE catalog_models SET image_path = NULL")
        (Path(self.temp.name) / path).write_bytes(b"")
        catalog_levels.assign_images(self.conn, self.collection)
        self.assertIsNone(self.conn.execute("SELECT image_path FROM catalog_models WHERE state = 'proposed'").fetchone()[0])

    def test_a_reviewed_collection_gets_its_picture_when_one_is_repaired(self):
        self.peek(["Piotr/Piotr_Body.stl", "Piotr/Piotr.jpg"])
        path = self.conn.execute("SELECT image_path FROM catalog_models WHERE state = 'proposed'").fetchone()[0]
        self.conn.row_factory = sqlite3.Row
        cards = archive_review.view(self.conn, self.collection)["cards"]
        cards[0]["name"] = "Piotr Renamed"
        archive_review.save(self.conn, self.collection, cards, True)
        with self.conn:
            self.conn.execute("UPDATE catalog_models SET image_path = NULL")
        self.assertEqual(catalog_levels.rebuild_unreviewed(self.conn)["reset"], 0)
        row = self.conn.execute("SELECT name, image_path FROM catalog_models WHERE name = 'Piotr Renamed'").fetchone()
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
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_models WHERE state = 'proposed'")[0], 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM model_artifacts")[0], 4)

    def test_the_model_takes_the_fuller_folder_name(self):
        self.archive(self.collection, self.PLAIN)
        self.build()
        self.assertEqual(self.one("SELECT name FROM catalog_models WHERE state = 'proposed'")[0], "Piotr Kvade Warrior of the Hollow Moon")

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
            self.conn.execute("UPDATE catalog_models SET name = 'Stale' WHERE id = ?", (self.collection,))
        catalog_levels.rebuild_unreviewed(self.conn)
        self.assertEqual(self.one("SELECT name FROM catalog_models WHERE id = ?", self.collection)[0], "Piotr Kvade Warrior of the Hollow Moon")
        self.assertEqual(self.one("SELECT COUNT(*) FROM catalog_models WHERE name = 'Renamed Oak'")[0], 1)
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
        row = self.one("SELECT state, merged_into FROM catalog_models WHERE id = ?", int(b["key"]))
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
        self.assertEqual(self.one("SELECT name FROM catalog_models WHERE id = ?", new)[0], "Next")
        self.assertGreater(self.one("SELECT MAX(id) FROM catalog_models")[0], 10**12)

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
            self.conn.execute("UPDATE models SET designer_name = 'Maker', release_month = '2026-10' WHERE id = ?", (self.collection,))
            self.conn.execute("INSERT INTO model_tags (model_id, tag) VALUES (?, 'undead')", (self.collection,))
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

    def test_an_inactive_collection_hides_its_only_model(self):
        with self.conn:
            self.conn.execute("UPDATE models SET is_active = 0 WHERE id = ?", (self.collection,))
        self.assertEqual(self.names(), [])
