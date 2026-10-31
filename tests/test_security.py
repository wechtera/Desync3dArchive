"""One test group for each security finding closed in Step 2: V1 to V5, V9 and part of V11."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

from db_helper import init_persistent_tables
from fastapi.testclient import TestClient

from desync_archival import settings
from desync_archival.api import server
from desync_archival.api.routes.settings import _merge_cookies
from desync_archival.api.routes.tasks import handle_get_scrapes_download
from desync_archival.api.routing import GENERIC_ERROR
from desync_archival.services import auth_service
from desync_archival.services import logger as log
from desync_archival.services.db import get_db_connection

JSON_HEADERS = {"Content-Type": "application/json"}
SECRET_VALUES = {
    "PHPSESSID": "php-secret-value",
    "cf_clearance": "cf-secret-value",
    "cookie_cf_clearance": "cookie-cf-secret",
    "cookie_phpsessid": "cookie-php-secret",
    "cookies_json": '[{"name": "cf_clearance", "value": "cookie-json-secret"}, {"name": "other", "value": "keep"}]',
    "mmf_access_token": "access-secret-value",
    "mmf_client_secret": "client-secret-value",
    "mmf_oauth_state": "state-secret-value",
    "mmf_refresh_token": "refresh-secret-value",
}


def token_response(access="new-access", refresh="new-refresh", expires_in=3600):
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "user_id": 7, "access_token": access, "expires_in": expires_in,
        "token_type": "Bearer", "refresh_token": refresh,
    }
    return response


class AppCase(unittest.TestCase):
    def setUp(self):
        conn = get_db_connection(server.DB_PATH)
        init_persistent_tables(conn)
        auth_service.set_settings(conn, {key: "" for key in auth_service.SECRET_SETTING_KEYS})
        auth_service.set_settings(conn, {"mmf_client_id": "client", "mmf_token_expires_at": "0", "mmf_redirect_uri": ""})
        conn.close()
        self.client = TestClient(
            server.create_app(manage_workers=False), base_url="http://localhost:8080", follow_redirects=False
        )

    def tearDown(self):
        self.set(mmf_redirect_uri="")

    def setting(self, key):
        conn = get_db_connection(server.DB_PATH)
        try:
            return auth_service.get_setting(conn, key)
        finally:
            conn.close()

    def set(self, **values):
        conn = get_db_connection(server.DB_PATH)
        auth_service.set_settings(conn, values)
        conn.close()


class V1BindTests(unittest.TestCase):
    def test_default_bind_is_loopback(self):
        with patch.dict(os.environ, {"DESYNC_HOST": "", "DESYNC_PORT": ""}):
            self.assertEqual((settings.host(), settings.port()), ("127.0.0.1", 8080))

    def test_host_and_port_come_from_settings(self):
        with patch.dict(os.environ, {"DESYNC_HOST": "127.0.0.2", "DESYNC_PORT": "9000"}):
            self.assertEqual((settings.host(), settings.port()), ("127.0.0.2", 9000))

    def test_invalid_port_is_refused(self):
        with patch.dict(os.environ, {"DESYNC_PORT": "abc"}), self.assertRaises(settings.SettingsError):
            settings.port()

    def test_bind_beyond_loopback_is_refused(self):
        for host in ("0.0.0.0", "192.168.1.5", "example.com"):
            with self.assertRaises(settings.SettingsError):
                server.check_bind_host(host)
        for host in ("127.0.0.1", "::1", "localhost"):
            server.check_bind_host(host)


class V2SecretExposureTests(AppCase):
    def setUp(self):
        super().setUp()
        self.set(**SECRET_VALUES)

    def test_get_settings_returns_no_secret_values(self):
        response = self.client.get("/api/settings")
        for key, value in SECRET_VALUES.items():
            self.assertNotIn(value, response.text, key)
            self.assertNotIn(key, {k for k in response.json() if k != "secrets_configured"})

    def test_get_settings_reports_what_is_configured(self):
        self.set(mmf_refresh_token="")
        configured = self.client.get("/api/settings").json()["secrets_configured"]
        self.assertTrue(configured["mmf_client_secret"])
        self.assertTrue(configured["cookies_json"])
        self.assertFalse(configured["mmf_refresh_token"])

    def test_auth_status_returns_no_secret_values(self):
        response = self.client.get("/api/auth/status")
        for value in SECRET_VALUES.values():
            self.assertNotIn(value, response.text)
        self.assertNotIn("client_secret", response.json())

    def test_blank_values_do_not_overwrite_secrets(self):
        body = {"cookies_json": "", "cookie_cf_clearance": " ", "cookie_phpsessid": "", "worker_concurrency": "3"}
        self.assertEqual(self.client.post("/api/settings", json=body).status_code, 200)
        self.assertEqual(self.setting("cookies_json"), SECRET_VALUES["cookies_json"])
        self.assertEqual(self.setting("cookie_cf_clearance"), SECRET_VALUES["cookie_cf_clearance"])
        self.assertEqual(self.setting("worker_concurrency"), "3")

    def test_a_secret_can_be_replaced(self):
        self.client.post("/api/settings", json={"cookies_json": '[{"name": "cf_clearance", "value": "fresh"}]'})
        self.assertIn("fresh", self.setting("cookies_json"))

    def test_tokens_can_not_be_injected_through_settings(self):
        self.client.post("/api/settings", json={"mmf_access_token": "injected", "mmf_oauth_state": "injected"})
        self.assertEqual(self.setting("mmf_access_token"), SECRET_VALUES["mmf_access_token"])
        self.assertEqual(self.setting("mmf_oauth_state"), SECRET_VALUES["mmf_oauth_state"])

    def test_replacing_one_cookie_keeps_the_others(self):
        self.client.post("/api/settings", json={"cookie_cf_clearance": "fresh-cf"})
        cookies = {c["name"]: c["value"] for c in json.loads(self.setting("cookies_json"))}
        self.assertEqual(cookies, {"cf_clearance": "fresh-cf", "other": "keep"})

    def test_merged_cookies_are_secure_with_the_right_domain(self):
        merged = json.loads(_merge_cookies("", {"cookie_rememberme": "x"}))[0]
        self.assertEqual((merged["name"], merged["domain"]), ("REMEMBERME", "www.myminifactory.com"))
        self.assertTrue(merged["secure"])

    def test_each_cookie_field_is_saved_hidden_and_merged(self):
        values = {key: f"value-{key}-123456" for key in auth_service.COOKIE_FIELDS}
        self.client.post("/api/settings", json=values)
        names = {c["name"]: c["value"] for c in json.loads(self.setting("cookies_json"))}
        for key, (name, _) in auth_service.COOKIE_FIELDS.items():
            self.assertEqual(names[name], values[key])
        response = self.client.get("/api/settings")
        self.assertNotIn("value-cookie_rememberme", response.text)
        self.assertTrue(response.json()["secrets_configured"]["cookie_rememberme"])

    def test_cookie_values_never_reach_the_logs(self):
        self.assertNotIn("abc123def", log.redact("REMEMBERME=abc123def; __cf_bm=abc123def"))

    def test_merge_adds_missing_cookies_and_survives_bad_json(self):
        merged = json.loads(_merge_cookies("not json", {"cookie_phpsessid": "x"}))
        self.assertEqual([(c["name"], c["value"]) for c in merged], [("PHPSESSID", "x")])


class V2ScrapeDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        run = self.root / "scrapes" / "scrape_1"
        run.mkdir(parents=True)
        for name in ("model_ids.txt", "models.json", "cookies.json"):
            (run / name).write_text(f"content of {name}")
        (self.root / "cookies.json").write_text("live cookies")
        (self.root / "library.db").write_text("db")
        self.manager = MagicMock(root_dir=self.root)

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, path):
        return handle_get_scrapes_download(None, None, {}, {"path": [path]}, task_manager=self.manager)

    def test_allowed_artifacts_are_served(self):
        self.assertEqual(self.fetch("scrapes/scrape_1/model_ids.txt"), ("content of model_ids.txt", 200))
        self.assertEqual(self.fetch("scrapes/scrape_1/models.json"), ("content of models.json", 200))

    def test_everything_else_is_refused(self):
        for path in ("cookies.json", "scrapes/scrape_1/cookies.json", "library.db", "scrapes/model_ids.txt",
                     "scrapes/a/b/model_ids.txt", "metadata/model_1.json"):
            _, status = self.fetch(path)
            self.assertEqual(status, 403, path)

    def test_traversal_is_refused(self):
        self.assertEqual(self.fetch("scrapes/../cookies.json")[1], 400)
        self.assertEqual(self.fetch("/etc/passwd")[1], 400)


class V3RequestValidationTests(AppCase):
    def test_no_wildcard_cors_header(self):
        response = self.client.get("/api/metrics")
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_foreign_host_is_refused(self):
        for host in ("evil.example", "evil.example:8080", "localhost.evil.example"):
            self.assertEqual(self.client.get("/api/settings", headers={"Host": host}).status_code, 400, host)

    def test_allowed_hosts_pass(self):
        for host in ("localhost:8080", "127.0.0.1:8080", "[::1]:8080"):
            self.assertEqual(self.client.get("/api/settings", headers={"Host": host}).status_code, 200, host)

    def test_extra_allowed_host_comes_from_settings(self):
        with patch.dict(os.environ, {"DESYNC_ALLOWED_HOSTS": "vault.local"}):
            self.assertEqual(self.client.get("/", headers={"Host": "vault.local"}).status_code, 200)

    def test_cross_origin_is_refused(self):
        for origin in ("https://evil.example", "null", "http://localhost:9999", "https://localhost:8080.evil.example"):
            response = self.client.post("/api/settings", json={}, headers={"Origin": origin})
            self.assertEqual(response.status_code, 403, origin)

    def test_cross_origin_read_is_refused(self):
        self.assertEqual(self.client.get("/api/settings", headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_same_origin_passes(self):
        response = self.client.post("/api/settings", json={}, headers={"Origin": "http://localhost:8080"})
        self.assertEqual(response.status_code, 200)

    def test_simple_form_post_is_refused(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            response = self.client.post("/api/settings", content=b'{"a": "b"}', headers={"Content-Type": content_type})
            self.assertEqual(response.status_code, 415, content_type)

    def test_body_without_content_type_is_refused(self):
        self.assertEqual(self.client.request("DELETE", "/api/stashes/1", content=b"{}").status_code, 415)

    def test_json_with_charset_and_empty_posts_pass(self):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        self.assertEqual(self.client.post("/api/settings", content=b"{}", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/disconnect").status_code, 200)


class ContentSecurityPolicyTests(AppCase):
    def test_every_response_has_the_policy(self):
        for path in ("/", "/api/settings", "/static/js/app.js", "/api/does-not-exist"):
            policy = self.client.get(path).headers["content-security-policy"]
            self.assertIn("script-src 'self'", policy)
            self.assertIn("object-src 'none'", policy)
            self.assertNotIn("unsafe-eval", policy)

    def test_scripts_allow_no_inline_code(self):
        policy = self.client.get("/").headers["content-security-policy"]
        script_rule = next(rule for rule in policy.split("; ") if rule.startswith("script-src"))
        self.assertEqual(script_rule, "script-src 'self'")

    def test_callback_page_has_no_inline_script(self):
        body = self.client.get("/api/auth/callback").text
        self.assertEqual(body.count("<script"), 1)
        self.assertIn('src="/static/js/auth_callback.js"', body)


class V4StaticServingTests(AppCase):
    def test_source_and_data_are_not_served(self):
        for path in ("/server.py", "/routes/auth.py", "/library.db", "/.env", "/static/../index.html/x",
                     "/static/../../pyproject.toml"):
            self.assertIn(self.client.get(path).status_code, (404, 405), path)

    def test_directory_listings_are_off(self):
        for path in ("/static/", "/static/js/", "/static/js/views/"):
            self.assertEqual(self.client.get(path).status_code, 404, path)


class V5OAuthFlowTests(AppCase):
    def login(self):
        data = self.client.get("/api/auth/login?redirect=0").json()
        return data["state"], urllib.parse.parse_qs(urllib.parse.urlsplit(data["auth_url"]).query)

    def test_login_uses_the_implicit_grant(self):
        _, query = self.login()
        self.assertEqual(query["response_type"], ["token"])
        self.assertEqual(query["redirect_uri"], [auth_service.DEFAULT_REDIRECT_URI])

    def test_a_stored_secret_changes_nothing(self):
        self.set(mmf_client_secret="an-api-key")
        _, query = self.login()
        self.assertEqual(query["response_type"], ["token"])
        self.assertNotIn("an-api-key", self.client.get("/api/auth/status").text)

    def test_the_api_can_not_store_a_secret(self):
        self.client.post("/api/auth/config", json={"client_id": "new", "client_secret": "x", "api_key": "y"})
        self.assertEqual(self.setting("mmf_client_secret"), "")

    def test_redirect_uri_is_a_setting(self):
        response = self.client.post("/api/auth/config", json={"redirect_uri": "http://localhost:9000/api/auth/callback"})
        self.assertEqual(response.status_code, 200)
        _, query = self.login()
        self.assertEqual(query["redirect_uri"], ["http://localhost:9000/api/auth/callback"])

    def test_invalid_redirect_uri_is_refused(self):
        for value in ("javascript:alert(1)", "not a url", "ftp://x/y"):
            self.assertEqual(self.client.post("/api/auth/config", json={"redirect_uri": value}).status_code, 400)

    def test_callback_page_is_served(self):
        response = self.client.get("/api/auth/callback")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/static/js/auth_callback.js", response.text)

    @patch("desync_archival.services.auth_service.fetch_user_profile")
    def test_token_callback_refuses_wrong_or_missing_state(self, profile):
        for body in ({"access_token": "injected"}, {"access_token": "injected", "state": "wrong"}):
            self.set(mmf_oauth_state="real-state")
            self.assertEqual(self.client.post("/api/auth/token_callback", json=body).status_code, 403)
        self.assertEqual(self.setting("mmf_access_token"), "")
        profile.assert_not_called()

    @patch("desync_archival.services.auth_service.fetch_user_profile", return_value=("maker", "7"))
    def test_state_is_single_use(self, _profile):
        self.set(mmf_oauth_state="real-state")
        body = {"access_token": "implicit-token", "state": "real-state"}
        self.assertEqual(self.client.post("/api/auth/token_callback", json=body).status_code, 200)
        self.assertEqual(self.setting("mmf_access_token"), "implicit-token")
        self.assertEqual(self.client.post("/api/auth/token_callback", json=body).status_code, 403)


class ProfileLookupTests(unittest.TestCase):
    def test_a_rejected_token_is_reported_without_crashing(self):
        for status in (401, 500):
            with patch("httpx.Client.get", return_value=MagicMock(status_code=status)):
                self.assertEqual(auth_service.fetch_user_profile("token"), ("", ""))


class NoSecretSettingTests(unittest.TestCase):
    """No route, service or view may read the API key. Step 9 blanks the stored value."""

    def test_only_the_hidden_list_names_it(self):
        root = Path(__file__).resolve().parent.parent
        sources = [*(root / "src").rglob("*.py"), *(root / "web" / "static" / "js").rglob("*.js"), root / "web" / "index.html"]
        readers = {
            path.name for path in sources
            if "vendor" not in path.parts and "mmf_client_secret" in path.read_text(encoding="utf-8")
        }
        # auth_service lists it to hide it from the API, and the logger redacts it.
        self.assertEqual(readers, {"auth_service.py", "logger.py"})
        source = (root / "src/desync_archival/services/auth_service.py").read_text(encoding="utf-8")
        self.assertNotIn('get_setting(conn, "mmf_client_secret")', source)


class SignInFollowUpTests(AppCase):
    def live_token(self):
        self.set(mmf_access_token="live-token", mmf_token_expires_at=str(int(time.time()) + 3600), mmf_username="")

    @patch("desync_archival.services.auth_service.fetch_user_profile", return_value=("maker", "7"))
    def test_status_fills_in_a_missing_username(self, profile):
        self.live_token()
        data = self.client.get("/api/auth/status").json()
        self.assertEqual((data["username"], data["user_id"]), ("maker", "7"))
        self.assertEqual(self.setting("mmf_username"), "maker")
        self.client.get("/api/auth/status")
        profile.assert_called_once()

    @patch("desync_archival.services.auth_service.fetch_user_profile", return_value=("", ""))
    def test_status_survives_a_failed_profile_lookup(self, _profile):
        self.live_token()
        self.assertTrue(self.client.get("/api/auth/status").json()["connected"])

    @patch("desync_archival.services.auth_service.fetch_user_profile", return_value=("maker", "7"))
    def test_workers_get_the_new_token_after_sign_in(self, _profile):
        manager = MagicMock()
        self.set(mmf_oauth_state="real-state")
        with patch.object(server, "download_manager", manager):
            body = {"access_token": "fresh-token", "state": "real-state"}
            self.assertEqual(self.client.post("/api/auth/token_callback", json=body).status_code, 200)
        manager._load_settings.assert_called_once()

    def test_workers_drop_the_token_on_disconnect(self):
        manager = MagicMock()
        with patch.object(server, "download_manager", manager):
            self.client.post("/api/auth/disconnect")
        manager._load_settings.assert_called_once()


class V9RedirectTests(AppCase):
    def test_login_is_a_real_redirect_with_encoded_values(self):
        self.set(mmf_client_id="x'><script>alert(1)</script>")
        response = self.client.get("/api/auth/login")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.text, "")
        self.assertNotIn("<script>", response.headers["location"])
        self.assertTrue(response.headers["location"].startswith(auth_service.MMF_AUTHORIZE_URL))


class V11ErrorAndBodyTests(AppCase):
    def test_exception_detail_goes_to_the_log_not_the_client(self):
        with patch("desync_archival.api.routes.catalog.queries.GET_METRICS_1", new="SELECT private path /secret/place"):
            response = self.client.get("/api/metrics")
        self.assertEqual((response.status_code, response.json()), (500, {"error": GENERIC_ERROR}))
        self.assertIn("Unhandled error in GET /api/metrics", log.APP_LOG_PATH.read_text(encoding="utf-8"))

    def test_oversized_body_is_refused_by_length(self):
        body = b'"' + b"x" * settings.MAX_BODY_BYTES + b'"'
        self.assertEqual(self.client.post("/api/settings", content=body, headers=JSON_HEADERS).status_code, 413)

    def test_oversized_stream_without_length_is_refused(self):
        def chunks():
            for _ in range(5):
                yield b"x" * (settings.MAX_BODY_BYTES // 2)

        response = self.client.post("/api/settings", content=chunks(), headers=JSON_HEADERS)
        self.assertEqual(response.status_code, 413)

    def test_body_at_the_limit_passes(self):
        padding = "x" * (settings.MAX_BODY_BYTES - 40)
        body = json.dumps({"worker_concurrency": "2", "note": padding}).encode()
        self.assertEqual(self.client.post("/api/settings", content=body, headers=JSON_HEADERS).status_code, 200)
        conn = sqlite3.connect(server.DB_PATH)
        conn.execute("DELETE FROM app_settings WHERE key = 'note'")
        conn.commit()
        conn.close()


if __name__ == "__main__":
    unittest.main()
