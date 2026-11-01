"""One class per item in the 2026-11-01 review's "Fix these first" table.

Each test fails on the code as reviewed. Class names carry the finding ID, so
`pytest tests/test_fix_first.py -k R03` proves one finding on its own.
"""

import json
import logging
import os
import sqlite3
import subprocess
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

from desync_archival import backup, dbtool, external
from desync_archival.api import server
from desync_archival.services import (
    auth_service,
    health,
    jobs,
    pending_deletion,
)
from desync_archival.services import logger as log
from desync_archival.services.analytics import run_startup_analytics
from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.db import get_db_connection
from desync_archival.services.downloader import DownloadManager
from desync_archival.services.paths import IMPORT_FOLDER, PENDING_DELETION_FOLDER
from desync_archival.services.reconciler import VaultReconciler
from desync_archival.services.stash_exporter import StashExporter
from desync_archival.services.task_manager import TaskManager

FILE_URL = "https://www.myminifactory.com/download/a"
SEVEN_ZIP = external.find_7zip()
REAL_CLIENT = httpx.Client


def replying_with(handler):
    """Routes every httpx.Client the downloader builds through a fake server."""
    def build(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
    return patch("desync_archival.services.downloader.httpx.Client", side_effect=build)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.db = self.base / "library.db"
        create_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)

    def setting(self, **values):
        with self.conn:
            auth_service.set_settings(self.conn, {key: str(value) for key, value in values.items()})


class DownloadCase(TempCase):
    CONTENT = b"a" * 600 + b"b" * 400

    def setUp(self):
        super().setUp()
        self.vault = self.base / "vault"
        self.vault.mkdir()
        self.work = self.base / "work"
        (self.work / "in_flight").mkdir(parents=True)
        self.setting(download_root_path=self.vault, download_work_path=self.work)
        with self.conn:
            self.conn.execute("""
                INSERT INTO collections (id, name, url, raw_backup_path, source, external_key, designer_name, release_month)
                VALUES (5, 'Model', '', '', 'mmf', '5', 'Maker', '2024-01')
            """)
            self.conn.execute("""
                INSERT INTO model_files (file_id, model_id, filename, size_bytes, download_url, source, external_key)
                VALUES (9, 5, ?, 1000, ?, 'mmf', '9')
            """, (self.filename(), FILE_URL))
            self.conn.execute("INSERT INTO download_jobs (id, model_id, status) VALUES (1, 5, 'pending')")
        self.dm = DownloadManager(self.db)
        self.dm.cookies = {}

    def filename(self):
        return "a.stl"

    def part(self):
        return self.work / "in_flight" / "5_9.part"

    def staged(self):
        return self.work / "staged" / "5" / self.filename()

    def nas_file(self):
        return self.vault / "Maker" / "2024-01" / "5_Model" / "files" / self.filename()

    def stage(self, reply=None):
        job = self.dm._claim_next_download_job()
        with replying_with(reply or (lambda request: httpx.Response(200, content=self.CONTENT))):
            self.dm._process_download_to_staging(1, job, self.work)

    def claim_offload(self):
        return self.dm._claim_next_offload_job()

    def row(self):
        return self.conn.execute("SELECT * FROM downloaded_files WHERE file_id = 9").fetchone()

    def job_status(self):
        return self.conn.execute("SELECT status FROM download_jobs WHERE id = 1").fetchone()[0]


class R01ResumedDownloads(DownloadCase):
    def resume(self, request):
        self.assertEqual(request.headers["range"], "bytes=600-")
        return httpx.Response(206, headers={"Content-Range": "bytes 600-999/1000"}, content=b"b" * 400)

    def test_a_resume_records_the_whole_file(self):
        self.part().write_bytes(b"a" * 600)
        self.stage(self.resume)
        self.assertEqual(self.row()["actual_bytes"], 1000)

    def test_a_resumed_download_reaches_the_vault(self):
        self.part().write_bytes(b"a" * 600)
        self.stage(self.resume)
        self.dm._process_nas_offload(1, self.claim_offload(), self.vault)
        self.assertEqual(self.nas_file().read_bytes(), self.CONTENT)
        self.assertEqual(self.job_status(), "completed")

    def test_a_row_written_by_the_old_code_still_offloads(self):
        self.stage()
        with self.conn:
            self.conn.execute("UPDATE downloaded_files SET actual_bytes = 400 WHERE file_id = 9")
        self.dm._process_nas_offload(1, self.claim_offload(), self.vault)
        self.assertEqual(self.job_status(), "completed")

    def test_a_server_ignoring_range_restarts_the_file(self):
        self.part().write_bytes(b"z" * 600)
        self.stage()
        self.assertEqual(self.staged().read_bytes(), self.CONTENT)
        self.assertEqual(self.row()["actual_bytes"], 1000)

    def test_a_failed_offload_waits_and_keeps_its_files(self):
        self.stage()
        with patch.object(self.dm, "_process_nas_offload", side_effect=OSError("NAS gone")), patch("time.sleep"):
            self.dm._offload_next(1)
        self.assertEqual(self.job_status(), "staging_complete")
        self.assertTrue(self.staged().exists())
        self.assertEqual(self.conn.execute("SELECT retry_count FROM download_jobs").fetchone()[0], 1)
        self.assertIsNone(self.claim_offload(), "a failed job must not be claimed again straight away")
        self.dm._offload_not_before[1] = 0
        self.assertIsNotNone(self.claim_offload())


