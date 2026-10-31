"""The library scan: preview, approval, inventory, resume, and what a failed read must never do."""

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database

from desync_archival.services import external_indexer, image_worker, inactive_log
from desync_archival.services.external_indexer import (
    ExternalLibraryIndexer,
    ScanReadError,
    key_for,
)
from desync_archival.services.image_worker import ImageWorker

SOLID = b"solid x\nendsolid\n"


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.db = self.base / "library.db"
        self.root = self.base / "Loot"
        self.previews = self.base / "previews"
        for name in ("Archmage", "Golem"):
            self.add_model("2024-01 - Drop", name)
        self.add_model("2024-02 - Next", "Dragon")
        create_database(self.db)
        conn = self.connect()
        with conn:
            conn.execute("INSERT INTO external_libraries (id, name, path, default_creator) VALUES (1, 'Loot', ?, 'Loot Studios')", (str(self.root),))
        conn.close()
        patcher = patch("desync_archival.services.external_indexer.app_config.previews_dir", return_value=self.previews)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.indexer = ExternalLibraryIndexer(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def add_model(self, release, name, filename="body.stl"):
        folder = self.root / release / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / filename).write_bytes(SOLID)
        return folder

    def rows(self, sql, *args):
        conn = self.connect()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def count(self, sql, *args):
        return self.rows(sql, *args)[0][0]

    def scan_everything(self):
        return self.indexer.scan_and_index_library(1)

    def model_count(self):
        return self.count("SELECT COUNT(*) FROM models WHERE source = 'external'")


