"""Duplicate decisions, moving a file aside, and the 14 day purge."""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from db_helper import create_database

from desync_archival.api.routes import reconcile as routes
from desync_archival.services import duplicates, pending_deletion
from desync_archival.services.reconciler import VaultReconciler
from desync_archival.services.task_manager import TaskManager


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.db = self.base / "library.db"
        create_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.conn.close)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        with self.conn:
            self.conn.execute("DELETE FROM app_settings WHERE key = 'download_root_path'")
            self.conn.execute("INSERT INTO app_settings (key, value) VALUES ('download_root_path', ?)", (str(self.vault),))

    def collection(self, name, source="mmf"):
        with self.conn:
            return self.conn.execute(
                "INSERT INTO collections (name, source, external_key, raw_backup_path) VALUES (?, ?, ?, '')",
                (name, source, name),
            ).lastrowid

    def stored(self, collection, name, sha="abc", body=b"solid", under=None):
        path = (under or self.vault) / str(collection) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        with self.conn:
            file_id = self.conn.execute(
                "INSERT INTO model_files (model_id, filename, size_bytes) VALUES (?, ?, ?)", (collection, name, len(body)),
            ).lastrowid
            self.conn.execute(
                "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, "
                "actual_bytes, file_extension, vault_path, sha256) VALUES (?, ?, ?, ?, ?, ?, '.stl', ?, ?)",
                (file_id, collection, name, name, name, len(body), str(path), sha),
            )
        return file_id, path

    def twins(self):
        self.a = self.collection("Alpha")
        self.b = self.collection("Beta")
        self.first, self.first_path = self.stored(self.a, "dragon.stl")
        self.second, self.second_path = self.stored(self.b, "dragon_copy.stl")

    def pairs(self):
        return duplicates.find_duplicates(self.conn)["pairs"]

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class PairTests(Case):
    def test_a_pair_shows_both_names_paths_and_sizes(self):
        self.twins()
        pair = self.pairs()[0]
        self.assertEqual(pair["kind"], "exact")
        sides = {pair["first"]["filename"]: pair["first"], pair["second"]["filename"]: pair["second"]}
        self.assertEqual(sides["dragon.stl"]["path"], str(self.first_path))
        self.assertEqual(sides["dragon_copy.stl"]["size_bytes"], 5)
        self.assertEqual(sides["dragon.stl"]["model_name"], "Alpha")

    def test_three_copies_make_three_pairs(self):
        self.twins()
        self.stored(self.collection("Gamma"), "third.stl")
        self.assertEqual(len(self.pairs()), 3)

    def test_two_copies_in_one_collection_are_not_a_pair(self):
        one = self.collection("Alpha")
        self.stored(one, "a.stl")
        self.stored(one, "b.stl")
        self.assertEqual(self.pairs(), [])

    def test_linked_files_cant_be_deleted(self):
        ours = self.collection("Alpha")
        self.stored(ours, "x.stl")
        linked = self.collection("Loot", "external")
        self.stored(linked, "x.stl")
        sides = self.pairs()[0]
        flags = {s["source"]: s["can_delete"] for s in (sides["first"], sides["second"])}
        self.assertEqual(flags, {"mmf": True, "external": False})


class DecisionTests(Case):
    def test_keep_and_ignore_both_stop_the_pair_returning(self):
        for decision in ("keep", "ignore"):
            self.setUp()
            self.twins()
            duplicates.decide(self.conn, self.second, self.first, decision)
            self.assertEqual(self.pairs(), [], decision)
            self.assertEqual(duplicates.find_duplicates(self.conn)["summary"]["exact_groups"], 0)

    def test_only_the_decided_pair_goes(self):
        self.twins()
        third, _ = self.stored(self.collection("Gamma"), "third.stl")
        duplicates.decide(self.conn, self.first, self.second, "keep")
        self.assertEqual(len(self.pairs()), 2)
        self.assertTrue(all(third in (p["first"]["file_id"], p["second"]["file_id"]) for p in self.pairs()))

    def test_deciding_twice_keeps_one_row(self):
        self.twins()
        duplicates.decide(self.conn, self.first, self.second, "keep")
        duplicates.decide(self.conn, self.second, self.first, "keep")
        self.assertEqual(self.count("duplicate_decisions"), 1)

    def test_bad_input_is_refused(self):
        self.twins()
        with self.assertRaises(duplicates.DecisionRefused):
            duplicates.decide(self.conn, self.first, self.second, "maybe")
        with self.assertRaises(duplicates.DecisionRefused):
            duplicates.decide(self.conn, self.first, 999999, "keep")

    def test_decisions_are_audited(self):
        self.twins()
        duplicates.decide(self.conn, self.first, self.second, "ignore")
        self.assertEqual(self.conn.execute("SELECT action FROM audit_log").fetchone()[0], "duplicate_decision")