class R02OffloadIntegrity(DownloadCase):
    def offload(self):
        self.dm._process_nas_offload(1, self.claim_offload(), self.vault)

    def test_a_good_copy_arrives_and_staging_is_cleared(self):
        self.stage()
        self.offload()
        self.assertEqual(self.nas_file().read_bytes(), self.CONTENT)
        self.assertFalse(self.staged().exists())
        self.assertEqual(self.row()["storage_tier"], "nas")

    def test_a_copy_with_wrong_bytes_keeps_the_staged_file(self):
        self.stage()
        bad_copy = patch("desync_archival.services.downloader.sha256_of", return_value="0" * 64)
        with bad_copy, self.assertRaisesRegex(OSError, "Checksum mismatch"):
            self.offload()
        self.assertTrue(self.staged().exists())
        self.assertFalse(self.nas_file().exists())
        self.assertFalse(self.nas_file().with_name("a.stl.part").exists())
        self.assertEqual(self.row()["storage_tier"], "local")

    def test_a_copy_cut_off_leaves_nothing_at_the_real_name(self):
        self.stage()
        dropped = patch("desync_archival.services.downloader.os.fsync", side_effect=OSError("share dropped"))
        with dropped, self.assertRaises(OSError):
            self.offload()
        self.assertFalse(self.nas_file().exists())
        self.assertTrue(self.staged().exists())

    def test_the_catalog_moves_to_the_nas_before_staging_is_deleted(self):
        self.stage()
        seen = []
        real_unlink = Path.unlink

        def watching(path, missing_ok=False):
            if path == self.staged():
                seen.append(self.row()["storage_tier"])
            real_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", watching):
            self.offload()
        self.assertEqual(seen, ["nas"])

    def test_a_row_with_no_hash_is_still_verified(self):
        self.stage()
        with self.conn:
            self.conn.execute("UPDATE downloaded_files SET sha256 = NULL WHERE file_id = 9")
        self.offload()
        self.assertEqual(self.nas_file().read_bytes(), self.CONTENT)

    def test_a_response_of_the_wrong_size_is_refused(self):
        page = b"<html>Checking your browser</html>"
        with self.assertRaisesRegex(RuntimeError, "Size mismatch"):
            self.stage(lambda request: httpx.Response(200, content=page))
        self.assertFalse(self.staged().exists())
        self.assertFalse(self.part().exists(), "bytes from the wrong response can't be resumed from")


class R03Export(TempCase):
    def build(self, members):
        vault = self.base / "vault"
        vault.mkdir()
        archive = vault / "pack.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for path, data in members.items():
                handle.writestr(path, data)
        with self.conn:
            self.conn.execute("""
                INSERT INTO collections (id, name, url, raw_backup_path, designer_name, vault_path)
                VALUES (100, 'Squad', '', '', 'Maker', ?)
            """, (str(vault),))
            self.conn.execute("""
                INSERT INTO downloaded_files
                (file_id, model_id, original_filename, cleaned_filename, local_relative_path, actual_bytes, vault_path, storage_tier)
                VALUES (500, 100, 'pack.zip', 'pack.zip', 'pack.zip', 1, ?, 'nas')
            """, (str(archive),))
            self.conn.execute("INSERT INTO file_archive_submodels (id, file_id, model_id, name) VALUES (201, 500, 100, 'Head')")
            for path, data in members.items():
                self.conn.execute("""
                    INSERT INTO file_archive_members (submodel_id, file_id, internal_path, filename, size_bytes)
                    VALUES (201, 500, ?, ?, ?)
                """, (path, Path(path).name, len(data)))
            self.conn.execute("INSERT INTO stashes (id, name) VALUES (1, 'Batch')")
            self.conn.execute("INSERT INTO stash_items (stash_id, item_type, model_id, submodel_id) VALUES (1, 'submodel', 100, 201)")
        return archive

    def exporter(self):
        (self.base / "work").mkdir(exist_ok=True)
        return StashExporter(
            db_path=self.db, export_root=self.base / "queue", work_path=self.base / "work",
            previews_root=self.base / "previews",
        )

    def test_same_named_files_both_survive_the_export(self):
        self.build({"Minis/Supported/Head.stl": b"S" * 500, "Minis/Unsupported/Head.stl": b"U" * 300})
        result = self.exporter().export_stash(stash_id=1, conn=self.conn)
        root = Path(result["export_path"])
        folder = root / "[Maker] Squad" / "Head"
        self.assertEqual((folder / "Supported" / "Head.stl").read_bytes(), b"S" * 500)
        self.assertEqual((folder / "Unsupported" / "Head.stl").read_bytes(), b"U" * 300)
        manifest = json.loads((root / "manifest.json").read_text())
        self.assertEqual((manifest["total_files"], manifest["total_bytes"]), (2, 800))
        self.assertEqual(sorted(manifest["items"][0]["files"]), ["Supported/Head.stl", "Unsupported/Head.stl"])

    def test_unique_names_stay_flat(self):
        self.build({"Minis/Arm.stl": b"a", "Minis/Leg.stl": b"l"})
        result = self.exporter().export_stash(stash_id=1, conn=self.conn)
        folder = Path(result["export_path"]) / "[Maker] Squad" / "Head"
        self.assertTrue((folder / "Arm.stl").is_file())
        self.assertTrue((folder / "Leg.stl").is_file())

    def test_a_stored_listing_header_row_doesnt_stop_the_export(self):
        self.build({"Minis/Arm.stl": b"a"})
        with self.conn:
            self.conn.execute("""
                INSERT INTO file_archive_members (submodel_id, file_id, internal_path, filename, size_bytes)
                VALUES (201, 500, '/Volumes/STL/Vault/pack.rar', 'pack.rar', 0)
            """)
        result = self.exporter().export_stash(stash_id=1, conn=self.conn)
        self.assertEqual(result["total_files"], 1)

    def test_a_member_climbing_out_stops_the_export(self):
        self.build({"Minis/Arm.stl": b"a"})
        with self.conn:
            self.conn.execute("UPDATE file_archive_members SET internal_path = '../../evil.stl'")
        with self.assertRaises(external.ExtractionRefused):
            self.exporter().export_stash(stash_id=1, conn=self.conn)

    def test_an_export_never_overwrites(self):
        exporter = self.exporter()
        archive = self.build({"a/Head.stl": b"1"})
        taken = self.base / "taken.stl"
        taken.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            exporter._surgical_extract_single_member(archive, "a/Head.stl", self.base / "work", taken)
        self.assertEqual(taken.read_bytes(), b"keep")

    def test_a_rar_unpack_keeps_every_same_named_file(self):
        def fake_extract_all(_extension, _archive, dest, _unrar, _seven_zip):
            for folder, data in (("Supported", b"S"), ("Unsupported", b"U")):
                (dest / folder).mkdir()
                (dest / folder / "Head.stl").write_bytes(data)

        dest, scratch = self.base / "dest", self.base / "scratch"
        scratch.mkdir()
        with patch("desync_archival.external.extract_all", side_effect=fake_extract_all):
            names, total = self.exporter()._unpack_archive_to_dest(Path("a.rar"), dest, scratch, "all")
        self.assertEqual(len(names), 2)
        self.assertEqual(total, 2)
        self.assertEqual(sorted(path.read_bytes() for path in dest.iterdir()), [b"S", b"U"])


