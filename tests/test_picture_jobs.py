"""E54's picture jobs: extract an archive image, copy a library image, render a chosen artifact.
A job only fills a picture that's missing or a stand-in, and an offline NAS only makes it wait."""

import json
import sqlite3
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import MagicMock

from db_helper import create_database

from desync_archival import settings as app_config
from desync_archival.services import catalog_browse, picture_jobs
from desync_archival.services.archive_peeker import ArchivePeeker


def png(width=64, height=48):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + chunk + struct.pack(">I", zlib.crc32(chunk))


class JobCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "library.db"
        create_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.conn.close)
        self.zip = self.root / "vault" / "pack.zip"
        self.zip.parent.mkdir()
        with zipfile.ZipFile(self.zip, "w") as archive:
            archive.writestr("Ghoul/Ghoul_A.stl", b"solid g\nendsolid g\n")
            archive.writestr("Ghoul/Ghoul_A.lys", b"lychee")
            archive.writestr("Renders/Ghoul_A.png", png())
        with self.conn:
            self.collection = self.conn.execute(
                "INSERT INTO collections (name, source, raw_backup_path) VALUES ('Pack', 'mmf', '')").lastrowid
            self.file_id = self.conn.execute(
                "INSERT INTO model_files (model_id, filename, size_bytes, download_url, source) VALUES (?, 'pack.zip', 1, '', 'mmf')",
                (self.collection,)).lastrowid
            self.conn.execute("""
                INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path,
                    actual_bytes, storage_tier, vault_path) VALUES (?, ?, 'pack.zip', 'pack.zip', 'pack.zip', 1, 'nas', ?)
            """, (self.file_id, self.collection, str(self.zip)))
        ArchivePeeker(self.db).inspect_and_record(self.collection, self.file_id, self.zip)
        self.model = self.conn.execute("SELECT model_id FROM model_artifacts WHERE filename = 'Ghoul_A.stl'").fetchone()[0]
        with self.conn:
            self.conn.execute("UPDATE models SET image_path = NULL, image_source = NULL WHERE id = ?", (self.model,))
        self.renderer = MagicMock()
        self.renderer.is_available.return_value = True
        self.renderer.render_primary_thumbnail.side_effect = lambda mesh, out: Path(out).write_bytes(png()) or True

    def job(self, kind, source, model_id=None, group_id=None):
        with self.conn:
            return self.conn.execute(
                "INSERT INTO picture_jobs (kind, model_id, group_id, collection_id, source_json) VALUES (?, ?, ?, ?, ?)",
                (kind, model_id or self.model, group_id, self.collection, json.dumps(source))).lastrowid

    def run_job(self):
        return picture_jobs.run_one(self.conn, self.renderer, ArchivePeeker(self.db))

    def outcome(self, job_id):
        return tuple(self.conn.execute("SELECT status, detail FROM picture_jobs WHERE id = ?", (job_id,)).fetchone())

    def picture(self, model_id=None):
        return tuple(self.conn.execute("SELECT image_path, image_source FROM models WHERE id = ?", (model_id or self.model,)).fetchone())

    def one_picture(self):
        return tuple(self.conn.execute("SELECT image_path, image_source, image_locked FROM models WHERE id = ?", (self.model,)).fetchone())

    def member(self, path):
        return self.conn.execute("SELECT id FROM file_archive_members WHERE internal_path = ?", (path,)).fetchone()[0]

    def artifact(self, name):
        return self.conn.execute("SELECT id FROM model_artifacts WHERE filename = ?", (name,)).fetchone()[0]


class ExtractTests(JobCase):
    def test_an_image_never_extracted_is_extracted_and_shown(self):
        preview = f"previews/{self.collection}/{self.file_id}/again_Ghoul_A.png"
        job = self.job("extract", {"member": self.member("Renders/Ghoul_A.png"), "file_id": self.file_id, "preview": preview, "source": "llm"})
        self.assertEqual(self.run_job(), picture_jobs.WORKING)
        self.assertEqual(self.outcome(job)[0], "done")
        self.assertEqual(self.picture(), (preview, "llm"))
        self.assertTrue((app_config.data_dir() / preview).is_file())

    def test_a_persons_choice_replaces_the_matchers_picture_and_is_locked(self):
        with self.conn:
            self.conn.execute("UPDATE models SET image_path = 'previews/matched.png', image_source = 'archive' WHERE id = ?", (self.model,))
        preview = f"previews/{self.collection}/{self.file_id}/chosen_Ghoul_A.png"
        self.job("extract", {"member": self.member("Renders/Ghoul_A.png"), "file_id": self.file_id, "preview": preview, "source": "owner"})
        self.run_job()
        self.assertEqual(self.one_picture(), (preview, "owner", 1))

    def test_a_job_never_replaces_the_matchers_picture(self):
        with self.conn:
            self.conn.execute("UPDATE models SET image_path = 'previews/matched.png', image_source = 'archive' WHERE id = ?", (self.model,))
        preview = f"previews/{self.collection}/{self.file_id}/llm_Ghoul_A.png"
        self.job("extract", {"member": self.member("Renders/Ghoul_A.png"), "file_id": self.file_id, "preview": preview, "source": "llm"})
        self.run_job()
        self.assertEqual(self.one_picture(), ("previews/matched.png", "archive", 0))

    def test_an_offline_vault_leaves_the_job_waiting(self):
        self.zip.rename(self.zip.with_suffix(".gone"))
        job = self.job("extract", {"member": self.member("Renders/Ghoul_A.png"), "file_id": self.file_id, "preview": "previews/x.png"})
        self.assertEqual(self.run_job(), picture_jobs.OFFLINE)
        self.assertEqual(self.outcome(job)[0], "pending")