class SavedResultTests(Case):
    def run_check(self):
        manager = object.__new__(TaskManager)
        manager.db_path = self.db
        reconciler = MagicMock()
        with patch.object(routes, "_get_managers", return_value=(reconciler, manager)):
            routes.handle_get_reconcile_duplicates(self.conn, None, {}, {})
        self.conn.commit()

    def test_results_stay_after_leaving_the_page(self):
        self.twins()
        self.run_check()
        saved = duplicates.latest(self.conn)
        self.assertEqual(len(saved["pairs"]), 1)
        self.assertTrue(saved["task_id"])

    def test_nothing_run_yet(self):
        self.assertEqual((duplicates.latest(self.conn)["pairs"], duplicates.latest(self.conn)["has_results"]), ([], False))

    def test_an_acted_on_pair_drops_out_of_saved_results(self):
        self.twins()
        self.run_check()
        duplicates.decide(self.conn, self.first, self.second, "keep")
        self.assertEqual(duplicates.latest(self.conn)["pairs"], [])

    def test_a_deleted_file_drops_out_of_saved_results(self):
        self.twins()
        self.run_check()
        pending_deletion.delete_file(self.conn, self.first)
        self.assertEqual(duplicates.latest(self.conn)["pairs"], [])


class DeleteTests(Case):
    def test_the_file_moves_aside_and_leaves_the_catalog(self):
        self.twins()
        result = pending_deletion.delete_file(self.conn, self.first)
        moved = Path(result["pending_path"])
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.relative_to(self.vault).parts[0], ".Pending-Deletion")
        self.assertFalse(self.first_path.exists())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM model_files WHERE file_id = ?", (self.first,)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM downloaded_files WHERE file_id = ?", (self.first,)).fetchone())
        self.assertTrue(self.second_path.exists())

    def test_a_tombstone_remembers_everything(self):
        self.twins()
        pending_deletion.delete_file(self.conn, self.first)
        row = self.conn.execute("SELECT * FROM deleted_files").fetchone()
        self.assertEqual((row["filename"], row["original_path"], row["sha256"], row["collection_name"]),
                         ("dragon.stl", str(self.first_path), "abc", "Alpha"))
        self.assertEqual(self.conn.execute("SELECT reason FROM inactive_log").fetchone()[0], "deleted_duplicate")
        self.assertEqual(self.conn.execute("SELECT action FROM audit_log").fetchone()[0], "duplicate_delete")

    def test_an_old_file_gets_a_fresh_date_when_moved(self):
        self.twins()
        os.utime(self.first_path, (time.time() - 900 * 86400,) * 2)
        moved = Path(pending_deletion.delete_file(self.conn, self.first)["pending_path"])
        self.assertLess(time.time() - moved.stat().st_mtime, 60)

    def test_same_names_never_collide(self):
        one, two = self.collection("One"), self.collection("Two")
        first, _ = self.stored(one, "same.stl", "a")
        second, _ = self.stored(two, "same.stl", "b")
        paths = {pending_deletion.delete_file(self.conn, f)["pending_path"] for f in (first, second)}
        self.assertEqual(len(paths), 2)

    def test_a_linked_library_file_never_moves(self):
        linked = self.collection("Loot", "external")
        file_id, path = self.stored(linked, "x.stl")
        with self.assertRaisesRegex(pending_deletion.DeleteRefused, "read-only"):
            pending_deletion.delete_file(self.conn, file_id)
        self.assertTrue(path.exists())
        self.assertEqual(self.count("deleted_files"), 0)

    def test_a_stashed_file_is_refused(self):
        self.twins()
        with self.conn:
            stash = self.conn.execute("INSERT INTO stashes (name) VALUES ('s')").lastrowid
            self.conn.execute(
                "INSERT INTO stash_items (stash_id, item_type, model_id, file_id) VALUES (?, 'file', ?, ?)", (stash, self.a, self.first),
            )
        with self.assertRaises(pending_deletion.DeleteRefused) as caught:
            pending_deletion.delete_file(self.conn, self.first)
        self.assertEqual(caught.exception.code, 409)
        self.assertTrue(self.first_path.exists())

    def test_an_offline_vault_is_refused_and_changes_nothing(self):
        self.twins()
        with self.conn:
            self.conn.execute("UPDATE app_settings SET value = '/nowhere' WHERE key = 'download_root_path'")
        with self.assertRaises(pending_deletion.DeleteRefused) as caught:
            pending_deletion.delete_file(self.conn, self.first)
        self.assertEqual((caught.exception.code, self.count("deleted_files")), (503, 0))

    def test_a_file_outside_the_vault_is_refused(self):
        outside = self.base / "elsewhere"
        file_id, path = self.stored(self.collection("Alpha"), "x.stl", under=outside)
        with self.assertRaisesRegex(pending_deletion.DeleteRefused, "isn't in the vault"):
            pending_deletion.delete_file(self.conn, file_id)
        self.assertTrue(path.exists())

    def test_a_failed_catalog_write_puts_the_file_back(self):
        self.twins()
        with self.conn:
            self.conn.execute("CREATE TRIGGER stop BEFORE DELETE ON model_files BEGIN SELECT RAISE(ABORT, 'no'); END")
        with self.assertRaises(pending_deletion.DeleteRefused):
            pending_deletion.delete_file(self.conn, self.first)
        self.assertTrue(self.first_path.exists())
        self.assertEqual(self.count("deleted_files"), 0)

    def test_the_route_needs_confirm(self):
        self.twins()
        body, _ = routes.handle_post_reconcile_duplicate_delete(self.conn, None, {"file_id": self.first}, {})
        self.assertTrue(body["dry_run"])
        self.assertTrue(self.first_path.exists())

    def test_the_route_waits_for_downloads(self):
        self.twins()
        downloads = MagicMock(active_downloads={1: {}})
        _, status = routes.handle_post_reconcile_duplicate_delete(self.conn, downloads, {"file_id": self.first, "confirm": True}, {})
        self.assertEqual(status, 409)
        self.assertTrue(self.first_path.exists())

    def test_a_not_downloaded_file_is_tombstoned_with_nothing_to_move(self):
        one = self.collection("Alpha")
        with self.conn:
            file_id = self.conn.execute("INSERT INTO model_files (model_id, filename, size_bytes) VALUES (?, 'x.stl', 5)", (one,)).lastrowid
        self.assertEqual(pending_deletion.delete_file(self.conn, file_id)["pending_path"], "")
        self.assertEqual(pending_deletion.summary(self.conn)["count"], 0)

    def test_the_orphan_audit_skips_the_folder(self):
        self.twins()
        pending_deletion.delete_file(self.conn, self.first)
        reconciler = object.__new__(VaultReconciler)
        def reader():
            conn = sqlite3.connect(self.db)
            conn.row_factory = sqlite3.Row
            return conn
        for name in os.listdir(self.vault):
            if not name.startswith("."):
                os.rename(self.vault / name, self.base / name)
        with patch.object(VaultReconciler, "_get_db", side_effect=reader), patch.object(VaultReconciler, "_save_run_snapshot"):
            result = reconciler._execute_reverse(self.vault, lambda *_: None, lambda *_: None, lambda: False)
        self.assertEqual(result["orphan_directories"], [])