class R03Extraction(TempCase):
    def zipped(self, members):
        archive = self.base / "pack.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            for path, data in members.items():
                handle.writestr(path, data)
        return archive

    def folder(self):
        path = self.base / "pass"
        path.mkdir()
        return path

    def test_exact_paths_come_first_and_wildcards_need_asking_for(self):
        tool = self.base / "tool"
        tool.write_bytes(b"")
        exact = external.member_commands(".rar", str(tool), str(tool), Path("/a.rar"), "x/Head.stl", "Head.stl", self.base)
        fuzzy = external.member_commands(".rar", str(tool), str(tool), Path("/a.rar"), "x/Head.stl", "Head.stl", self.base, fuzzy=True)
        self.assertFalse([part for cmd in exact for part in cmd if "*" in part])
        self.assertTrue([part for cmd in fuzzy for part in cmd if "*" in part])

    def test_the_exact_member_is_returned(self):
        archive = self.zipped({"Supported/Head.stl": b"S", "Unsupported/Head.stl": b"U"})
        found = external.extract_member(archive, "Unsupported/Head.stl", self.folder(), None, None)
        self.assertEqual(found.read_bytes(), b"U")

    def test_a_name_match_is_refused_when_the_name_repeats(self):
        archive = self.zipped({"Supported/Head.stl": b"S", "Unsupported/Head.stl": b"U"})
        with self.assertRaises(external.ExtractionRefused):
            external.extract_member(archive, "Renamed/Head.stl", self.folder(), None, None)

    def test_a_name_match_is_allowed_when_it_is_unique(self):
        archive = self.zipped({"Elsewhere/Head.stl": b"E"})
        found = external.extract_member(archive, "Renamed/Head.stl", self.folder(), None, None)
        self.assertEqual(found.read_bytes(), b"E")

    def test_the_wrong_size_is_refused(self):
        archive = self.zipped({"a/Head.stl": b"short"})
        with self.assertRaisesRegex(external.ExtractionRefused, "the catalog says 99"):
            external.extract_member(archive, "a/Head.stl", self.folder(), None, None, expected_size=99)

    def test_a_tool_is_not_asked_to_guess_a_repeated_name(self):
        archive = self.base / "pack.rar"
        archive.write_bytes(b"")
        listing = [external.ArchiveMember("a/Head.stl", 1), external.ArchiveMember("b/Head.stl", 1)]
        with patch("desync_archival.external.list_members", return_value=listing), \
                patch("desync_archival.external.member_commands", return_value=[["tool"]]) as commands, \
                patch("desync_archival.external.run", return_value=MagicMock(stderr="", stdout="")), \
                self.assertRaises(external.ExtractionRefused):
            external.extract_member(archive, "c/Head.stl", self.folder(), "tool", None)
        self.assertEqual([call.args[7] for call in commands.call_args_list], [False], "only exact attempts may run")

    @unittest.skipUnless(SEVEN_ZIP, "needs 7-Zip")
    def test_a_real_archive_gives_back_the_copy_asked_for(self):
        source = self.base / "src"
        for folder, data in (("Supported", b"S" * 50), ("Unsupported", b"U" * 30)):
            (source / folder).mkdir(parents=True)
            (source / folder / "Head.stl").write_bytes(data)
        archive = self.base / "pack.7z"
        subprocess.run([SEVEN_ZIP, "a", str(archive), "Supported", "Unsupported"], cwd=source, check=True, capture_output=True)
        for member, expected in (("Supported/Head.stl", b"S" * 50), ("Unsupported/Head.stl", b"U" * 30)):
            folder = self.base / f"pass_{expected[:1].decode()}"
            folder.mkdir()
            self.assertEqual(external.extract_member(archive, member, folder, None, SEVEN_ZIP).read_bytes(), expected)

    def test_same_named_previews_stay_two_pictures(self):
        archive = self.zipped({"a/render.jpg": b"first", "b/render.jpg": b"second", "c/other.jpg": b"third"})
        peeker = ArchivePeeker.__new__(ArchivePeeker)
        dest = self.base / "previews"
        dest.mkdir()
        found = peeker._extract_images(archive, ["a/render.jpg", "b/render.jpg", "c/other.jpg"], dest, "zip")
        self.assertEqual(found["a/render.jpg"].read_bytes(), b"first")
        self.assertEqual(found["b/render.jpg"].read_bytes(), b"second")
        self.assertEqual(found["c/other.jpg"].name, "other.jpg", "a unique name must not change")

    def test_export_paths_refuse_a_member_climbing_out(self):
        with self.assertRaises(external.ExtractionRefused):
            external.export_paths(["../../etc/Head.stl"])

    def test_a_zip_member_past_the_limit_is_refused(self):
        archive = self.zipped({"a/big.stl": b"x" * 100})
        folder = self.folder()
        with patch("desync_archival.external.MAX_EXTRACTED_BYTES", 10), self.assertRaises(external.ExtractionRefused):
            external.extract_member(archive, "a/big.stl", folder, None, None)
        self.assertEqual(list(folder.iterdir()), [])