class RenderTests(JobCase):
    def test_the_chosen_artifact_is_rendered_into_the_models_picture(self):
        job = self.job("render", {"artifact": self.artifact("Ghoul_A.stl")})
        self.run_job()
        self.assertEqual(self.outcome(job)[0], "done")
        self.assertEqual(self.picture(), (f"previews/renders/{self.model}.jpg", "render"))
        mesh = self.renderer.render_primary_thumbnail.call_args.args[0]
        self.assertEqual(Path(mesh).name, "Ghoul_A.stl")

    def test_a_model_that_got_a_picture_meanwhile_keeps_it(self):
        with self.conn:
            self.conn.execute("UPDATE models SET image_path = 'previews/mine.png', image_source = 'owner', image_locked = 1 WHERE id = ?", (self.model,))
        job = self.job("render", {"artifact": self.artifact("Ghoul_A.stl")})
        self.run_job()
        self.assertEqual(self.outcome(job)[0], "skipped")
        self.assertEqual(self.picture(), ("previews/mine.png", "owner"))
        self.renderer.render_primary_thumbnail.assert_not_called()

    def test_only_an_stl_is_rendered(self):
        job = self.job("render", {"artifact": self.artifact("Ghoul_A.lys")})
        self.run_job()
        self.assertEqual(self.outcome(job), ("failed", "Ghoul_A.lys isn't an STL, so it can't be rendered"))

    def test_without_blender_the_job_waits(self):
        self.renderer.is_available.return_value = False
        job = self.job("render", {"artifact": self.artifact("Ghoul_A.stl")})
        self.assertEqual(self.run_job(), picture_jobs.NO_BLENDER)
        self.assertEqual(self.outcome(job)[0], "pending")

    def test_a_listed_cover_is_replaced_by_the_render(self):
        cover = app_config.previews_dir() / "777" / "cover.jpg"
        cover.parent.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(b"old size-picked render")
        self.addCleanup(cover.unlink, missing_ok=True)
        self.job("render", {"artifact": self.artifact("Ghoul_A.stl"), "covers": [777]})
        self.run_job()
        self.assertEqual(cover.read_bytes(), png())

    def test_a_failing_job_never_stops_the_next(self):
        bad = self.job("render", {"artifact": 999999})
        good = self.job("render", {"artifact": self.artifact("Ghoul_A.stl")})
        self.run_job()
        self.run_job()
        self.assertEqual((self.outcome(bad)[0], self.outcome(good)[0]), ("skipped", "done"))


class CopyTests(JobCase):
    def test_a_library_image_is_copied_here_and_shown(self):
        source = self.root / "loot" / "2022-01" / "Mask.png"
        source.parent.mkdir(parents=True)
        source.write_bytes(png())
        job = self.job("copy", {"origin": str(source), "source": "llm"})
        self.run_job()
        path, kind = self.picture()
        self.assertEqual((self.outcome(job)[0], kind), ("done", "llm"), self.outcome(job))
        self.assertEqual((app_config.data_dir() / path).read_bytes(), png())


class GroupPictureTests(JobCase):
    def test_a_model_without_a_picture_shows_its_groups(self):
        with self.conn:
            group = self.conn.execute("INSERT INTO model_groups (collection_id, name, image_path, image_source) VALUES (?, 'Ghouls', 'previews/g.png', 'llm')",
                                      (self.collection,)).lastrowid
            self.conn.execute("INSERT INTO model_group_members (group_id, model_id) VALUES (?, ?)", (group, self.model))
        detail = catalog_browse.model_detail(self.conn, self.model)
        self.assertEqual(detail["image_path"], "previews/g.png")
        self.assertEqual(catalog_browse.group_detail(self.conn, group)["image_path"], "previews/g.png")
