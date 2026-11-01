"""One class per finding from the review's priority list, after "Fix these first".

Each test fails on the code as reviewed. Class names carry the finding ID, so
`pytest tests/test_priority_items.py -k R09` proves one finding on its own.
E48 has its own file, tests/test_connections.py.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database

from desync_archival import backup, dbtool, external, migrations
from desync_archival import settings as app_config
from desync_archival.services import data_bundle, login, secret_store
from desync_archival.services.stash_exporter import StashExporter

# Stored relative to the data folder or an archive, so a move can't change them.
NOT_MOVED_WITH_A_BUNDLE = {
    ("collections", "raw_backup_path"),
    ("downloaded_files", "local_relative_path"),
    ("file_archive_members", "internal_path"),
    ("file_archive_submodels", "preview_image_path"),
    ("file_archive_submodels", "relative_dir"),
    ("library_candidates", "folders_json"),
    ("library_candidates", "rel_path"),
    ("library_model_folders", "rel_path"),
    ("library_release_trees", "folders_json"),
    ("model_artifacts", "path"),
    ("models", "image_path"),
}


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)


class SecretsR09Tests(TempCase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "secrets.json"
        env = patch.dict(os.environ, {"DESYNC_SECRETS_FILE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)
        secret_store.write_file({"login": {"salt": "s", "hash": "h"}, "settings": {"mmf_access_token": "keep"}})
        self.before = self.path.read_bytes()

    def failing_read(self):
        return patch.object(Path, "read_text", side_effect=OSError(24, "Too many open files"))

    def test_a_failed_read_refuses_to_save(self):
        with self.failing_read(), self.assertRaises(secret_store.SecretsError):
            secret_store.save({"cookies_json": "[]"})
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_a_corrupt_file_refuses_to_save(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(secret_store.SecretsError):
            secret_store.save({"cookies_json": "[]"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_a_file_that_isnt_an_object_refuses_to_save(self):
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaises(secret_store.SecretsError):
            secret_store.save({"cookies_json": "[]"})

    def test_a_failed_read_refuses_a_password_change(self):
        with self.failing_read(), self.assertRaises(secret_store.SecretsError):
            login.set_password("a-long-enough-password")
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_a_missing_file_is_a_normal_first_run(self):
        self.path.unlink()
        secret_store.save({"cookies_json": "[]"})
        self.assertEqual(secret_store.load(), {"cookies_json": "[]"})

    def test_reads_survive_a_failed_read_and_log_it(self):
        with self.failing_read(), self.assertLogs("vault", logging.ERROR) as logs:
            self.assertEqual(secret_store.load(), {})
        self.assertIn("secrets file", logs.output[0])

    def test_a_failure_never_prints_contents(self):
        self.path.write_text('{"mmf_access_token": "hunter2', encoding="utf-8")
        with self.assertRaises(secret_store.SecretsError) as caught:
            secret_store.read_file()
        self.assertNotIn("hunter2", str(caught.exception))

    def test_a_password_change_keeps_everything_else(self):
        login.set_password("a-long-enough-password")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["settings"], {"mmf_access_token": "keep"})
        self.assertNotEqual(data["login"], {"salt": "s", "hash": "h"})

    def test_a_write_is_private_synced_and_leaves_no_temp_file(self):
        with patch("os.fsync", wraps=os.fsync) as fsync:
            secret_store.save({"cookies_json": "[]"})
        fsync.assert_called()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.suffix == ".tmp"), [])

    def test_a_failed_write_leaves_the_file_and_no_temp_file(self):
        with patch("os.fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
            secret_store.save({"cookies_json": "[]"})
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.suffix == ".tmp"), [])

    def test_two_processes_keep_each_others_writes(self):
        # dbtool runs beside the server, and a threading lock can't see across processes.
        script = (
            "import sys\n"
            "from desync_archival.services import secret_store\n"
            "for i in range(40):\n"
            "    secret_store.save({f'{sys.argv[1]}{i}': 'x'})\n"
        )
        env = {**os.environ, "DESYNC_SECRETS_FILE": str(self.path)}
        workers = [subprocess.Popen([sys.executable, "-c", script, name], env=env) for name in ("a", "b", "c")]
        for worker in workers:
            self.assertEqual(worker.wait(timeout=60), 0)
        saved = secret_store.load()
        self.assertEqual(len(saved), 121)

    def test_the_secrets_path_is_still_the_setting(self):
        self.assertEqual(app_config.secrets_file(), self.path.resolve())


class MigrationR08Tests(TempCase):
    def setUp(self):
        super().setUp()
        self.data = self.root / "data"
        self.data.mkdir()
        self.db = self.data / "library.db"
        env = patch.dict(os.environ, {"DESYNC_DATA_DIR": str(self.data), "DESYNC_BACKUP_DIR": str(self.root / "backups")})
        env.start()
        self.addCleanup(env.stop)
        self.steps = migrations.load_migrations()
        conn = sqlite3.connect(self.db)
        conn.executescript(self.steps[0][1].read_text(encoding="utf-8"))
        conn.execute("INSERT INTO stashes (name) VALUES ('Queue')")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

    def with_extra_step(self, sql):
        folder = self.root / "migrations"
        folder.mkdir()
        for _, path in self.steps:
            shutil.copy(path, folder)
        (folder / f"{len(self.steps) + 1:04d}_extra.sql").write_text(sql, encoding="utf-8")
        return patch.object(migrations, "MIGRATIONS_DIR", folder)

    def version(self):
        conn = sqlite3.connect(self.db)
        try:
            return migrations.current_version(conn)
        finally:
            conn.close()

    def stashes(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM stashes").fetchone()[0]
        finally:
            conn.close()

    def leftovers(self):
        return sorted(p.name for p in self.data.iterdir() if p.name != "library.db")

    def test_a_row_count_failure_leaves_the_old_database(self):
        with self.with_extra_step("DELETE FROM stashes;"), self.assertRaises(migrations.MigrationError) as caught:
            migrations.migrate_database(self.db)
        self.assertIn("stashes", str(caught.exception))
        self.assertEqual(self.version(), 1)
        self.assertEqual(self.stashes(), 1)
        self.assertEqual(self.leftovers(), [])

    def test_a_broken_reference_leaves_the_old_database(self):
        # An earlier step leaves foreign keys on, so this one has to turn them off to do the damage.
        sql = (
            f"{migrations.NO_TRANSACTION}\nPRAGMA foreign_keys = OFF;\n"
            "CREATE TABLE child (parent INTEGER REFERENCES stashes(id));\nINSERT INTO child VALUES (999);\n"
        )
        with self.with_extra_step(sql), self.assertRaises(migrations.MigrationError) as caught:
            migrations.migrate_database(self.db)
        self.assertIn("broken references", str(caught.exception))
        self.assertEqual(self.version(), 1)
        self.assertEqual(self.leftovers(), [])

    def test_bad_sql_undoes_the_steps_before_it_too(self):
        with self.with_extra_step("NOT VALID SQL;"), self.assertRaises(sqlite3.Error):
            migrations.migrate_database(self.db)
        self.assertEqual(self.version(), 1)
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_run_can_be_rerun(self):
        with self.with_extra_step("DELETE FROM stashes;"), self.assertRaises(migrations.MigrationError):
            migrations.migrate_database(self.db)
        result = migrations.migrate_database(self.db)
        self.assertEqual((result["from"], result["to"]), (1, migrations.latest_version()))
        self.assertEqual(self.stashes(), 1)

    def test_success_crosses_the_no_transaction_step_and_ends_in_wal(self):
        self.assertIn(migrations.NO_TRANSACTION, self.steps[1][1].read_text(encoding="utf-8"))
        result = migrations.migrate_database(self.db)
        self.assertEqual(result["to"], migrations.latest_version())
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()
        self.assertEqual(self.leftovers(), [])

    def test_a_database_past_the_wal_step_stays_in_wal(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(self.steps[1][1].read_text(encoding="utf-8"))
        conn.execute("PRAGMA user_version = 2")
        conn.close()
        migrations.migrate_database(self.db)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        conn.close()

    def test_the_file_keeps_its_permissions(self):
        self.db.chmod(0o600)
        migrations.migrate_database(self.db)
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)

    def test_a_database_something_else_has_open_is_refused(self):
        # A swap would leave that process writing to the old file, and its writes would vanish.
        holder = sqlite3.connect(self.db)
        holder.execute("PRAGMA journal_mode = WAL")
        holder.execute("INSERT INTO stashes (name) VALUES ('Second')")
        holder.commit()
        with self.assertRaises(migrations.MigrationError) as caught:
            migrations.migrate_database(self.db)
        self.assertIn("still open", str(caught.exception))
        holder.close()
        self.assertEqual(self.version(), 1)
        migrations.migrate_database(self.db)
        self.assertEqual(self.stashes(), 2)

    def test_a_backup_is_still_taken_first(self):
        result = migrations.migrate_database(self.db)
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertEqual(backup.row_counts(Path(result["backup"]))["stashes"], 1)


class BundleImportR14Tests(TempCase):
    def setUp(self):
        super().setUp()
        self.data = self.root / "data"
        (self.data / "metadata").mkdir(parents=True)
        (self.data / "metadata" / "model_1.json").write_text("{}", encoding="utf-8")
        (self.data / "previews" / "1").mkdir(parents=True)
        (self.data / "previews" / "1" / "cover.jpg").write_bytes(b"jpeg")
        self.db = self.data / "library.db"
        create_database(self.db)
        env = patch.dict(os.environ, {
            "DESYNC_DATA_DIR": str(self.data), "DESYNC_BACKUP_DIR": str(self.root / "backups"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.fresh = self.root / "fresh"
        self.seed()
        self.bundle = self.root / "bundle"
        data_bundle.export_bundle(self.db, self.bundle)

    def seed(self):
        snapshot = json.dumps({"rows": {"downloaded_files": [{
            "file_id": 9, "vault_path": "/old/vault/a/f.zip", "staging_path": "/old/vaultish/f.zip", "note": "/old/vault",
            "actual_bytes": 3,
        }]}, "modified": 1.5})
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("INSERT INTO external_libraries (name, path) VALUES ('Loot', '/old/vault/loot')")
            model = conn.execute(
                "INSERT INTO collections (name, source, vault_path, raw_backup_path) VALUES ('A', 'mmf', '/old/vault/a', 'metadata/1.json')"
            ).lastrowid
            conn.execute(
                "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,"
                " actual_bytes, vault_path, staging_path) VALUES (1, ?, 'f', 'f', 'f', 1, '/old/vault/a/f', '/old/stage/f')",
                (model,),
            )
            conn.execute("INSERT INTO download_jobs (model_id, destination_path) VALUES (?, '/old/vault/a')", (model,))
            conn.execute(
                "INSERT INTO deleted_files (file_id, collection_id, filename, original_path, pending_path, snapshot_json)"
                " VALUES (9, ?, 'f.zip', '/old/vault/a/f.zip', '/old/vault/.Pending-Deletion/9/f.zip', ?)",
                (model, snapshot),
            )
        conn.close()

    def fresh_rows(self, sql):
        conn = sqlite3.connect(self.fresh / "library.db")
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def leftovers(self):
        return sorted(p.name for p in self.fresh.iterdir()) if self.fresh.exists() else []

    def test_every_path_column_is_accounted_for(self):
        # A new column that stores a path has to choose a side here, or a moved install breaks silently.
        conn = sqlite3.connect(self.db)
        pattern = re.compile(r"path|_dir$|root|folder|location")
        found = set()
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
            found |= {(table, row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")') if pattern.search(row[1])}
        conn.close()
        self.assertEqual(found - set(data_bundle.PATH_COLUMNS) - NOT_MOVED_WITH_A_BUNDLE, set())

    def test_every_listed_column_moves(self):
        result = data_bundle.import_bundle(self.bundle, self.fresh, {"/old/vault": "/new/vault", "/old/stage": "/new/stage"})
        self.assertEqual(self.fresh_rows("SELECT path FROM external_libraries"), [("/new/vault/loot",)])
        self.assertEqual(self.fresh_rows("SELECT vault_path, raw_backup_path FROM collections WHERE name = 'A'"), [("/new/vault/a", "metadata/1.json")])
        self.assertEqual(self.fresh_rows("SELECT vault_path, staging_path FROM downloaded_files"), [("/new/vault/a/f", "/new/stage/f")])
        self.assertEqual(self.fresh_rows("SELECT destination_path FROM download_jobs"), [("/new/vault/a",)])
        self.assertEqual(
            self.fresh_rows("SELECT original_path, pending_path FROM deleted_files"),
            [("/new/vault/a/f.zip", "/new/vault/.Pending-Deletion/9/f.zip")],
        )
        self.assertEqual(result["paths_rewritten"], 8)

    def test_paths_inside_a_held_files_snapshot_move(self):
        data_bundle.import_bundle(self.bundle, self.fresh, {"/old/vault": "/new/vault"})
        snapshot = json.loads(self.fresh_rows("SELECT snapshot_json FROM deleted_files")[0][0])
        row = snapshot["rows"]["downloaded_files"][0]
        self.assertEqual(row["vault_path"], "/new/vault/a/f.zip")
        self.assertEqual(row["staging_path"], "/old/vaultish/f.zip", "a similar folder name must not match")
        self.assertEqual(row["actual_bytes"], 3)
        self.assertEqual(snapshot["modified"], 1.5)

    def test_a_failed_unpack_leaves_nothing_and_can_be_rerun(self):
        real = tarfile.open

        def fail_on_previews(path, *args, **kwargs):
            if Path(path).name == data_bundle.PREVIEWS:
                raise tarfile.ReadError("damaged")
            return real(path, *args, **kwargs)

        with patch("tarfile.open", fail_on_previews), self.assertRaises(data_bundle.BundleError):
            data_bundle.import_bundle(self.bundle, self.fresh, {})
        self.assertEqual(self.leftovers(), [])
        data_bundle.import_bundle(self.bundle, self.fresh, {})
        self.assertTrue((self.fresh / "library.db").is_file())
        self.assertTrue((self.fresh / "metadata" / "model_1.json").is_file())

    def test_a_failed_rewrite_leaves_nothing_and_can_be_rerun(self):
        failure = sqlite3.OperationalError("disk I/O error")
        with patch.object(data_bundle, "_rewrite_prefix", side_effect=failure), self.assertRaises(data_bundle.BundleError):
            data_bundle.import_bundle(self.bundle, self.fresh, {"/old/vault": "/new/vault"})
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(data_bundle.import_bundle(self.bundle, self.fresh, {"/old/vault": "/new/vault"})["success"])

    def test_a_data_folder_that_already_has_logs_is_fine(self):
        (self.fresh / "logs").mkdir(parents=True)
        (self.fresh / "logs" / "vault.log").write_text("x", encoding="utf-8")
        data_bundle.import_bundle(self.bundle, self.fresh, {})
        self.assertEqual(self.leftovers(), ["library.db", "logs", "metadata", "previews"])

    def test_dbtool_reports_a_bad_map_without_a_traceback(self):
        for value in ("no-equals", "=/new", "/old="):
            with patch.object(sys, "argv", ["dbtool", "import", str(self.bundle), "--map", value]), \
                    patch.object(dbtool, "refuse_if_app_running"), patch("sys.stderr") as stderr:
                self.assertEqual(dbtool.main(), 1)
            self.assertIn("--map", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_dbtool_reports_a_bundle_error_without_a_traceback(self):
        with patch.object(sys, "argv", ["dbtool", "import", str(self.root / "missing")]), \
                patch.object(dbtool, "refuse_if_app_running"), patch("sys.stderr") as stderr:
            self.assertEqual(dbtool.main(), 1)
        self.assertIn("isn't a data bundle", "".join(call.args[0] for call in stderr.write.call_args_list))