class BackupCase(TempCase):
    def setUp(self):
        super().setUp()
        self.folder = self.base / "backups"
        self.folder.mkdir()
        for target, value in (("backup_dir", self.folder), ("app_config.backup_mirror_dir", None)):
            patcher = patch(f"desync_archival.backup.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class R06Backups(BackupCase):
    def tasks(self):
        tasks = object.__new__(TaskManager)
        tasks.db_path = self.db
        tasks._lock = threading.Lock()
        tasks.active_task_id = None
        tasks._finalize_task = MagicMock()
        tasks.start_task = MagicMock(return_value={"success": True})
        return tasks

    def test_a_write_during_the_backup_doesnt_fail_it(self):
        real_copy = backup._copy_database

        def copy_then_write(source, target, journal_mode):
            real_copy(source, target, journal_mode)
            with self.conn:
                self.conn.execute("INSERT INTO audit_log (action, dry_run, outcome) VALUES ('download', 0, 'ok')")

        with patch("desync_archival.backup._copy_database", side_effect=copy_then_write):
            self.assertTrue(backup.create_backup(self.db).is_file())

    def test_a_damaged_copy_is_still_refused(self):
        damaged = patch("desync_archival.backup.verify", side_effect=backup.BackupError("Integrity check failed"))
        with damaged, self.assertRaises(backup.BackupError):
            backup.create_backup(self.db)
        self.assertEqual(list(self.folder.glob("*.db")), [])

    def test_a_failed_backup_keeps_the_last_good_date(self):
        self.setting(last_backup_epoch=1000)
        with patch("desync_archival.services.task_manager.backup.create_backup", side_effect=backup.BackupError("no")):
            self.tasks()._run_backup_worker(1, {})
        self.assertEqual(auth_service.get_setting(self.conn, "last_backup_epoch"), "1000")
        self.assertTrue(auth_service.get_setting(self.conn, "last_backup_result").startswith("Failed"))

    def test_a_failed_backup_tries_again_in_hours_not_a_month(self):
        tasks = self.tasks()
        self.setting(last_backup_epoch=0, last_backup_attempt_epoch=time.time())
        self.assertFalse(tasks.run_scheduled_backup(), "it should wait after a fresh failure")
        self.setting(last_backup_attempt_epoch=time.time() - jobs.BACKUP_RETRY_INTERVAL - 1)
        self.assertTrue(tasks.run_scheduled_backup())

    def test_a_good_backup_restarts_the_month(self):
        tasks = self.tasks()
        with patch("desync_archival.services.task_manager.backup.create_backup", return_value=self.folder / "x.db"):
            tasks._run_backup_worker(1, {})
        self.assertGreater(float(auth_service.get_setting(self.conn, "last_backup_epoch")), 0)
        self.assertFalse(tasks.run_scheduled_backup())

    def test_health_reports_a_failure_even_with_no_good_backup_yet(self):
        self.setting(last_backup_result="Failed: OSError", last_backup_attempt_epoch=time.time())
        self.assertEqual(health._backup(self.conn)["state"], health.PROBLEM)


class R07Retention(BackupCase):
    def name(self, day):
        return f"library-202601{day:02d}-120000-000000.db"

    def fill(self, days, full):
        for day in days:
            path = self.folder / self.name(day)
            path.write_bytes(b"x")
            if full:
                backup.metadata_copy(path).write_bytes(b"m")

    def test_quick_copies_cant_push_out_full_backups(self):
        self.fill(range(1, 4), full=True)
        self.fill(range(4, 29), full=False)
        backup.prune(self.folder, 14)
        for day in range(1, 4):
            self.assertTrue((self.folder / self.name(day)).exists())
        self.assertEqual(len(list(self.folder.glob("*.db"))), 3 + 14)

    def test_full_backups_keep_their_own_limit(self):
        self.fill(range(1, 21), full=True)
        backup.prune(self.folder, 14)
        self.assertEqual(len(list(self.folder.glob("*.db"))), 14)
        self.assertFalse((self.folder / self.name(1)).exists())
        self.assertFalse(backup.metadata_copy(self.folder / self.name(1)).exists())

    def test_a_new_full_backup_counts_as_full_when_pruning(self):
        metadata = self.base / "metadata"
        metadata.mkdir()
        (metadata / "model_1.json").write_text("{}")
        with patch("desync_archival.backup.app_config.metadata_dir", return_value=metadata):
            self.fill(range(1, 15), full=True)
            newest = backup.create_backup(self.db, keep=14, full=True)
        self.assertTrue(newest.exists())
        self.assertEqual(len(list(self.folder.glob("*.metadata.tar.gz"))), 14)


class R04Workers(DownloadCase):
    def run_loop(self, loop, target):
        calls = []

        def fails_then_stops(*_):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            self.dm.is_running = False

        self.dm.is_running = True
        with patch.object(self.dm, target, side_effect=fails_then_stops), patch("time.sleep"):
            loop(1)
        return len(calls)

    def test_a_download_worker_survives_a_database_error(self):
        self.assertEqual(self.run_loop(self.dm._download_worker_loop, "_claim_next_download_job"), 2)

    def test_an_offload_worker_survives_a_database_error(self):
        self.assertEqual(self.run_loop(self.dm._offload_worker_loop, "_claim_next_offload_job"), 2)

    def test_health_shows_a_dead_worker(self):
        self.dm.is_running = True
        self.dm.workers = [threading.Thread(target=lambda: None)]
        self.dm.offloader_workers = []
        check = health._workers(self.dm)
        self.dm.is_running = False
        self.assertEqual(check["state"], health.PROBLEM)
        self.assertIn("0 of 1", check["detail"])


class R05Settings(TempCase):
    NUMERIC = (
        "worker_concurrency", "offloader_concurrency", "batch_size",
        "batch_cooldown_seconds", "stream_chunk_size_mb", "offload_chunk_size_mb",
    )

    def test_junk_in_any_numeric_setting_cant_stop_startup(self):
        for key in self.NUMERIC:
            for junk in ("abc", "", "nan", "inf", "-inf"):
                with self.subTest(key=key, junk=junk):
                    self.setting(**{key: junk})
                    manager = DownloadManager(self.db)
                    self.assertGreaterEqual(manager.worker_concurrency, 1)
                    self.assertGreater(manager.stream_chunk_size, 0)
            self.setting(**{key: "1"})

    def test_a_bad_value_falls_back_to_the_default(self):
        self.setting(worker_concurrency="abc")
        self.assertEqual(DownloadManager(self.db).worker_concurrency, 2)

    def test_the_route_refuses_bad_numbers_and_saves_nothing(self):
        create_database(server.DB_PATH)
        client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
        conn = get_db_connection(server.DB_PATH)
        self.addCleanup(conn.close)
        auth_service.set_settings(conn, {"worker_concurrency": "2"})
        for body in ({"worker_concurrency": "abc"}, {"worker_concurrency": 99}, {"stream_chunk_size_mb": 0}):
            with self.subTest(body=body):
                self.assertEqual(client.post("/api/settings", json=body).status_code, 400)
        self.assertEqual(auth_service.get_setting(conn, "worker_concurrency"), "2")
        self.assertEqual(client.post("/api/settings", json={"worker_concurrency": "3"}).status_code, 200)
        self.assertEqual(auth_service.get_setting(conn, "worker_concurrency"), "3")
        auth_service.set_settings(conn, {"worker_concurrency": "2"})


class R10Logging(TempCase):
    def setUp(self):
        super().setUp()
        saved = log.get_logging_config()
        self.addCleanup(log.set_logging_config, saved["level"], saved["layers"])
        log.set_logging_config("INFO", [])

    def lines(self):
        return "\n".join(log.get_ui_logs(1000))

    def test_an_error_gets_through_with_its_layer_switched_off(self):
        log.log_layer("storage", logging.ERROR, "probe-error-line")
        self.assertIn("probe-error-line", self.lines())

    def test_a_warning_gets_through_too(self):
        log.log_layer("http", logging.WARNING, "probe-warning-line")
        self.assertIn("probe-warning-line", self.lines())

    def test_routine_lines_still_wait_for_their_layer(self):
        log.log_layer("storage", logging.INFO, "probe-info-line")
        self.assertNotIn("probe-info-line", self.lines())

    def test_saved_layers_come_back_at_startup(self):
        create_database(server.DB_PATH)
        conn = get_db_connection(server.DB_PATH)
        self.addCleanup(conn.close)
        auth_service.set_settings(conn, {"log_level": "INFO", "log_layers": "storage,http"})
        self.addCleanup(auth_service.set_settings, conn, {"log_level": "INFO", "log_layers": ""})
        with patch.object(server, "TaskManager"), patch.object(server, "DownloadManager"), \
                patch.object(server.image_worker, "ImageWorker"), patch.object(server.threading, "Thread"):
            server.start_managers()
        self.assertEqual(log.get_logging_config()["layers"], ["http", "storage"])


class RouteCase(TempCase):
    def setUp(self):
        super().setUp()
        self.vault = self.base / "vault"
        self.work = self.base / "work"
        self.stash = self.base / "stash"
        for folder in (self.vault, self.work / "in_flight", self.stash):
            folder.mkdir(parents=True)
        create_database(server.DB_PATH)
        self.live = get_db_connection(server.DB_PATH)
        self.addCleanup(self.live.close)
        for table in ("audit_log", "pipeline_tasks", "download_jobs", "deleted_files", "downloaded_files", "collections", "releases"):
            self.live.execute(f"DELETE FROM {table}")
        self.live.commit()
        auth_service.set_settings(self.live, {
            "download_root_path": str(self.vault), "download_work_path": str(self.work),
            "stash_download_path": str(self.stash),
        })
        self.addCleanup(
            auth_service.set_settings, self.live,
            {"download_root_path": "", "download_work_path": "", "stash_download_path": ""},
        )
        self.client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")

    def add_collection(self, model_id, path="", key=None):
        with self.live:
            self.live.execute("""
                INSERT INTO collections (id, name, url, raw_backup_path, source, external_key, vault_path)
                VALUES (?, 'Model', '', '', 'mmf', ?, ?)
            """, (model_id, key or str(model_id), str(path)))

    def audit(self):
        return [tuple(row)[:2] for row in self.live.execute("SELECT action, dry_run FROM audit_log ORDER BY id")]


class S02OrphanDeletes(RouteCase):
    def orphan(self, name="999_Gone"):
        folder = self.vault / "Maker" / "2024-01" / name
        (folder / "files").mkdir(parents=True)
        (folder / "files" / "a.stl").write_bytes(b"12345")
        return folder

    def delete(self, path):
        return self.client.post("/api/reconcile/delete-orphans", json={"paths": [str(path)], "confirm": True}).json()

    def test_an_orphan_is_held_not_erased(self):
        folder = self.orphan()
        data = self.delete(folder)
        self.assertEqual(data["deleted_count"], 1)
        self.assertFalse(folder.exists())
        held = Path(self.live.execute("SELECT pending_path FROM deleted_files").fetchone()[0])
        self.assertEqual((held / "files" / "a.stl").read_bytes(), b"12345")

    def test_a_held_orphan_can_be_restored(self):
        folder = self.orphan()
        self.delete(folder)
        tombstone = self.live.execute("SELECT id FROM deleted_files").fetchone()[0]
        result = pending_deletion.restore_file(self.live, tombstone)
        self.assertTrue(result["success"])
        self.assertEqual((folder / "files" / "a.stl").read_bytes(), b"12345")

    def test_a_held_orphan_is_purged_only_after_the_wait(self):
        self.delete(self.orphan())
        tombstone, pending = self.live.execute("SELECT id, pending_path FROM deleted_files").fetchone()
        early = pending_deletion.purge(self.live, confirm=True, ids=[tombstone])
        self.assertEqual(early["purged"], 0)
        stale = time.time() - (pending_deletion.WAIT_DAYS + 1) * 86400
        os.utime(pending, (stale, stale))
        late = pending_deletion.purge(self.live, confirm=True, ids=[tombstone])
        self.assertEqual(late["purged"], 1)
        self.assertFalse(Path(pending).exists())

    def test_a_folder_the_catalog_uses_is_refused(self):
        folder = self.orphan("123_Kept")
        self.add_collection(5, folder)
        data = self.delete(folder)
        self.assertEqual(data["deleted_count"], 0)
        self.assertIn("catalog", data["errors"][0])
        self.assertTrue(folder.exists())

    def test_a_creator_folder_holding_a_catalogued_model_is_refused(self):
        folder = self.orphan("123_Kept")
        self.add_collection(5, folder)
        self.assertEqual(self.delete(self.vault / "Maker")["deleted_count"], 0)
        self.assertTrue(folder.exists())

    def test_a_model_added_after_the_audit_is_refused(self):
        folder = self.orphan("777_Late")
        self.add_collection(5, "", key="777")
        self.assertEqual(self.delete(folder)["deleted_count"], 0)
        self.assertTrue(folder.exists())

    def test_a_catalog_row_at_the_vault_root_doesnt_protect_every_orphan(self):
        self.add_collection(5, self.vault)
        folder = self.orphan()
        self.assertEqual(self.delete(folder)["deleted_count"], 1)

    def test_the_two_fixed_folders_are_refused(self):
        for name in (IMPORT_FOLDER, PENDING_DELETION_FOLDER):
            with self.subTest(name=name):
                folder = self.vault / name / "thing"
                folder.mkdir(parents=True)
                self.assertEqual(self.delete(folder)["deleted_count"], 0)
                self.assertEqual(self.delete(self.vault / name)["deleted_count"], 0)
                self.assertTrue(folder.exists())


class S02StashPurge(RouteCase):
    def export_folder(self, name, manifest=True):
        folder = self.stash / name
        folder.mkdir()
        (folder / "a.stl").write_bytes(b"1234")
        if manifest:
            (folder / "manifest.json").write_text("{}")
        stale = time.time() - 30 * 86400
        os.utime(folder, (stale, stale))
        return folder

    def purge(self, **body):
        return self.client.post("/api/stashes/purge", json={"confirm": True, **body})

    def test_only_folders_with_a_manifest_are_purged(self):
        export = self.export_folder("export")
        other = self.export_folder("not_an_export", manifest=False)
        data = self.purge(days=14).json()
        self.assertEqual((data["purged_count"], data["skipped_no_manifest"]), (1, 1))
        self.assertFalse(export.exists())
        self.assertTrue(other.exists())

    def test_zero_or_negative_days_are_refused(self):
        export = self.export_folder("export")
        for days in (0, -5):
            self.assertEqual(self.purge(days=days).status_code, 400)
        self.assertTrue(export.exists())

    def test_a_stash_path_on_the_vault_is_refused(self):
        kept = self.vault / "Maker"
        kept.mkdir()
        (kept / "manifest.json").write_text("{}")
        stale = time.time() - 30 * 86400
        os.utime(kept, (stale, stale))
        auth_service.set_settings(self.live, {"stash_download_path": str(self.vault)})
        self.assertEqual(self.purge(days=14).status_code, 400)
        self.assertTrue(kept.exists())


class S03Realign(RouteCase):
    def setUp(self):
        super().setUp()
        self.current = self.vault / "old_name"
        (self.current / "files").mkdir(parents=True)
        (self.current / "files" / "a.stl").write_bytes(b"1")
        self.canonical = self.vault / "Maker" / "5_Model"
        self.add_collection(5, self.current)
        items = [{"model_id": 5, "current_path": str(self.current), "canonical_path": str(self.canonical)}]
        patcher = patch(
            "desync_archival.services.reconciler.VaultReconciler.scan_path_drift",
            return_value={"success": True, "items": items},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, **body):
        return self.client.post("/api/reconcile/drift/realign", json=body)

    def test_the_default_is_a_dry_run_that_moves_nothing(self):
        data = self.post(model_ids=[5]).json()
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["would_move"][0]["canonical_path"], str(self.canonical))
        self.assertTrue(self.current.exists())
        self.assertEqual(self.audit(), [("path_realign", 1)])

    def test_confirm_needs_a_list_of_models(self):
        for body in ({"confirm": True}, {"confirm": True, "model_ids": []}):
            self.assertEqual(self.post(**body).status_code, 400)
        self.assertTrue(self.current.exists())

    def test_confirm_runs_as_a_job_and_is_audited(self):
        with patch.object(TaskManager, "start_task", return_value={"success": True, "task_id": 44}) as start:
            data = self.post(model_ids=[5], confirm=True).json()
        self.assertEqual(data["task_id"], 44)
        start.assert_called_once_with("path_realign", {"model_ids": [5]})
        self.assertEqual(self.audit(), [("path_realign", 0)])

    def test_confirm_waits_for_running_work(self):
        with patch("desync_archival.services.jobs.busy_reason", return_value="Downloads are running."):
            self.assertEqual(self.post(model_ids=[5], confirm=True).status_code, 409)
        self.assertTrue(self.current.exists())

    def reconciler(self):
        return VaultReconciler(server.DB_PATH)

    def test_an_existing_target_is_refused_rather_than_nested_inside(self):
        self.canonical.mkdir(parents=True)
        result = self.reconciler().realign_model_paths([5])
        self.assertEqual(result["realigned_count"], 0)
        self.assertIn("already exists", result["errors"][0])
        self.assertEqual(list(self.canonical.iterdir()), [])
        self.assertTrue(self.current.exists())

    def test_a_good_move_updates_the_catalog(self):
        self.assertEqual(self.reconciler().realign_model_paths([5])["realigned_count"], 1)
        self.assertTrue((self.canonical / "files" / "a.stl").exists())
        self.assertEqual(self.live.execute("SELECT vault_path FROM collections WHERE id = 5").fetchone()[0], str(self.canonical))

    def test_a_failed_catalog_update_puts_the_folder_back(self):
        queries = "desync_archival.services.reconciler.queries.UPDATE_COLLECTIONS_VAULT_PATH_BY_ID"
        with patch(queries, "UPDATE no_such_table SET x = ?, y = ?"):
            result = self.reconciler().realign_model_paths([5])
        self.assertEqual(result["realigned_count"], 0)
        self.assertTrue(self.current.exists())
        self.assertFalse(self.canonical.exists())


