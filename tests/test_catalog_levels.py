import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database

from desync_archival.services import catalog_levels
from desync_archival.services.archive_peeker import ArchivePeeker


class CatalogLevelTests(unittest.TestCase):
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


class BuildTests(CatalogLevelTests):
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
