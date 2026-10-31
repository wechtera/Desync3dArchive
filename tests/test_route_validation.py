"""Each domain router rejects bad request bodies, and keeps every URL and alias."""

import sqlite3
import unittest
from unittest.mock import patch

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival.api import server

JSON = {"Content-Type": "application/json"}


class RouteCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        create_database(server.DB_PATH)
        cls.client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")

    def refused(self, path, body, method="POST"):
        response = self.client.request(method, path, json=body)
        self.assertEqual(response.status_code, 400, f"{path}: {response.text}")
        self.assertIn("error", response.json())
        return response.json()["error"]


class CatalogValidationTests(RouteCase):
    def test_bad_render_options_are_refused(self):
        self.assertIn("force", self.refused("/api/models/1/render-view", {"force": "maybe"}))
        self.refused("/api/render/stl", {"view": 5})

    def test_a_body_must_be_an_object(self):
        self.assertIn("JSON object", self.refused("/api/render/stl", [1, 2]))

    def test_non_numeric_ids_do_not_match(self):
        self.assertEqual(self.client.get("/api/models/abc").status_code, 404)


class ArchiveValidationTests(RouteCase):
    def test_bad_model_ids_are_refused(self):
        self.assertIn("model_ids", self.refused("/api/reconcile/archive-peek", {"model_ids": ["x"]}))


class DownloadValidationTests(RouteCase):
    def test_bad_queue_bodies_are_refused(self):
        self.refused("/api/downloads/queue", {"model_ids": "all"})
        self.refused("/api/downloads/queue", {"model_ids": ["x"]}, method="DELETE")
        self.refused("/api/downloads/dequeue", {"purge_incomplete": "maybe"})

    def test_bad_control_action_is_refused(self):
        self.refused("/api/downloads/control", {"action": 5})


class StashValidationTests(RouteCase):
    def test_bad_stash_bodies_are_refused(self):
        self.refused("/api/stashes", {"make_active": "maybe"})
        self.refused("/api/stashes/1/items", {"item_type": "model", "model_id": "x"})
        self.refused("/api/stashes/1/bulk-models", {"model_ids": "all"})
        self.refused("/api/stashes/1/export", {"sync": "maybe"})

    def test_trailing_slashes_still_work(self):
        self.assertEqual(self.client.get("/api/stashes/active/").status_code, self.client.get("/api/stashes/active").status_code)


class SettingsValidationTests(RouteCase):
    def test_path_fields_must_be_text(self):
        self.refused("/api/settings", {"download_root_path": 5})
        self.refused("/api/storage/config", {"path": ["x"]})
        self.refused("/api/settings/libraries", {"path": 5})

    def test_bad_logging_body_is_refused(self):
        self.refused("/api/settings/logging", {"layers": "http"})

    def test_settings_keep_open_keys(self):
        self.assertEqual(self.client.post("/api/settings", json={"batch_size": "5"}).status_code, 200)


class TaskValidationTests(RouteCase):
    def test_bad_ingest_bodies_are_refused(self):
        self.refused("/api/ingest/start", {"raw_ids": 5})
        self.refused("/api/tasks/ingest", {"model_ids": "all"})


class ReconcileValidationTests(RouteCase):
    def test_bad_reconcile_bodies_are_refused(self):
        self.refused("/api/reconcile/reset", {"model_ids": ["x"]})
        self.refused("/api/reconcile/delete-orphans", {"paths": "/"})
        self.refused("/api/reconcile/staging/purge", {"paths": 5})
        self.refused("/api/reconcile/ingest-untracked", {"items": ["x"]})
        self.refused("/api/reconcile/scan", {"scan_mode": 5})


class AuthValidationTests(RouteCase):
    def test_bad_auth_bodies_are_refused(self):
        self.refused("/api/auth/token_callback", {"access_token": "x", "expires_in": "soon"})
        self.refused("/api/auth/config", {"client_id": 5})


class ErrorPathTests(RouteCase):
    """Failures are logged and return fixed text, with no exception detail."""

    def test_stash_failures_return_generic_text(self):
        target = "desync_archival.services.stash_service.StashService.create_stash"
        with patch(target, side_effect=RuntimeError("private detail")):
            response = self.client.post("/api/stashes", json={"name": "x"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private detail", response.text)

    def test_render_failure_returns_generic_text(self):
        target = "desync_archival.services.mesh_renderer.MeshRenderer.render_ephemeral"
        conn = sqlite3.connect(server.DB_PATH)
        conn.execute("INSERT OR IGNORE INTO models (id, name, url, raw_backup_path) VALUES (77, 'M', '', '')")
        conn.execute("""
            INSERT OR REPLACE INTO downloaded_files (
                file_id, model_id, original_filename, cleaned_filename, local_relative_path, actual_bytes, vault_path
            ) VALUES (770, 77, 'a.stl', 'a.stl', 'a.stl', 1, '/tmp/a.stl')
        """)
        conn.commit()
        conn.close()
        with patch(target, side_effect=RuntimeError("private detail")), \
                patch("desync_archival.services.mesh_renderer.MeshRenderer.is_available", return_value=True):
            response = self.client.post("/api/render/stl", json={"source_type": "file", "id": 770})
        self.assertNotIn("private detail", response.text)
        self.assertIn(response.status_code, (404, 500))