class S01Cookies(DownloadCase):
    def setUp(self):
        super().setUp()
        self.dm.cookies = {"PHPSESSID": "SECRET"}
        self.seen = []

    def redirecting(self, request):
        if "/api/v2/objects" in request.url.path:
            return httpx.Response(404)
        self.seen.append((request.url.host, request.headers.get("cookie"), request.headers.get("authorization")))
        if request.url.host == "www.myminifactory.com":
            return httpx.Response(302, headers={"location": "https://cdn.example-storage.net/f"})
        return httpx.Response(200, content=self.CONTENT)

    def use_url(self, url):
        with self.conn:
            self.conn.execute("UPDATE model_files SET download_url = ?", (url,))

    def test_cookies_stop_at_mmf(self):
        self.stage(self.redirecting)
        self.assertEqual(self.seen[0], ("www.myminifactory.com", "PHPSESSID=SECRET", None))
        self.assertEqual(self.seen[1], ("cdn.example-storage.net", None, None))

    def test_the_token_goes_to_mmf_and_stops_at_the_redirect(self):
        self.dm.access_token = "TOKEN"
        self.stage(self.redirecting)
        downloads = [row for row in self.seen if row[0] == "cdn.example-storage.net"]
        self.assertEqual(downloads[0][2], None)
        self.assertIn(("www.myminifactory.com", "PHPSESSID=SECRET", "Bearer TOKEN"), self.seen)

    def test_a_url_on_another_host_gets_no_token(self):
        self.dm.access_token = "TOKEN"
        self.use_url("https://cdn.example-storage.net/f")
        self.stage(self.redirecting)
        self.assertEqual(self.seen[-1][1:], (None, None))

    def test_a_plain_http_url_is_refused(self):
        self.use_url("http://www.myminifactory.com/f")
        with self.assertRaisesRegex(RuntimeError, "isn't https"):
            self.stage(self.redirecting)

    def test_a_redirect_to_another_host_is_logged_without_its_url(self):
        saved = log.get_logging_config()
        self.addCleanup(log.set_logging_config, saved["level"], saved["layers"])
        log.set_logging_config("INFO", ["http"])
        self.stage(self.redirecting)
        lines = "\n".join(log.get_ui_logs(1000))
        self.assertIn("redirected to cdn.example-storage.net", lines)
        self.assertNotIn("/f", lines.split("redirected to")[-1].split("\n")[0])

    def test_the_ingest_client_gets_bound_cookies_too(self):
        jar = auth_service.mmf_cookies({"a": "b"})
        self.assertEqual([cookie.domain for cookie in jar.jar], [".myminifactory.com"])

    def test_the_browser_user_agent_stays(self):
        agents = []

        def reply(request):
            agents.append(request.headers["user-agent"])
            return httpx.Response(200, content=self.CONTENT)

        self.stage(reply)
        self.assertIn("Chrome/130", agents[0], "S-15: the owner kept this on purpose")


