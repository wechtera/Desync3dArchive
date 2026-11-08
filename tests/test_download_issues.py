"""Download Issues: a bad file never stops a job, a size MMF lists wrong is kept after two tries
agree, and the owner settles each flagged file by keeping it or uploading a replacement."""

import hashlib
import sqlite3
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from test_fix_first import FILE_URL, DownloadCase, replying_with

from desync_archival import settings as app_config
from desync_archival.api import server
from desync_archival.services import (
    download_issues,
    identity,
    issue_upload,
    pending_deletion,
)
from desync_archival.services.db import get_db_connection
from desync_archival.services.paths import PENDING_DELETION_FOLDER

SECOND_URL = "https://www.myminifactory.com/download/b"


class Server:
    """A fake MMF. Each URL answers from its own list, and repeats the last answer once it runs out."""

    def __init__(self, answers):
        self.answers = {url: list(replies) for url, replies in answers.items()}
        self.seen = []

    def __call__(self, request):
        url = str(request.url)
        self.seen.append(url)
        if "/api/v2/" in url:
            return httpx.Response(404)
        replies = self.answers[url]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        return reply() if callable(reply) else reply

    def calls(self, url):
        return self.seen.count(url)


def ok(content, **headers):
    return httpx.Response(200, content=content, headers=headers)


class IssueCase(DownloadCase):
    def issues(self, open_only=True):
        where = " WHERE resolved_at IS NULL" if open_only else ""
        return self.conn.execute(f"SELECT * FROM download_issues{where} ORDER BY id").fetchall()

    def attempts(self):
        return [
            tuple(row) for row in self.conn.execute(
                "SELECT file_id, attempt_number, status FROM download_audit_log WHERE file_id IS NOT NULL ORDER BY id"
            )
        ]

    def add_second_file(self):
        with self.conn:
            self.conn.execute("""
                INSERT INTO model_files (file_id, model_id, filename, size_bytes, download_url, source, external_key)
                VALUES (10, 5, 'b.stl', 1000, ?, 'mmf', '10')
            """, (SECOND_URL,))

    def run_stage(self, server):
        job = self.dm._claim_next_download_job()
        with replying_with(server):
            self.dm._process_download_to_staging(1, job, self.work)

    def run_job(self, server):
        """The worker's own path, so a failure lands on the job the way it does live."""
        with replying_with(server), patch("desync_archival.services.downloader.time.sleep"), \
                self.assertLogs("vault", "INFO"):
            self.dm._download_next(1)


class SizeMmfListsWrongTests(IssueCase):
    SHORT = b"c" * 700

    def test_the_same_size_twice_is_kept_and_flagged(self):
        server = Server({FILE_URL: [ok(self.SHORT, **{"content-type": "application/octet-stream",
                                                      "content-disposition": 'attachment; filename="a.stl"'})]})
        self.run_stage(server)
        self.assertEqual(server.calls(FILE_URL), 2)
        self.assertEqual(self.staged().read_bytes(), self.SHORT)
        self.assertEqual(self.job_status(), "staging_complete")
        issue = self.issues()[0]
        self.assertEqual((issue["kind"], issue["listed_bytes"], issue["received_bytes"]), ("size_differs", 1000, 700))
        self.assertIn("application/octet-stream, named a.stl", issue["detail"])
        self.assertEqual(self.row()["expected_bytes"], 1000, "the record keeps what MMF listed")
        self.assertEqual(self.row()["actual_bytes"], 700)

    def test_each_try_is_logged_against_its_file_and_run(self):
        with self.conn:
            self.conn.execute("UPDATE download_jobs SET retry_count = 2")
        self.run_stage(Server({FILE_URL: [ok(self.SHORT)]}))
        self.assertEqual(self.attempts(), [(9, 3, "retrying"), (9, 3, "kept")])

    def test_a_right_size_on_the_retry_is_a_clean_download(self):
        self.run_stage(Server({FILE_URL: [ok(self.SHORT), ok(self.CONTENT)]}))
        self.assertEqual(self.staged().read_bytes(), self.CONTENT)
        self.assertEqual(self.issues(), [])

    def test_two_different_wrong_sizes_are_not_kept(self):
        self.add_second_file()
        server = Server({FILE_URL: [ok(self.SHORT), ok(b"d" * 800)], SECOND_URL: [ok(self.CONTENT)]})
        self.run_stage(server)
        self.assertFalse(self.staged().exists())
        self.assertFalse(self.part().exists(), "a refused file leaves nothing to resume from")
        issue = self.issues()[0]
        self.assertEqual((issue["file_id"], issue["kind"], issue["received_bytes"]), (9, "not_downloaded", 800))

    def test_a_web_page_is_never_kept_whatever_its_size(self):
        page = b"<!DOCTYPE html><html>Just a moment...</html>"
        with self.assertRaisesRegex(RuntimeError, "a web page, not the file"):
            self.run_stage(Server({FILE_URL: [ok(page)]}))
        self.assertFalse(self.staged().exists())
        self.assertEqual(self.issues()[0]["kind"], "not_downloaded")

    def test_html_by_its_stated_type_is_a_page_too(self):
        with self.assertRaises(RuntimeError):
            self.run_stage(Server({FILE_URL: [ok(self.SHORT, **{"content-type": "text/html; charset=utf-8"})]}))
        self.assertEqual(self.issues()[0]["kind"], "not_downloaded")


