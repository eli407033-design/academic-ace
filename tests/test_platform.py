import hashlib
import http.cookiejar
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = ROOT / "tests" / f"academic-ace-test-{secrets.token_hex(8)}.sqlite3"
if TEST_DATABASE.exists():
    raise RuntimeError("Refusing to overwrite an existing integration-test database.")
os.environ["APP_SECRET"] = "test-only-secret-that-is-longer-than-thirty-two-characters"
os.environ["DATABASE_PATH"] = str(TEST_DATABASE)
os.environ["APP_ORIGIN"] = "http://127.0.0.1:0"
os.environ["APP_ENV"] = "test"
sys.path.insert(0, str(ROOT))
import server as app  # noqa: E402


class PlatformIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.migrate()
        app.migrate()
        salt = b"integration-test-salt"
        password = "CorrectHorseBatteryStaple!2026"
        hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
        with app.connect_db() as db:
            db.execute("INSERT INTO administrators(email,role,password_hash,password_salt) VALUES(?,?,?,?)", ("owner@example.test", "owner", hashed, salt))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.httpd.daemon_threads = True
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        app.APP_ORIGIN = cls.origin
        cls.cookies = http.cookiejar.CookieJar()
        cls.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cls.cookies))
        cls.password = password

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=3)
        cls.httpd.server_close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(TEST_DATABASE) + suffix).unlink(missing_ok=True)

    def request(self, method, path, payload=None, headers=None, client=None):
        request_headers = {"Origin": self.origin}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.origin + path, data=body, headers=request_headers, method=method)
        try:
            with (client or self.client).open(request, timeout=20) as response:
                raw = response.read()
                parsed = json.loads(raw) if raw and "application/json" in response.headers.get("Content-Type", "") else raw.decode("utf-8") if raw else None
                return response.status, parsed, response.headers
        except urllib.error.HTTPError as error:
            raw = error.read()
            parsed = json.loads(raw) if raw and "application/json" in error.headers.get("Content-Type", "") else raw.decode("utf-8") if raw else None
            return error.code, parsed, error.headers

    def test_public_production_bind_requires_explicit_trusted_ingress(self):
        env = os.environ.copy()
        env.update({
            "APP_ENV": "production",
            "APP_SECRET": "deployment-test-secret-with-more-than-thirty-two-characters",
            "APP_ORIGIN": "https://academic-ace.example",
            "HOST": "0.0.0.0",
            "TRUSTED_HTTPS_INGRESS": "0",
        })
        rejected = subprocess.run([sys.executable, "-c", "import server"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("TRUSTED_HTTPS_INGRESS", rejected.stderr)
        env["TRUSTED_HTTPS_INGRESS"] = "1"
        accepted = subprocess.run([sys.executable, "-c", "import server"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_0_admin_login_logout_csrf_and_route_authorization(self):
        anonymous_jar = http.cookiejar.CookieJar()
        anonymous = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(anonymous_jar))
        protected = [
            ("GET", "/api/admin/summary"), ("GET", "/api/admin/audit"),
            ("GET", "/api/admin/inquiries"), ("GET", "/api/admin/pricing"),
            ("GET", "/api/admin/projects"), ("GET", "/api/admin/samples"),
            ("GET", "/api/admin/business"),
            ("PATCH", "/api/admin/inquiries/1"), ("PUT", "/api/admin/pricing"),
            ("PUT", "/api/admin/business"), ("POST", "/api/admin/quotes"),
            ("POST", "/api/admin/quotes/1/issue"),
            ("POST", "/api/admin/projects/1/milestones"), ("PATCH", "/api/admin/milestones/1"),
            ("POST", "/api/admin/payments"), ("POST", "/api/admin/samples"),
            ("PATCH", "/api/admin/samples/1"), ("PATCH", "/api/admin/projects/1"),
        ]
        for method, path in protected:
            with self.subTest(method=method, path=path):
                status, _, _ = self.request(method, path, {}, client=anonymous)
                self.assertEqual(status, 401)

        status, _, _ = self.request("POST", "/api/admin/login", {"email": "owner@example.test", "password": "incorrect-password"}, client=anonymous)
        self.assertEqual(status, 401)
        status, _, _ = self.request("POST", "/api/admin/login", {"email": "owner@example.test", "password": self.password}, client=anonymous)
        self.assertEqual(status, 200)
        status, session, _ = self.request("GET", "/api/admin/session", client=anonymous)
        self.assertTrue(session["authenticated"])
        status, _, _ = self.request("PUT", "/api/admin/business", {}, {"X-CSRF-Token": "invalid"}, anonymous)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/admin/logout", {}, {"X-CSRF-Token": session["csrf_token"]}, anonymous)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", _.get("Set-Cookie", ""))
        status, session, _ = self.request("GET", "/api/admin/session", client=anonymous)
        self.assertFalse(session["authenticated"])
        status, _, _ = self.request("GET", "/api/admin/inquiries", client=anonymous)
        self.assertEqual(status, 401)

    def test_0_database_failure_is_generic_and_does_not_leak(self):
        original = app.DATABASE_PATH
        try:
            app.DATABASE_PATH = ROOT / "tests"
            status, response, _ = self.request("GET", "/api/health")
        finally:
            app.DATABASE_PATH = original
        self.assertEqual(status, 500)
        self.assertEqual(response["error"], "The service is temporarily unavailable. Please try again later.")
        self.assertNotIn(str(ROOT), json.dumps(response))

    def test_0_app_secret_is_required_even_for_owner_setup(self):
        environment = os.environ.copy()
        environment.pop("APP_SECRET", None)
        environment["DATABASE_PATH"] = str(TEST_DATABASE)
        result = subprocess.run(
            [sys.executable, str(ROOT / "server.py"), "create-admin", "owner@example.test"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("APP_SECRET must be set", result.stderr + result.stdout)

        insecure_environment = os.environ.copy()
        insecure_environment.update({
            "APP_ENV": "production",
            "APP_SECRET": "replace-with-at-least-32-random-characters",
            "APP_ORIGIN": "https://academic-ace.example",
            "HOST": "127.0.0.1",
        })
        insecure = subprocess.run(
            [sys.executable, str(ROOT / "server.py"), "serve"],
            cwd=ROOT,
            env=insecure_environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(insecure.returncode, 0)
        self.assertIn("unique random secret", insecure.stderr + insecure.stdout)

        exposed_environment = os.environ.copy()
        exposed_environment.update({
            "APP_ENV": "production",
            "APP_SECRET": secrets.token_urlsafe(48),
            "APP_ORIGIN": "https://academic-ace.example",
            "HOST": "0.0.0.0",
        })
        exposed = subprocess.run(
            [sys.executable, str(ROOT / "server.py"), "serve"],
            cwd=ROOT,
            env=exposed_environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(exposed.returncode, 0)
        self.assertIn("TRUSTED_HTTPS_INGRESS", exposed.stderr + exposed.stdout)

        previous_environment = app.APP_ENV
        try:
            app.APP_ENV = "production"
            cookie = app.Handler.session_cookie(object.__new__(app.Handler), "test-session-token")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Strict", cookie)
            self.assertIn("Secure", cookie)
        finally:
            app.APP_ENV = previous_environment

        database = ROOT / "tests" / f"owner-setup-{secrets.token_hex(8)}.sqlite3"
        self.assertFalse(database.exists())
        original_database = app.DATABASE_PATH
        owner_password = secrets.token_urlsafe(24)
        try:
            app.DATABASE_PATH = database
            with patch.object(sys, "argv", [str(ROOT / "server.py"), "create-admin", "secure-owner@example.test"]):
                with patch("getpass.getpass", side_effect=[owner_password, owner_password]):
                    app.main()
            owner_db = sqlite3.connect(database)
            try:
                owner = owner_db.execute("SELECT role,password_hash,password_salt FROM administrators WHERE email=?", ("secure-owner@example.test",)).fetchone()
                self.assertIsNotNone(owner)
                self.assertEqual(owner[0], "owner")
                self.assertEqual(owner[1], hashlib.pbkdf2_hmac("sha256", owner_password.encode("utf-8"), owner[2], 600_000))
            finally:
                owner_db.close()
        finally:
            app.DATABASE_PATH = original_database
            for suffix in ("", "-wal", "-shm"):
                Path(str(database) + suffix).unlink(missing_ok=True)

    def test_1_inquiry_validation_persistence_and_idempotency(self):
        home_status, home, home_headers = self.request("GET", "/")
        self.assertEqual(home_status, 200)
        self.assertIn("Academic Ace", home)
        self.assertIn("nonce-", home_headers.get("Content-Security-Policy", ""))
        status, pricing, _ = self.request("GET", "/api/pricing")
        self.assertEqual(status, 200)
        self.assertEqual(pricing["configuration"]["basis"], "per_page")
        self.assertFalse(pricing["approved"])
        self.assertTrue(all(isinstance(v, int) for v in pricing["configuration"]["rates_zmw"].values()))
        with app.connect_db() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")], [1, 2])

        payload = {
            "preferred_name": "Alias One", "contact_method": "email", "contact": "student@example.test",
            "project_type": "proposal", "academic_level": "masters", "discipline": "Public health",
            "institution": "University", "working_title": "Research question", "style_guide": "APA 7",
            "word_count": "2000-2500", "deadline_local": "2026-11-12T17:00", "timezone": "Africa/Lusaka",
            "citation_requirements": "Recent sources", "support_requested": "Method feedback", "rubric": "Discuss method",
            "integrity_confirmed": True, "privacy_consent": True, "website": "",
        }
        key = "integration-test-key-000001"
        status, saved, _ = self.request("POST", "/api/inquiries", payload, {"Idempotency-Key": key})
        self.assertEqual(status, 201)
        self.assertRegex(saved["reference"], r"^AA-\d{6}-[A-F0-9]{8}$")
        status, repeated, _ = self.request("POST", "/api/inquiries", payload, {"Idempotency-Key": key})
        self.assertEqual(status, 200)
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["reference"], saved["reference"])
        with app.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT contact FROM inquiries").fetchone()[0], "student@example.test")

        bad = dict(payload, contact="invalid")
        status, error, _ = self.request("POST", "/api/inquiries", bad, {"Idempotency-Key": "integration-test-key-000002"})
        self.assertEqual(status, 400)
        self.assertIn("valid email", error["error"])
        status, _, _ = self.request("POST", "/api/inquiries", payload, {"Idempotency-Key": "integration-test-key-000003", "Origin": "https://not-academic-ace.invalid"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/admin/inquiries")
        self.assertEqual(status, 401)
        for private_path in ("/.env", "/data/academic_ace.sqlite3", "/backups/database.sqlite3", "/migrations/001_initial.sql"):
            with self.subTest(private_path=private_path):
                self.assertEqual(self.request("GET", private_path)[0], 404)

    def test_2_admin_quote_project_payment_samples_and_retention(self):
        status, login, _ = self.request("POST", "/api/admin/login", {"email": "owner@example.test", "password": self.password})
        self.assertEqual(status, 200)
        self.assertTrue(login["authenticated"])
        status, session, _ = self.request("GET", "/api/admin/session")
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["role"], "owner")
        csrf = session["csrf_token"]
        auth_headers = {"X-CSRF-Token": csrf}
        status, inquiry_list, _ = self.request("GET", "/api/admin/inquiries")
        self.assertEqual(status, 200)
        self.assertEqual(len(inquiry_list["inquiries"]), 1)
        self.assertEqual(len(inquiry_list["inquiries"][0]["client_history"]), 1)
        status, initial_business, _ = self.request("GET", "/api/business")
        self.assertEqual(status, 200)
        self.assertEqual(initial_business["contact_email"], "")
        contact_config = {"contact_email": "hello@academic-ace.example", "whatsapp_number": "+260971000001", "signal_number": "", "contact_hours": "Weekdays, CAT"}
        status, _, _ = self.request("PUT", "/api/admin/business", contact_config, auth_headers)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/business")[1]["whatsapp_number"], "+260971000001")
        status, _, _ = self.request("PUT", "/api/admin/pricing", {"configuration": {}}, {"X-CSRF-Token": ""})
        self.assertEqual(status, 403, "Administrator mutations must require CSRF protection.")
        status, quote_page, headers = self.request("GET", "/quote/accept")
        self.assertEqual(status, 200)
        self.assertIn("Quotation review", quote_page)
        self.assertIn("nonce-", headers.get("Content-Security-Policy", ""))
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

        admin_salt = b"administrator-test-salt"
        admin_password = "AnotherCorrectPassword!2026"
        admin_hash = hashlib.pbkdf2_hmac("sha256", admin_password.encode(), admin_salt, 600_000)
        with app.connect_db() as db:
            db.execute("INSERT INTO administrators(email,role,password_hash,password_salt) VALUES(?,?,?,?)", ("staff@example.test", "administrator", admin_hash, admin_salt))
        admin_jar = http.cookiejar.CookieJar()
        admin_client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(admin_jar))
        status, _, _ = self.request("POST", "/api/admin/login", {"email": "staff@example.test", "password": admin_password}, client=admin_client)
        self.assertEqual(status, 200)
        status, admin_session, _ = self.request("GET", "/api/admin/session", client=admin_client)
        self.assertEqual(admin_session["role"], "administrator")
        status, _, _ = self.request("PUT", "/api/admin/pricing", {"configuration": {}, "approved": True}, {"X-CSRF-Token": admin_session["csrf_token"]}, admin_client)
        self.assertEqual(status, 403, "Only the owner role may change public pricing.")
        status, _, _ = self.request("PUT", "/api/admin/business", contact_config, {"X-CSRF-Token": admin_session["csrf_token"]}, admin_client)
        self.assertEqual(status, 403, "Only the owner role may change public contact settings.")

        pricing = self.request("GET", "/api/admin/pricing")[1]
        config = pricing["configuration"]
        config["rates_zmw"]["proposal"] = 333
        status, updated, _ = self.request("PUT", "/api/admin/pricing", {"configuration": config, "approved": True}, auth_headers)
        self.assertEqual(status, 201)
        self.assertEqual(updated["version"], 2)
        self.assertTrue(updated["approved"])
        self.assertGreaterEqual(len(self.request("GET", "/api/admin/audit")[1]["events"]), 2)

        inquiry_id = 1
        status, created, _ = self.request("POST", "/api/admin/quotes", {"inquiry_id": inquiry_id, "amount_zmw": 1550, "scope": "Proposal consultation and review"}, auth_headers)
        self.assertEqual(status, 201)
        quote_id = created["quote_id"]
        quote_history = self.request("GET", "/api/admin/inquiries")[1]["inquiries"][0]["quotations"]
        self.assertEqual(quote_history[0]["status"], "draft")
        link_path = created["acceptance_link"].removeprefix(self.origin)
        token = link_path.split("token=", 1)[1]
        status, _, _ = self.request("GET", "/api/quotes/accept?token=" + token)
        self.assertEqual(status, 404, "Draft quotes must not accept client responses yet.")
        status, _, _ = self.request("POST", f"/api/admin/quotes/{quote_id}/issue", {}, auth_headers)
        self.assertEqual(status, 200)
        status, public_quote, _ = self.request("GET", "/api/quotes/accept?token=" + token)
        self.assertEqual(status, 200)
        self.assertEqual(public_quote["quotation"]["amount_zmw"], 1550)
        self.assertEqual(public_quote["quotation"]["currency"], "ZMW")
        with app.connect_db() as db:
            stored = db.execute("SELECT pricing_version,pricing_snapshot_json FROM quotations WHERE id=?", (quote_id,)).fetchone()
            self.assertEqual(stored["pricing_version"], 2)
            self.assertEqual(json.loads(stored["pricing_snapshot_json"])["configuration"]["rates_zmw"]["proposal"], 333)

        newer_config = self.request("GET", "/api/admin/pricing")[1]["configuration"]
        newer_config["rates_zmw"]["proposal"] = 444
        status, newer_pricing, _ = self.request("PUT", "/api/admin/pricing", {"configuration": newer_config, "approved": True}, auth_headers)
        self.assertEqual(status, 201)
        self.assertEqual(newer_pricing["version"], 3)
        with app.connect_db() as db:
            stored = db.execute("SELECT pricing_version,pricing_snapshot_json FROM quotations WHERE id=?", (quote_id,)).fetchone()
            self.assertEqual(stored["pricing_version"], 2)
            self.assertEqual(json.loads(stored["pricing_snapshot_json"])["configuration"]["rates_zmw"]["proposal"], 333)

        status, accepted, _ = self.request("POST", "/api/quotes/accept?token=" + token, {"response": "accept"})
        self.assertEqual(status, 200)
        self.assertTrue(accepted["project_created"])
        projects = self.request("GET", "/api/admin/projects")[1]["projects"]
        self.assertEqual(len(projects), 1)
        project = projects[0]
        self.assertEqual(project["status"], "confirmed")
        status, milestone, _ = self.request("POST", f"/api/admin/projects/{project['id']}/milestones", {"title": "Methods review", "due_at": "2026-11-01T12:00"}, auth_headers)
        self.assertEqual(status, 201)
        status, _, _ = self.request("PATCH", f"/api/admin/milestones/{milestone['milestone_id']}", {"status": "in_progress"}, auth_headers)
        self.assertEqual(status, 200)
        status, _, _ = self.request("PATCH", f"/api/admin/projects/{project['id']}", {"status": "waiting_on_client"}, auth_headers)
        self.assertEqual(status, 200)
        project = self.request("GET", "/api/admin/projects")[1]["projects"][0]
        self.assertEqual(project["milestones"][-1]["status"], "in_progress")
        self.assertEqual(project["status"], "waiting_on_client")

        payment = {"project_id": project["id"], "quotation_id": quote_id, "amount_zmw": 1000, "status": "successful", "transaction_reference": "TEST-TXN-001", "note": "Test-only verified entry"}
        status, _, _ = self.request("POST", "/api/admin/payments", dict(payment, transaction_reference=""), auth_headers)
        self.assertEqual(status, 400, "A successful payment record must include a verified transaction reference.")
        status, _, _ = self.request("POST", "/api/admin/payments", dict(payment, amount_zmw=100_000_001, transaction_reference="TEST-TXN-OVER-LIMIT"), auth_headers)
        self.assertEqual(status, 400, "Payment amounts above the configured record limit must be rejected.")
        status, _, _ = self.request("POST", "/api/admin/payments", dict(payment, status="unknown", transaction_reference="TEST-TXN-INVALID-STATUS"), auth_headers)
        self.assertEqual(status, 400, "Unknown payment statuses must be rejected.")
        status, record, _ = self.request("POST", "/api/admin/payments", payment, auth_headers)
        self.assertEqual(status, 201)
        self.assertEqual(record["recording_method"], "administrator_recorded")
        status, _, _ = self.request("POST", "/api/admin/payments", dict(payment, amount_zmw=600, transaction_reference="TEST-TXN-002"), auth_headers)
        self.assertEqual(status, 409, "Successful recorded payments must not exceed the quote total.")
        status, _, _ = self.request("POST", "/api/admin/payments", payment, auth_headers)
        self.assertEqual(status, 409, "Transaction references must not be duplicated.")

        status, created_sample, _ = self.request("POST", "/api/admin/samples", {"title": "Example methodology", "category": "methodology", "description": "Illustrative only", "preview_text": "A sample paragraph.", "published": False}, auth_headers)
        self.assertEqual(status, 201)
        private_samples = self.request("GET", "/api/admin/samples")[1]["samples"]
        self.assertEqual(len(private_samples), 1)
        self.assertFalse(private_samples[0]["published"])
        self.assertEqual(self.request("GET", "/api/samples")[1]["samples"], [])
        status, _, _ = self.request("PATCH", f"/api/admin/samples/{created_sample['sample_id']}", {"published": True}, auth_headers)
        self.assertEqual(status, 200)
        public_samples = self.request("GET", "/api/samples?q=method")[1]["samples"]
        self.assertEqual(len(public_samples), 1)

        # Snapshot and restore a real test-only database backup before retention deletes old records.
        backup = ROOT / "tests" / f"recovery-{secrets.token_hex(8)}.sqlite3"
        restored = ROOT / "tests" / f"restored-{secrets.token_hex(8)}.sqlite3"
        self.assertFalse(backup.exists())
        self.assertFalse(restored.exists())
        app.make_backup(str(backup))
        saved_db = sqlite3.connect(backup)
        try:
            self.assertEqual(saved_db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(saved_db.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0], 1)
            self.assertEqual(saved_db.execute("SELECT COUNT(*) FROM quotations").fetchone()[0], 1)
            self.assertEqual(saved_db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
            restore_db = sqlite3.connect(restored)
            try:
                saved_db.backup(restore_db)
            finally:
                restore_db.close()
        finally:
            saved_db.close()
        try:
            restored_db = sqlite3.connect(restored)
            try:
                self.assertEqual(restored_db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(restored_db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
            finally:
                restored_db.close()
        finally:
            for target in (backup, restored):
                for suffix in ("", "-wal", "-shm"):
                    Path(str(target) + suffix).unlink(missing_ok=True)

        # Restart the local HTTP server on the same isolated database and confirm records remain available.
        self.httpd.shutdown()
        self.thread.join(timeout=3)
        self.httpd.server_close()
        self.__class__.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.__class__.httpd.daemon_threads = True
        self.__class__.thread = threading.Thread(target=self.__class__.httpd.serve_forever, daemon=True)
        self.__class__.thread.start()
        self.origin = f"http://127.0.0.1:{self.__class__.httpd.server_address[1]}"
        self.__class__.origin = self.origin
        app.APP_ORIGIN = self.origin
        self.assertEqual(self.request("GET", "/api/admin/inquiries")[0], 200)
        self.assertEqual(len(self.request("GET", "/api/admin/inquiries")[1]["inquiries"]), 1)
        self.assertEqual(len(self.request("GET", "/api/admin/projects")[1]["projects"]), 1)
        self.assertEqual(self.request("GET", "/api/admin/projects")[1]["projects"][0]["status"], "waiting_on_client")

        status, expired_quote, _ = self.request("POST", "/api/admin/quotes", {"inquiry_id": inquiry_id, "amount_zmw": 900, "scope": "Expired test quotation", "valid_until": "2099-01-01T00:00:00Z"}, auth_headers)
        self.assertEqual(status, 201)
        expired_token = expired_quote["acceptance_link"].split("token=", 1)[1]
        with app.connect_db() as db:
            db.execute("UPDATE quotations SET valid_until='2000-01-01T00:00:00Z' WHERE id=?", (expired_quote["quote_id"],))
        status, _, _ = self.request("POST", f"/api/admin/quotes/{expired_quote['quote_id']}/issue", {}, auth_headers)
        self.assertEqual(status, 409, "Expired drafts must not be marked as shared.")
        with app.connect_db() as db:
            db.execute("UPDATE quotations SET status='sent' WHERE id=?", (expired_quote["quote_id"],))
        status, expired_response, _ = self.request("POST", "/api/quotes/accept?token=" + expired_token, {"response": "accept"})
        self.assertEqual(status, 409)
        with app.connect_db() as db:
            self.assertEqual(db.execute("SELECT status FROM quotations WHERE id=?", (expired_quote["quote_id"],)).fetchone()[0], "expired")

        # The retention command is tested only against this isolated test database.
        os.environ["RETENTION_DAYS"] = "30"
        with app.connect_db() as db:
            old = "2000-01-01T00:00:00Z"
            db.execute("UPDATE projects SET status='completed',updated_at=? WHERE id=?", (old, project["id"]))
        dry = app.purge_expired(dry_run=True)
        self.assertEqual(dry["projects"], 1)
        self.assertEqual(dry["inquiries"], 1)
        deleted = app.purge_expired(dry_run=False)
        self.assertEqual(deleted["projects"], 1)
        with app.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM payment_records").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0], 0)

    def test_3_documented_serve_command_starts_on_loopback_and_persists(self):
        database = ROOT / "tests" / f"cli-startup-{secrets.token_hex(8)}.sqlite3"
        self.assertFalse(database.exists())
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update({
            "APP_ENV": "development",
            "APP_SECRET": secrets.token_urlsafe(48),
            "APP_ORIGIN": origin,
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "DATABASE_PATH": str(database),
            "RETENTION_DAYS": "365",
            "TRUST_PROXY": "0",
        })

        def start_server():
            return subprocess.Popen(
                [sys.executable, str(ROOT / "server.py"), "serve"],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        process = start_server()
        try:
            ready = False
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urllib.request.urlopen(origin + "/api/health", timeout=1) as response:
                        ready = response.status == 200 and json.loads(response.read())["status"] == "ok"
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            self.assertTrue(ready, "The documented server command did not become healthy on loopback.")
            with urllib.request.urlopen(origin + "/", timeout=3) as response:
                self.assertIn("Academic Ace", response.read().decode("utf-8"))
            inquiry = {
                "preferred_name": "CLI Test", "contact_method": "email", "contact": "cli-test@example.test",
                "project_type": "assignment", "academic_level": "undergraduate", "integrity_confirmed": True,
                "privacy_consent": True,
            }
            request = urllib.request.Request(
                origin + "/api/inquiries", data=json.dumps(inquiry).encode("utf-8"), method="POST",
                headers={"Origin": origin, "Content-Type": "application/json", "Idempotency-Key": "cli-startup-inquiry-0001"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 201)
            process.terminate()
            process.communicate(timeout=5)
            ready = False
            process = start_server()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urllib.request.urlopen(origin + "/api/health", timeout=1) as response:
                        ready = response.status == 200
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            self.assertTrue(ready, "The server did not restart against the same test database.")
            db = sqlite3.connect(database)
            try:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0], 1)
            finally:
                db.close()
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
            for suffix in ("", "-wal", "-shm"):
                Path(str(database) + suffix).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