class PreviewTests(ScanCase):
    def test_a_preview_writes_no_catalog_rows(self):
        result = self.indexer.preview_library(1)
        self.assertEqual(result["new"], 3)
        self.assertEqual(self.model_count(), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_candidates WHERE status = 'proposed'"), 3)

    def test_nothing_is_added_without_approval(self):
        self.indexer.preview_library(1)
        self.assertEqual(self.indexer.add_approved(1)["models_added"], 0)
        self.assertEqual(self.model_count(), 0)

    def test_a_skipped_release_stays_out(self):
        self.indexer.preview_library(1)
        conn = self.connect()
        self.assertEqual(self.indexer.approve_candidates(conn, 1, ["2024-01 - Drop"]), {"approved": 1, "skipped": 2})
        conn.close()
        self.indexer.add_approved(1)
        self.assertEqual([r[0] for r in self.rows("SELECT name FROM models WHERE source = 'external'")], ["Dragon"])

    def test_known_folders_are_never_reclassified(self):
        conn = self.connect()
        with conn:
            conn.execute(
                "INSERT INTO models (name, source, library_id, external_key, vault_path, raw_backup_path) VALUES ('Old', 'external', 1, ?, ?, '')",
                (key_for(1, "2024-01 - Drop"), str(self.root / "2024-01 - Drop")),
            )
        conn.close()
        self.assertEqual(self.indexer.preview_library(1)["new"], 1)
        self.assertEqual(self.rows("SELECT rel_path FROM library_candidates")[0][0], "2024-02 - Next/dragon")

    def test_a_new_folder_is_found_and_added_alone(self):
        self.scan_everything()
        before = {r[0]: r[1] for r in self.rows("SELECT name, id FROM models WHERE source = 'external'")}
        self.add_model("2024-01 - Drop", "Newcomer")

        self.assertEqual(self.indexer.preview_library(1)["new"], 1)
        conn = self.connect()
        self.indexer.approve_candidates(conn, 1, [])
        conn.close()
        self.assertEqual(self.indexer.add_approved(1)["models_added"], 1)

        after = {r[0]: r[1] for r in self.rows("SELECT name, id FROM models WHERE source = 'external'")}
        self.assertEqual(set(after) - set(before), {"Newcomer"})
        self.assertEqual({k: after[k] for k in before}, before)


class CharacterLayoutTests(ScanCase):
    """Monthly releases since 2022: scale/category/variant/Character_32mm_Variant."""

    def setUp(self):
        super().setUp()
        base = "April 2024/32mm_Forge/All_Forge_Construct_32mm"
        for variant in ("LYCHEE", "ReadyToSlice", "UnSupported"):
            self.add_model(f"{base}/Forge_Construct_32mm_{variant}", f"BrazenBull_32mm_{variant}", f"bull_{variant}.stl")
            self.add_model(
                f"April 2024/75mm_Forge/All_Forge_Construct_75mm/Forge_Construct_75mm_{variant}",
                f"BrazenBull_75mm_{variant}", f"bull75_{variant}.stl",
            )
        self.add_model(f"{base}/Forge_Construct_32mm_LYCHEE", "ClayGolem_32mm_LYCHEE", "golem.lys")
        self.add_model("April 2024/Forge_Busts/Forge_Bust_LYCHEE", "BrazenBull_Bust_LYCHEE", "bust.stl")
        (self.root / "April 2024" / "Stat_Blocks.zip").write_bytes(b"zip")

    def bull(self):
        return self.rows("SELECT * FROM models WHERE name = 'Brazen Bull'")[0]

    def test_one_character_is_one_model_across_scales_and_variants(self):
        self.scan_everything()
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE name = 'Brazen Bull'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM model_files WHERE model_id = ?", self.bull()["id"]), 6)
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_model_folders WHERE model_id = ?", self.bull()["id"]), 6)

    def test_a_bust_is_its_own_model(self):
        self.scan_everything()
        row = self.rows("SELECT primary_category FROM models WHERE name = 'Brazen Bull Bust'")[0]
        self.assertEqual(row[0], "Busts")

    def test_the_category_comes_from_the_folders(self):
        self.scan_everything()
        self.assertEqual(self.bull()["primary_category"], "Construct")

    def test_the_cover_folder_is_unsupported_32mm(self):
        self.scan_everything()
        self.assertTrue(self.bull()["vault_path"].endswith("BrazenBull_32mm_UnSupported"))

    def test_months_are_read_from_named_releases(self):
        self.scan_everything()
        self.assertEqual(self.bull()["release_month"], "2024-04")

    def test_unsupported_is_not_presupported(self):
        self.assertFalse(external_indexer.is_presupported("BrazenBull_32mm_UnSupported/a.stl"))
        self.assertTrue(external_indexer.is_presupported("BrazenBull_32mm_LYCHEE/a.lys"))
        self.assertTrue(external_indexer.is_presupported("X_Supported_Solid/a.stl"))

    def test_rescanning_a_character_changes_nothing(self):
        self.scan_everything()
        files = self.count("SELECT COUNT(*) FROM model_files")
        self.assertEqual(self.indexer.rescan_known(1)["models_changed"], 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM model_files"), files)
        self.assertEqual(self.indexer.preview_library(1)["new"], 0)

    def test_a_later_scale_extends_the_character(self):
        self.scan_everything()
        model_id = self.bull()["id"]
        self.add_model("April 2024/54mm_Forge/All_Forge_Construct_54mm/Forge_Construct_54mm_UnSupported", "BrazenBull_54mm_UnSupported", "b54.stl")
        self.assertEqual(self.indexer.preview_library(1)["new"], 1)
        conn = self.connect()
        self.indexer.approve_candidates(conn, 1, [])
        conn.close()
        self.indexer.add_approved(1)
        self.assertEqual(self.bull()["id"], model_id)
        self.assertEqual(self.count("SELECT COUNT(*) FROM model_files WHERE model_id = ?", model_id), 7)

    def test_a_lost_variant_keeps_the_model_and_logs_its_files(self):
        self.scan_everything()
        shutil.rmtree(self.root / "April 2024" / "75mm_Forge")
        self.indexer.rescan_known(1)
        self.assertEqual(self.bull()["is_active"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM inactive_log WHERE reason = 'file_missing' AND item_name LIKE 'bull75%'"), 3)

    def test_losing_every_folder_flags_it_inactive(self):
        self.scan_everything()
        shutil.rmtree(self.root / "April 2024")
        self.assertGreaterEqual(self.indexer.rescan_known(1)["models_missing"], 1)
        self.assertEqual(self.bull()["is_active"], 0)

    def test_files_from_every_scale_keep_their_real_paths(self):
        self.scan_everything()
        paths = [r[0] for r in self.rows("SELECT vault_path FROM downloaded_files WHERE model_id = ?", self.bull()["id"])]
        self.assertEqual(len(paths), 6)
        self.assertTrue(all(Path(path).is_file() for path in paths))


class InventoryTests(ScanCase):
    def test_inventory_never_renders_or_opens_archives(self):
        self.add_model("2024-02 - Next", "Boxed", "pack.zip")
        with patch("desync_archival.services.mesh_renderer.MeshRenderer.render_view") as render:
            self.scan_everything()
        render.assert_not_called()
        self.assertEqual(self.count("SELECT COUNT(*) FROM file_archive_submodels"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_work_queue WHERE kind = 'peek'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_work_queue WHERE kind = 'cover'"), 4)

    def test_rescanning_writes_nothing_new(self):
        self.scan_everything()
        counts = [self.count(f"SELECT COUNT(*) FROM {t}") for t in ("models", "model_files", "downloaded_files", "library_work_queue", "model_tags")]
        stamps = self.rows("SELECT id, updated_at FROM models ORDER BY id")
        result = self.indexer.rescan_known(1)
        self.assertEqual(result["models_changed"], 0)
        self.assertEqual([self.count(f"SELECT COUNT(*) FROM {t}") for t in ("models", "model_files", "downloaded_files", "library_work_queue", "model_tags")], counts)
        self.assertEqual([tuple(r) for r in self.rows("SELECT id, updated_at FROM models ORDER BY id")], [tuple(r) for r in stamps])

    def test_a_changed_file_is_picked_up(self):
        self.scan_everything()
        path = self.root / "2024-02 - Next" / "Dragon" / "body.stl"
        path.write_bytes(SOLID * 3)
        self.assertEqual(self.indexer.rescan_known(1)["models_changed"], 1)
        self.assertEqual(self.count("SELECT actual_bytes FROM downloaded_files WHERE vault_path = ?", str(path)), len(SOLID) * 3)

    def test_tags_are_added_and_never_removed(self):
        self.scan_everything()
        model_id = self.count("SELECT id FROM models WHERE name = 'Dragon'")
        conn = self.connect()
        with conn:
            conn.execute("INSERT INTO model_tags (model_id, tag) VALUES (?, 'my tag')", (model_id,))
        conn.close()
        (self.root / "2024-02 - Next" / "Dragon" / "extra.stl").write_bytes(SOLID)
        self.indexer.rescan_known(1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM model_tags WHERE model_id = ? AND tag = 'my tag'", model_id), 1)

    def test_a_changed_mount_path_duplicates_nothing(self):
        self.scan_everything()
        ids = {r[0]: r[1] for r in self.rows("SELECT name, id FROM models WHERE source = 'external'")}
        moved = self.base / "Loot-1"
        self.root.rename(moved)
        conn = self.connect()
        with conn:
            conn.execute("UPDATE external_libraries SET path = ? WHERE id = 1", (str(moved),))
        conn.close()

        self.indexer.rescan_known(1)

        self.assertEqual({r[0]: r[1] for r in self.rows("SELECT name, id FROM models WHERE source = 'external'")}, ids)
        self.assertEqual(self.count("SELECT COUNT(*) FROM downloaded_files WHERE vault_path LIKE ?", f"{moved}/%"), 3)
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE vault_path LIKE ?", f"{moved}/%"), 3)

    def test_rows_without_keys_are_adopted_not_duplicated(self):
        self.scan_everything()
        conn = self.connect()
        with conn:
            conn.execute("UPDATE models SET external_key = NULL WHERE source = 'external'")
            conn.execute("UPDATE model_files SET external_key = NULL WHERE source = 'external'")
        conn.close()
        ids = sorted(r[0] for r in self.rows("SELECT id FROM models WHERE source = 'external'"))
        files = self.count("SELECT COUNT(*) FROM model_files")

        self.scan_everything()

        self.assertEqual(sorted(r[0] for r in self.rows("SELECT id FROM models WHERE source = 'external'")), ids)
        self.assertEqual(self.count("SELECT COUNT(*) FROM model_files"), files)
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE source = 'external' AND external_key IS NULL"), 0)

    def test_a_stopped_add_keeps_its_work_and_resumes(self):
        self.indexer.preview_library(1)
        conn = self.connect()
        self.indexer.approve_candidates(conn, 1, [])
        conn.close()
        calls = iter([False, True])
        stopped = self.indexer.add_approved(1, cancel_cb=lambda: next(calls))
        self.assertTrue(stopped["stopped"])
        self.assertEqual(stopped["models_added"], 1)
        self.assertIsNone(self.rows("SELECT last_scanned_at FROM external_libraries")[0][0])

        resumed = self.indexer.add_approved(1)
        self.assertEqual(resumed["models_added"], 2)
        self.assertEqual(self.model_count(), 3)

    def test_a_stopped_rescan_skips_what_it_finished(self):
        self.scan_everything()
        calls = iter([False, True])
        self.indexer.rescan_known(1, cancel_cb=lambda: next(calls))
        run = self.count("SELECT MAX(id) FROM library_scan_runs WHERE mode = 'full'")
        self.assertEqual(self.rows("SELECT status FROM library_scan_runs WHERE id = ?", run)[0][0], "stopped")

        with patch.object(self.indexer, "_sync_model", wraps=self.indexer._sync_model) as sync:
            result = self.indexer.rescan_known(1)
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(result["models_checked"], 2)
        self.assertEqual(self.count("SELECT MAX(id) FROM library_scan_runs WHERE mode = 'full'"), run)

    def test_stopping_keeps_the_library_totals(self):
        self.scan_everything()
        before = tuple(self.rows("SELECT total_models, last_scanned_at FROM external_libraries")[0])
        self.indexer.rescan_known(1, cancel_cb=lambda: True)
        self.assertEqual(tuple(self.rows("SELECT total_models, last_scanned_at FROM external_libraries")[0]), before)

    def test_only_a_finished_rescan_records_a_scan_time(self):
        self.indexer.preview_library(1)
        self.assertIsNone(self.rows("SELECT last_scanned_at FROM external_libraries")[0][0])
        self.scan_everything()
        self.assertIsNotNone(self.rows("SELECT last_scanned_at FROM external_libraries")[0][0])

    def test_storage_is_never_changed(self):
        before = {str(p): p.stat().st_mtime_ns for p in self.root.rglob("*")}
        self.scan_everything()
        self.assertEqual({str(p): p.stat().st_mtime_ns for p in self.root.rglob("*")}, before)


class MountFailureTests(ScanCase):
    def test_a_read_error_mid_model_changes_nothing(self):
        self.scan_everything()
        before = [tuple(r) for r in self.rows("SELECT id, pre_supported, is_active FROM models ORDER BY id")]
        tags = self.count("SELECT COUNT(*) FROM model_tags")
        with patch.object(external_indexer, "walk_files", side_effect=ScanReadError("gone")):
            result = self.indexer.rescan_known(1)
        self.assertFalse(result["success"])
        self.assertIn("could not be read", result["error"])
        self.assertEqual([tuple(r) for r in self.rows("SELECT id, pre_supported, is_active FROM models ORDER BY id")], before)
        self.assertEqual(self.count("SELECT COUNT(*) FROM model_tags"), tags)

    def test_an_unmounted_library_flags_nothing_inactive(self):
        self.scan_everything()
        shutil.rmtree(self.root)
        result = self.indexer.rescan_known(1)
        self.assertFalse(result["success"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE is_active = 0"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM inactive_log"), 0)

    def test_an_empty_root_counts_as_unmounted(self):
        self.scan_everything()
        shutil.rmtree(self.root)
        self.root.mkdir()
        self.assertFalse(self.indexer.rescan_known(1)["success"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE is_active = 0"), 0)

    def test_a_failed_listing_is_not_an_empty_folder(self):
        with patch("os.scandir", side_effect=OSError("Input/output error")), self.assertRaises(ScanReadError):
            external_indexer.list_dirs(self.root)

    def test_a_failed_walk_is_not_an_empty_model(self):
        with self.assertRaises(ScanReadError):
            list(external_indexer.walk_files(self.base / "missing"))


class MissingTests(ScanCase):
    def test_a_missing_folder_goes_inactive_and_is_logged(self):
        self.scan_everything()
        shutil.rmtree(self.root / "2024-01 - Drop" / "Golem")
        self.assertEqual(self.indexer.rescan_known(1)["models_missing"], 1)
        row = self.rows("SELECT id, is_active FROM models WHERE name = 'Golem'")[0]
        self.assertEqual(row["is_active"], 0)
        log = self.rows("SELECT process, reason, item_name, resolved_at FROM inactive_log WHERE item_id = ?", row["id"])
        self.assertEqual(tuple(log[0]), ("library_scan", "folder_missing", "Golem", None))

    def test_a_returning_folder_comes_back_with_the_same_id(self):
        self.scan_everything()
        folder = self.root / "2024-01 - Drop" / "Golem"
        model_id = self.count("SELECT id FROM models WHERE name = 'Golem'")
        shutil.move(str(folder), str(self.base / "held"))
        self.indexer.rescan_known(1)
        shutil.move(str(self.base / "held"), str(folder))
        self.indexer.rescan_known(1)
        self.assertEqual(self.count("SELECT is_active FROM models WHERE id = ?", model_id), 1)
        self.assertEqual(self.model_count(), 3)
        self.assertIsNotNone(self.rows("SELECT resolved_at FROM inactive_log WHERE item_id = ?", model_id)[0][0])

    def test_a_renamed_folder_is_a_new_model_and_the_old_goes_inactive(self):
        self.scan_everything()
        drop = self.root / "2024-01 - Drop"
        (drop / "Golem").rename(drop / "Stone Golem")
        self.indexer.rescan_known(1)
        self.assertEqual(self.count("SELECT is_active FROM models WHERE name = 'Golem'"), 0)

        self.assertEqual(self.indexer.preview_library(1)["new"], 1)
        conn = self.connect()
        self.indexer.approve_candidates(conn, 1, [])
        conn.close()
        self.indexer.add_approved(1)
        self.assertEqual(self.count("SELECT is_active FROM models WHERE name = 'Stone Golem'"), 1)

    def test_too_many_missing_at_once_is_held(self):
        for index in range(12):
            self.add_model("2024-03 - Big", f"Model {index}")
        self.scan_everything()
        shutil.rmtree(self.root / "2024-03 - Big")
        result = self.indexer.rescan_known(1)
        self.assertEqual(result["missing_held"], 12)
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE is_active = 0"), 0)
        self.assertEqual(self.rows("SELECT status FROM library_scan_runs WHERE mode = 'full' ORDER BY id DESC LIMIT 1")[0][0], "held")

    def test_a_missing_file_is_logged_once_and_resolves(self):
        self.add_model("2024-02 - Next", "Dragon", "wing.stl")
        self.scan_everything()
        path = self.root / "2024-02 - Next" / "Dragon" / "wing.stl"
        path.unlink()
        self.indexer.rescan_known(1)
        self.indexer.rescan_known(1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM inactive_log WHERE reason = 'file_missing'"), 1)
        path.write_bytes(SOLID)
        self.indexer.rescan_known(1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM inactive_log WHERE reason = 'file_missing' AND resolved_at IS NULL"), 0)


class RenderChoiceTests(ScanCase):
    def test_unsupported_folders_are_used_for_renders(self):
        folder = self.add_model("x", "Bull_32mm_UnSupported", "bull.stl")
        self.assertEqual(ImageWorker(self.db).renderer.select_best_render_candidate(folder).name, "bull.stl")

    def test_supported_is_used_when_nothing_else_exists(self):
        folder = self.add_model("x", "Bull_32mm_Supported_Solid", "bull.stl")
        self.assertEqual(ImageWorker(self.db).renderer.select_best_render_candidate(folder).name, "bull.stl")

    def test_unsupported_beats_supported(self):
        folder = self.add_model("x", "Pack", "bull_supported.stl")
        (folder / "bull.stl").write_bytes(SOLID)
        self.assertEqual(ImageWorker(self.db).renderer.select_best_render_candidate(folder).name, "bull.stl")


class ImageWorkerTests(ScanCase):
    def setUp(self):
        super().setUp()
        self.worker = ImageWorker(self.db)
        self.worker.indexer = self.indexer
        patcher = patch("desync_archival.services.image_worker.app_config.previews_dir", return_value=self.previews)
        patcher.start()
        self.addCleanup(patcher.stop)

    def drain(self):
        states = []
        while True:
            state = self.worker.run_once()
            states.append(state)
            if state != image_worker.WORKING:
                return states

    def render_ok(self):
        def fake(_stl, out):
            Path(out).write_bytes(b"jpg")
            return True
        return patch.object(self.worker.renderer, "render_primary_thumbnail", side_effect=fake)

    def test_artwork_wins_over_a_render(self):
        self.scan_everything()
        (self.root / "2024-02 - Next" / "Dragon" / "dragon_render.png").write_bytes(b"art")
        with self.render_ok() as render, patch.object(self.worker.renderer, "is_available", return_value=True):
            self.drain()
        self.assertEqual(render.call_count, 2)
        model_id = self.count("SELECT id FROM models WHERE name = 'Dragon'")
        self.assertEqual((self.previews / str(model_id) / "cover.jpg").read_bytes(), b"art")
        self.assertEqual(self.count("SELECT COUNT(*) FROM models WHERE primary_image_url LIKE '/previews/%'"), 3)

    def test_it_waits_while_inventory_runs(self):
        self.scan_everything()
        conn = self.connect()
        with conn:
            conn.execute("INSERT INTO pipeline_tasks (task_type, status) VALUES ('external_library_reconcile', 'running')")
        conn.close()
        self.assertEqual(self.worker.run_once(), image_worker.WAITING_FOR_INVENTORY)
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_work_queue WHERE status = 'pending'"), 3)

    def test_pausing_survives_and_resumes(self):
        self.scan_everything()
        conn = self.connect()
        image_worker.set_paused(conn, True)
        self.assertEqual(self.worker.run_once(), image_worker.PAUSED)
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_work_queue WHERE status = 'pending'"), 3)
        image_worker.set_paused(conn, False)
        conn.close()
        with self.render_ok(), patch.object(self.worker.renderer, "is_available", return_value=True):
            self.assertEqual(self.drain()[-1], image_worker.IDLE)

    def test_an_offline_mount_waits_and_fails_nothing(self):
        self.scan_everything()
        held = self.base / "held"
        self.root.rename(held)
        self.assertEqual(self.worker.run_once(), image_worker.STORAGE_OFFLINE)
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_work_queue WHERE status != 'pending'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM inactive_log"), 0)

    def test_a_failed_render_is_logged_and_not_retried_until_asked(self):
        self.scan_everything()
        with patch.object(self.worker.renderer, "render_primary_thumbnail", return_value=False) as render, \
                patch.object(self.worker.renderer, "is_available", return_value=True):
            self.drain()
            self.assertEqual(render.call_count, 3)
            self.indexer.rescan_known(1)
            self.drain()
            self.assertEqual(render.call_count, 3)
        self.assertEqual(self.count("SELECT COUNT(*) FROM inactive_log WHERE reason = 'render_failed' AND process = 'image_worker'"), 3)

        conn = self.connect()
        self.assertEqual(image_worker.retry_failed(conn), 3)
        conn.close()
        with self.render_ok(), patch.object(self.worker.renderer, "is_available", return_value=True):
            self.drain()
        self.assertEqual(self.count("SELECT COUNT(*) FROM inactive_log WHERE reason = 'render_failed' AND resolved_at IS NULL"), 0)

    def test_nothing_to_render_is_recorded_once(self):
        self.add_model("2024-02 - Next", "Boxed", "pack.zip")
        self.scan_everything()
        with patch.object(self.worker, "_peek", return_value=image_worker.WORKING) as peek, self.render_ok(), \
                patch.object(self.worker.renderer, "is_available", return_value=True):
            self.worker.run_once()
            self.worker.run_once()
            self.worker.run_once()
            self.worker.run_once()
            self.assertEqual(peek.call_count, 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_work_queue WHERE status = 'no_source'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM inactive_log WHERE reason = 'no_source'"), 1)

    def test_covers_go_before_peeks(self):
        self.add_model("2024-02 - Next", "Boxed", "pack.zip")
        self.scan_everything()
        order = self.rows("""
            SELECT kind FROM library_work_queue WHERE status = 'pending'
            ORDER BY CASE kind WHEN 'cover' THEN 0 ELSE 1 END, id
        """)
        self.assertEqual(order[-1][0], "peek")

    def test_blender_missing_waits_instead_of_failing(self):
        self.scan_everything()
        with patch.object(self.worker.renderer, "is_available", return_value=False):
            self.assertEqual(self.worker.run_once(), image_worker.BLENDER_UNAVAILABLE)
        self.assertEqual(self.count("SELECT COUNT(*) FROM library_work_queue WHERE status = 'pending'"), 3)


class InactiveLogTests(ScanCase):
    def test_a_repeat_adds_no_second_open_row(self):
        conn = self.connect()
        with conn:
            inactive_log.record(conn, "model", 1, "A", "library_scan", "folder_missing")
            inactive_log.record(conn, "model", 1, "A", "library_scan", "folder_missing")
        self.assertEqual(self.count("SELECT COUNT(*) FROM inactive_log"), 1)
        conn.close()

    def test_history_is_kept_after_it_resolves(self):
        conn = self.connect()
        with conn:
            inactive_log.record(conn, "model", 1, "A", "library_scan", "folder_missing")
            inactive_log.resolve(conn, "model", 1)
            inactive_log.record(conn, "model", 1, "A", "library_scan", "folder_missing")
        self.assertEqual(inactive_log.query(conn)["total"], 1)
        self.assertEqual(inactive_log.query(conn, include_resolved=True)["total"], 2)
        conn.close()

    def test_filters_and_counts(self):
        conn = self.connect()
        with conn:
            inactive_log.record(conn, "model", 1, "A", "library_scan", "folder_missing")
            inactive_log.record(conn, "model", 2, "B", "image_worker", "render_failed")
        self.assertEqual(inactive_log.query(conn, process="image_worker")["items"][0]["item_name"], "B")
        self.assertEqual(len(inactive_log.query(conn)["open_counts"]), 2)
        conn.close()

    def test_unlinking_logs_each_model(self):
        self.scan_everything()
        conn = self.connect()
        external_indexer.deactivate_library(conn, 1)
        conn.close()
        self.assertEqual(self.count("SELECT COUNT(*) FROM inactive_log WHERE process = 'library_unlink'"), 3)


class MigrationTests(unittest.TestCase):
    """Starts from schema 7 with rows like production's, then applies 0008."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "library.db"
        create_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("INSERT INTO external_libraries (id, name, path) VALUES (1, 'Loot', '/Volumes/STL/Loot')")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def run_backfill(self):
        sql = Path(__file__).resolve().parents[1].joinpath("migrations", "0008_library_scan.sql").read_text(encoding="utf-8")
        backfill = sql[sql.index("-- A linked model's identity"):]
        with self.conn:
            self.conn.executescript(backfill)

    def test_existing_rows_get_relative_keys_and_keep_their_ids(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO models (id, name, source, library_id, vault_path, raw_backup_path) VALUES (2000000000, 'A', 'external', 1, '/Volumes/STL/Loot/Rel/A', '')"
            )
            self.conn.execute("INSERT INTO model_files (file_id, model_id, source, filename) VALUES (7, 2000000000, 'external', 'a.stl')")
            self.conn.execute(
                "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, actual_bytes, vault_path)"
                " VALUES (7, 2000000000, 'a', 'a', 'a', 1, '/Volumes/STL/Loot/Rel/A/a.stl')"
            )
        self.run_backfill()
        self.assertEqual(self.conn.execute("SELECT external_key FROM models WHERE id = 2000000000").fetchone()[0], "1/Rel/A")
        self.assertEqual(self.conn.execute("SELECT external_key FROM model_files WHERE file_id = 7").fetchone()[0], "1/Rel/A/a.stl")

    def test_existing_tombstones_appear_in_the_log(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO models (id, name, source, description, raw_backup_path) VALUES (5, '[Unavailable Model 5]', 'mmf', 'HTTP 404', '')"
            )
        self.run_backfill()
        row = self.conn.execute("SELECT process, reason, detail FROM inactive_log WHERE item_id = 5").fetchone()
        self.assertEqual(row, ("metadata_ingest", "unavailable", "HTTP 404"))


if __name__ == "__main__":
    unittest.main()
