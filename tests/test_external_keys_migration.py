"""Migration 0006 copies MMF IDs into external_key and leaves linked library rows without one."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from desync_archival import migrations


def migrate_to(path, version):
    conn = sqlite3.connect(path)
    for number, file in migrations.load_migrations():
        if number <= version:
            migrations._apply_file(conn, number, file)
    return conn


class ExternalKeyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = migrate_to(Path(self.temp.name) / "library.db", 5)
        self.conn.execute("INSERT INTO external_libraries (id, name, path) VALUES (1, 'Lib', '/nas/Lib')")
        self.conn.execute("INSERT INTO external_libraries (id, name, path) VALUES (2, 'Lib deep', '/nas/Lib/deep')")
        rows = [
            (96576, "MMF", "mmf", None),
            (2000000000, "Local", "external", "/nas/Lib/2024/Golem"),
            (2000000001, "Deep", "external", "/nas/Lib/deep/Elf"),
            (2000000002, "Similar", "external", "/nas/Lib2/Orc"),
        ]
        self.conn.executemany(
            "INSERT INTO models (id, name, source, vault_path, raw_backup_path) VALUES (?, ?, ?, ?, '')", rows
        )
        self.conn.execute("INSERT INTO model_files (file_id, model_id, filename) VALUES (563, 96576, 'a.zip')")
        self.conn.execute("INSERT INTO model_files (file_id, model_id, filename) VALUES (2000000000, 2000000000, 'b.stl')")
        self.conn.commit()
        migrations._apply_file(self.conn, 6, dict(migrations.load_migrations())[6])

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def model(self, model_id):
        return self.conn.execute("SELECT external_key, library_id FROM models WHERE id = ?", (model_id,)).fetchone()

    def test_mmf_models_get_their_id_as_text(self):
        self.assertEqual(self.model(96576), ("96576", None))

    def test_external_models_have_no_key_and_point_at_their_library(self):
        self.assertEqual(self.model(2000000000), (None, 1))

    def test_the_most_specific_library_wins(self):
        self.assertEqual(self.model(2000000001), (None, 2))

    def test_a_path_that_only_shares_a_prefix_matches_nothing(self):
        self.assertEqual(self.model(2000000002), (None, None))

    def test_files_take_their_source_from_the_model(self):
        rows = self.conn.execute("SELECT file_id, source, external_key FROM model_files ORDER BY file_id").fetchall()
        self.assertEqual(rows, [(563, "mmf", "563"), (2000000000, "external", None)])


if __name__ == "__main__":
    unittest.main()
