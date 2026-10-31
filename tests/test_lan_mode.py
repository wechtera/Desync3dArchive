"""LAN mode: anonymous requests fail, login works, and the app refuses to start unprotected."""

import http.client
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from db_helper import create_database
from fastapi.testclient import TestClient

from desync_archival import settings
from desync_archival.api import server
from desync_archival.services import login

PASSWORD = "correct horse battery"


class LanCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {
            "DESYNC_REQUIRE_LOGIN": "1", "DESYNC_SECRETS_FILE": str(self.base / "conf" / "secrets.json"),
        })
        self.env.start()
        create_database(server.DB_PATH)
        login.clear_sessions()
        login.clear_failures()
        login.set_password(PASSWORD)
        self.client = TestClient(
            server.create_app(manage_workers=False), base_url="https://localhost:8443", follow_redirects=False
        )

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def log_in(self, password=PASSWORD):
        return self.client.post("/api/login", json={"password": password})


class PasswordTests(LanCase):
    def test_stored_as_a_salted_hash_in_a_private_file(self):
        path = settings.secrets_file()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        text = path.read_text()
        self.assertNotIn(PASSWORD, text)
        self.assertNotEqual(login.hash_password("same")["hash"], login.hash_password("same")["hash"])

    def test_verification(self):
        self.assertTrue(login.verify_password(PASSWORD))
        self.assertFalse(login.verify_password("wrong password here"))

    def test_short_passwords_are_refused(self):
        with self.assertRaises(login.WeakPasswordError):
            login.set_password("short")

    def test_no_file_means_no_password(self):
        with patch.dict(os.environ, {"DESYNC_SECRETS_FILE": str(self.base / "none.json")}):
            self.assertFalse(login.has_password())
            self.assertFalse(login.verify_password("anything at all"))

    def test_changing_the_password_signs_everyone_out(self):
        self.log_in()
        login.set_password("a brand new password")
        self.assertEqual(self.client.get("/api/settings").status_code, 401)


