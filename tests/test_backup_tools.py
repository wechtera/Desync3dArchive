"""Second copies, metadata archives, chosen folders, and moving to another machine."""

import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database

from desync_archival import backup, settings
from desync_archival.services import data_bundle


class BackupCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "data"
        (self.data / "metadata").mkdir(parents=True)
        (self.data / "metadata" / "model_1.json").write_text("{}", encoding="utf-8")
        (self.data / "previews" / "1").mkdir(parents=True)
        (self.data / "previews" / "1" / "cover.jpg").write_bytes(b"jpeg")
        self.db = self.data / "library.db"
        create_database(self.db)
        self.local = self.root / "backups"
        self.share = self.root / "nas"
        self.share.mkdir()
        self.mirror = self.share / "copies"
        env = {
            "DESYNC_DATA_DIR": str(self.data), "DESYNC_BACKUP_DIR": str(self.local),
            "DESYNC_BACKUP_MIRROR_DIR": str(self.mirror), "DESYNC_SECRETS_FILE": str(self.root / "secrets.json"),
        }
        patcher = patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / "secrets.json").write_text('{"settings": {"mmf_access_token": "x"}}', encoding="utf-8")


class MirrorTests(BackupCase):
    def test_a_full_backup_reaches_the_second_location_without_secrets(self):
        target = backup.create_backup(self.db, full=True)
        self.assertTrue((self.mirror / target.name).is_file())
        self.assertTrue(backup.metadata_copy(self.mirror / target.name).is_file())
        self.assertTrue(backup.secrets_copy(target).is_file())
        self.assertFalse(backup.secrets_copy(self.mirror / target.name).exists())

    def test_a_quick_backup_stays_local(self):
        backup.create_backup(self.db)
        self.assertFalse(self.mirror.exists())

    def test_a_missing_share_is_skipped_and_never_created(self):
        missing = self.root / "gone" / "copies"
        with patch.dict("os.environ", {"DESYNC_BACKUP_MIRROR_DIR": str(missing)}):
            target = backup.create_backup(self.db, full=True)
        self.assertTrue(target.is_file())
        self.assertFalse(missing.parent.exists())

    def test_a_failed_copy_leaves_the_local_backup_standing(self):
        with patch("desync_archival.backup.shutil.copyfile", side_effect=OSError("share dropped")):
            target = backup.create_backup(self.db, full=True)
        self.assertTrue(target.is_file())
        self.assertEqual(list(self.mirror.glob("*.partial")), [])

    def test_old_copies_are_pruned_with_their_metadata(self):
        for _ in range(3):
            backup.create_backup(self.db, keep=2, full=True)
        self.assertEqual(len(list(self.local.glob("*.db"))), 2)
        self.assertEqual(len(list(self.mirror.glob("*.db"))), 2)
        self.assertEqual(len(list(self.mirror.glob("*.metadata.tar.gz"))), 2)


class MetadataTests(BackupCase):
    def test_metadata_is_archived_beside_a_full_backup(self):
        target = backup.create_backup(self.db, full=True)
        with tarfile.open(backup.metadata_copy(target)) as archive:
            self.assertIn("metadata/model_1.json", archive.getnames())


class ListingTests(BackupCase):
    def test_the_listing_says_what_is_safe_where(self):
        target = backup.create_backup(self.db, full=True)
        listing = backup.list_backups()
        self.assertTrue(listing["mirror_online"])
        self.assertEqual(listing["backups"][0]["name"], target.name)
        self.assertTrue(listing["backups"][0]["mirrored"])
        self.assertTrue(listing["backups"][0]["has_metadata"])

    def test_checking_a_backup_refuses_anything_but_a_backup_name(self):
        with self.assertRaisesRegex(backup.BackupError, "isn't a backup"):
            backup.check_backup("../../etc/passwd")

    def test_a_good_backup_checks_out(self):
        target = backup.create_backup(self.db)
        self.assertGreater(backup.check_backup(target.name)["tables"], 10)

    def test_a_chosen_folder_beats_the_environment(self):
        chosen = self.root / "chosen"
        settings.save_backup_paths(str(chosen), "")
        self.assertEqual(settings.backup_dir(), chosen.resolve())
        self.assertEqual(backup.create_backup(self.db).parent, chosen.resolve())


class BundleTests(BackupCase):
    def seed(self):
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("INSERT INTO external_libraries (name, path) VALUES ('Loot', '/Volumes/STL/Loot')")
            conn.execute("INSERT INTO collections (name, source, vault_path, raw_backup_path) VALUES ('A', 'mmf', '/Volumes/STL/Loot/a', '')")
            conn.execute("INSERT INTO collections (name, source, vault_path, raw_backup_path) VALUES ('B', 'mmf', '/Volumes/STL/Lootish/b', '')")
        conn.close()

    def test_a_bundle_round_trips_onto_an_empty_machine(self):
        self.seed()
        bundle = self.root / "bundle"
        result = data_bundle.export_bundle(self.db, bundle)
        self.assertFalse(result["has_secrets"])
        self.assertFalse((bundle / data_bundle.SECRETS).exists())
        self.assertEqual(json.loads((bundle / data_bundle.MANIFEST).read_text())["stored_roots"]["libraries"], ["/Volumes/STL/Loot"])

        fresh = self.root / "fresh"
        imported = data_bundle.import_bundle(bundle, fresh, {"/Volumes/STL/Loot": "/mnt/nas/Loot"})
        self.assertTrue((fresh / "metadata" / "model_1.json").is_file())
        self.assertTrue((fresh / "previews" / "1" / "cover.jpg").is_file())
        conn = sqlite3.connect(fresh / "library.db")
        paths = dict(conn.execute("SELECT name, vault_path FROM collections WHERE name IN ('A', 'B')").fetchall())
        conn.close()
        self.assertEqual(paths["A"], "/mnt/nas/Loot/a")
        self.assertEqual(paths["B"], "/Volumes/STL/Lootish/b", "a similar folder name must not match")
        self.assertEqual(imported["paths_rewritten"], 2)

    def test_secrets_only_travel_when_asked(self):
        bundle = self.root / "bundle"
        data_bundle.export_bundle(self.db, bundle, with_secrets=True)
        self.assertTrue((bundle / data_bundle.SECRETS).is_file())

    def test_import_never_replaces_a_catalog(self):
        bundle = self.root / "bundle"
        data_bundle.export_bundle(self.db, bundle)
        with self.assertRaisesRegex(data_bundle.BundleError, "already exists"):
            data_bundle.import_bundle(bundle, self.data, {})

    def test_export_refuses_a_folder_with_files_in_it(self):
        bundle = self.root / "bundle"
        bundle.mkdir()
        (bundle / "keep.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(data_bundle.BundleError, "isn't empty"):
            data_bundle.export_bundle(self.db, bundle)

    def test_export_stays_out_of_the_data_folder(self):
        with self.assertRaisesRegex(data_bundle.BundleError, "outside"):
            data_bundle.export_bundle(self.db, self.data / "bundle")