class PurgeTests(Case):
    def held(self, days=20):
        self.twins()
        moved = Path(pending_deletion.delete_file(self.conn, self.first)["pending_path"])
        os.utime(moved, (time.time() - days * 86400,) * 2)
        return moved

    def test_the_default_only_lists(self):
        moved = self.held()
        result = pending_deletion.purge(self.conn)
        self.assertEqual((result["dry_run"], len(result["items"])), (True, 1))
        self.assertTrue(moved.exists())

    def test_files_under_14_days_are_left_and_counted(self):
        moved = self.held(13)
        result = pending_deletion.purge(self.conn, confirm=True, ids=[1])
        self.assertEqual((result["items"], result["waiting"], result["purged"]), ([], 1, 0))
        self.assertTrue(moved.exists())

    def test_a_confirmed_purge_removes_only_what_was_listed(self):
        moved = self.held()
        item = pending_deletion.purge(self.conn)["items"][0]
        result = pending_deletion.purge(self.conn, confirm=True, ids=[item["id"]])
        self.assertEqual(result["purged"], 1)
        self.assertFalse(moved.exists())
        self.assertFalse(moved.parent.exists())
        self.assertEqual(pending_deletion.summary(self.conn)["count"], 0)
        self.assertEqual(self.count("deleted_files"), 1)

    def test_an_unlisted_id_is_not_touched(self):
        moved = self.held()
        self.assertEqual(pending_deletion.purge(self.conn, confirm=True, ids=[])["purged"], 0)
        self.assertTrue(moved.exists())

    def test_a_path_outside_the_folder_is_never_removed(self):
        moved = self.held()
        outside = self.base / "keep.stl"
        outside.write_bytes(b"x")
        os.utime(outside, (time.time() - 40 * 86400,) * 2)
        with self.conn:
            self.conn.execute("UPDATE deleted_files SET pending_path = ?", (str(outside),))
        self.assertEqual(pending_deletion.purge(self.conn, confirm=True, ids=[1])["items"], [])
        self.assertTrue(outside.exists() and moved.exists())

    def test_an_offline_vault_purges_nothing(self):
        moved = self.held()
        with self.conn:
            self.conn.execute("UPDATE app_settings SET value = '/nowhere' WHERE key = 'download_root_path'")
        _, status = routes.handle_post_pending_deletion_purge(self.conn, None, {"confirm": True, "ids": [1]}, {})
        self.assertEqual(status, 503)
        self.assertTrue(moved.exists())

    def test_every_purge_is_audited(self):
        self.held()
        pending_deletion.purge(self.conn)
        self.assertEqual(self.conn.execute("SELECT action, dry_run FROM audit_log ORDER BY id DESC").fetchone()[0], "pending_deletion_purge")


if __name__ == "__main__":
    unittest.main()