class AnonymousTests(LanCase):
    def test_api_returns_401(self):
        for path in ("/api/settings", "/api/models", "/api/tasks/status", "/api/stashes"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.json(), {"error": "Login required"})

    def test_pages_and_files_redirect_to_login(self):
        for path in ("/", "/index.html", "/static/js/app.js", "/previews/1/cover.jpg"):
            response = self.client.get(path)
            self.assertEqual((response.status_code, response.headers["location"]), (303, "/login"), path)

    def test_writes_are_blocked(self):
        for path in ("/api/settings", "/api/reconcile/delete-orphans", "/api/tasks/clear", "/api/auth/token_callback"):
            self.assertEqual(self.client.post(path, json={}).status_code, 401, path)
        self.assertEqual(self.client.delete("/api/stashes/1").status_code, 401)

    def test_every_route_is_protected_except_the_public_list(self):
        for route in server.create_app(manage_workers=False).routes:
            path = getattr(route, "path", "")
            if path in server.LoginMiddleware.PUBLIC_PATHS or path.startswith("/static") or "{rest" in path:
                continue
            target = path.replace("{model_id:int}", "1").replace("{rel_path:path}", "a").replace("{stash_id:int}", "1")
            target = target.replace("{item_id:int}", "1").replace("{file_id:int}", "1").replace("{library_id:int}", "1")
            for method in getattr(route, "methods", None) or ():
                if method != "HEAD":
                    response = self.client.request(method, target)
                    self.assertIn(response.status_code, (401, 303), f"{method} {target}")

    def test_a_forged_cookie_fails(self):
        self.client.cookies.set(login.COOKIE_NAME, "forged-value")
        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_login_assets_stay_open(self):
        for path in ("/login", "/static/css/vault.css", "/static/js/login.js"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_callback_page_stays_open_and_holds_no_data(self):
        response = self.client.get("/api/auth/callback")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("mmf_", response.text)

    def test_session_status_reveals_nothing(self):
        self.assertEqual(self.client.get("/api/session").json(), {"login_required": True, "authenticated": False})


class LoginTests(LanCase):
    def test_login_opens_the_app_and_logout_closes_it(self):
        response = self.log_in()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/settings").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertTrue(self.client.get("/api/session").json()["authenticated"])
        self.assertEqual(self.client.post("/api/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_a_logged_out_cookie_can_not_be_reused(self):
        self.log_in()
        token = self.client.cookies.get(login.COOKIE_NAME)
        self.client.post("/api/logout")
        self.client.cookies.set(login.COOKIE_NAME, token)
        self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_cookie_flags(self):
        cookie = self.log_in().headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=strict"):
            self.assertIn(flag, cookie)

    def test_expired_session_fails(self):
        self.log_in()
        with patch("desync_archival.services.login.time.time", return_value=time.time() + login.SESSION_SECONDS + 5):
            self.assertEqual(self.client.get("/api/settings").status_code, 401)

    def test_wrong_password_is_refused(self):
        self.assertEqual(self.log_in("wrong password here").status_code, 401)
        self.assertNotIn(login.COOKIE_NAME, self.client.cookies)

    def test_bad_bodies_are_refused(self):
        headers = {"Content-Type": "application/json"}
        for body in (b"{}", b"not json", b'{"password": 5}'):
            self.assertEqual(self.client.post("/api/login", content=body, headers=headers).status_code, 400)

    def test_lockout_after_repeated_failures(self):
        for _ in range(login.MAX_FAILURES):
            self.assertEqual(self.log_in("wrong password here").status_code, 401)
        self.assertEqual(self.log_in().status_code, 429)

    def test_lockout_ends_after_the_window(self):
        for _ in range(login.MAX_FAILURES):
            self.log_in("wrong password here")
        later = time.time() + login.LOCKOUT_SECONDS + 5
        with patch("desync_archival.services.login.time.time", return_value=later):
            self.assertFalse(login.is_locked_out("testclient"))

    def test_success_clears_failures(self):
        for _ in range(login.MAX_FAILURES - 1):
            self.log_in("wrong password here")
        self.log_in()
        self.client.cookies.clear()
        self.assertEqual(self.log_in("wrong password here").status_code, 401)

    def test_passwords_never_reach_the_logs(self):
        from desync_archival.services import logger as log

        self.log_in("a-distinct-wrong-password")
        self.assertNotIn("a-distinct-wrong-password", log.APP_LOG_PATH.read_text(encoding="utf-8"))


class LoopbackTests(unittest.TestCase):
    def test_loopback_needs_no_login(self):
        with patch.dict(os.environ, {"DESYNC_REQUIRE_LOGIN": "", "DESYNC_HOST": "127.0.0.1"}):
            self.assertFalse(settings.login_required())
            client = TestClient(server.create_app(manage_workers=False), base_url="http://localhost")
            self.assertEqual(client.get("/api/session").json()["login_required"], False)

    def test_a_lan_address_requires_login(self):
        for host in ("0.0.0.0", "192.168.1.20"):
            with patch.dict(os.environ, {"DESYNC_HOST": host}):
                self.assertTrue(settings.login_required())


class StartupRefusalTests(LanCase):
    def certificate(self):
        cert, key = self.base / "cert.pem", self.base / "key.pem"
        cert.write_text("cert")
        key.write_text("key")
        return {"DESYNC_TLS_CERT": str(cert), "DESYNC_TLS_KEY": str(key)}

    def test_refuses_without_a_password(self):
        with patch.dict(os.environ, {"DESYNC_HOST": "0.0.0.0", "DESYNC_SECRETS_FILE": str(self.base / "x.json"), **self.certificate()}), \
                self.assertRaisesRegex(settings.SettingsError, "no password"):
            server.check_network_ready("0.0.0.0")

    def test_refuses_without_a_certificate(self):
        with patch.dict(os.environ, {"DESYNC_HOST": "0.0.0.0", "DESYNC_TLS_CERT": "", "DESYNC_TLS_KEY": ""}), \
                self.assertRaisesRegex(settings.SettingsError, "DESYNC_TLS_CERT"):
            server.check_network_ready("0.0.0.0")

    def test_refuses_when_certificate_files_are_missing(self):
        env = {"DESYNC_HOST": "0.0.0.0", "DESYNC_TLS_CERT": "/none/c.pem", "DESYNC_TLS_KEY": "/none/k.pem"}
        with patch.dict(os.environ, env), self.assertRaises(settings.SettingsError):
            server.check_network_ready("0.0.0.0")

    def test_starts_with_both(self):
        with patch.dict(os.environ, {"DESYNC_HOST": "0.0.0.0", **self.certificate()}):
            server.check_network_ready("0.0.0.0")


class RealLanRefusalTests(unittest.TestCase):
    def test_the_server_command_will_not_bind_to_the_lan_unprotected(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp).resolve()
            create_database(data / "library.db")
            env = {
                **os.environ, "APP_ENV": "test", "DESYNC_DATA_DIR": str(data), "DESYNC_ALLOWED_ROOTS": str(data),
                "DESYNC_HOST": "0.0.0.0", "DESYNC_SECRETS_FILE": str(data / "secrets.json"),
                "DESYNC_TLS_CERT": "", "DESYNC_TLS_KEY": "",
            }
            result = subprocess.run(
                [sys.executable, "-m", "desync_archival.api.server"],
                env=env, capture_output=True, text=True, timeout=30, check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("set-password", result.stderr + result.stdout)


class RealTlsServerTests(unittest.TestCase):
    """Runs the real server command over TLS, with a throwaway certificate, on loopback."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name).resolve()
        create_database(self.data / "library.db")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost", "-days", "1",
             "-keyout", str(self.data / "key.pem"), "-out", str(self.data / "cert.pem")],
            check=True, capture_output=True,
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.env = {
            **os.environ, "APP_ENV": "test", "DESYNC_DATA_DIR": str(self.data), "DESYNC_PORT": str(self.port),
            "DESYNC_ALLOWED_ROOTS": str(self.data), "DESYNC_REQUIRE_LOGIN": "1",
            "DESYNC_SECRETS_FILE": str(self.data / "secrets.json"),
            "DESYNC_TLS_CERT": str(self.data / "cert.pem"), "DESYNC_TLS_KEY": str(self.data / "key.pem"),
        }

    def tearDown(self):
        self.temp.cleanup()

    def request(self, method, path, body=None, cookie=None):
        context = ssl.create_default_context(cafile=str(self.data / "cert.pem"))
        context.check_hostname = False
        connection = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=10, context=context)
        headers = {"Content-Type": "application/json", "Host": f"localhost:{self.port}"}
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=json.dumps(body) if body else None, headers=headers)
        response = connection.getresponse()
        response.read()
        connection.close()
        return response

    def test_tls_login_and_anonymous_refusal(self):
        result = subprocess.run(
            [sys.executable, "-m", "desync_archival.dbtool", "set-password"],
            input=PASSWORD + "\n", env=self.env, capture_output=True, text=True, check=True,
        )
        self.assertNotIn(PASSWORD, result.stdout)
        process = subprocess.Popen(
            [sys.executable, "-m", "desync_archival.api.server"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    self.assertEqual(self.request("GET", "/api/settings").status, 401)
                    break
                except (ConnectionError, ssl.SSLError):
                    self.assertIsNone(process.poll(), "the server exited")
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.1)

            answer = self.request("POST", "/api/login", {"password": PASSWORD})
            self.assertEqual(answer.status, 200)
            cookie = answer.getheader("Set-Cookie").split(";")[0]
            self.assertEqual(self.request("GET", "/api/settings", cookie=cookie).status, 200)
        finally:
            process.terminate()
            process.wait(timeout=15)

    def test_plain_http_is_not_served(self):
        subprocess.run(
            [sys.executable, "-m", "desync_archival.dbtool", "set-password"],
            input=PASSWORD + "\n", env=self.env, capture_output=True, text=True, check=True,
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "desync_archival.api.server"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    self.request("GET", "/login")
                    break
                except (ConnectionError, ssl.SSLError):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.1)
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            with self.assertRaises((http.client.HTTPException, ConnectionError)):
                connection.request("GET", "/api/settings")
                connection.getresponse().read()
            connection.close()
        finally:
            process.terminate()
            process.wait(timeout=15)


if __name__ == "__main__":
    unittest.main()
