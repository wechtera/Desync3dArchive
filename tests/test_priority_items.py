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

import httpx
from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival import backup, dbtool, external, migrations
from desync_archival import settings as app_config
from desync_archival.api import server
from desync_archival.services import (
    archive_peeker,
    data_bundle,
    duplicates,
    health,
    jobs,
    login,
    scheduler,
    secret_store,
    task_manager,
)
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.db import get_db_connection, scoped_connection
from desync_archival.services.downloader import (
    DownloadManager,
    StagedFileMissing,
    queue_models,
)
from desync_archival.services.reconciler import VaultReconciler
from desync_archival.services.stash_exporter import StashExporter
from desync_archival.services.task_manager import TaskManager

# Always relative to the data folder or an archive, so a move can't change them.
NOT_MOVED_WITH_A_BUNDLE = {
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
        copy = Path(str(result["backup"]))
        self.assertTrue(copy.is_file())
        self.assertEqual(backup.row_counts(copy)["stashes"], 1)


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
        # The database is shared, and a live job left here would make other tests' routes busy.
        self.addCleanup(self.clear_jobs)
        self.client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def clear_jobs(self):
        with self.conn:
            self.conn.execute("DELETE FROM download_jobs")

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


class DuplicateNamesR13Tests(unittest.TestCase):
    def test_whole_words_only(self):
        ignored = duplicates.parse_ignored(duplicates.DEFAULT_IGNORE_NAMES)
        for name in ("presupported_head.stl", "Unsupported Torso.stl", "Supported/Arm.stl", "Baseline.stl", "database.zip"):
            self.assertFalse(duplicates.is_ignored(name, ignored), name)
        for name in ("base_01.stl", "Support.stl", "75mm Base.zip", "Bases.zip", "Hero-Supports.stl", "round.BASE.stl"):
            self.assertTrue(duplicates.is_ignored(name, ignored), name)

    def test_a_setting_can_name_several_words_in_a_row(self):
        ignored = duplicates.parse_ignored("support stls, golem")
        self.assertTrue(duplicates.is_ignored("Hero Support STLs.zip", ignored))
        self.assertTrue(duplicates.is_ignored("golem_01.stl", ignored))
        self.assertFalse(duplicates.is_ignored("support for stls.zip", ignored))
        self.assertFalse(duplicates.is_ignored("Hero Support.zip", ignored))

    def test_an_empty_setting_ignores_nothing(self):
        self.assertFalse(duplicates.is_ignored("base.stl", duplicates.parse_ignored(" , ")))

    def scan(self, *names):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        migrations.apply_pending(conn)
        with conn:
            for model in (1, 2):
                conn.execute(
                    "INSERT INTO collections (id, name, source, raw_backup_path) VALUES (?, ?, 'mmf', 'm.json')",
                    (model, f"M{model}"),
                )
            for index, name in enumerate(names):
                for model in (1, 2):
                    file_id = model * 100 + index
                    conn.execute("INSERT INTO model_files (file_id, model_id, filename, size_bytes) VALUES (?, ?, ?, 10)", (file_id, model, name))
                    conn.execute(
                        "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,"
                        " actual_bytes, sha256, vault_path) VALUES (?, ?, ?, ?, ?, 10, ?, ?)",
                        (file_id, model, name, name, name, f"hash{index}", f"/v/{file_id}"),
                    )
        result = duplicates.find_duplicates(conn)
        conn.close()
        return result

    def test_presupported_files_reach_the_check(self):
        result = self.scan("Hero_Presupported.zip")
        self.assertEqual([group["filename"] for group in result["files"]], ["Hero_Presupported.zip"])

    def test_the_summary_says_how_many_were_left_out(self):
        result = self.scan("Hero_Presupported.zip", "base_01.zip", "Support.zip")
        self.assertEqual(result["summary"]["excluded_files"], 4)
        self.assertEqual(len(result["files"]), 1)


class PeekFallbackE47Tests(TempCase):
    def setUp(self):
        super().setUp()
        self.previews = self.root / "previews"
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE collections (id INTEGER PRIMARY KEY, primary_image_url TEXT, external_key TEXT)")
        self.addCleanup(self.conn.close)
        self.peeker = object.__new__(ArchivePeeker)
        self.peeker.previews_root = self.previews
        self.peeker._get_db = lambda: self.conn
        self.folder = self.previews / "7" / "99"

    def collection(self, url):
        self.conn.execute("INSERT INTO collections (id, primary_image_url) VALUES (7, ?)", (url,))
        self.conn.commit()

    def cover(self, content=b"cached cover"):
        path = self.previews / "7" / "cover.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_a_cached_cover_means_no_request(self):
        self.collection("https://assets.myminifactory.com/a.jpg")
        self.cover()
        with patch("desync_archival.services.archive_peeker.httpx.Client") as client:
            found = self.peeker._fetch_catalog_fallback_image(7, self.folder)
        client.assert_not_called()
        self.assertEqual(found, self.folder / "catalog_primary.jpg")
        self.assertEqual(found.read_bytes(), b"cached cover")

    def test_an_external_collections_local_cover_works(self):
        # primary_image_url is a local path for these, which httpx can't fetch.
        self.collection("/previews/7/cover.jpg")
        self.cover()
        found = self.peeker._fetch_catalog_fallback_image(7, self.folder)
        self.assertEqual(found.read_bytes(), b"cached cover")

    def test_a_local_path_with_no_cover_finds_nothing_and_makes_no_request(self):
        self.collection("/previews/7/cover.jpg")
        with patch("desync_archival.services.archive_peeker.httpx.Client") as client:
            self.assertIsNone(self.peeker._fetch_catalog_fallback_image(7, self.folder))
        client.assert_not_called()

    def test_with_no_cover_it_fetches_once_and_keeps_it_as_the_cover(self):
        self.collection("https://assets.myminifactory.com/a.jpg")
        response = MagicMock(status_code=200, content=b"fetched")
        with patch("desync_archival.services.archive_peeker.httpx.Client") as client:
            client.return_value.__enter__.return_value.get.return_value = response
            first = self.peeker._fetch_catalog_fallback_image(7, self.folder)
            second = self.peeker._fetch_catalog_fallback_image(7, self.previews / "7" / "100")
        self.assertEqual(client.call_count, 1)
        self.assertEqual((self.previews / "7" / "cover.jpg").read_bytes(), b"fetched")
        self.assertEqual(first.read_bytes(), b"fetched")
        self.assertEqual(second.read_bytes(), b"fetched")

    def test_an_empty_cover_doesnt_count(self):
        self.collection("https://assets.myminifactory.com/a.jpg")
        self.cover(b"")
        response = MagicMock(status_code=200, content=b"fetched")
        with patch("desync_archival.services.archive_peeker.httpx.Client") as client:
            client.return_value.__enter__.return_value.get.return_value = response
            found = self.peeker._fetch_catalog_fallback_image(7, self.folder)
        self.assertEqual(found.read_bytes(), b"fetched")

    def test_a_failed_fetch_leaves_no_files(self):
        self.collection("https://assets.myminifactory.com/a.jpg")
        with patch("desync_archival.services.archive_peeker.httpx.Client") as client:
            client.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("no")
            self.assertIsNone(self.peeker._fetch_catalog_fallback_image(7, self.folder))
        self.assertFalse((self.previews / "7" / "cover.jpg").exists())

    def test_it_keeps_the_stored_path_format(self):
        self.collection("/previews/7/cover.jpg")
        self.cover()
        found = self.peeker._fetch_catalog_fallback_image(7, self.folder)
        self.assertEqual(found.relative_to(self.previews).as_posix(), "7/99/catalog_primary.jpg")


class StatusPollP01Tests(TempCase):
    def setUp(self):
        super().setUp()
        create_database(server.DB_PATH)
        self.conn = get_db_connection(server.DB_PATH)
        self.addCleanup(self.conn.close)
        with self.conn:
            self.conn.execute("DELETE FROM pipeline_tasks")
            self.conn.execute("DELETE FROM download_jobs")
            self.conn.execute("DELETE FROM collections")
        self.manager = object.__new__(TaskManager)
        self.manager.db_path = server.DB_PATH
        self.client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        for patcher in (patch.object(server, "task_manager", self.manager),):
            patcher.start()
            self.addCleanup(patcher.stop)

    def task(self, status="completed", log="a log line\n", summary=None):
        with self.conn:
            return self.conn.execute(
                "INSERT INTO pipeline_tasks (task_type, status, log_output, result_summary_json, artifacts_json)"
                " VALUES ('stash_export', ?, ?, ?, '{\"folder\": \"x\"}')",
                (status, log, json.dumps(summary or {"files": 3})),
            ).lastrowid

    def test_the_poll_leaves_out_logs_and_reports(self):
        self.task(log="x" * 500_000, summary={"pairs": ["y" * 500_000]})
        body = self.client.get("/api/tasks/status")
        self.assertLess(len(body.content), 2_000)
        recent = body.json()["recent_tasks"][0]
        for field in ("log_output", "result_summary", "artifacts", "artifacts_json", "result_summary_json"):
            self.assertNotIn(field, recent)
        self.assertEqual(recent["status"], "completed")

    def test_the_active_task_keeps_its_live_log(self):
        self.task(status="running", log="working\n")
        self.assertEqual(self.client.get("/api/tasks/status").json()["active_task"]["log_output"], "working\n")

    def test_one_task_can_be_fetched_whole(self):
        task = self.task()
        data = self.client.get(f"/api/tasks/{task}").json()
        self.assertEqual(data["log_output"], "a log line\n")
        self.assertEqual(data["result_summary"], {"files": 3})
        self.assertEqual(data["artifacts"], {"folder": "x"})

    def test_an_unknown_task_is_a_404(self):
        self.assertEqual(self.client.get("/api/tasks/999999").status_code, 404)

    def test_status_is_not_read_as_a_task_id(self):
        self.assertEqual(self.client.get("/api/tasks/status").status_code, 200)

    def queue(self, count):
        with self.conn:
            for index in range(count):
                model = self.conn.execute(
                    "INSERT INTO collections (name, source, raw_backup_path) VALUES (?, 'mmf', 'm.json')", (f"M{index}",)
                ).lastrowid
                self.conn.execute("INSERT INTO download_jobs (model_id, status) VALUES (?, 'pending')", (model,))
        manager = object.__new__(DownloadManager)
        manager.db_path = server.DB_PATH
        manager._lock = threading.Lock()
        manager.active_downloads = {}
        manager.active_offloads = {}
        manager.is_running = True
        manager.is_paused = False
        manager.access_token = ""
        manager.token_expires_at = 0
        return manager.get_status

    def test_a_long_queue_comes_back_one_page_with_the_real_total(self):
        data = self.queue(DownloadManager.QUEUED_JOBS_PAGE + 25)()
        self.assertEqual(len(data["queued_jobs"]), DownloadManager.QUEUED_JOBS_PAGE)
        self.assertEqual(data["queued_total"], DownloadManager.QUEUED_JOBS_PAGE + 25)

    def test_a_short_queue_is_all_there(self):
        data = self.queue(3)()
        self.assertEqual((len(data["queued_jobs"]), data["queued_total"]), (3, 3))


class LogWritesP02Tests(TempCase):
    def setUp(self):
        super().setUp()
        self.db = self.root / "library.db"
        create_database(self.db)
        self.manager = object.__new__(TaskManager)
        self.manager.db_path = self.db
        self.manager._lock = threading.Lock()
        self.manager.active_task_id = None
        self.manager._cancel_events = {}
        conn = sqlite3.connect(self.db)
        with conn:
            self.task = conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('vault_reconcile', 'running')").lastrowid
            self.other = conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('vault_reconcile', 'running')").lastrowid
        conn.close()
        self.addCleanup(task_manager.TASK_LOGS.flush)

    def log(self, task=None):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT log_output FROM pipeline_tasks WHERE id = ?", (task or self.task,)).fetchone()[0] or ""
        finally:
            conn.close()

    def test_both_factories_use_normal_sync(self):
        # WAL with NORMAL can lose the last commits in a power cut, but can't corrupt. Approved 2026-11-01.
        with scoped_connection(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
        conn = get_db_connection(self.db)
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
        conn.close()

    def test_a_burst_of_lines_is_one_write(self):
        with patch.object(task_manager, "scoped_connection", wraps=scoped_connection) as connect:
            for index in range(100):
                self.manager._append_log(self.task, f"line {index}")
            self.assertEqual(connect.call_count, 0)
            self.manager._flush_log(self.task)
        self.assertEqual(connect.call_count, 1)
        lines = self.log().splitlines()
        self.assertEqual(len(lines), 100)
        self.assertTrue(lines[0].endswith("line 0"))
        self.assertTrue(lines[99].endswith("line 99"))

    def test_a_quiet_task_still_shows_its_lines(self):
        with patch.object(task_manager, "LOG_FLUSH_SECONDS", 0.02):
            self.manager._append_log(self.task, "only line")
            deadline = time.monotonic() + 3
            while "only line" not in self.log() and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertIn("only line", self.log())

    def test_the_last_lines_are_written_before_the_task_finishes(self):
        self.manager._append_log(self.task, "the last line")
        self.manager._finalize_task(self.task, jobs.COMPLETED, "done", {})
        self.assertIn("the last line", self.log())

    def test_a_worker_that_returns_without_a_result_keeps_its_lines(self):
        self.manager._append_log(self.task, "the last line")
        self.manager._close_row(self.task)
        self.assertIn("the last line", self.log())

    def test_tasks_keep_their_own_lines(self):
        self.manager._append_log(self.task, "first task")
        self.manager._append_log(self.other, "second task")
        self.manager._flush_log(self.task)
        self.assertIn("first task", self.log())
        self.assertEqual(self.log(self.other), "")
        task_manager.TASK_LOGS.flush()
        self.assertIn("second task", self.log(self.other))

    def test_a_failed_write_doesnt_stop_the_worker(self):
        self.manager._append_log(self.task, "kept in vault.log")
        with patch.object(task_manager, "scoped_connection", side_effect=sqlite3.OperationalError("database is locked")):
            self.manager._flush_log(self.task)

    def test_lines_are_still_redacted(self):
        self.manager._append_log(self.task, "failed with Authorization: Bearer abc123secret")
        self.manager._flush_log(self.task)
        self.assertNotIn("abc123secret", self.log())


class BackfillP03Tests(TempCase):
    def setUp(self):
        super().setUp()
        self.db = self.root / "library.db"
        create_database(self.db)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.previews = self.root / "previews"
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('download_root_path', ?)", (str(self.vault),))
            self.collection = conn.execute(
                "INSERT INTO collections (name, source, vault_path, raw_backup_path) VALUES ('Pack', 'mmf', ?, 'm.json')", (str(self.vault),)
            ).lastrowid
            for file_id in (1, 2, 3):
                archive = self.vault / f"pack{file_id}.zip"
                with zipfile.ZipFile(archive, "w") as handle:
                    handle.writestr(f"Hero{file_id}/Hero{file_id}_Body.stl", b"solid")
                conn.execute("INSERT INTO model_files (file_id, model_id, filename) VALUES (?, ?, ?)", (file_id, self.collection, archive.name))
                conn.execute(
                    "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,"
                    " actual_bytes, vault_path, file_extension) VALUES (?, ?, ?, ?, ?, 5, ?, '.zip')",
                    (file_id, self.collection, archive.name, archive.name, archive.name, str(archive)),
                )
        conn.close()
        env = patch.dict(os.environ, {"DESYNC_DATA_DIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def counting(self):
        return patch(
            "desync_archival.services.archive_peeker.catalog_levels.build_archive_models",
            wraps=archive_peeker.catalog_levels.build_archive_models,
        )

    def reconciler(self):
        reconciler = object.__new__(VaultReconciler)
        reconciler.db_path = self.db
        reconciler.previews_root = self.previews
        reconciler.storage = MagicMock()
        reconciler.storage.get_root_path.return_value = self.vault
        return reconciler

    def artifacts(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM model_artifacts").fetchone()[0]
        finally:
            conn.close()

    def test_a_backfill_builds_once_at_the_end(self):
        with self.counting() as build:
            report = self.reconciler().backfill_archive_peeks()
        self.assertEqual(report["inspected"], 3)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(build.call_args.args[1], None, "one build covers every collection")

    def test_what_a_backfill_peeked_reaches_the_catalog(self):
        self.reconciler().backfill_archive_peeks()
        self.assertEqual(self.artifacts(), 3)

    def test_a_cancelled_backfill_still_builds_what_it_peeked(self):
        answers = iter([False, True])
        with self.counting() as build:
            report = self.reconciler().backfill_archive_peeks(cancel_cb=lambda: next(answers))
        self.assertEqual(report["inspected"], 1)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(self.artifacts(), 1)

    def test_a_rerun_builds_what_a_crash_left_behind(self):
        # A crash after the last peek leaves archives a rerun won't select, because they have submodels.
        peeker = ArchivePeeker(self.db)
        for file_id in (1, 2, 3):
            peeker.inspect_and_record(self.collection, file_id, self.vault / f"pack{file_id}.zip", build_catalog=False)
        self.assertEqual(self.artifacts(), 0)
        report = self.reconciler().backfill_archive_peeks()
        self.assertEqual(report["total_archives"], 0)
        self.assertEqual(self.artifacts(), 3)

    def test_a_backfill_that_stopped_at_once_still_builds_once(self):
        with self.counting() as build:
            self.reconciler().backfill_archive_peeks(cancel_cb=lambda: True)
        self.assertEqual(build.call_count, 1)

    def test_a_failed_build_doesnt_fail_the_backfill(self):
        with patch(
            "desync_archival.services.archive_peeker.catalog_levels.build_archive_models",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            report = self.reconciler().backfill_archive_peeks()
        self.assertTrue(report["success"])
        self.assertEqual(report["inspected"], 3)

    def test_a_single_peek_still_builds_on_its_own(self):
        with self.counting() as build:
            ArchivePeeker(self.db).inspect_and_record(self.collection, 1, self.vault / "pack1.zip")
        self.assertEqual(build.call_count, 1)
        self.assertEqual(self.artifacts(), 1)

    def test_a_repeek_builds_once_too(self):
        with self.counting() as build:
            summary = archive_peeker.repeek_files(self.db, {1, 2, 3})
        self.assertEqual(summary["repaired"], 3)
        self.assertEqual(build.call_count, 1)


class SecondReviewS04Tests(TempCase):
    def setUp(self):
        super().setUp()
        self.folder = self.root / "scratch"
        self.folder.mkdir()
        self.archive = self.root / "pack.rar"
        self.archive.write_bytes(b"not read by the fake tool")

    def free(self, usable):
        free = external.FREE_SPACE_RESERVE + usable
        return patch("desync_archival.external.shutil.disk_usage", return_value=shutil._ntuple_diskusage(free, 0, free))

    def fake_unrar(self, payload_bytes):
        tool = self.root / "unrar"
        tool.write_text(
            f"#!{sys.executable}\nimport sys, time\n"
            f"open(sys.argv[-1] + '/big.stl', 'wb').write(b'x' * {payload_bytes})\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        tool.chmod(0o755)
        return str(tool)

    def test_a_whole_archive_stops_at_free_space(self):
        with self.free(1024), patch.object(external, "list_members", return_value=[]), \
                self.assertRaises(external.ExtractionRefused):
            external.extract_all(".rar", self.archive, self.folder, self.fake_unrar(4096), None)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_full_disk_refuses_a_whole_archive_before_a_tool_runs(self):
        with self.free(-1), patch.object(external, "run") as run, self.assertRaises(external.ExtractionRefused):
            external.extract_all(".rar", self.archive, self.folder, "unrar", None)
        run.assert_not_called()

    def test_declared_sizes_past_free_space_are_refused_first(self):
        members = [external.ArchiveMember("a.stl", 4096)]
        with self.free(1024), patch.object(external, "list_members", return_value=members), \
                patch.object(external, "run") as run, self.assertRaises(external.ExtractionRefused):
            external.extract_all(".rar", self.archive, self.folder, "unrar", None)
        run.assert_not_called()

    def test_member_limit_works_for_a_folder_not_made_yet(self):
        self.assertGreater(external.member_limit(self.folder / "not" / "yet"), -external.FREE_SPACE_RESERVE)

    def zip_with(self, name, size):
        archive = self.root / "pack.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr(name, b"x" * size)
        return archive

    def test_a_whole_zip_export_stops_at_free_space(self):
        archive = self.zip_with("a/big.stl", 4096)
        exporter = object.__new__(StashExporter)
        dest = self.root / "export"
        dest.mkdir()
        with self.free(1024), self.assertRaises(external.ExtractionRefused):
            exporter._unpack_archive_to_dest(archive, dest, self.folder, "all")
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_peeked_image_stops_at_free_space(self):
        archive = self.zip_with("a/big.jpg", 4096)
        peeker = object.__new__(ArchivePeeker)
        with self.free(1024):
            found = peeker._extract_images(archive, ["a/big.jpg"], self.folder, "zip")
        self.assertEqual(found, {})

    def test_free_space_is_checked_again_before_each_stash_item(self):
        exporter = object.__new__(StashExporter)
        checks = []
        exporter._check_free_space = lambda needed, *folders: checks.append(needed)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        migrations.apply_pending(conn)
        with conn:
            conn.execute("INSERT INTO collections (id, name, source, raw_backup_path) VALUES (1, 'A', 'mmf', 'm.json')")
            conn.execute("INSERT INTO stashes (id, name) VALUES (1, 'S')")
            for file_id in (1, 2):
                conn.execute("INSERT INTO model_files (file_id, model_id, filename) VALUES (?, 1, 'f.zip')", (file_id,))
                conn.execute("INSERT INTO stash_items (stash_id, item_type, model_id, file_id) VALUES (1, 'file', 1, ?)", (file_id,))
        exporter.resolve_paths = lambda _conn: (self.root / "e", self.folder, self.root / "p")
        (self.root / "e").mkdir()
        exporter._export_single_item = lambda **_kwargs: {"files_count": 0, "bytes_count": 0, "manifest_entry": {}}
        exporter.export_stash(1, conn=conn)
        self.assertEqual(len(checks), 3, "once for the whole stash, then once per item")
        conn.close()


class SecondReviewR17Tests(QueueR17Tests):
    def test_a_live_job_row_counts_as_busy_with_idle_workers(self):
        # Offload backoff leaves a staging_complete job in neither in-memory list.
        self.job("staging_complete")
        manager = MagicMock(active_downloads={}, active_offloads={})
        self.assertIn("in progress", jobs.busy_reason(self.conn, manager))

    def test_the_reset_route_refuses_while_a_job_is_live(self):
        model = self.job("staging_complete")
        response = self.client.post("/api/reconcile/reset", json={"model_ids": [model], "confirm": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.status(model), "staging_complete")

    def test_the_reset_itself_leaves_live_jobs_alone(self):
        live, failed = self.job("staging_complete"), self.job("failed")
        with self.conn:
            for file_id, model in ((91, live), (92, failed)):
                self.conn.execute("INSERT INTO model_files (file_id, model_id, filename) VALUES (?, ?, 'f')", (file_id, model))
                self.conn.execute(
                    "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,"
                    " actual_bytes) VALUES (?, ?, 'f', 'f', 'f', 1)", (file_id, model),
                )
        reconciler = object.__new__(VaultReconciler)
        reconciler.db_path = server.DB_PATH
        result = reconciler.reset_reconciliation_models([live, failed], dry_run=False)
        self.assertEqual((result["reset_count"], result["skipped_live"]), (1, 1))
        self.assertEqual(self.status(live), "staging_complete")
        kept = self.conn.execute("SELECT COUNT(*) FROM downloaded_files WHERE model_id = ?", (live,)).fetchone()[0]
        self.assertEqual(kept, 1)


class SecondReviewR16Tests(CancelR16Tests):
    def tool(self):
        script = self.root / "slow_tool.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        return [sys.executable, str(script)]

    def spawn_tool(self, task, finish):
        folder = self.root / "work"
        folder.mkdir(exist_ok=True)
        started = threading.Event()
        caught = []

        def worker():
            started.set()
            try:
                external.run(self.tool(), 30, cap=(folder, 1024))
            except external.ExtractionStopped as e:
                caught.append(e)
            finish(task)

        self.manager._spawn(task, "stash_export", worker, ())
        self.assertTrue(started.wait(5))
        return caught

    def test_force_kill_stops_an_unpacking_tool(self):
        task = self.stale_row()
        self.manager._begin(task)
        began = time.monotonic()
        caught = self.spawn_tool(task, lambda t: self.manager._finalize_task(t, jobs.CANCELLED, "stopped", {}))
        self.manager.kill_active_task()
        join_task_threads()
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(len(caught), 1)
        self.assertEqual(self.row(task)[0], "cancelled")

    def test_cancel_stops_an_unpacking_tool_too(self):
        task = self.stale_row()
        self.manager._begin(task)
        caught = self.spawn_tool(task, lambda t: self.manager._finalize_task(t, jobs.CANCELLED, "stopped", {}))
        self.manager.cancel_task(task)
        join_task_threads()
        self.assertEqual(len(caught), 1)

    def test_a_stopped_tool_is_not_retried_with_the_next_one(self):
        self.assertTrue(issubclass(external.ExtractionStopped, external.ExtractionRefused))

    def test_a_thread_with_no_task_is_unaffected(self):
        script = self.root / "ok.py"
        script.write_text("print('fine')\n", encoding="utf-8")
        result = external.run([sys.executable, str(script)], 10, cap=(self.root, 10**9))
        self.assertEqual(result.stdout.strip(), "fine")

    def test_a_stopped_export_ends_as_cancelled(self):
        task = self.stale_row()
        self.manager._begin(task)
        exporter = MagicMock()
        exporter.export_stash.side_effect = external.ExtractionStopped("stopped")
        with patch("desync_archival.services.stash_exporter.StashExporter", return_value=exporter):
            self.manager._run_stash_export_worker(task, {"stash_id": 1})
        self.assertEqual(self.row(task)[0], "cancelled")

    def test_an_unknown_task_type_gives_the_slot_back(self):
        result = self.manager.start_task("no_such_task")
        self.assertFalse(result["success"])
        self.assertIsNone(self.manager.active_task_id)
        self.assertEqual(self.manager._cancel_events, {})


class SecondReviewR14Tests(BundleImportR14Tests):
    def setting(self, key, value):
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        conn.close()
        shutil.rmtree(self.bundle)
        data_bundle.export_bundle(self.db, self.bundle)

    def test_an_underscore_in_a_map_isnt_a_wildcard(self):
        self.setting("download_root_path", "/Volumes/NASx1/vault")
        data_bundle.import_bundle(self.bundle, self.fresh, {"/Volumes/NAS_1": "/mnt/nas"})
        rows = self.fresh_rows("SELECT value FROM app_settings WHERE key = 'download_root_path'")
        self.assertEqual(rows, [("/Volumes/NASx1/vault",)])

    def test_settings_match_case_exactly(self):
        self.setting("download_root_path", "/OLD/VAULT/x")
        data_bundle.import_bundle(self.bundle, self.fresh, {"/old/vault": "/new/vault"})
        rows = self.fresh_rows("SELECT value FROM app_settings WHERE key = 'download_root_path'")
        self.assertEqual(rows, [("/OLD/VAULT/x",)])

    def test_a_matching_setting_still_moves(self):
        self.setting("download_root_path", "/old/vault/x")
        data_bundle.import_bundle(self.bundle, self.fresh, {"/old/vault": "/new/vault"})
        rows = self.fresh_rows("SELECT value FROM app_settings WHERE key = 'download_root_path'")
        self.assertEqual(rows, [("/new/vault/x",)])

    def test_the_longest_map_wins_whatever_the_order(self):
        data_bundle.import_bundle(self.bundle, self.fresh, {"/old": "/short", "/old/vault/loot": "/long"})
        self.assertEqual(self.fresh_rows("SELECT path FROM external_libraries"), [("/long",)])

    def test_a_damaged_metadata_archive_is_a_bundle_error(self):
        path = self.bundle / data_bundle.METADATA
        path.write_bytes(path.read_bytes()[:20])
        with self.assertRaises(data_bundle.BundleError):
            data_bundle.import_bundle(self.bundle, self.fresh, {})
        self.assertEqual(self.leftovers(), [])

    def test_a_bad_snapshot_is_a_bundle_error(self):
        conn = sqlite3.connect(self.bundle / data_bundle.DATABASE)
        with conn:
            conn.execute("UPDATE deleted_files SET snapshot_json = '{not json'")
        conn.close()
        with self.assertRaises(data_bundle.BundleError):
            data_bundle.import_bundle(self.bundle, self.fresh, {"/old/vault": "/new/vault"})

    def test_a_killed_imports_temp_folder_is_cleared(self):
        stale = self.fresh / ".import-old"
        stale.mkdir(parents=True)
        (stale / "library.db").write_bytes(b"old catalog copy")
        data_bundle.import_bundle(self.bundle, self.fresh, {})
        self.assertFalse(stale.exists())

    def test_an_absolute_metadata_path_moves(self):
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE collections SET raw_backup_path = '/old/vault/meta/1.json' WHERE name = 'A'")
        conn.close()
        shutil.rmtree(self.bundle)
        data_bundle.export_bundle(self.db, self.bundle)
        data_bundle.import_bundle(self.bundle, self.fresh, {"/old/vault": "/new/vault"})
        rows = self.fresh_rows("SELECT raw_backup_path FROM collections WHERE name = 'A'")
        self.assertEqual(rows, [("/new/vault/meta/1.json",)])


class SecondReviewSmallFixTests(TempCase):
    def test_a_linked_database_path_keeps_its_link(self):
        real = self.root / "real" / "library.db"
        real.parent.mkdir()
        create_database(real)
        conn = sqlite3.connect(real)
        conn.execute("PRAGMA user_version = 16")
        conn.close()
        link = self.root / "library.db"
        link.symlink_to(real)
        with patch.object(migrations, "apply_pending", side_effect=lambda c: 16), \
                patch.object(migrations, "latest_version", return_value=17), \
                patch.dict(os.environ, {"DESYNC_BACKUP_DIR": str(self.root / "backups")}):
            migrations.migrate_database(link)
        self.assertTrue(link.is_symlink())

    def test_a_broken_secrets_file_answers_login_with_a_clear_503(self):
        client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        with patch.object(login, "verify_password", side_effect=secret_store.SecretsError("nope")), \
                patch.object(login, "record_failure") as failure:
            reply = client.post("/api/login", json={"password": "anything-long"})
        self.assertEqual(reply.status_code, 503)
        self.assertIn("error", reply.json())
        failure.assert_not_called()

    def test_set_password_reports_a_broken_secrets_file_without_a_traceback(self):
        with patch.object(sys, "argv", ["dbtool", "set-password"]), \
                patch.object(login, "set_password", side_effect=secret_store.SecretsError("nope")), \
                patch("sys.stdin") as stdin, patch("sys.stderr") as stderr:
            stdin.isatty.return_value = False
            stdin.readline.return_value = "a-long-enough-password\n"
            self.assertEqual(dbtool.main(), 1)
        self.assertIn("nope", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_a_failed_cover_copy_doesnt_fail_the_peek(self):
        previews = self.root / "previews"
        (previews / "7").mkdir(parents=True)
        (previews / "7" / "cover.jpg").write_bytes(b"cover")
        peeker = object.__new__(ArchivePeeker)
        peeker.previews_root = previews
        with patch("desync_archival.services.archive_peeker.shutil.copyfile", side_effect=OSError(28, "No space left")):
            self.assertIsNone(peeker._fetch_catalog_fallback_image(7, previews / "7" / "99"))

    def test_a_blocked_log_write_cant_be_overtaken(self):
        db = self.root / "library.db"
        create_database(db)
        conn = sqlite3.connect(db)
        with conn:
            task = conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('x', 'running')").lastrowid
        conn.close()
        buffer = task_manager.TaskLogBuffer()
        started, release = threading.Event(), threading.Event()
        real = scoped_connection

        def slow_first(path):
            if not started.is_set():
                started.set()
                release.wait(5)
            return real(path)

        with patch.object(task_manager, "scoped_connection", slow_first), patch.object(task_manager, "LOG_FLUSH_SECONDS", 60):
            buffer.add(db, task, "first")
            first = threading.Thread(target=buffer.flush)
            first.start()
            self.assertTrue(started.wait(5))
            buffer.add(db, task, "second")
            second = threading.Thread(target=buffer.flush, args=(db, task))
            second.start()
            time.sleep(0.1)
            release.set()
            first.join(5)
            second.join(5)
        buffer.flush()
        conn = sqlite3.connect(db)
        log = conn.execute("SELECT log_output FROM pipeline_tasks WHERE id = ?", (task,)).fetchone()[0]
        conn.close()
        self.assertEqual(log.split(), ["first", "second"])

    def test_a_vanished_staged_file_fails_its_job(self):
        # Retrying can't bring it back, and a job stuck in staging_complete holds up every destructive action.
        db = self.root / "library.db"
        create_database(db)
        conn = sqlite3.connect(db)
        with conn:
            model = conn.execute("INSERT INTO collections (name, source, raw_backup_path) VALUES ('A', 'mmf', 'm.json')").lastrowid
            job = conn.execute("INSERT INTO download_jobs (model_id, status) VALUES (?, 'offloading')", (model,)).lastrowid
        manager = object.__new__(DownloadManager)
        manager.db_path = db
        manager._lock = threading.Lock()
        manager.is_paused = False
        manager.active_offloads = {}
        manager._offload_not_before = {}
        manager.storage = MagicMock()
        row = {"id": job, "model_id": model, "model_name": "A", "downloaded_bytes": 0, "retry_count": 0}
        with patch.object(manager, "_claim_next_offload_job", return_value=row), \
                patch.object(manager, "_process_nas_offload", side_effect=StagedFileMissing("gone")):
            manager._offload_next(0)
        status, message = conn.execute("SELECT status, error_message FROM download_jobs WHERE id = ?", (job,)).fetchone()
        conn.close()
        self.assertEqual(status, "failed")
        self.assertIn("gone", message)
        self.assertEqual(manager._offload_not_before, {})



class ImageRequestsS15Tests(TempCase):
    """Images go out as ourselves. Proven against MMF's image host on 2026-11-01, 5 of 5."""

    IMAGE = "https://assets.myminifactory.com/object-assets/a/b/c.jpg"

    def setUp(self):
        super().setUp()
        self.seen = []
        real_client = httpx.Client

        def answer(request):
            self.seen.append(request)
            return httpx.Response(200, content=b"image bytes", headers={"content-type": "image/jpeg"})

        patcher = patch("httpx.Client", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(answer), **kwargs))
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_plain(self):
        self.assertEqual(len(self.seen), 1)
        headers = self.seen[0].headers
        self.assertTrue(headers["user-agent"].startswith("python-httpx/"), headers["user-agent"])
        for name in ("referer", "cookie", "authorization"):
            self.assertNotIn(name, headers)

    def test_a_cover_during_ingest(self):
        manager = object.__new__(TaskManager)
        manager.root_dir = self.root
        manager._cache_cover(7, self.IMAGE)
        self.assert_plain()
        self.assertEqual((self.root / "previews" / "7" / "cover.jpg").read_bytes(), b"image bytes")

    def test_the_cover_backfill(self):
        db = self.root / "library.db"
        create_database(db)
        conn = sqlite3.connect(db)
        with conn:
            conn.execute(
                "INSERT INTO collections (id, name, source, raw_backup_path, primary_image_url) VALUES (7, 'A', 'mmf', 'm.json', ?)",
                (self.IMAGE,),
            )
        conn.close()
        reconciler = object.__new__(VaultReconciler)
        reconciler.db_path = db
        reconciler.previews_root = self.root / "previews"
        reconciler.backfill_catalog_previews()
        self.assert_plain()
        self.assertEqual((self.root / "previews" / "7" / "cover.jpg").read_bytes(), b"image bytes")

    def test_a_peek_fallback(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE collections (id INTEGER PRIMARY KEY, primary_image_url TEXT, external_key TEXT)")
        conn.execute("INSERT INTO collections (id, primary_image_url) VALUES (7, ?)", (self.IMAGE,))
        peeker = object.__new__(ArchivePeeker)
        peeker.previews_root = self.root / "previews"
        peeker._get_db = lambda: conn
        found = peeker._fetch_catalog_fallback_image(7, self.root / "previews" / "7" / "99")
        self.assert_plain()
        self.assertEqual(found.read_bytes(), b"image bytes")

    def test_browser_signals_stay_out_of_the_image_code(self):
        # Moving the other three requests to OAuth (E50) lowers these counts, and they should only fall.
        services = Path(__file__).resolve().parents[1] / "src/desync_archival/services"
        for name, allowed in (("reconciler.py", 0), ("archive_peeker.py", 0), ("task_manager.py", 0), ("downloader.py", 1)):
            self.assertEqual((services / name).read_text(encoding="utf-8").count("Mozilla/"), allowed, name)


class ScheduledSyncRemovedTests(unittest.TestCase):
    """Tucked away on feature/scheduled-sync until an API can list a user's library."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        migrations.apply_pending(self.conn)
        self.tasks = MagicMock()
        self.downloads = MagicMock(active_downloads={}, active_offloads={})

    def set(self, **values):
        with self.conn:
            for key, value in values.items():
                self.conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))

    def test_a_setting_left_on_starts_nothing(self):
        # Production holds sync_enabled = 1 from before this was removed.
        self.set(sync_enabled=1, sync_interval_days=1, cookies_json='[{"name": "a", "value": "b"}]')
        scheduler.tick(self.conn, self.tasks, self.downloads)
        self.tasks.start_task.assert_not_called()
        self.tasks.start_metadata_ingest.assert_not_called()

    def test_a_sync_left_part_way_is_ignored(self):
        self.set(sync_stage="scrape", sync_task_id=1)
        scheduler.tick(self.conn, self.tasks, self.downloads)
        self.tasks.start_metadata_ingest.assert_not_called()

    def test_the_integrity_schedule_still_runs(self):
        self.set(integrity_enabled=1)
        self.tasks.start_task.return_value = {"success": True, "task_id": 3}
        scheduler.tick(self.conn, self.tasks, self.downloads)
        self.assertEqual(self.tasks.start_task.call_args.args[0], "vault_reconcile")

    def test_settings_only_offer_the_integrity_schedule(self):
        self.set(sync_enabled=1)
        self.assertEqual(sorted(scheduler.settings(self.conn)), ["integrity_enabled", "integrity_interval_days", "integrity_mode"])

    def test_health_has_no_sync_row(self):
        self.set(sync_enabled=1, last_sync_result="Failed: x")
        report = health.report(self.conn, None)
        self.assertNotIn("sync", [check["key"] for check in report["checks"]])

    def test_nothing_scheduled_can_start_a_library_retrieval(self):
        source = (Path(__file__).resolve().parents[1] / "src/desync_archival/services/scheduler.py").read_text(encoding="utf-8")
        self.assertNotIn("library_scrape", source)
        self.assertNotIn("start_metadata_ingest", source)
