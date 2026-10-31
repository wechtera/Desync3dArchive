"""The cutover tool: stages work on copies, verification catches damage, and rollback is safe."""

import importlib.util
import json
import shutil
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database

from desync_archival import backup, migrations

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cutover.py"
spec = importlib.util.spec_from_file_location("cutover", SCRIPT)
assert spec and spec.loader
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)


def seed(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM models")
    conn.execute("INSERT INTO models (id, name, designer_name, source, external_key, raw_backup_path) VALUES (1, 'A', 'D', 'mmf', '1', '')")
    conn.execute("INSERT INTO models (id, name, designer_name, source, external_key, raw_backup_path) VALUES (2, 'B', 'D', 'mmf', '2', '')")
    conn.execute("INSERT INTO model_files (file_id, model_id, source, external_key, filename) VALUES (5, 1, 'mmf', '5', 'a.zip')")
    conn.execute(
        "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, actual_bytes)"
        " VALUES (5, 1, 'a', 'a', 'a', 1)"
    )
    conn.commit()


class CutoverCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.db = self.base / "library.db"
        create_database(self.db)
        conn = sqlite3.connect(self.db)
        seed(conn)
        conn.close()

    def tearDown(self):
        self.temp.cleanup()


class PreflightTests(CutoverCase):
    def test_a_clean_database_passes(self):
        result = cutover.preflight(self.db)
        self.assertTrue(result["passed"], result["problems"])
        self.assertEqual(result["row_counts"]["models"], 2)

    def test_broken_references_are_reported(self):
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO model_tags (model_id, tag) VALUES (99, 'orphan')")
        conn.commit()
        conn.close()
        result = cutover.preflight(self.db)
        self.assertFalse(result["passed"])
        self.assertIn("broken references", result["problems"][0])

    def test_the_source_is_never_written(self):
        before = self.db.read_bytes()
        cutover.preflight(self.db)
        self.assertEqual(self.db.read_bytes(), before)


class SampleTests(CutoverCase):
    def test_awkward_rows_come_first(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO stashes (id, name) VALUES (1, 'S')")
        conn.execute("INSERT INTO stash_items (stash_id, item_type, model_id) VALUES (1, 'model', 2)")
        self.assertEqual(cutover.pick_sample(conn, 1)[0], 2)
        conn.close()

    def test_reducing_removes_everything_else(self):
        copy = self.base / "sample.db"
        shutil.copy(self.db, copy)
        with patch.object(cutover, "pick_sample", return_value=[1]):
            cutover.reduce_to_sample(copy, 1)
        conn = sqlite3.connect(copy)
        self.assertEqual(conn.execute("SELECT id FROM models").fetchall(), [(1,)])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.close()


class VerifyTests(CutoverCase):
    def setUp(self):
        super().setUp()
        self.after = self.base / "after.db"
        shutil.copy(self.db, self.after)

    def change(self, sql):
        conn = sqlite3.connect(self.after)
        conn.execute(sql)
        conn.commit()
        conn.close()

    def test_an_exact_copy_passes(self):
        self.assertEqual(cutover.verify(self.db, self.after)["problems"], [])

    def test_a_changed_value_fails(self):
        self.change("UPDATE models SET name = 'Changed' WHERE id = 1")
        self.assertIn("models", cutover.verify(self.db, self.after)["problems"][0])

    def test_a_lost_row_fails(self):
        self.change("DELETE FROM downloaded_files")
        self.assertIn("downloaded_files", cutover.verify(self.db, self.after)["problems"][0])

    def test_a_wrong_external_key_fails(self):
        self.change("UPDATE models SET external_key = '77' WHERE id = 1")
        self.assertTrue(any("external_key" in p for p in cutover.verify(self.db, self.after)["problems"]))

    def test_a_low_sequence_fails(self):
        self.change("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'models'")
        self.assertTrue(any("sequence" in p for p in cutover.verify(self.db, self.after)["problems"]))

    def test_rows_only_skips_the_key_rules(self):
        self.change("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'models'")
        self.assertTrue(cutover.verify(self.db, self.after, rows_only=True)["passed"])


class RewriteTests(CutoverCase):
    def setUp(self):
        super().setUp()
        self.after = self.base / "after.db"
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE models SET raw_backup_path = '/old/data/metadata/model_1.json' WHERE id = 1")
        conn.execute("UPDATE models SET raw_backup_path = 'metadata/model_2.json' WHERE id = 2")
        conn.commit()
        conn.close()
        shutil.copy(self.db, self.after)

    def test_absolute_paths_under_the_old_folder_become_relative(self):
        self.assertEqual(cutover.rewrite_paths(self.after, Path("/old/data")), 1)
        conn = sqlite3.connect(self.after)
        rows = conn.execute("SELECT raw_backup_path FROM models ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(rows, [("metadata/model_1.json",), ("metadata/model_2.json",)])

    def test_paths_elsewhere_are_left_alone(self):
        self.assertEqual(cutover.rewrite_paths(self.after, Path("/other")), 0)

    def test_the_rewrite_command_reports_its_count(self):
        result = cutover.rewrite_stored_paths(self.after, Path("/old/data"))
        self.assertEqual(result["paths_rewritten"], 1)
        self.assertTrue(result["passed"])

    def test_the_rewrite_command_refuses_a_missing_database(self):
        with self.assertRaises(SystemExit):
            cutover.rewrite_stored_paths(self.base / "missing.db", Path("/old/data"))

    def test_the_rewrite_command_refuses_production_paths_outside_production(self):
        from desync_archival import settings
        with patch.object(settings, "is_protected", return_value=True), self.assertRaises(settings.ProductionPathError):
            cutover.rewrite_stored_paths(self.after, Path("/old/data"))

    def test_isolating_points_real_storage_inside_the_stage(self):
        conn = sqlite3.connect(self.after)
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('download_root_path', '/Volumes/STL/Vault')")
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('metadata_backup_path', 'metadata')")
        conn.execute("DELETE FROM external_libraries")
        conn.execute("INSERT INTO external_libraries (name, path) VALUES ('Loot', '/Volumes/Loot')")
        conn.commit()
        conn.close()

        stage = self.base / "stage"
        changed = cutover.isolate_paths(self.after, stage)

        self.assertEqual(changed["download_root_path"], "/Volumes/STL/Vault")
        self.assertIn("/Volumes/Loot", changed.values())
        self.assertNotIn("metadata_backup_path", changed)
        conn = sqlite3.connect(self.after)
        stored = dict(conn.execute("SELECT key, value FROM app_settings"))
        library = conn.execute("SELECT path FROM external_libraries").fetchone()[0]
        conn.close()
        self.assertEqual(stored["metadata_backup_path"], "metadata")
        self.assertTrue(Path(stored["download_root_path"]).is_relative_to(stage))
        self.assertTrue(Path(library).is_relative_to(stage))

    def test_verify_allows_only_the_expected_rewrite(self):
        cutover.rewrite_paths(self.after, Path("/old/data"))
        self.assertTrue(cutover.verify(self.db, self.after, old_root=Path("/old/data"))["passed"])
        self.assertFalse(cutover.verify(self.db, self.after)["passed"])


class RollbackTests(CutoverCase):
    def setUp(self):
        super().setUp()
        self.data = self.base / "data"
        self.data.mkdir()
        self.live = self.data / "library.db"
        self.saved = self.base / "library-backup.db"
        shutil.copy(self.db, self.saved)
        shutil.copy(self.db, self.live)
        conn = sqlite3.connect(self.live)
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.execute("INSERT INTO models (name, source, external_key, raw_backup_path) VALUES ('New', 'mmf', '9', '')")
        self.new_id = cursor.lastrowid
        file_id = conn.execute("INSERT INTO model_files (model_id, source, external_key, filename) VALUES (?, 'mmf', '9', 'n.zip')", (self.new_id,)).lastrowid
        conn.execute(
            "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, actual_bytes,"
            " vault_path) VALUES (?, ?, 'n.zip', 'n.zip', 'n.zip', 1, '/vault/n.zip')", (file_id, self.new_id),
        )
        conn.commit()
        conn.close()
        with sqlite3.connect(self.live) as conn:
            self.newer_rows = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def models(self):
        conn = sqlite3.connect(self.live)
        count = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        conn.close()
        return count

    def test_a_dry_run_changes_nothing(self):
        result = cutover.rollback(self.data, self.saved, self.free_port(), confirm=False)
        self.assertTrue(result["passed"])
        self.assertEqual(self.models(), self.newer_rows)
        self.assertEqual(sorted(p.name for p in self.data.iterdir() if p.name.startswith("rolled")), [])

    def test_it_refuses_while_the_app_is_running(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            result = cutover.rollback(self.data, self.saved, sock.getsockname()[1], confirm=True)
        self.assertFalse(result["passed"])
        self.assertEqual(self.models(), self.newer_rows)

    def test_it_restores_and_keeps_the_rolled_back_database(self):
        result = cutover.rollback(self.data, self.saved, self.free_port(), confirm=True)
        self.assertTrue(result["passed"])
        self.assertEqual(self.models(), 2)
        self.assertTrue((Path(result["rolled_back_copy"]) / "library.db").is_file())
        self.assertTrue(cutover.verify(self.saved, self.live, rows_only=True)["passed"])

    def test_it_names_what_the_restore_dropped(self):
        result = cutover.rollback(self.data, self.saved, self.free_port(), confirm=True)
        self.assertEqual([m["id"] for m in result["dropped"]["models"]], [self.new_id])
        self.assertEqual(result["dropped"]["downloaded_files"][0]["vault_path"], "/vault/n.zip")
        self.assertIn("delete-orphans", result["warning"])
        saved = json.loads((Path(result["rolled_back_copy"]) / "rollback_report.json").read_text())
        self.assertEqual(len(saved["dropped"]["models"]), 1)

    def test_a_damaged_backup_is_refused_before_anything_moves(self):
        bad = self.base / "library-bad.db"
        bad.write_bytes(b"not a database")
        with self.assertRaises((backup.BackupError, sqlite3.DatabaseError)):
            cutover.rollback(self.data, bad, self.free_port(), confirm=True)
        self.assertEqual(self.models(), self.newer_rows)

    def test_a_failed_restore_puts_the_database_back(self):
        with patch.object(backup, "restore", side_effect=backup.BackupError("disk full")), self.assertRaises(backup.BackupError):
            cutover.rollback(self.data, self.saved, self.free_port(), confirm=True)
        self.assertEqual(self.models(), self.newer_rows)
        self.assertEqual([p.name for p in self.data.iterdir() if p.name.startswith("rolled")], [])


class FullRoundTripTests(CutoverCase):
    def test_migrate_then_roll_back_returns_every_row(self):
        old = self.base / "old.db"
        conn = sqlite3.connect(old)
        for number, file in migrations.load_migrations():
            if number <= 5:
                migrations._apply_file(conn, number, file)
        conn.execute("INSERT INTO models (id, name, source, raw_backup_path) VALUES (9, 'A', 'mmf', '')")
        conn.commit()
        conn.close()
        data = self.base / "round"
        data.mkdir()
        live = data / "library.db"
        shutil.copy(old, live)
        result = migrations.migrate_database(live)
        self.assertTrue(cutover.verify(old, live)["passed"])
        cutover.rollback(data, Path(result["backup"]), 1, confirm=True)
        self.assertEqual(migrations.current_version(sqlite3.connect(live)), 5)
        self.assertTrue(cutover.verify(old, live, rows_only=True)["passed"])


if __name__ == "__main__":
    unittest.main()
