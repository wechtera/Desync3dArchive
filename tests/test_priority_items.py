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
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival import backup, dbtool, external, migrations
from desync_archival import settings as app_config
from desync_archival.api import server
from desync_archival.services import data_bundle, jobs, login, secret_store
from desync_archival.services.db import get_db_connection
from desync_archival.services.downloader import DownloadManager, queue_models
from desync_archival.services.stash_exporter import StashExporter
from desync_archival.services.task_manager import TaskManager

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


def join_task_threads():
    for thread in threading.enumerate():
        if thread.name.startswith("task-"):
            thread.join(5)


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


class ExtractionLimitS04Tests(TempCase):
    def setUp(self):
        super().setUp()
        self.folder = self.root / "scratch"
        self.folder.mkdir()
        self.archive = self.root / "pack.rar"
        self.archive.write_bytes(b"not read by the fake tool")

    def fake_tool(self, body):
        tool = self.root / "tool.py"
        tool.write_text(body, encoding="utf-8")
        return [sys.executable, str(tool), str(self.folder)]

    def free_space(self, free):
        return patch("desync_archival.external.shutil.disk_usage", return_value=shutil._ntuple_diskusage(free, 0, free))

    def test_a_tool_that_writes_past_the_limit_is_stopped(self):
        command = self.fake_tool(
            "import sys, time\n"
            "with open(sys.argv[1] + '/big.bin', 'wb') as out:\n"
            "    out.write(b'x' * 4096)\n"
            "    out.flush()\n"
            "    time.sleep(30)\n"
        )
        started = time.monotonic()
        with self.assertRaises(external.ExtractionRefused):
            external.run(command, 30, cap=(self.folder, 1024))
        self.assertLess(time.monotonic() - started, 10)

    def test_a_capped_run_still_returns_the_tools_output(self):
        command = self.fake_tool("print('listed')\n")
        result = external.run(command, 10, cap=(self.folder, 1024))
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "listed"))

    def test_a_capped_run_still_times_out(self):
        command = self.fake_tool("import time\ntime.sleep(30)\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            external.run(command, 0.2, cap=(self.folder, 1024))

    def rar_tool(self, payload_bytes):
        tool = self.root / "unrar"
        tool.write_text(
            f"#!{sys.executable}\n"
            "import sys, time\n"
            "folder = sys.argv[-1]\n"
            f"open(folder + '/member.stl', 'wb').write(b'x' * {payload_bytes})\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        tool.chmod(0o755)
        return str(tool)

    def test_a_rar_member_that_lies_about_its_size_is_stopped_and_removed(self):
        with patch.object(external, "MAX_EXTRACTED_BYTES", 1024), self.assertRaises(external.ExtractionRefused):
            external.extract_member(self.archive, "a/member.stl", self.folder, self.rar_tool(4096), None)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_member_too_big_for_the_disk_is_refused_before_a_tool_runs(self):
        with self.free_space(external.FREE_SPACE_RESERVE + 100), patch.object(external, "run") as run, \
                self.assertRaises(external.ExtractionRefused) as caught:
            external.extract_member(self.archive, "a/member.stl", self.folder, "unrar", None, expected_size=4096)
        run.assert_not_called()
        self.assertIn("free space", str(caught.exception))

    def test_a_full_disk_refuses_with_no_size_known(self):
        with self.free_space(100), patch.object(external, "run") as run, self.assertRaises(external.ExtractionRefused):
            external.extract_member(self.archive, "a/member.stl", self.folder, "unrar", None)
        run.assert_not_called()

    def test_a_zip_member_stops_at_free_space_not_just_the_global_limit(self):
        archive = self.root / "pack.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("a/member.stl", b"x" * 4096)
        with self.free_space(external.FREE_SPACE_RESERVE + 1024), self.assertRaises(external.ExtractionRefused):
            external.extract_member(archive, "a/member.stl", self.folder, None, None)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_whole_archive_unpack_is_capped_while_it_runs(self):
        with patch.object(external, "MAX_EXTRACTED_BYTES", 1024), \
                patch.object(external, "list_members", return_value=[]), \
                self.assertRaises(external.ExtractionRefused):
            external.extract_all(".rar", self.archive, self.folder, self.rar_tool(4096), None)
        self.assertEqual(list(self.folder.iterdir()), [])


class ExportSpaceS04Tests(TempCase):
    def setUp(self):
        super().setUp()
        self.db = self.root / "library.db"
        create_database(self.db)
        self.export_root = self.root / "exports"
        self.work = self.root / "work"
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('stash_download_path', ?)", (str(self.export_root),))
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('download_work_path', ?)", (str(self.work),))
            model = conn.execute("INSERT INTO collections (name, source, vault_path, raw_backup_path) VALUES ('A', 'mmf', '/v/a', 'm.json')").lastrowid
            conn.execute("INSERT INTO model_files (file_id, model_id, filename) VALUES (1, ?, 'a.zip')", (model,))
            conn.execute(
                "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,"
                " actual_bytes, vault_path) VALUES (1, ?, 'a.zip', 'a.zip', 'a.zip', 1000, '/v/a/a.zip')", (model,),
            )
            conn.execute(
                "INSERT INTO file_archive_members (file_id, internal_path, filename, size_bytes) VALUES (1, 'x.stl', 'x.stl', 5000)"
            )
            stash = conn.execute("INSERT INTO stashes (name) VALUES ('S')").lastrowid
            conn.execute("INSERT INTO stash_items (stash_id, item_type, model_id, file_id) VALUES (?, 'file', ?, 1)", (stash, model))
        conn.close()
        self.stash = stash
        self.exporter = StashExporter(self.db)

    def test_a_stash_too_big_for_the_disk_is_refused_before_anything_is_made(self):
        usage = shutil._ntuple_diskusage(external.FREE_SPACE_RESERVE + 100, 0, external.FREE_SPACE_RESERVE + 100)
        with patch("desync_archival.services.stash_exporter.shutil.disk_usage", return_value=usage), \
                self.assertRaises(ValueError) as caught:
            self.exporter.export_stash(self.stash)
        self.assertIn("free", str(caught.exception))
        self.assertEqual(list(self.export_root.iterdir()) if self.export_root.exists() else [], [])

    def test_the_estimate_counts_what_a_peeked_archive_unpacks_to(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        items = conn.execute("SELECT * FROM stash_items").fetchall()
        self.assertEqual(self.exporter._estimated_bytes(conn, items), 5000)
        conn.close()

    def test_every_item_type_can_be_estimated(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        model = conn.execute("SELECT id FROM collections WHERE name = 'A'").fetchone()[0]
        items = [
            {"item_type": "model", "model_id": model, "file_id": None, "submodel_id": None, "archive_member_id": None},
            {"item_type": "archive_member", "model_id": model, "file_id": None, "submodel_id": None, "archive_member_id": 1},
            {"item_type": "submodel", "model_id": model, "file_id": None, "submodel_id": 404, "archive_member_id": None},
        ]
        self.assertEqual(self.exporter._estimated_bytes(conn, items), 5000 + 5000)
        conn.close()


class QueueR17Tests(unittest.TestCase):
    LIVE = ("downloading", "staging_complete", "offloading")

    def setUp(self):
        create_database(server.DB_PATH)
        self.conn = get_db_connection(server.DB_PATH)
        self.addCleanup(self.conn.close)
        self.conn.execute("DELETE FROM download_jobs")
        self.conn.execute("DELETE FROM collections")
        self.conn.commit()
        self.client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def job(self, status):
        with self.conn:
            model = self.conn.execute(
                "INSERT INTO collections (name, source, raw_backup_path) VALUES (?, 'mmf', 'm.json')", (status,)
            ).lastrowid
            self.conn.execute("INSERT INTO download_jobs (model_id, status) VALUES (?, ?)", (model, status))
        return model

    def status(self, model):
        row = self.conn.execute("SELECT status FROM download_jobs WHERE model_id = ?", (model,)).fetchone()
        return row[0] if row else None

    def delete(self, body):
        return self.client.request("DELETE", "/api/downloads/queue", json=body)

    def test_queuing_a_live_job_leaves_it_alone(self):
        for status in self.LIVE:
            model = self.job(status)
            self.assertEqual(queue_models(self.conn, None, [model]), 0)
            self.assertEqual(self.status(model), status)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM download_jobs WHERE model_id = ?", (model,)).fetchone()[0], 1)

    def test_finished_jobs_can_still_be_queued_again(self):
        for status in ("failed", "completed"):
            model = self.job(status)
            self.assertEqual(queue_models(self.conn, None, [model]), 1)
            self.assertEqual(self.status(model), "pending")

    def test_a_new_model_gets_a_job(self):
        with self.conn:
            model = self.conn.execute(
                "INSERT INTO collections (name, source, raw_backup_path) VALUES ('New', 'mmf', 'm.json')"
            ).lastrowid
        self.assertEqual(queue_models(self.conn, None, [model]), 1)
        self.assertEqual(self.status(model), "pending")

    def test_the_route_counts_only_what_it_really_queued(self):
        live, waiting = self.job("downloading"), self.job("failed")
        data = self.client.post("/api/downloads/queue", json={"model_ids": [live, waiting]}).json()
        self.assertEqual(data["queued_count"], 1)

    def test_removing_by_model_keeps_live_and_finished_jobs(self):
        kept = [self.job(status) for status in (*self.LIVE, "completed")]
        gone = [self.job(status) for status in ("pending", "failed")]
        data = self.delete({"model_ids": kept + gone}).json()
        self.assertEqual((data["removed"], data["kept"]), (2, 4))
        self.assertEqual([self.status(m) for m in gone], [None, None])
        self.assertEqual([self.status(m) for m in kept], [*self.LIVE, "completed"])

    def test_purge_incomplete_spares_live_jobs(self):
        live = [self.job(status) for status in self.LIVE]
        gone = [self.job(status) for status in ("pending", "failed")]
        self.assertEqual(self.delete({"purge_incomplete": True}).status_code, 200)
        self.assertEqual([self.status(m) for m in live], list(self.LIVE))
        self.assertEqual([self.status(m) for m in gone], [None, None])

    def test_the_control_route_purge_spares_live_jobs(self):
        manager = MagicMock()
        live = [self.job(status) for status in self.LIVE]
        with patch.object(server, "download_manager", manager):
            response = self.client.post("/api/downloads/control", json={"action": "purge_incomplete"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([self.status(m) for m in live], list(self.LIVE))

    def test_clearing_a_live_status_is_refused(self):
        for status in self.LIVE:
            model = self.job(status)
            self.assertEqual(self.delete({"clear_status": status}).status_code, 409)
            self.assertEqual(self.status(model), status)

    def test_clearing_a_waiting_status_still_works(self):
        model = self.job("failed")
        self.assertEqual(self.delete({"clear_status": "failed"}).status_code, 200)
        self.assertIsNone(self.status(model))

    def test_an_offload_counts_as_busy(self):
        manager = MagicMock(active_downloads={}, active_offloads={0: {"model_id": 1}})
        self.assertIn("vault", jobs.busy_reason(self.conn, manager))

    def test_idle_workers_are_not_busy(self):
        manager = MagicMock(active_downloads={}, active_offloads={})
        self.assertIsNone(jobs.busy_reason(self.conn, manager))

    def test_the_manager_helpers_share_the_guard(self):
        live = [self.job(status) for status in self.LIVE]
        manager = object.__new__(DownloadManager)
        manager.db_path = server.DB_PATH
        self.assertEqual(manager.dequeue_models(live), 0)
        self.assertEqual(manager.clear_queue(), 0)
        for status, model in zip(self.LIVE, live, strict=True):
            self.assertEqual(manager.clear_queue(status), 0)
            self.assertEqual(self.status(model), status)


class CancelR16Tests(TempCase):
    """Real threads, because the bug only exists while a cancelled worker is still running."""

    def setUp(self):
        super().setUp()
        self.db = self.root / "library.db"
        create_database(self.db)
        self.manager = object.__new__(TaskManager)
        self.manager.db_path = self.db
        self.manager.root_dir = self.root
        self.manager._lock = threading.Lock()
        self.manager.active_task_id = None
        self.manager.active_process = None
        self.manager._cancel_events = {}
        self.gate = threading.Event()
        # Runs last to first: open the gate, wait for the threads, then the temp folder can go.
        self.addCleanup(join_task_threads)
        self.addCleanup(self.gate.set)
        self.exited = threading.Event()

    def worker(self, task_id, _params):
        self.gate.wait(10)
        status = jobs.CANCELLED if self.manager._cancelled(task_id) else jobs.COMPLETED
        self.manager._finalize_task(task_id, status, "done", {})
        self.manager._release(task_id)
        self.exited.set()

    def start(self):
        with patch.object(self.manager, "_run_backup_worker", self.worker):
            return self.manager.start_task("database_backup")["task_id"]

    def row(self, task_id):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT status, progress_label FROM pipeline_tasks WHERE id = ?", (task_id,)).fetchone()
        finally:
            conn.close()

    def busy(self):
        conn = sqlite3.connect(self.db)
        try:
            return jobs.busy_reason(conn)
        finally:
            conn.close()

    def finish(self):
        self.gate.set()
        self.assertTrue(self.exited.wait(5))
        join_task_threads()

    def test_a_cancelled_task_stays_running_until_its_worker_stops(self):
        task = self.start()
        self.manager.cancel_task(task)
        self.assertEqual(self.row(task)[0], "running")
        self.assertIn("Cancelling", self.row(task)[1])
        self.assertIsNotNone(self.busy())
        self.finish()
        self.assertEqual(self.row(task)[0], "cancelled")

    def test_nothing_else_starts_while_a_cancelled_worker_runs(self):
        task = self.start()
        self.manager.cancel_active_task()
        result = self.manager.start_task("database_backup")
        self.assertFalse(result["success"])
        self.assertEqual(result["task_id"], task)
        self.finish()

    def test_a_killed_task_keeps_its_slot_until_the_thread_ends(self):
        task = self.start()
        process = MagicMock()
        process.poll.return_value = None
        self.manager.active_process = process
        self.manager.kill_active_task()
        process.kill.assert_called_once()
        self.assertEqual(self.manager.active_task_id, task)
        self.assertEqual(self.row(task)[0], "running")
        self.assertIsNotNone(self.busy())
        self.finish()
        self.assertEqual(self.row(task)[0], "cancelled")
        self.assertIsNone(self.manager.active_task_id)

    def test_cancelling_one_task_leaves_the_next_alone(self):
        self.manager._begin(1)
        self.manager._begin(2)
        self.manager._cancel_events[1].set()
        self.assertTrue(self.manager._cancelled(1))
        self.assertFalse(self.manager._cancelled(2))

    def test_starting_a_task_cant_uncancel_an_older_one(self):
        self.manager._begin(1)
        self.manager._cancel_events[1].set()
        self.manager._begin(2)
        self.assertTrue(self.manager._cancelled(1))

    def test_an_old_worker_cant_clear_a_newer_tasks_slot(self):
        self.manager._begin(1)
        self.manager._begin(2)
        self.manager._release(1)
        self.assertEqual(self.manager.active_task_id, 2)
        self.manager._release(2)
        self.assertIsNone(self.manager.active_task_id)

    def stale_row(self):
        conn = sqlite3.connect(self.db)
        with conn:
            task = conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('vault_reconcile', 'running')").lastrowid
        conn.close()
        return task

    def test_a_row_with_no_worker_can_still_be_cancelled(self):
        # Nothing is left to report back, so waiting would leave the row running forever.
        task = self.stale_row()
        self.manager.cancel_task(task)
        self.assertEqual(self.row(task)[0], "cancelled")

    def test_cancelling_with_no_active_worker_clears_stale_rows(self):
        task = self.stale_row()
        self.assertTrue(self.manager.cancel_active_task())
        self.assertEqual(self.row(task)[0], "cancelled")

    def test_killing_a_row_with_no_worker_clears_it(self):
        task = self.stale_row()
        self.assertTrue(self.manager.kill_active_task())
        self.assertEqual(self.row(task)[0], "cancelled")

    def test_a_worker_that_ignores_cancel_reports_what_really_happened(self):
        task = self.stale_row()
        self.manager._begin(task)
        self.manager.cancel_task(task)
        self.manager._finalize_task(task, jobs.COMPLETED, "finished anyway", {})
        self.assertEqual(self.row(task)[0], "completed")

    def test_a_cancelled_worker_that_returns_without_a_result_is_closed(self):
        task = self.stale_row()
        self.manager._begin(task)
        done = threading.Event()
        self.manager.cancel_task(task)
        self.manager._spawn(task, "vault_reconcile", done.set, ())
        self.assertTrue(done.wait(5))
        deadline = time.monotonic() + 5
        while self.row(task)[0] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.row(task)[0], "cancelled")

    def test_a_worker_that_returns_without_a_result_doesnt_look_busy_forever(self):
        task = self.stale_row()
        self.manager._begin(task)
        self.manager._spawn(task, "vault_reconcile", lambda: None, ())
        deadline = time.monotonic() + 5
        while self.row(task)[0] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.row(task)[0], "failed")

    def test_the_time_limit_uses_the_tasks_own_cancel(self):
        task = self.stale_row()
        self.manager._begin(task)
        self.manager._on_time_limit(task, "vault_reconcile")
        self.assertTrue(self.manager._cancelled(task))
        self.assertEqual(self.row(task)[0], "running")

    def test_no_worker_reads_a_shared_flag(self):
        source = (Path(__file__).resolve().parents[1] / "src/desync_archival/services/task_manager.py").read_text(encoding="utf-8")
        self.assertNotIn("_cancel_requested", source)