class TheJobCarriesOnTests(IssueCase):
    def setUp(self):
        super().setUp()
        self.add_second_file()

    def test_a_file_that_fails_twice_doesnt_stop_the_next(self):
        server = Server({FILE_URL: [httpx.Response(500)], SECOND_URL: [ok(self.CONTENT)]})
        self.run_stage(server)
        self.assertEqual(server.calls(FILE_URL), 2)
        self.assertEqual(server.calls(SECOND_URL), 1)
        self.assertEqual(self.job_status(), "staging_complete")
        self.assertEqual([row["file_id"] for row in self.issues()], [9])
        self.assertEqual(self.conn.execute("SELECT downloaded_files FROM download_jobs").fetchone()[0], 1)

    def test_a_failure_that_would_fail_every_file_still_stops_the_job(self):
        cloudflare = httpx.Response(403, headers={"cf-mitigated": "challenge"})
        server = Server({FILE_URL: [cloudflare], SECOND_URL: [ok(self.CONTENT)]})
        self.run_job(server)
        self.assertEqual(server.calls(SECOND_URL), 0)
        self.assertEqual(self.job_status(), "failed")
        self.assertEqual(self.issues(), [], "the retry will meet these files again")

    def test_when_nothing_downloads_the_job_fails_and_names_each_file(self):
        self.run_job(Server({FILE_URL: [httpx.Response(500)], SECOND_URL: [httpx.Response(502)]}))
        self.assertEqual(self.job_status(), "failed")
        error = self.conn.execute("SELECT error_message FROM download_jobs").fetchone()[0]
        self.assertIn("a.stl: HTTP 500", error)
        self.assertIn("b.stl: HTTP 502", error)
        self.assertEqual(len(self.issues()), 2)

    def test_the_jobs_failure_row_carries_its_run(self):
        with self.conn:
            self.conn.execute("UPDATE download_jobs SET retry_count = 1")
        self.run_job(Server({FILE_URL: [httpx.Response(500)], SECOND_URL: [httpx.Response(500)]}))
        row = self.conn.execute("SELECT attempt_number FROM download_audit_log WHERE file_id IS NULL").fetchone()
        self.assertEqual(row[0], 2)

    def test_a_clean_download_later_settles_the_issue(self):
        self.run_stage(Server({FILE_URL: [httpx.Response(500)], SECOND_URL: [ok(self.CONTENT)]}))
        self.dm._process_nas_offload(1, self.claim_offload(), self.vault)
        with self.conn:
            self.conn.execute("UPDATE download_jobs SET status = 'pending'")
        self.run_stage(Server({FILE_URL: [ok(self.CONTENT)], SECOND_URL: [ok(self.CONTENT)]}))
        self.assertEqual(self.issues(), [])
        self.assertEqual(self.issues(open_only=False)[0]["resolution"], "downloaded")

    def test_a_flagged_file_still_reaches_the_vault(self):
        self.run_stage(Server({FILE_URL: [ok(b"c" * 700)], SECOND_URL: [ok(self.CONTENT)]}))
        self.dm._process_nas_offload(1, self.claim_offload(), self.vault)
        self.assertEqual(self.nas_file().read_bytes(), b"c" * 700)
        self.assertEqual(self.job_status(), "completed")
        self.assertEqual(len(self.issues()), 1)


