"""Tests JSON log output, correlation IDs, redaction and the UI log buffer."""

import json
import logging
import threading
import unittest

from desync_archival.services import logger as log


def read_entries():
    for handler in log.logger.handlers:
        handler.flush()
    lines = log.APP_LOG_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def last_entry():
    return read_entries()[-1]


class RedactionTests(unittest.TestCase):
    def assert_redacted(self, text, secret):
        result = log.redact(text)
        self.assertNotIn(secret, result)
        self.assertIn(log.REDACTED, result)

    def test_bearer_tokens(self):
        self.assert_redacted("Authorization: Bearer abcdef1234567890", "abcdef1234567890")
        self.assert_redacted("headers bearer abcdef1234567890", "abcdef1234567890")

    def test_key_value_tokens(self):
        self.assert_redacted("access_token=abc123def", "abc123def")
        self.assert_redacted("client_secret: abc123def", "abc123def")
        self.assert_redacted("mmf_refresh_token=abc123def next", "abc123def")

    def test_json_values(self):
        self.assert_redacted('{"access_token": "abc123def", "ok": 1}', "abc123def")
        self.assert_redacted("{'client_secret': 'abc123def'}", "abc123def")
        self.assert_redacted('{"cookies_json": "[{\\"value\\": \\"abc123def\\"}]"}', "abc123def")

    def test_cookies(self):
        self.assert_redacted("cf_clearance=abc123def", "abc123def")
        self.assert_redacted("PHPSESSID=abc123def", "abc123def")
        result = log.redact("Cookie: PHPSESSID=abc123def; cf_clearance=xyz987uvw")
        self.assertNotIn("abc123def", result)
        self.assertNotIn("xyz987uvw", result)

    def test_signed_urls(self):
        url = "https://cdn.example.com/a.zip?X-Amz-Signature=abc123&X-Amz-Credential=xyz789&name=a"
        result = log.redact(url)
        self.assertNotIn("abc123", result)
        self.assertNotIn("xyz789", result)
        self.assertIn("name=a", result)
        self.assert_redacted("https://x.example/f?token=abc123def", "abc123def")
        self.assert_redacted("https://x.example/f?a=1&signature=abc123def", "abc123def")

    def test_ordinary_text_is_kept(self):
        for text in (
            "Worker #1: Claimed Model #12",
            "Saved to /data/vault/Creator/2026-08/file.zip",
            "https://www.myminifactory.com/download/1?archive_id=2",
            "Token refresh failed with status 401",
        ):
            self.assertEqual(log.redact(text), text)


class JsonOutputTests(unittest.TestCase):
    def test_each_line_is_json_with_expected_fields(self):
        log.logger.info("json check")
        entry = last_entry()
        self.assertEqual(entry["message"], "json check")
        self.assertEqual(entry["level"], "INFO")
        for field in ("ts", "thread", "layer", "request_id", "job_id"):
            self.assertIn(field, entry)

    def test_message_arguments_are_rendered(self):
        log.logger.info("model %s of %s", 1, 2)
        self.assertEqual(last_entry()["message"], "model 1 of 2")

    def test_secrets_never_reach_the_file_or_ui_buffer(self):
        log.logger.warning("download failed access_token=abc123def")
        self.assertNotIn("abc123def", log.APP_LOG_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("abc123def", "\n".join(log.get_ui_logs()))

    def test_secret_in_message_arguments_is_redacted(self):
        log.logger.warning("sent %s", "access_token=abc123xyz")
        self.assertNotIn("abc123xyz", log.APP_LOG_PATH.read_text(encoding="utf-8"))

    def test_exception_text_is_recorded_and_redacted(self):
        try:
            raise ValueError("bad access_token=abc123exc")
        except ValueError:
            log.logger.exception("failed")
        entry = last_entry()
        self.assertIn("ValueError", entry["exception"])
        self.assertNotIn("abc123exc", entry["exception"])


class ContextTests(unittest.TestCase):
    def test_request_and_job_ids_are_recorded(self):
        with log.log_context(request_id="req-1", job_id="task-7"):
            log.logger.info("inside")
        entry = last_entry()
        self.assertEqual((entry["request_id"], entry["job_id"]), ("req-1", "task-7"))

    def test_ids_are_cleared_after_the_block(self):
        with log.log_context(request_id="req-2"):
            pass
        log.logger.info("outside")
        self.assertIsNone(last_entry()["request_id"])

    def test_nested_contexts_keep_outer_values(self):
        with log.log_context(request_id="req-3"), log.log_context(job_id="task-8"):
            log.logger.info("nested")
        entry = last_entry()
        self.assertEqual((entry["request_id"], entry["job_id"]), ("req-3", "task-8"))

    def test_job_logged_binds_id_in_a_worker_thread(self):
        class Worker:
            @log.job_logged(lambda self, task_id: f"task-{task_id}")
            def run(self, task_id):
                log.logger.info("worker line")

        thread = threading.Thread(target=Worker().run, args=(42,))
        thread.start()
        thread.join()
        self.assertEqual(last_entry()["job_id"], "task-42")

    def test_concurrent_threads_keep_separate_ids(self):
        barrier = threading.Barrier(2)

        @log.job_logged(lambda task_id: f"task-{task_id}")
        def run(task_id):
            barrier.wait(timeout=5)
            log.logger.info("concurrent %s", task_id)

        threads = [threading.Thread(target=run, args=(i,)) for i in (101, 102)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        entries = [e for e in read_entries() if e["message"].startswith("concurrent")]
        self.assertEqual(
            {(e["message"].split()[-1], e["job_id"]) for e in entries[-2:]},
            {("101", "task-101"), ("102", "task-102")},
        )


class ExistingBehaviorTests(unittest.TestCase):
    def test_ui_buffer_stays_human_readable(self):
        log.logger.info("ui line")
        line = log.get_ui_logs(1)[0]
        self.assertIn(" | INFO", line)
        self.assertIn("ui line", line)

    def test_disabled_layer_is_suppressed_and_enabled_layer_is_tagged(self):
        previous = log.get_logging_config()
        try:
            log.set_logging_config("INFO", [])
            log.log_layer("storage", logging.INFO, "hidden line")
            self.assertNotEqual(last_entry()["message"], "[STORAGE] hidden line")
            log.set_logging_config("INFO", ["storage"])
            log.log_layer("storage", logging.INFO, "shown line")
            entry = last_entry()
            self.assertEqual((entry["message"], entry["layer"]), ("[STORAGE] shown line", "storage"))
        finally:
            log.set_logging_config(previous["level"], previous["layers"])


if __name__ == "__main__":
    unittest.main()
