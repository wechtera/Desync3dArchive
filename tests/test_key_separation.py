"""Internal keys and MMF IDs always differ here, so any code that mixes them up fails."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from db_helper import create_database

from desync_archival.services import identity
from desync_archival.services.reconciler import VaultReconciler
from desync_archival.services.task_manager import extract_normalized_records

MMF_ID = 863982
LOCAL_ID = 3000000123


def row_values(mmf_id=MMF_ID, name="Golem", files=None):
    data = {"name": name, "designer": {"name": "Forge"}, "files": {"items": files if files is not None else [
        {"id": 7001, "filename": "a.zip", "size": 10, "download_url": "https://x/a"},
        {"id": 7002, "filename": "b.zip", "size": 20, "download_url": "https://x/b"},
    ]}}
    model_row, _, file_rows = extract_normalized_records(mmf_id, data, Path("metadata/model.json"))
    return model_row[1:], [(row[0], row[2], row[3], row[4], row[5]) for row in file_rows]


class KeyCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "library.db"
        create_database(self.path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("DELETE FROM collections")
        # A row at a high key, so the next insert gets a value nothing like the MMF ID.
        self.conn.execute(
            "INSERT INTO collections (id, name, source, raw_backup_path) VALUES (?, 'seed', 'external', '')", (LOCAL_ID - 1,)
        )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def save(self, **kwargs):
        values, files = row_values(**kwargs)
        model_id = identity.save_mmf_model(self.conn, kwargs.get("mmf_id", MMF_ID), values)
        identity.save_mmf_files(self.conn, model_id, files)
        return model_id


class IngestTests(KeyCase):
    def found(self, source):
        row = self.conn.execute(
            "SELECT id FROM collections WHERE source = ? AND external_key = ?", (source, str(MMF_ID)),
        ).fetchone()
        return row[0] if row else None

    def test_a_new_model_gets_its_own_key(self):
        model_id = self.save()
        self.assertEqual(model_id, LOCAL_ID)
        self.assertEqual(self.found("mmf"), LOCAL_ID)

    def test_re_ingest_updates_in_place(self):
        first = self.save()
        ids = self.conn.execute("SELECT file_id FROM model_files WHERE model_id = ? ORDER BY file_id", (first,)).fetchall()
        second = self.save(name="Renamed")
        self.assertEqual(first, second)
        self.assertEqual(self.conn.execute("SELECT name FROM collections WHERE id = ?", (first,)).fetchone()[0], "Renamed")
        self.assertEqual(
            self.conn.execute("SELECT file_id FROM model_files WHERE model_id = ? ORDER BY file_id", (first,)).fetchall(), ids
        )

    def test_a_file_mmf_dropped_goes_unless_it_was_downloaded(self):
        model_id = self.save()
        kept = self.conn.execute("SELECT file_id FROM model_files WHERE external_key = '7001'").fetchone()[0]
        self.conn.execute(
            "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,"
            " actual_bytes) VALUES (?, ?, 'a', 'a', 'a', 1)", (kept, model_id),
        )
        self.save(files=[])
        keys = [row[0] for row in self.conn.execute("SELECT external_key FROM model_files WHERE model_id = ?", (model_id,))]
        self.assertEqual(keys, ["7001"])

    def test_the_same_number_from_two_sources_does_not_collide(self):
        model_id = self.save()
        self.conn.execute(
            "INSERT INTO collections (name, source, external_key, raw_backup_path) VALUES ('Other', 'thingiverse', ?, '')", (str(MMF_ID),)
        )
        self.assertEqual(self.found("mmf"), model_id)
        self.assertNotEqual(self.found("thingiverse"), model_id)


class ReverseAuditTests(KeyCase):
    def audit(self, vault):
        self.conn.commit()
        reconciler = VaultReconciler(self.path)
        return reconciler._execute_reverse(vault, lambda *_: None, lambda *_: None, lambda: False)

    def make_folder(self, name):
        vault = Path(self.temp.name) / "vault"
        folder = vault / "Forge" / "2024-01" / name
        (folder / "files").mkdir(parents=True)
        (folder / "files" / "a.zip").write_bytes(b"x")
        return vault

    def test_a_folder_named_by_mmf_id_belongs_to_its_model(self):
        self.save()
        report = self.audit(self.make_folder(f"{MMF_ID}_Golem"))
        self.assertEqual(report["orphan_directories"], [])

    def test_a_folder_named_by_internal_key_is_an_orphan(self):
        self.save()
        report = self.audit(self.make_folder(f"{LOCAL_ID}_Golem"))
        self.assertEqual(len(report["orphan_directories"]), 1)

    def test_untracked_files_are_recorded_against_the_internal_key(self):
        model_id = self.save()
        vault = self.make_folder(f"{MMF_ID}_Golem")
        (next(vault.glob("*/*/*/files")) / "extra.stl").write_bytes(b"x")
        report = self.audit(vault)
        self.assertEqual({item["model_id"] for item in report["untracked_files"]}, {model_id})


class DriftTests(KeyCase):
    def test_external_libraries_are_never_in_the_drift_scan(self):
        self.conn.execute("UPDATE collections SET vault_path = '/nas/Loot Studios/Golem'")
        self.conn.commit()
        reconciler = VaultReconciler(self.path)
        reconciler.storage.configured_root = lambda: Path(self.temp.name)
        self.assertEqual(reconciler.scan_path_drift()["items"], [])

    def test_canonical_folders_use_the_mmf_id(self):
        model_id = self.save()
        self.conn.execute("UPDATE collections SET vault_path = '/somewhere/else', release_month = '2024-01' WHERE id = ?", (model_id,))
        self.conn.commit()
        reconciler = VaultReconciler(self.path)
        reconciler.storage.configured_root = lambda: Path(self.temp.name)
        item = reconciler.scan_path_drift()["items"][0]
        self.assertTrue(Path(item["canonical_path"]).name.startswith(f"{MMF_ID}_"))


if __name__ == "__main__":
    unittest.main()