class ConfirmedSizeTests(IssueCase):
    def confirm(self, size):
        with self.conn:
            self.conn.execute("UPDATE model_files SET size_bytes = ?, size_confirmed_at = CURRENT_TIMESTAMP WHERE file_id = 9", (size,))

    def size(self):
        return self.conn.execute("SELECT size_bytes FROM model_files WHERE file_id = 9").fetchone()[0]

    def test_a_metadata_refresh_keeps_a_confirmed_size(self):
        self.confirm(700)
        with self.conn:
            identity.save_mmf_files(self.conn, 5, [("9", "a.stl", 1000, FILE_URL, None)])
        self.assertEqual(self.size(), 700)

    def test_a_link_refresh_keeps_a_confirmed_size(self):
        self.confirm(700)
        self.dm.access_token = "token"
        api = httpx.Response(200, json={"files": {"items": [{"id": 9, "download_url": SECOND_URL, "size": 1000}]}})
        with replying_with(lambda request: api):
            self.dm._refresh_links(1, self.dm._claim_next_download_job())
        self.assertEqual(self.size(), 700)

    def test_an_unconfirmed_size_still_follows_mmf(self):
        with self.conn:
            identity.save_mmf_files(self.conn, 5, [("9", "a.stl", 1234, FILE_URL, None)])
        self.assertEqual(self.size(), 1234)

    def test_a_confirmed_size_downloads_clean(self):
        self.confirm(700)
        self.run_stage(Server({FILE_URL: [ok(b"c" * 700)]}))
        self.assertEqual(self.issues(), [])


class KeepTests(IssueCase):
    def flag_and_archive(self):
        self.run_stage(Server({FILE_URL: [ok(b"c" * 700)]}))
        self.dm._process_nas_offload(1, self.claim_offload(), self.vault)
        return self.issues()[0]["id"]

    def test_keep_confirms_the_real_size_and_settles_it(self):
        issue_id = self.flag_and_archive()
        download_issues.keep(self.conn, issue_id)
        row = self.conn.execute("SELECT size_bytes, size_confirmed_at FROM model_files WHERE file_id = 9").fetchone()
        self.assertEqual(row[0], 700)
        self.assertIsNotNone(row[1])
        self.assertEqual(self.issues(open_only=False)[0]["resolution"], "kept")
        self.assertEqual(download_issues.open_count(self.conn), 0)

    def test_a_file_that_never_arrived_cant_be_kept(self):
        with self.assertRaises(RuntimeError):
            self.run_stage(Server({FILE_URL: [httpx.Response(500)]}))
        with self.assertRaises(download_issues.IssueRefused) as caught:
            download_issues.keep(self.conn, self.issues()[0]["id"])
        self.assertEqual(caught.exception.code, 400)

    def test_a_file_still_in_staging_cant_be_kept_yet(self):
        self.run_stage(Server({FILE_URL: [ok(b"c" * 700)]}))
        with self.assertRaises(download_issues.IssueRefused) as caught:
            download_issues.keep(self.conn, self.issues()[0]["id"])
        self.assertEqual(caught.exception.code, 409)

    def test_a_settled_issue_cant_be_settled_again(self):
        issue_id = self.flag_and_archive()
        download_issues.keep(self.conn, issue_id)
        with self.assertRaises(download_issues.IssueRefused) as caught:
            download_issues.keep(self.conn, issue_id)
        self.assertEqual(caught.exception.code, 409)

    def test_the_listing_shows_links_and_where_the_file_is(self):
        self.flag_and_archive()
        item = download_issues.listing(self.conn)["items"][0]
        self.assertEqual((item["filename"], item["download_url"], item["stored"]), ("a.stl", FILE_URL, True))
        self.assertEqual(download_issues.listing(self.conn, resolved=True)["items"], [])


