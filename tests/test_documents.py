"""PDFs from every source: inventoried once, listed from SQLite, stashable, and rescan safe."""

import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival import external
from desync_archival.api import server
from desync_archival.api.routes import settings as routes
from desync_archival.services import documents, file_import
from desync_archival.services.external_indexer import ExternalLibraryIndexer
from desync_archival.services.stash_exporter import StashExporter
from desync_archival.services.stash_service import StashService

# Enough of a PDF for Ghostscript to draw one blank page.
SMALL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 300]>>endobj\ntrailer<</Root 1 0 R>>\n"
)


class DocumentCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.db = self.base / "library.db"
        create_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        self.root = self.base / "Loot"
        self.data = self.base / "data"
        (self.data / "previews").mkdir(parents=True)
        with self.conn:
            self.conn.execute("DELETE FROM app_settings WHERE key = 'download_root_path'")
            self.conn.execute("INSERT INTO app_settings (key, value) VALUES ('download_root_path', ?)", (str(self.vault),))
            self.conn.execute("INSERT INTO external_libraries (id, name, path, default_creator) VALUES (1, 'Loot', ?, 'Loot Studios')", (str(self.root),))
        for target, value in (
            ("desync_archival.settings.previews_dir", self.data / "previews"),
            ("desync_archival.settings.data_dir", self.data),
            ("desync_archival.settings.db_path", self.db),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("desync_archival.settings.is_allowed", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = []

    def collection(self, name, source="mmf", key="1"):
        with self.conn:
            return self.conn.execute(
                "INSERT INTO collections (name, designer_name, source, external_key, raw_backup_path) VALUES (?, 'Maker', ?, ?, '')",
                (name, source, key),
            ).lastrowid

    def listed_file(self, collection, filename, disk_path=None):
        with self.conn:
            file_id = self.conn.execute(
                "INSERT INTO model_files (model_id, filename, size_bytes) VALUES (?, ?, 100)", (collection, filename),
            ).lastrowid
            if disk_path:
                self.conn.execute(
                    "INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, "
                    "actual_bytes, file_extension, vault_path) VALUES (?, ?, ?, ?, ?, 100, '.pdf', ?)",
                    (file_id, collection, filename, filename, filename, str(disk_path)),
                )
        return file_id

    def loot_pdf(self, rel, body=SMALL_PDF):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def inventory(self):
        library = self.conn.execute("SELECT * FROM external_libraries WHERE id = 1").fetchone()
        return documents.inventory_library(self.conn, library, self.log.append)

    def docs(self, **filters):
        return documents.listing(self.conn, **filters)


class ListingTests(DocumentCase):
    def test_a_listed_mmf_pdf_shows_as_not_downloaded(self):
        self.listed_file(self.collection("LOTP Books"), "fear-the-old-god.pdf")
        doc = self.docs()["documents"][0]
        self.assertEqual((doc["title"], doc["state"], doc["source"]), ("fear the old god", "not_downloaded", "MyMiniFactory"))

    def test_a_zipped_pdf_product_is_listed_before_it_can_be_named(self):
        self.listed_file(self.collection("Dragonseekers (PDF)"), "Dragonseekers.zip")
        doc = self.docs()["documents"][0]
        self.assertEqual((doc["title"], doc["state"]), ("Dragonseekers (PDF)", "unread"))

    def test_a_peeked_archive_replaces_that_entry_with_its_pdfs(self):
        collection = self.collection("Dragonseekers (PDF)")
        file_id = self.listed_file(collection, "Dragonseekers.zip", self.base / "Dragonseekers.zip")
        with self.conn:
            self.conn.execute("UPDATE model_files SET peeked_at = CURRENT_TIMESTAMP WHERE file_id = ?", (file_id,))
            self.conn.execute(
                "INSERT INTO file_archive_members (file_id, internal_path, filename, file_extension) "
                "VALUES (?, 'Book/Dragon Guide.pdf', 'Dragon Guide.pdf', '.pdf')", (file_id,),
            )
        found = self.docs()["documents"]
        self.assertEqual([(d["title"], d["state"]) for d in found], [("Dragon Guide", "in_archive")])

    def test_other_files_never_appear(self):
        self.listed_file(self.collection("Ghouls"), "ghoul.stl")
        self.assertEqual(self.docs()["total"], 0)

    def test_filters_combine(self):
        first = self.collection("Sands Adventure")
        self.listed_file(first, "sands-one-shot.pdf")
        self.listed_file(first, "eula.pdf")
        other = self.collection("Paint Pack", key="2")
        self.listed_file(other, "painting-guide.pdf")
        self.assertEqual(self.docs(kind="Licence")["total"], 1)
        self.assertEqual(self.docs(kind="Adventure", search="sands")["total"], 1)
        self.assertEqual(self.docs(search="sands guide")["total"], 0)
        self.assertEqual(self.docs(creator="Maker", state="not_downloaded")["total"], 3)
        self.assertEqual(self.docs(source="Linked library")["total"], 0)
        self.assertEqual(self.docs()["kinds"]["Painting guide"], 1)

    def test_an_inactive_collection_is_left_out(self):
        collection = self.collection("Gone")
        self.listed_file(collection, "a.pdf")
        with self.conn:
            self.conn.execute("UPDATE collections SET is_active = 0 WHERE id = ?", (collection,))
        self.assertEqual(self.docs()["total"], 0)

    def test_listing_works_with_the_nas_offline(self):
        self.listed_file(self.collection("Books"), "a.pdf", "/Volumes/not/mounted/a.pdf")
        with patch.object(Path, "exists", side_effect=AssertionError), patch.object(Path, "is_file", side_effect=AssertionError):
            self.assertEqual(self.docs()["documents"][0]["state"], "on_disk")

    def test_the_route_reads_filters(self):
        self.listed_file(self.collection("Books"), "guide.pdf")
        body, status = routes.handle_get_documents(self.conn, params={"q": ["guide"], "page": ["x"]})
        self.assertEqual((status, body["total"], body["page"]), (200, 1, 1))

    def test_paths_stay_out_of_the_list(self):
        self.listed_file(self.collection("Books"), "guide.pdf", "/Volumes/x/guide.pdf")
        self.assertNotIn("disk_path", self.docs()["documents"][0])


class LibraryInventoryTests(DocumentCase):
    def test_library_pdfs_become_ordinary_rows_grouped_by_release(self):
        self.loot_pdf("2021-08 - Toll Collectors/Wanted.pdf")
        self.loot_pdf("2021-08 - Toll Collectors/Books/Deep.pdf")
        self.assertEqual(self.inventory(), 2)
        row = self.conn.execute("SELECT source, name, primary_category, library_id FROM collections WHERE source = 'document'").fetchall()
        self.assertEqual([tuple(r) for r in row], [("document", "2021-08 - Toll Collectors PDFs", "Documents", 1)])
        self.assertEqual({d["state"] for d in self.docs()["documents"]}, {"on_disk"})
        self.assertEqual(self.docs()["documents"][0]["creator"], "Loot Studios")

    def test_running_it_again_adds_nothing(self):
        self.loot_pdf("2021-08 - Drop/a.pdf")
        self.inventory()
        self.assertEqual(self.inventory(), 0)
        self.assertEqual(self.docs()["total"], 1)

    def test_an_offline_library_keeps_its_rows(self):
        self.loot_pdf("2021-08 - Drop/a.pdf")
        self.inventory()
        shutil.rmtree(self.root)
        self.assertEqual(self.inventory(), 0)
        self.assertEqual(self.docs()["total"], 1)
        self.assertTrue(any("offline" in line for line in self.log))

    def test_a_rescan_leaves_pdfs_and_models_alone(self):
        (self.root / "2021-08 - Drop" / "Ghoul").mkdir(parents=True)
        (self.root / "2021-08 - Drop" / "Ghoul" / "ghoul.stl").write_bytes(b"solid")
        self.loot_pdf("2021-08 - Drop/Mag.pdf")
        indexer = ExternalLibraryIndexer(self.db)
        self.inventory()
        before = self.conn.execute("SELECT COUNT(*) FROM model_files").fetchone()[0]
        indexer.rescan_known(1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM model_files").fetchone()[0], before)
        self.assertEqual(self.conn.execute("SELECT is_active FROM collections WHERE source = 'document'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inactive_log").fetchone()[0], 0)

    def test_a_root_level_file_is_skipped(self):
        self.loot_pdf("readme.pdf")
        self.assertEqual(self.inventory(), 0)


class ImportTests(DocumentCase):
    def test_import_takes_pdfs_and_they_reach_the_list(self):
        source = self.base / "in" / "Guide.pdf"
        source.parent.mkdir()
        source.write_bytes(SMALL_PDF)
        file_import.import_paths(self.conn, self.db, file_import.check_paths([str(source)]))
        doc = self.docs()["documents"][0]
        self.assertEqual((doc["source"], doc["state"]), ("Ad-hoc import", "on_disk"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM model_artifacts").fetchone()[0], 0)

    def test_a_folder_of_stls_and_a_pdf_keeps_both(self):
        folder = self.base / "in" / "Pack"
        folder.mkdir(parents=True)
        (folder / "dragon.stl").write_bytes(b"solid")
        (folder / "Guide.pdf").write_bytes(SMALL_PDF)
        file_import.import_paths(self.conn, self.db, file_import.check_paths([str(folder)]))
        self.assertEqual(self.docs()["total"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM model_artifacts").fetchone()[0], 1)


class OpenTests(DocumentCase):
    def test_a_file_on_disk_is_found(self):
        path = self.loot_pdf("2021-08 - Drop/a.pdf")
        self.inventory()
        key = self.docs()["documents"][0]["key"]
        self.assertEqual(documents.locate(self.conn, key), path)

    def test_offline_says_so(self):
        self.listed_file(self.collection("Books"), "a.pdf", "/Volumes/not/mounted/a.pdf")
        with self.assertRaises(documents.DocumentUnavailable) as caught:
            documents.locate(self.conn, self.docs()["documents"][0]["key"])
        self.assertEqual(caught.exception.code, 503)

    def test_not_downloaded_says_so(self):
        self.listed_file(self.collection("Books"), "a.pdf")
        with self.assertRaises(documents.DocumentUnavailable) as caught:
            documents.locate(self.conn, self.docs()["documents"][0]["key"])
        self.assertEqual(caught.exception.code, 409)

    def test_an_unknown_key_is_not_found(self):
        with self.assertRaises(documents.DocumentUnavailable) as caught:
            documents.locate(self.conn, "file-999")
        self.assertEqual(caught.exception.code, 404)

    def archived(self):
        archive = self.vault / "pack.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("Book/Guide.pdf", SMALL_PDF)
        collection = self.collection("Pack")
        file_id = self.listed_file(collection, "pack.zip", archive)
        with self.conn:
            self.conn.execute("UPDATE model_files SET peeked_at = CURRENT_TIMESTAMP WHERE file_id = ?", (file_id,))
            member = self.conn.execute(
                "INSERT INTO file_archive_members (file_id, internal_path, filename, file_extension, size_bytes) "
                "VALUES (?, 'Book/Guide.pdf', 'Guide.pdf', '.pdf', 10)", (file_id,),
            ).lastrowid
        return archive, f"member-{member}"

    def test_a_pdf_in_an_archive_is_cached_locally_and_the_nas_rests_after(self):
        if not external.find_7zip():
            self.skipTest("7-Zip isn't installed")
        archive, key = self.archived()
        cached = documents.locate(self.conn, key)
        self.assertEqual(cached.read_bytes(), SMALL_PDF)
        self.assertTrue(cached.is_relative_to(self.data))
        archive.unlink()
        self.assertEqual(documents.locate(self.conn, key), cached)


class OpenRouteTests(unittest.TestCase):
    def client(self):
        return TestClient(server.create_app(manage_workers=False), base_url="http://localhost")

    def test_a_pdf_opens_in_the_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Guide.pdf"
            path.write_bytes(SMALL_PDF)
            with patch.object(server, "get_db_connection"), patch.object(documents, "locate", return_value=path):
                response = self.client().get("/api/documents/file-1/file")
        self.assertEqual((response.status_code, response.headers["content-type"]), (200, "application/pdf"))
        self.assertTrue(response.headers["content-disposition"].startswith("inline"))

    def test_an_unavailable_pdf_explains_itself(self):
        problem = documents.DocumentUnavailable("Offline.", 503)
        with patch.object(server, "get_db_connection"), patch.object(documents, "locate", side_effect=problem):
            response = self.client().get("/api/documents/file-1/file")
        self.assertEqual((response.status_code, response.json()), (503, {"error": "Offline."}))


class StashTests(DocumentCase):
    def setUp(self):
        super().setUp()
        service = StashService()
        stash = service.create_stash("Reading", conn=self.conn)
        service.set_active_stash(stash["id"], conn=self.conn)
        self.stash = stash["id"]

    def test_a_stored_pdf_goes_in_and_exports(self):
        self.loot_pdf("2021-08 - Drop/Mag.pdf")
        self.inventory()
        key = self.docs()["documents"][0]["key"]
        body, status = routes.handle_post_document_stash(self.conn, body={"key": key})
        self.assertEqual((status, body["stash_id"]), (200, self.stash))
        exporter = StashExporter(self.db, self.base / "out", self.base / "work", self.base / "prev")
        result = exporter.export_stash(self.stash, conn=self.conn)
        self.assertTrue(result.get("success"), result)
        self.assertEqual(len(list((self.base / "out").rglob("Mag.pdf"))), 1)

    def test_not_downloaded_is_refused(self):
        self.listed_file(self.collection("Books"), "a.pdf")
        _, status = routes.handle_post_document_stash(self.conn, body={"key": self.docs()["documents"][0]["key"]})
        self.assertEqual(status, 409)


class PreviewTests(DocumentCase):
    def make(self):
        return documents.make_previews(self.conn, self.log.append, lambda *_: None, lambda: False)

    def test_a_first_page_is_stored_locally_and_used(self):
        if not external.find_ghostscript():
            self.skipTest("Ghostscript isn't installed")
        self.loot_pdf("2021-08 - Drop/a.pdf")
        self.inventory()
        self.assertEqual(self.make()["made"], 1)
        doc = self.docs()["documents"][0]
        self.assertTrue(doc["preview_url"].endswith(f"{doc['key']}.jpg"))
        self.assertEqual(self.docs()["previews_waiting"], 0)

    def test_no_ghostscript_keeps_the_collection_picture(self):
        collection = self.collection("Books")
        self.listed_file(collection, "a.pdf", self.base / "a.pdf")
        with self.conn:
            self.conn.execute("UPDATE collections SET primary_image_url = 'https://x/c.jpg' WHERE id = ?", (collection,))
        with patch("desync_archival.services.documents.external.find_ghostscript", return_value=None):
            self.assertEqual(self.make(), {"made": 0, "failed": 0})
        self.assertEqual(self.docs()["documents"][0]["preview_url"], "https://x/c.jpg")

    def test_a_bad_pdf_isnt_retried_and_an_offline_one_is(self):
        self.loot_pdf("2021-08 - Drop/bad.pdf", b"not a pdf")
        self.inventory()
        offline = self.listed_file(self.collection("Books"), "gone.pdf", "/Volumes/not/mounted/gone.pdf")
        with patch.object(documents, "_render", return_value=False):
            self.assertEqual(self.make()["failed"], 2)
        waiting = self.docs()["previews_waiting"]
        self.assertEqual(waiting, 1, offline)