class ReleaseCase(TempCase):
    def add(self, model_id, designer="Maker", designer_id=7, month="2024-01", source="mmf", files=0, tags=()):
        with self.conn:
            self.conn.execute("""
                INSERT INTO collections (id, name, url, raw_backup_path, designer_name, designer_id, release_month, source)
                VALUES (?, 'Model', '', '', ?, ?, ?, ?)
            """, (model_id, designer, designer_id, month, source))
            for number in range(files):
                self.conn.execute("INSERT INTO model_files (model_id, filename, size_bytes) VALUES (?, 'f', 100)", (model_id,))
            for tag in tags:
                self.conn.execute("INSERT INTO collection_tags (model_id, tag) VALUES (?, ?)", (model_id, tag))

    def rows(self):
        run_startup_analytics(self.conn)
        return {row["release_key"]: row for row in self.conn.execute("SELECT * FROM releases")}


class R11ReleaseFigures(ReleaseCase):
    def test_a_file_counts_once_whatever_its_tags(self):
        self.add(1, files=2, tags=("a", "b", "c"))
        row = self.rows()["7_2024-01"]
        self.assertEqual((row["total_files"], row["total_size_bytes"]), (2, 200))
        self.assertEqual(set(row["common_tags"].split(",")), {"a", "b", "c"})

    def test_a_release_adds_up_across_models(self):
        self.add(1, files=2, tags=("a", "b"))
        self.add(2, files=3, tags=("b",))
        row = self.rows()["7_2024-01"]
        self.assertEqual((row["model_count"], row["total_files"]), (2, 5))

    def test_a_rebuild_leaves_linked_library_releases_alone(self):
        with self.conn:
            self.conn.execute("""
                INSERT INTO releases (release_key, designer_name, designer_id, release_month, model_count, total_files, total_size_bytes)
                VALUES ('external_Loot_2020-09', 'Loot', 0, '2020-09', 3, 0, 0)
            """)
        self.add(1, designer="Loot", designer_id=None, month="2020-09", source="external", files=4)
        self.add(2, files=1)
        self.assertEqual(sorted(self.rows()), ["7_2024-01", "external_Loot_2020-09"])

    def test_two_designers_with_no_id_in_one_month_dont_stop_the_rebuild(self):
        self.add(1, designer="First", designer_id=None)
        self.add(2, designer="Second", designer_id=None)
        self.assertEqual(sorted(self.rows()), ["name:First_2024-01", "name:Second_2024-01"])