class KeepAllTests(IssueCase):
    """E60 and E64: a model whose files MMF lists with each other's sizes, settled in one go."""

    def swapped(self, offload=True):
        # MMF lists a.stl at 1000 and b.stl at 700, and serves 700 and 1000 bytes: each size is the other's.
        self.add_second_file()
        with self.conn:
            self.conn.execute("UPDATE model_files SET size_bytes = 700 WHERE file_id = 10")
        self.run_stage(Server({FILE_URL: [ok(b"c" * 700)], SECOND_URL: [ok(b"d" * 1000)]}))
        if offload:
            self.dm._process_nas_offload(1, self.claim_offload(), self.vault)

    def test_a_reshuffled_size_list_is_spotted_on_the_model(self):
        self.swapped()
        model = download_issues.listing(self.conn)["models"][0]
        self.assertEqual((model["model_id"], model["open"], model["keepable"], model["waiting"], model["scrambled"]), (5, 2, 2, 0, True))

    def test_keep_all_settles_every_stored_file_of_the_model(self):
        self.swapped()
        result = download_issues.keep_all(self.conn, 5)
        self.assertEqual((len(result["kept"]), result["left"]), (2, 0))
        sizes = dict(self.conn.execute("SELECT file_id, size_bytes FROM model_files WHERE size_confirmed_at IS NOT NULL").fetchall())
        self.assertEqual(sizes, {9: 700, 10: 1000})
        self.assertEqual(download_issues.listing(self.conn)["models"], [])

    def test_files_still_moving_to_the_nas_wait(self):
        self.swapped(offload=False)
        model = download_issues.listing(self.conn)["models"][0]
        self.assertEqual((model["keepable"], model["waiting"]), (0, 2))
        with self.assertRaises(download_issues.IssueRefused) as caught:
            download_issues.keep_all(self.conn, 5)
        self.assertEqual(caught.exception.code, 409)

    def test_sizes_that_are_just_wrong_arent_called_a_reshuffle(self):
        self.add_second_file()
        self.run_stage(Server({FILE_URL: [ok(b"c" * 700)], SECOND_URL: [ok(b"d" * 800)]}))
        self.assertFalse(download_issues.scrambled(self.conn, 5))

    def test_still_spotted_after_some_files_were_kept(self):
        # Keep writes the real size over MMF's, so the check reads what MMF listed from the issue.
        self.swapped()
        download_issues.keep(self.conn, self.issues()[0]["id"])
        self.assertTrue(download_issues.scrambled(self.conn, 5))

    def test_the_route_keeps_all_and_says_how_many(self):
        self.swapped()
        app = server.create_app(manage_workers=False)
        app.state.open_database = lambda: get_db_connection(self.db, shared=True)
        response = TestClient(app, base_url="http://localhost").post("/api/download-issues/models/5/keep")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["kept"]), 2)


class MigrationTests(IssueCase):
    def test_one_open_issue_per_file(self):
        with self.conn:
            download_issues.record(self.conn, 9, 5, "not_downloaded", 1000, None, "first")
            download_issues.record(self.conn, 9, 5, "size_differs", 1000, 700, "second")
        rows = self.issues()
        self.assertEqual((len(rows), rows[0]["kind"], rows[0]["detail"]), (1, "size_differs", "second"))
        with self.assertRaises(sqlite3.IntegrityError), self.conn:
            self.conn.execute("INSERT INTO download_issues (file_id, model_id, kind) VALUES (9, 5, 'size_differs')")


