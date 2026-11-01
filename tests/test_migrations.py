"""Tests the migration runner, adoption of existing databases, and backup and restore."""

import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desync_archival import backup, migrations, settings
from desync_archival.services.analytics import run_startup_analytics

BASELINE = migrations.load_migrations()[0][1]


def schema_of(conn):
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return [(kind, name, " ".join(sql.split()) if sql else sql) for kind, name, sql in rows]


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.data = self.base / "data"
        self.backups = self.base / "backups"
        self.data.mkdir()
        self.db = self.data / "library.db"
        self.env = patch.dict(os.environ, {
            "DESYNC_DATA_DIR": str(self.data), "DESYNC_BACKUP_DIR": str(self.backups),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def make_version_zero(self):
        """A database like the live one: the baseline schema with user_version 0."""
        conn = sqlite3.connect(self.db)
        conn.executescript(BASELINE.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('worker_concurrency', '2')")
        conn.execute("INSERT INTO stashes (name) VALUES ('Queue')")
        conn.commit()
        conn.close()


class RunnerTests(TempCase):
    def test_migrations_are_numbered_with_no_gaps(self):
        self.assertEqual([v for v, _ in migrations.load_migrations()], list(range(1, migrations.latest_version() + 1)))

    def test_new_database_reaches_latest(self):
        conn = sqlite3.connect(self.db)
        self.assertEqual(migrations.apply_pending(conn), migrations.latest_version())
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        conn.close()

    def test_apply_is_idempotent(self):
        conn = sqlite3.connect(self.db)
        migrations.apply_pending(conn)
        before = schema_of(conn)
        migrations.apply_pending(conn)
        self.assertEqual(schema_of(conn), before)
        conn.close()

    def test_existing_database_is_adopted_with_no_structural_change(self):
        self.make_version_zero()
        conn = sqlite3.connect(self.db)
        before = schema_of(conn)
        migrations.adopt_baseline(conn)
        self.assertEqual(migrations.current_version(conn), 1)
        self.assertEqual(schema_of(conn), before)
        conn.close()

    def test_adopted_and_new_databases_end_up_identical(self):
        self.make_version_zero()
        adopted = sqlite3.connect(self.db)
        migrations.apply_pending(adopted)
        fresh = sqlite3.connect(self.data / "fresh.db")
        migrations.apply_pending(fresh)
        self.assertEqual(schema_of(adopted), schema_of(fresh))
        adopted.close()
        fresh.close()

    def test_partial_database_is_refused(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE models (id INTEGER PRIMARY KEY)")
        with self.assertRaises(migrations.MigrationError):
            migrations.apply_pending(conn)
        self.assertEqual(migrations.current_version(conn), 0)
        conn.close()

    def test_failed_migration_rolls_back(self):
        directory = self.base / "bad_migrations"
        directory.mkdir()
        shutil.copy(BASELINE, directory / BASELINE.name)
        (directory / "0002_bad.sql").write_text("CREATE TABLE half_done (id INTEGER);\nNOT VALID SQL;\n")
        conn = sqlite3.connect(self.db)
        with patch.object(migrations, "MIGRATIONS_DIR", directory), self.assertRaises(sqlite3.Error):
            migrations.apply_pending(conn)
        self.assertEqual(migrations.current_version(conn), 1)
        self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'half_done'").fetchone())
        conn.close()

    def test_badly_named_or_gapped_files_are_refused(self):
        directory = self.base / "gapped"
        directory.mkdir()
        (directory / "0001_a.sql").write_text("SELECT 1;")
        (directory / "0003_b.sql").write_text("SELECT 1;")
        with patch.object(migrations, "MIGRATIONS_DIR", directory), self.assertRaises(migrations.MigrationError):
            migrations.load_migrations()

    def test_require_current_refuses_an_old_database(self):
        self.make_version_zero()
        conn = sqlite3.connect(self.db)
        with self.assertRaises(migrations.MigrationError) as caught:
            migrations.require_current(conn)
        self.assertIn("dbtool migrate", str(caught.exception))
        conn.close()


class MigrateDatabaseTests(TempCase):
    def test_backs_up_first_and_keeps_row_counts(self):
        self.make_version_zero()
        before = backup.row_counts(self.db)
        result = migrations.migrate_database(self.db)
        self.assertEqual((result["from"], result["to"]), (0, migrations.latest_version()))
        self.assertTrue(Path(result["backup"]).is_file())
        after = backup.row_counts(self.db)
        self.assertEqual({table: after[table] for table in before}, before)

    def test_already_current_does_nothing(self):
        self.make_version_zero()
        migrations.migrate_database(self.db)
        self.assertIsNone(migrations.migrate_database(self.db)["backup"])

    def test_production_needs_explicit_confirmation(self):
        self.make_version_zero()
        with patch.dict(os.environ, {"APP_ENV": "production"}):
            with self.assertRaises(migrations.MigrationError):
                migrations.migrate_database(self.db)
            conn = sqlite3.connect(self.db)
            self.assertEqual(migrations.current_version(conn), 0)
            conn.close()
            self.assertEqual(migrations.migrate_database(self.db, confirm_production=True)["to"], migrations.latest_version())

    def test_protected_path_is_refused_outside_production(self):
        with patch.dict(os.environ, {"DESYNC_PROTECTED_PATHS": str(self.data)}), \
                self.assertRaises(settings.ProductionPathError):
            migrations.migrate_database(self.db)

    def test_failed_backup_stops_the_migration(self):
        self.make_version_zero()
        with patch.object(backup, "create_backup", side_effect=backup.BackupError("no")), \
                self.assertRaises(backup.BackupError):
            migrations.migrate_database(self.db)
        conn = sqlite3.connect(self.db)
        self.assertEqual(migrations.current_version(conn), 0)
        conn.close()


class BackupTests(TempCase):
    def setUp(self):
        super().setUp()
        self.make_version_zero()

    def test_backup_is_private_verified_and_named(self):
        path = backup.create_backup(self.db)
        self.assertRegex(path.name, backup.BACKUP_PATTERN)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(backup.row_counts(path), backup.row_counts(self.db))
        self.assertEqual(list(self.backups.glob("*.partial")), [])

    def test_source_database_is_not_changed(self):
        before = self.db.read_bytes()
        backup.create_backup(self.db)
        self.assertEqual(self.db.read_bytes(), before)

    def test_backup_inside_data_directory_is_refused(self):
        with patch.dict(os.environ, {"DESYNC_BACKUP_DIR": str(self.data / "backups")}), \
                self.assertRaises(backup.BackupError):
            backup.create_backup(self.db)

    def test_backup_dir_is_required(self):
        with patch.dict(os.environ, {"DESYNC_BACKUP_DIR": ""}), self.assertRaises(settings.SettingsError):
            backup.create_backup(self.db)

    def test_retention_removes_only_old_backups(self):
        self.backups.mkdir()
        keep_me = self.backups / "notes.txt"
        keep_me.write_text("mine")
        made = [backup.create_backup(self.db, keep=2) for _ in range(4)]
        remaining = sorted(p for p in self.backups.iterdir() if p.suffix == ".db")
        self.assertEqual(remaining, made[-2:])
        self.assertTrue(keep_me.exists())

    def test_row_count_mismatch_fails_and_leaves_no_file(self):
        with patch.object(backup, "verify", side_effect=backup.BackupError("counts")), \
                self.assertRaises(backup.BackupError):
            backup.create_backup(self.db)
        self.assertEqual([p for p in self.backups.iterdir()], [])

    def test_verify_detects_changed_counts(self):
        with self.assertRaises(backup.BackupError):
            backup.verify(self.db, expected={"app_settings": 999})

    def test_restore_matches_and_is_private(self):
        path = backup.create_backup(self.db)
        target = self.base / "restored" / "library.db"
        self.assertEqual(backup.restore(path, target), backup.row_counts(self.db))
        self.assertEqual(backup.row_counts(target), backup.row_counts(self.db))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_a_backup_of_a_wal_database_is_one_file(self):
        migrations.migrate_database(self.db)
        for path in self.backups.iterdir():
            path.unlink()
        made = backup.create_backup(self.db)
        backup.row_counts(made)
        self.assertEqual([p.name for p in self.backups.iterdir()], [made.name])

    def test_restored_database_uses_wal(self):
        migrations.migrate_database(self.db)
        target = self.base / "restored.db"
        backup.restore(backup.create_backup(self.db), target)
        conn = sqlite3.connect(target)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(migrations.current_version(conn), migrations.latest_version())
        conn.close()

    def test_restore_never_overwrites(self):
        path = backup.create_backup(self.db)
        before = self.db.read_bytes()
        with self.assertRaises(backup.BackupError):
            backup.restore(path, self.db)
        self.assertEqual(self.db.read_bytes(), before)

    def test_corrupt_backup_is_refused(self):
        path = backup.create_backup(self.db)
        data = bytearray(path.read_bytes())
        data[0:64] = b"\0" * 64
        path.write_bytes(data)
        with self.assertRaises(sqlite3.DatabaseError):
            backup.restore(path, self.base / "bad.db")
        self.assertFalse((self.base / "bad.db").exists())

    def test_restore_into_a_protected_path_is_refused(self):
        path = backup.create_backup(self.db)
        protected = self.base / "protected"
        with patch.dict(os.environ, {"DESYNC_PROTECTED_PATHS": str(protected)}), \
                self.assertRaises(settings.ProductionPathError):
            backup.restore(path, protected / "library.db")


class SchemaUseTests(TempCase):
    def test_release_rebuild_works_on_the_real_schema(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        migrations.apply_pending(conn)
        conn.execute("""
            INSERT INTO collections (id, name, url, designer_name, designer_id, published_at, raw_backup_path)
            VALUES (1, 'Dragon', '', 'Maker', 5, '2026-05-01', '')
        """)
        conn.commit()
        run_startup_analytics(conn)
        self.assertEqual(conn.execute("SELECT count(*) FROM releases").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