class R11RebuildCommand(ReleaseCase):
    def run_command(self, *flags, production=False):
        with patch("sys.argv", ["dbtool", "rebuild-releases", *flags]), \
                patch("desync_archival.dbtool.settings.db_path", return_value=self.db), \
                patch("desync_archival.dbtool.settings.is_production", return_value=production), \
                patch("desync_archival.dbtool.refuse_if_app_running"), \
                patch("desync_archival.dbtool.backup.create_backup", return_value="copy") as copy:
            return dbtool.main(), copy

    def stale_release(self):
        self.add(1, files=2, tags=("a", "b", "c"))
        with self.conn:
            self.conn.execute("""
                INSERT INTO releases (release_key, designer_name, designer_id, release_month, model_count, total_files, total_size_bytes)
                VALUES ('7_2024-01', 'Maker', 7, '2024-01', 1, 6, 600)
            """)

    def stored_files(self):
        return self.conn.execute("SELECT total_files FROM releases WHERE release_key = '7_2024-01'").fetchone()[0]

    def test_it_backs_up_then_corrects_the_stored_figures(self):
        self.stale_release()
        result, copy = self.run_command()
        self.assertEqual(result, 0)
        copy.assert_called_once()
        self.assertEqual(self.stored_files(), 2)

    def test_production_needs_the_confirm_flag(self):
        self.stale_release()
        self.assertEqual(self.run_command(production=True)[0], 1)
        self.assertEqual(self.stored_files(), 6)
        self.assertEqual(self.run_command("--confirm-production", production=True)[0], 0)
        self.assertEqual(self.stored_files(), 2)