class UploadCase(IssueCase):
    GOOD = b"g" * 900

    def flag_and_archive(self):
        """A kept file on the NAS, as Duncan Shadow's Chitubox files would be."""
        self.run_stage(Server({FILE_URL: [ok(b"c" * 700)]}))
        self.dm._process_nas_offload(1, self.claim_offload(), self.vault)
        return self.issues()[0]["id"]

    def fail_outright(self):
        """A file that never arrived, on a model with nothing in the vault yet."""
        with patch("desync_archival.services.downloader.time.sleep"), replying_with(Server({FILE_URL: [httpx.Response(500)]})), \
                self.assertLogs("vault", "INFO"):
            self.dm._download_next(1)
        return self.issues()[0]["id"]

    def upload_file(self, content=None):
        path = self.base / "upload.part"
        path.write_bytes(self.GOOD if content is None else content)
        return path

    def place(self, issue_id, content=None, name="a.stl", confirm=False):
        path = self.upload_file(content)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return issue_upload.place(self.conn, issue_id, path, name=name, sha256=digest, confirm_name=confirm)

    def refused(self, issue_id, code, **kwargs):
        with self.assertRaises(download_issues.IssueRefused) as caught:
            self.place(issue_id, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return str(caught.exception)


class UploadReplacesTests(UploadCase):
    def test_an_upload_takes_the_files_place_and_holds_the_old_copy(self):
        issue_id = self.flag_and_archive()
        result = self.place(issue_id)
        self.assertEqual(self.nas_file().read_bytes(), self.GOOD)
        held = self.vault / PENDING_DELETION_FOLDER
        self.assertEqual([p.read_bytes() for p in held.rglob("a.stl")], [b"c" * 700])
        self.assertEqual(result["replaced_copy"], str(next(held.rglob("a.stl"))))

    def test_the_upload_overwrites_what_mmf_listed(self):
        self.place(self.flag_and_archive())
        size, confirmed = self.conn.execute("SELECT size_bytes, size_confirmed_at FROM model_files WHERE file_id = 9").fetchone()
        self.assertEqual(size, 900)
        self.assertIsNotNone(confirmed)
        row = self.row()
        self.assertEqual((row["actual_bytes"], row["sha256"]), (900, hashlib.sha256(self.GOOD).hexdigest()))
        self.assertEqual(self.issues(open_only=False)[0]["resolution"], "uploaded")

    def test_the_replaced_copy_is_a_normal_pending_deletion(self):
        self.place(self.flag_and_archive())
        item = pending_deletion.listing(self.conn)["items"][0]
        self.assertTrue(item["replaced"])
        with self.assertRaises(pending_deletion.DeleteRefused) as caught:
            pending_deletion.restore_file(self.conn, item["id"])
        self.assertEqual(caught.exception.code, 409)
        self.assertIn("An upload replaced this copy", str(caught.exception))
        self.assertEqual(self.nas_file().read_bytes(), self.GOOD, "the refused restore changed nothing")

    def test_a_file_that_never_arrived_is_added_where_a_download_would_put_it(self):
        issue_id = self.fail_outright()
        self.place(issue_id)
        self.assertEqual(self.nas_file().read_bytes(), self.GOOD)
        row = self.row()
        self.assertEqual((row["storage_tier"], row["vault_path"]), ("nas", str(self.nas_file())))
        self.assertEqual(row["local_relative_path"], "Maker/2024-01/5_Model/files/a.stl")
        folder = self.nas_file().parent.parent
        vault_path = self.conn.execute("SELECT vault_path FROM collections WHERE id = 5").fetchone()[0]
        self.assertEqual(vault_path, str(folder))
        self.assertTrue((folder / "metadata.json").is_file())

    def test_a_browser_copy_name_counts_as_the_same_file(self):
        self.place(self.flag_and_archive(), name="a (1).stl")
        self.assertEqual(self.nas_file().read_bytes(), self.GOOD)

    def test_the_audit_and_attempt_log_record_the_upload(self):
        self.place(self.flag_and_archive())
        status = self.conn.execute("SELECT status FROM download_audit_log WHERE file_id = 9 ORDER BY id DESC").fetchone()[0]
        self.assertEqual(status, "uploaded")
        action = self.conn.execute("SELECT action FROM audit_log ORDER BY id DESC").fetchone()[0]
        self.assertEqual(action, "download_issue_upload")


class UploadRefusedTests(UploadCase):
    def setUp(self):
        super().setUp()
        self.issue_id = self.flag_and_archive()

    def assert_unchanged(self):
        self.assertEqual(self.nas_file().read_bytes(), b"c" * 700)
        self.assertEqual(len(self.issues()), 1)
        self.assertFalse(list(self.nas_file().parent.glob("*.part")))

    def test_another_kind_of_file_is_refused(self):
        self.assertIn("needs a .stl file", self.refused(self.issue_id, 400, name="a.chitubox"))
        self.assert_unchanged()

    def test_another_name_needs_a_confirm(self):
        self.refused(self.issue_id, 409, name="other.stl")
        self.assert_unchanged()
        self.place(self.issue_id, name="other.stl", confirm=True)
        self.assertEqual(self.nas_file().read_bytes(), self.GOOD)

    def test_a_queued_model_is_refused(self):
        with self.conn:
            self.conn.execute("UPDATE download_jobs SET status = 'pending'")
        self.refused(self.issue_id, 409)
        self.assert_unchanged()

    def test_an_offline_vault_is_refused(self):
        self.setting(download_root_path=self.base / "unplugged")
        self.refused(self.issue_id, 503)

    def test_a_web_page_is_refused(self):
        self.refused(self.issue_id, 400, content=b"<html><body>Sign in</body></html>")
        self.assert_unchanged()

    def test_an_empty_file_is_refused(self):
        self.refused(self.issue_id, 400, content=b"")
        self.assert_unchanged()

    def test_a_settled_issue_is_refused(self):
        download_issues.keep(self.conn, self.issue_id)
        self.refused(self.issue_id, 409)

    def test_a_failed_catalog_write_puts_both_files_back(self):
        with patch.object(issue_upload.queries, "CONFIRM_SIZE", "UPDATE no_such_table SET x = ? WHERE y = ?"):
            self.refused(self.issue_id, 500)
        self.assert_unchanged()
        self.assertFalse(list((self.vault / PENDING_DELETION_FOLDER).rglob("a.stl")))
        self.assertEqual(pending_deletion.listing(self.conn)["items"], [])

    def test_a_copy_that_doesnt_match_changes_nothing(self):
        path = self.upload_file()
        with self.assertRaises(download_issues.IssueRefused) as caught:
            issue_upload.place(self.conn, self.issue_id, path, name="a.stl", sha256="0" * 64)
        self.assertIn("didn't match", str(caught.exception))
        self.assert_unchanged()


class UploadRouteTests(UploadCase):
    def setUp(self):
        super().setUp()
        self.issue_id = self.flag_and_archive()
        app = server.create_app(manage_workers=False)
        app.state.open_database = lambda: get_db_connection(self.db, shared=True)
        app.state.download_manager = lambda: self.dm
        self.client = TestClient(app, base_url="http://localhost")

    def put(self, content, name="a.stl", headers=None, issue_id=None):
        return self.client.put(
            f"/api/download-issues/{issue_id or self.issue_id}/file",
            params={"name": name}, content=content,
            headers=headers or {"content-type": "application/octet-stream"},
        )

    def test_a_streamed_upload_is_placed(self):
        response = self.put(self.GOOD)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.nas_file().read_bytes(), self.GOOD)
        self.assertEqual(list((self.work / "uploads").iterdir()), [], "the received copy is cleaned up")

    def test_an_upload_may_be_larger_than_any_other_request(self):
        with patch.object(app_config, "MAX_BODY_BYTES", 100):
            self.assertEqual(self.put(self.GOOD).status_code, 200)
            other = self.client.post("/api/downloads/queue", content=b"x" * 200, headers={"content-type": "application/json"})
            self.assertEqual(other.status_code, 413)

    def test_an_upload_past_its_own_limit_is_refused(self):
        with patch.object(app_config, "MAX_UPLOAD_BYTES", 100):
            self.assertEqual(self.put(self.GOOD).status_code, 413)
        self.assertEqual(self.nas_file().read_bytes(), b"c" * 700)

    def test_only_raw_bytes_are_taken(self):
        response = self.put(self.GOOD, headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 415)

    def test_another_site_is_refused(self):
        headers = {"content-type": "application/octet-stream", "origin": "https://evil.example"}
        self.assertEqual(self.put(self.GOOD, headers=headers).status_code, 403)

    def test_a_refusal_comes_before_the_transfer(self):
        response = self.put(self.GOOD, name="a.chitubox")
        self.assertEqual(response.status_code, 400)
        self.assertFalse((self.work / "uploads").exists(), "nothing was received for a refused upload")

    def test_an_unknown_issue_is_404(self):
        self.assertEqual(self.put(self.GOOD, issue_id=999).status_code, 404)

    def test_a_full_work_folder_is_refused(self):
        full = type("Usage", (), {"free": 10})()
        with patch("desync_archival.api.routes.download_issues.shutil.disk_usage", return_value=full):
            self.assertEqual(self.put(self.GOOD).status_code, 507)

    def test_keep_and_the_listing(self):
        self.assertEqual(self.client.get("/api/download-issues").json()["open"], 1)
        self.assertEqual(self.client.post(f"/api/download-issues/{self.issue_id}/keep").status_code, 200)
        self.assertEqual(self.client.get("/api/download-issues").json()["items"], [])
        resolved = self.client.get("/api/download-issues", params={"state": "resolved"}).json()["items"]
        self.assertEqual(resolved[0]["resolution"], "kept")

    def test_history_shows_a_finished_job_with_issues(self):
        history = self.client.get("/api/downloads/history").json()["history"]
        self.assertEqual((history[0]["status"], history[0]["open_issues"]), ("completed", 1))
        only = self.client.get("/api/downloads/history", params={"status": "issues"}).json()
        self.assertEqual(only["total"], 1)
        download_issues.keep(self.conn, self.issue_id)
        self.assertEqual(self.client.get("/api/downloads/history", params={"status": "issues"}).json()["total"], 0)

    def test_the_job_window_lists_the_flagged_files(self):
        detail = self.client.get("/api/downloads/jobs/1").json()
        self.assertEqual([issue["filename"] for issue in detail["issues"]], ["a.stl"])

    def test_the_status_poll_counts_open_issues(self):
        self.assertEqual(self.dm.get_status()["open_issues"], 1)