class R12ReleaseQueue(RouteCase):
    def add_model(self, model_id, designer_id=362414, designer="Maker", month="2021-05", source="mmf"):
        with self.live:
            self.live.execute("""
                INSERT INTO collections (id, name, url, raw_backup_path, designer_name, designer_id, release_month, source)
                VALUES (?, 'Model', '', '', ?, ?, ?, ?)
            """, (model_id, designer, designer_id, month, source))

    def job(self, model_id, status):
        with self.live:
            self.live.execute("INSERT INTO download_jobs (model_id, status) VALUES (?, ?)", (model_id, status))

    def statuses(self):
        return dict(self.live.execute("SELECT model_id, status FROM download_jobs ORDER BY model_id").fetchall())

    def queue(self, key):
        return self.client.post("/api/downloads/queue", json={"release_id": key})

    def test_queue_entire_pack_finds_the_release(self):
        self.add_model(1)
        self.add_model(2)
        self.add_model(3, month="2021-06")
        data = self.queue("362414_2021-05").json()
        self.assertEqual(data["queued_count"], 2)
        self.assertEqual(self.statuses(), {1: "pending", 2: "pending"})

    def test_a_designer_with_no_id_can_be_queued(self):
        self.add_model(1, designer_id=None)
        self.assertEqual(self.queue("name:Maker_2021-05").json()["queued_count"], 1)

    def test_active_and_finished_jobs_are_left_alone(self):
        for model_id, status in ((1, "downloading"), (2, "completed"), (3, "offloading"), (4, "failed")):
            self.add_model(model_id)
            self.job(model_id, status)
        self.add_model(5)
        self.assertEqual(self.queue("362414_2021-05").json()["queued_count"], 2)
        self.assertEqual(
            self.statuses(),
            {1: "downloading", 2: "completed", 3: "offloading", 4: "pending", 5: "pending"},
        )

    def test_a_linked_library_release_queues_nothing(self):
        self.add_model(1, designer_id=None, designer="Loot", source="external")
        self.assertEqual(self.queue("external_Loot_2021-05").status_code, 400)
        self.assertEqual(self.statuses(), {})

    def test_cancel_removes_only_waiting_jobs_of_that_release(self):
        self.add_model(1)
        self.add_model(2)
        self.add_model(3, designer_id=99)
        self.job(1, "pending")
        self.job(2, "downloading")
        self.job(3, "pending")
        response = self.client.request("DELETE", "/api/downloads/queue", json={"release_id": "362414_2021-05"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.statuses(), {2: "downloading", 3: "pending"})


if __name__ == "__main__":
    unittest.main()
