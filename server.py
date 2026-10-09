#!/usr/bin/env python3
"""Academic Ace small-business application server (Python standard library only).

Run behind an HTTPS reverse proxy in production. Do not expose the development
HTTP server directly to the public internet.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import http.cookies
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"
APP_ENV = os.environ.get("APP_ENV", "development").lower()
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", str(ROOT / "data" / "academic_ace.sqlite3")))
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "127.0.0.1")
APP_ORIGIN = os.environ.get("APP_ORIGIN", f"http://127.0.0.1:{PORT}").rstrip("/")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
TRUSTED_HTTPS_INGRESS = os.environ.get("TRUSTED_HTTPS_INGRESS", "0") == "1"
SESSION_SECONDS = 8 * 60 * 60
MAX_BODY_BYTES = 256 * 1024
STATUSES = {"new", "under_review", "awaiting_client", "quoted", "accepted", "declined", "closed"}
PROJECT_STATUSES = {"confirmed", "in_progress", "waiting_on_client", "completed", "cancelled"}
PAYMENT_STATUSES = {"pending", "successful", "failed", "cancelled", "refunded"}
SAMPLE_CATEGORIES = {"proposal", "methodology", "literature_review", "referencing", "editing", "data_analysis"}
PROJECT_TYPES = {"assignment", "proposal", "thesis", "editing"}
LEVELS = {"undergraduate", "masters", "phd"}
CONTACT_METHODS = {"email", "signal", "whatsapp"}
BUSINESS_SETTING_KEYS = {"contact_email", "whatsapp_number", "signal_number", "contact_hours"}

try:
    APP_SECRET = os.environ["APP_SECRET"].encode("utf-8")
except KeyError:
    APP_SECRET = b""

if len(APP_SECRET) < 32:
    raise SystemExit("APP_SECRET must be set to at least 32 characters. See .env.example and README.md.")
if APP_SECRET == b"replace-with-at-least-32-random-characters":
    raise SystemExit("Replace the example APP_SECRET with a unique random secret.")
if APP_ENV == "production" and not APP_ORIGIN.startswith("https://"):
    raise SystemExit("APP_ORIGIN must use HTTPS when APP_ENV=production.")
if APP_ENV == "production" and HOST not in {"127.0.0.1", "::1", "localhost"} and not TRUSTED_HTTPS_INGRESS:
    raise SystemExit("Public production binds require TRUSTED_HTTPS_INGRESS=1 behind a managed HTTPS ingress.")

DEFAULT_PRICING = {
    "basis": "per_page",
    "rates_zmw": {"assignment": 150, "proposal": 220, "thesis": 280, "editing": 90},
    "level_multipliers": {"undergraduate": 1.0, "masters": 1.25, "phd": 1.5},
    "urgency_multipliers": {"24h": 2.2, "3d": 1.65, "7d": 1.25, "14d": 1.0},
    "maximum_pages": 500,
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, fields: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.fields = fields or {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def secret_hash(value: str) -> str:
    return hmac.new(APP_SECRET, value.encode("utf-8"), hashlib.sha256).hexdigest()


def connect_db() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


class ClosingConnection(sqlite3.Connection):
    """Give `with connection:` both transactional and close semantics."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def migrate() -> None:
    with connect_db() as db:
        for migration in sorted(MIGRATIONS.glob("[0-9]*.sql")):
            version = int(migration.name.split("_", 1)[0])
            already = db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone() if table_exists(db, "schema_migrations") else None
            if already:
                continue
            script = migration.read_text(encoding="utf-8")
            db.executescript(f"BEGIN IMMEDIATE;\n{script}\nINSERT INTO schema_migrations(version) VALUES({version});\nCOMMIT;")
        if not db.execute("SELECT 1 FROM pricing_configurations LIMIT 1").fetchone():
            db.execute(
                "INSERT INTO pricing_configurations(version,configuration_json,approved) VALUES(1,?,0)",
                (json.dumps(DEFAULT_PRICING, separators=(",", ":")),),
            )
        db.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (utc_iso(),))
        db.execute("DELETE FROM rate_limits WHERE expires_at < ?", (int(time.time()),))


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def normalize_text(value: object, field: str, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        if required:
            raise ApiError(400, "Please review the required fields.", {field: "This field is required."})
        return ""
    result = value.strip()
    if required and not result:
        raise ApiError(400, "Please review the required fields.", {field: "This field is required."})
    if len(result) > limit:
        raise ApiError(400, "One or more fields are too long.", {field: f"Use {limit} characters or fewer."})
    return result


def valid_local_deadline(value: str) -> bool:
    if not value:
        return True
    try:
        datetime.fromisoformat(value)
        return len(value) <= 32
    except ValueError:
        return False


def config_latest(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute("SELECT * FROM pricing_configurations ORDER BY version DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("Pricing configuration was not initialized")
    return row


def audit(db: sqlite3.Connection, admin_id: int | None, action: str, entity_type: str, entity_id: object, details: dict | None = None) -> None:
    db.execute(
        "INSERT INTO audit_events(administrator_id,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?)",
        (admin_id, action, entity_type, str(entity_id), json.dumps(details or {}, separators=(",", ":"))),
    )


def check_pricing_config(data: object) -> dict:
    if not isinstance(data, dict):
        raise ApiError(400, "Pricing configuration is invalid.")
    rates = data.get("rates_zmw")
    levels = data.get("level_multipliers")
    urgency = data.get("urgency_multipliers")
    if not isinstance(rates, dict) or set(rates) != PROJECT_TYPES:
        raise ApiError(400, "Pricing configuration must include all four project types.")
    for key, value in rates.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000:
            raise ApiError(400, f"The {key} rate must be a whole ZMW amount from K1 to K1,000,000 per page.")
    if not isinstance(levels, dict) or set(levels) != LEVELS:
        raise ApiError(400, "Pricing configuration must include all academic levels.")
    for value in levels.values():
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0.1 <= value <= 10:
            raise ApiError(400, "Academic-level multipliers must be between 0.1 and 10.")
    if not isinstance(urgency, dict) or set(urgency) != {"24h", "3d", "7d", "14d"}:
        raise ApiError(400, "Pricing configuration must include all turnaround options.")
    for value in urgency.values():
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0.1 <= value <= 10:
            raise ApiError(400, "Turnaround multipliers must be between 0.1 and 10.")
    maximum = data.get("maximum_pages", 500)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 5000:
        raise ApiError(400, "Maximum pages must be a whole number from 1 to 5,000.")
    return {
        "basis": "per_page",
        "rates_zmw": {key: rates[key] for key in sorted(PROJECT_TYPES)},
        "level_multipliers": {key: float(levels[key]) for key in sorted(LEVELS)},
        "urgency_multipliers": {key: float(urgency[key]) for key in ("24h", "3d", "7d", "14d")},
        "maximum_pages": maximum,
    }


def serialize_inquiry(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["integrity_confirmed"] = bool(result["integrity_confirmed"])
    result["privacy_consent"] = bool(result["privacy_consent"])
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "AcademicAce/1.0"
    sys_version = ""

    def log_message(self, _format: str, *_args) -> None:
        # Never log request bodies, query strings, contact data, or session tokens.
        address_tag = secret_hash(self.client_address[0])[:16] if self.client_address else "unknown"
        print(f"{self.log_date_time_string()} {address_tag} {self.command} {urlparse(self.path).path}", file=sys.stderr)

    def send_headers(self, status: int, content_type: str, extra: list[tuple[str, str]] | None = None, length: int | None = None, csp: str | None = None, referrer_policy: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", referrer_policy or "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", csp or "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")
        if length is not None:
            self.send_header("Content-Length", str(length))
        for key, value in extra or []:
            self.send_header(key, value)
        self.end_headers()

    def json_response(self, status: int, value: dict, extra: list[tuple[str, str]] | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_headers(status, "application/json; charset=utf-8", extra, len(payload))
        self.wfile.write(payload)

    def html_response(self, status: int, file_name: str) -> None:
        content = (ROOT / file_name).read_text(encoding="utf-8")
        nonce = secrets.token_urlsafe(18)
        content = content.replace("<script>", f'<script nonce="{nonce}">')
        payload = content.encode("utf-8")
        csp = f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        referrer_policy = "no-referrer" if file_name == "quote.html" else None
        self.send_headers(status, "text/html; charset=utf-8", length=len(payload), csp=csp, referrer_policy=referrer_policy)
        self.wfile.write(payload)

    def body_json(self) -> dict:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ApiError(400, "Request size is invalid.") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ApiError(413, "Request is too large.")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "Request must contain valid JSON.") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "Request body must be an object.")
        return data

    def ensure_same_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != APP_ORIGIN:
            raise ApiError(403, "This request origin is not allowed.")

    def db(self) -> sqlite3.Connection:
        return connect_db()

    def rate_limit(self, purpose: str, limit: int, seconds: int) -> None:
        remote = self.client_address[0] if self.client_address else "unknown"
        if TRUST_PROXY and remote in {"127.0.0.1", "::1"}:
            forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
            try:
                remote = str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        key = secret_hash(f"{purpose}:{remote}")
        now = int(time.time())
        with self.db() as db:
            row = db.execute("SELECT * FROM rate_limits WHERE key_hash=?", (key,)).fetchone()
            if row is None or now - row["window_started_at"] >= seconds:
                db.execute(
                    "INSERT INTO rate_limits(key_hash,window_started_at,request_count,expires_at) VALUES(?,?,1,?) "
                    "ON CONFLICT(key_hash) DO UPDATE SET window_started_at=excluded.window_started_at,request_count=1,expires_at=excluded.expires_at",
                    (key, now, now + seconds * 2),
                )
                return
            if row["request_count"] >= limit:
                raise ApiError(429, "Too many requests. Please wait a little and try again.")
            db.execute("UPDATE rate_limits SET request_count=request_count+1 WHERE key_hash=?", (key,))

    def session(self, required: bool = True) -> sqlite3.Row | None:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            jar = http.cookies.SimpleCookie()
        cookie = jar.get("aa_admin_session")
        if not cookie:
            if required:
                raise ApiError(401, "Administrator sign-in is required.")
            return None
        token_hash = secret_hash(cookie.value)
        with self.db() as db:
            row = db.execute(
                "SELECT s.*,a.email,a.role FROM admin_sessions s JOIN administrators a ON a.id=s.administrator_id WHERE s.token_hash=? AND s.expires_at>?",
                (token_hash, utc_iso()),
            ).fetchone()
        if row is None and required:
            raise ApiError(401, "Your administrator session has expired. Please sign in again.")
        return row

    def require_admin(self, mutate: bool = False) -> sqlite3.Row:
        session = self.session()
        if mutate:
            self.ensure_same_origin()
            supplied = self.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(supplied, session["csrf_token"]):
                raise ApiError(403, "Security token is missing or expired. Refresh and try again.")
        return session

    def do_GET(self) -> None:
        try:
            self.route("GET")
        except ApiError as error:
            self.json_response(error.status, {"error": error.message, "fields": error.fields})
        except (sqlite3.Error, OSError) as error:
            print(f"Request failed ({type(error).__name__})", file=sys.stderr)
            self.json_response(500, {"error": "The service is temporarily unavailable. Please try again later."})
        except Exception as error:
            print(f"Unexpected request failure ({type(error).__name__})", file=sys.stderr)
            self.json_response(500, {"error": "The service is temporarily unavailable. Please try again later."})

    def do_POST(self) -> None:
        try:
            self.route("POST")
        except ApiError as error:
            self.json_response(error.status, {"error": error.message, "fields": error.fields})
        except (sqlite3.Error, OSError) as error:
            print(f"Request failed ({type(error).__name__})", file=sys.stderr)
            self.json_response(500, {"error": "The service is temporarily unavailable. Please try again later."})
        except Exception as error:
            print(f"Unexpected request failure ({type(error).__name__})", file=sys.stderr)
            self.json_response(500, {"error": "The service is temporarily unavailable. Please try again later."})

    def do_PATCH(self) -> None:
        try:
            self.route("PATCH")
        except ApiError as error:
            self.json_response(error.status, {"error": error.message, "fields": error.fields})
        except (sqlite3.Error, OSError) as error:
            print(f"Request failed ({type(error).__name__})", file=sys.stderr)
            self.json_response(500, {"error": "The service is temporarily unavailable. Please try again later."})
        except Exception as error:
            print(f"Unexpected request failure ({type(error).__name__})", file=sys.stderr)
            self.json_response(500, {"error": "The service is temporarily unavailable. Please try again later."})

    def do_PUT(self) -> None:
        try:
            self.route("PUT")
        except ApiError as error:
            self.json_response(error.status, {"error": error.message, "fields": error.fields})
        except (sqlite3.Error, OSError) as error:
            print(f"Request failed ({type(error).__name__})", file=sys.stderr)
            self.json_response(500, {"error": "The service is temporarily unavailable. Please try again later."})
        except Exception as error:
            print(f"Unexpected request failure ({type(error).__name__})", file=sys.stderr)
            self.json_response(500, {"error": "The service is temporarily unavailable. Please try again later."})

    def route(self, method: str) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"} and method == "GET":
            return self.html_response(200, "index.html")
        if path == "/admin" and method == "GET":
            return self.html_response(200, "admin.html")
        if path == "/privacy" and method == "GET":
            return self.html_response(200, "privacy.html")
        if path == "/terms" and method == "GET":
            return self.html_response(200, "terms.html")
        if path == "/api/health" and method == "GET":
            with self.db() as db:
                db.execute("SELECT 1").fetchone()
            return self.json_response(200, {"status": "ok"})
        if path == "/api/pricing" and method == "GET":
            with self.db() as db:
                row = config_latest(db)
            return self.json_response(200, {"version": row["version"], "approved": bool(row["approved"]), "configuration": json.loads(row["configuration_json"])})
        if path == "/api/business" and method == "GET":
            with self.db() as db:
                settings = {row["setting_key"]: row["setting_value"] for row in db.execute("SELECT setting_key,setting_value FROM business_settings").fetchall()}
            return self.json_response(200, {key: settings.get(key, "") for key in BUSINESS_SETTING_KEYS})
        if path == "/api/samples" and method == "GET":
            query = parse_qs(urlparse(self.path).query)
            search = normalize_text(query.get("q", [""])[0], "q", 100).casefold()
            category = query.get("category", [""])[0]
            if category and category not in SAMPLE_CATEGORIES:
                raise ApiError(400, "Choose a valid sample category.")
            with self.db() as db:
                rows = db.execute("SELECT id,slug,title,category,description,preview_text,created_at FROM samples WHERE published=1 ORDER BY category,title").fetchall()
            samples = [dict(row) for row in rows if (not category or row["category"] == category) and (not search or search in (row["title"] + " " + row["description"] + " " + row["category"]).casefold())]
            return self.json_response(200, {"samples": samples})
        match = re.fullmatch(r"/quote/accept", path)
        if match and method == "GET":
            return self.html_response(200, "quote.html")
        if path == "/api/inquiries" and method == "POST":
            return self.create_inquiry()
        if path == "/api/admin/login" and method == "POST":
            return self.admin_login()
        if path == "/api/admin/session" and method == "GET":
            session = self.session(required=False)
            if session is None:
                return self.json_response(200, {"authenticated": False})
            return self.json_response(200, {"authenticated": True, "email": session["email"], "role": session["role"], "csrf_token": session["csrf_token"]})
        if path == "/api/admin/logout" and method == "POST":
            session = self.require_admin(mutate=True)
            with self.db() as db:
                db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (session["token_hash"],))
                audit(db, session["administrator_id"], "logout", "administrator", session["administrator_id"])
            return self.json_response(200, {"ok": True}, [("Set-Cookie", self.expire_cookie())])
        if path.startswith("/api/quotes/accept"):
            query = parse_qs(urlparse(self.path).query)
            token = query.get("token", [""])[0]
            if method == "GET":
                return self.get_public_quote(token)
            if method == "POST":
                return self.respond_public_quote(token)
        if path.startswith("/api/admin/"):
            return self.admin_routes(method, path)
        return self.json_response(404, {"error": "Page or service not found."})

    def create_inquiry(self) -> None:
        self.ensure_same_origin()
        self.rate_limit("inquiries", 6, 3600)
        data = self.body_json()
        if normalize_text(data.get("website", ""), "website", 150):
            raise ApiError(400, "We could not accept this inquiry. Please try again.")
        idempotency_key = self.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{16,100}", idempotency_key):
            raise ApiError(400, "Refresh the page and try submitting again.")
        method = normalize_text(data.get("contact_method"), "contact_method", 20, True).lower()
        if method not in CONTACT_METHODS:
            raise ApiError(400, "Choose a supported contact method.", {"contact_method": "Choose email, Signal, or WhatsApp."})
        contact = normalize_text(data.get("contact"), "contact", 254, True)
        if method == "email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", contact):
            raise ApiError(400, "Enter a valid email address.", {"contact": "Check the email address."})
        if method != "email" and not re.fullmatch(r"\+?[0-9][0-9 ()-]{7,22}", contact):
            raise ApiError(400, "Enter a phone number with its country code.", {"contact": "Use digits and an optional leading +."})
        project = normalize_text(data.get("project_type"), "project_type", 30, True).lower()
        if project not in PROJECT_TYPES:
            raise ApiError(400, "Choose a valid project type.")
        level = normalize_text(data.get("academic_level"), "academic_level", 30)
        if level and level not in LEVELS:
            raise ApiError(400, "Choose a valid academic level.")
        text_fields = {
            "preferred_name": (100, False), "discipline": (120, False), "institution": (160, False),
            "working_title": (240, False), "style_guide": (60, False), "word_count": (60, False),
            "deadline_local": (32, False), "timezone": (80, False), "citation_requirements": (1500, False),
            "rubric": (5000, False), "support_requested": (2000, False),
        }
        values: dict[str, str] = {}
        for key, (limit, required) in text_fields.items():
            values[key] = normalize_text(data.get(key, ""), key, limit, required)
        if not valid_local_deadline(values["deadline_local"]):
            raise ApiError(400, "Enter a valid deadline date and time.", {"deadline_local": "Use the date and time picker."})
        if not data.get("integrity_confirmed") or not data.get("privacy_consent"):
            raise ApiError(400, "Please confirm the academic-integrity and privacy statements.", {"consent": "Both acknowledgements are required."})
        with self.db() as db:
            reference = "AA-" + utc_now().strftime("%y%m%d") + "-" + secrets.token_hex(4).upper()
            cursor = db.execute(
                "INSERT OR IGNORE INTO inquiries(public_reference,idempotency_key,preferred_name,contact_method,contact,project_type,academic_level,discipline,institution,working_title,style_guide,word_count,deadline_local,timezone,citation_requirements,rubric,support_requested,integrity_confirmed,privacy_consent) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (reference, idempotency_key, values["preferred_name"], method, contact, project, level, values["discipline"], values["institution"], values["working_title"], values["style_guide"], values["word_count"], values["deadline_local"], values["timezone"], values["citation_requirements"], values["rubric"], values["support_requested"], 1, 1),
            )
            if not cursor.rowcount:
                previous = db.execute("SELECT public_reference FROM inquiries WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if previous:
                    return self.json_response(200, {"reference": previous["public_reference"], "duplicate": True})
                raise ApiError(409, "This inquiry could not be saved with that request key. Refresh and try again.")
        return self.json_response(201, {"reference": reference, "status": "received", "next_steps": "Your inquiry is saved. The team can review its scope and prepare a quotation."})

    def admin_login(self) -> None:
        self.ensure_same_origin()
        self.rate_limit("admin-login", 8, 900)
        data = self.body_json()
        email = normalize_text(data.get("email"), "email", 254, True).lower()
        password = data.get("password")
        if not isinstance(password, str) or len(password) > 1024:
            raise ApiError(401, "Email or password is incorrect.")
        with self.db() as db:
            row = db.execute("SELECT * FROM administrators WHERE email=?", (email,)).fetchone()
            salt = row["password_salt"] if row else b"academic-ace-constant-time-check"
            expected = row["password_hash"] if row else hashlib.pbkdf2_hmac("sha256", b"not-a-real-password", salt, 600_000)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
            if row is None or not hmac.compare_digest(actual, expected):
                raise ApiError(401, "Email or password is incorrect.")
            raw_token = secrets.token_urlsafe(40)
            csrf = secrets.token_urlsafe(32)
            expires = utc_iso(utc_now() + timedelta(seconds=SESSION_SECONDS))
            token_hash = secret_hash(raw_token)
            db.execute("INSERT INTO admin_sessions(token_hash,administrator_id,csrf_token,expires_at) VALUES(?,?,?,?)", (token_hash, row["id"], csrf, expires))
            audit(db, row["id"], "login", "administrator", row["id"])
        return self.json_response(200, {"authenticated": True}, [("Set-Cookie", self.session_cookie(raw_token))])

    def session_cookie(self, token: str) -> str:
        cookie = f"aa_admin_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
        if APP_ENV == "production":
            cookie += "; Secure"
        return cookie

    @staticmethod
    def expire_cookie() -> str:
        return "aa_admin_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"

    def admin_routes(self, method: str, path: str) -> None:
        mutate = method in {"POST", "PATCH", "PUT", "DELETE"}
        session = self.require_admin(mutate=mutate)
        admin_id = session["administrator_id"]
        if session["role"] not in {"owner", "administrator"}:
            raise ApiError(403, "Your administrator role does not allow this action.")
        if path == "/api/admin/summary" and method == "GET":
            with self.db() as db:
                summary = {
                    "new_inquiries": db.execute("SELECT COUNT(*) FROM inquiries WHERE status='new'").fetchone()[0],
                    "open_projects": db.execute("SELECT COUNT(*) FROM projects WHERE status IN ('confirmed','in_progress','waiting_on_client')").fetchone()[0],
                    "open_quotes": db.execute("SELECT COUNT(*) FROM quotations WHERE status IN ('draft','sent')").fetchone()[0],
                    "samples": db.execute("SELECT COUNT(*) FROM samples WHERE published=1").fetchone()[0],
                }
            return self.json_response(200, summary)
        if path == "/api/admin/audit" and method == "GET":
            with self.db() as db:
                rows = [dict(row) for row in db.execute("SELECT e.id,e.action,e.entity_type,e.entity_id,e.details_json,e.created_at,a.email FROM audit_events e LEFT JOIN administrators a ON a.id=e.administrator_id ORDER BY e.created_at DESC LIMIT 100").fetchall()]
            for row in rows:
                row["details"] = json.loads(row.pop("details_json"))
            return self.json_response(200, {"events": rows})
        if path == "/api/admin/inquiries" and method == "GET":
            params = parse_qs(urlparse(self.path).query)
            status = params.get("status", [""])[0]
            query = normalize_text(params.get("q", [""])[0], "q", 100)
            if status and status not in STATUSES:
                raise ApiError(400, "Choose a valid inquiry status.")
            sql = "SELECT * FROM inquiries WHERE 1=1"
            args: list[object] = []
            if status:
                sql += " AND status=?"
                args.append(status)
            if query:
                like = f"%{query}%"
                sql += " AND (public_reference LIKE ? OR preferred_name LIKE ? OR contact LIKE ? OR project_type LIKE ? OR discipline LIKE ? OR working_title LIKE ?)"
                args.extend([like] * 6)
            sql += " ORDER BY created_at DESC LIMIT 200"
            with self.db() as db:
                rows = [serialize_inquiry(row) for row in db.execute(sql, args).fetchall()]
                for inquiry in rows:
                    inquiry["quotations"] = [dict(row) for row in db.execute("SELECT id,amount_zmw,currency,scope,status,valid_until,pricing_version,created_at FROM quotations WHERE inquiry_id=? ORDER BY created_at DESC", (inquiry["id"],)).fetchall()]
                    inquiry["client_history"] = [dict(row) for row in db.execute("SELECT id,public_reference,status,project_type,created_at FROM inquiries WHERE contact=? AND contact_method=? ORDER BY created_at DESC LIMIT 20", (inquiry["contact"], inquiry["contact_method"])).fetchall()]
            return self.json_response(200, {"inquiries": rows})
        match = re.fullmatch(r"/api/admin/inquiries/(\d+)", path)
        if match and method == "PATCH":
            return self.update_inquiry(int(match.group(1)), admin_id)
        if path == "/api/admin/pricing" and method == "GET":
            with self.db() as db:
                row = config_latest(db)
            return self.json_response(200, {"version": row["version"], "approved": bool(row["approved"]), "configuration": json.loads(row["configuration_json"])})
        if path == "/api/admin/pricing" and method == "PUT":
            if session["role"] != "owner":
                raise ApiError(403, "Only the business owner can change customer-facing pricing.")
            data = self.body_json()
            config = check_pricing_config(data.get("configuration"))
            approved = data.get("approved") is True
            with self.db() as db:
                latest = config_latest(db)
                version = latest["version"] + 1
                db.execute("INSERT INTO pricing_configurations(version,configuration_json,approved,created_by) VALUES(?,?,?,?)", (version, json.dumps(config, separators=(",", ":")), int(approved), admin_id))
                audit(db, admin_id, "pricing_updated", "pricing_configuration", version, {"approved": approved})
            return self.json_response(201, {"version": version, "approved": approved, "configuration": config})
        if path == "/api/admin/business" and method == "GET":
            with self.db() as db:
                settings = {row["setting_key"]: row["setting_value"] for row in db.execute("SELECT setting_key,setting_value FROM business_settings").fetchall()}
            return self.json_response(200, {key: settings.get(key, "") for key in BUSINESS_SETTING_KEYS})
        if path == "/api/admin/business" and method == "PUT":
            if session["role"] != "owner":
                raise ApiError(403, "Only the business owner can change public business details.")
            data = self.body_json()
            settings = {key: normalize_text(data.get(key, ""), key, 254 if key == "contact_email" else 300) for key in BUSINESS_SETTING_KEYS}
            if settings["contact_email"] and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", settings["contact_email"]):
                raise ApiError(400, "Enter a valid business email address.")
            for key in ("whatsapp_number", "signal_number"):
                if settings[key] and not re.fullmatch(r"\+?[0-9][0-9 ()-]{7,22}", settings[key]):
                    raise ApiError(400, f"Enter a valid business phone number for {key.replace('_',' ')}.")
            with self.db() as db:
                db.executemany("INSERT INTO business_settings(setting_key,setting_value,updated_by,updated_at) VALUES(?,?,?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_by=excluded.updated_by,updated_at=excluded.updated_at", [(key, value, admin_id, utc_iso()) for key, value in settings.items()])
                audit(db, admin_id, "business_settings_updated", "business_settings", "public_contact")
            return self.json_response(200, settings)
        if path == "/api/admin/quotes" and method == "POST":
            return self.create_quote(admin_id)
        match = re.fullmatch(r"/api/admin/quotes/(\d+)/issue", path)
        if match and method == "POST":
            return self.issue_quote(int(match.group(1)), admin_id)
        if path == "/api/admin/projects" and method == "GET":
            with self.db() as db:
                rows = [dict(row) for row in db.execute("SELECT p.*,i.public_reference,i.preferred_name,i.contact,q.amount_zmw,q.status AS quote_status FROM projects p JOIN inquiries i ON i.id=p.inquiry_id JOIN quotations q ON q.id=p.quotation_id ORDER BY p.created_at DESC LIMIT 200").fetchall()]
                for row in rows:
                    row["milestones"] = [dict(item) for item in db.execute("SELECT id,title,due_at,status FROM project_milestones WHERE project_id=? ORDER BY id", (row["id"],)).fetchall()]
                    row["payments"] = [dict(item) for item in db.execute("SELECT id,amount_zmw,currency,status,provider,transaction_reference,note,created_at FROM payment_records WHERE project_id=? ORDER BY created_at DESC", (row["id"],)).fetchall()] if session["role"] == "owner" else []
            return self.json_response(200, {"projects": rows})
        match = re.fullmatch(r"/api/admin/projects/(\d+)", path)
        if match and method == "PATCH":
            return self.update_project(int(match.group(1)), admin_id)
        match = re.fullmatch(r"/api/admin/projects/(\d+)/milestones", path)
        if match and method == "POST":
            return self.create_milestone(int(match.group(1)), admin_id)
        match = re.fullmatch(r"/api/admin/milestones/(\d+)", path)
        if match and method == "PATCH":
            return self.update_milestone(int(match.group(1)), admin_id)
        if path == "/api/admin/payments" and method == "POST":
            if session["role"] != "owner":
                raise ApiError(403, "Only the business owner can record payment information.")
            return self.record_payment(admin_id)
        if path == "/api/admin/samples" and method == "GET":
            with self.db() as db:
                rows = [dict(row) for row in db.execute("SELECT * FROM samples ORDER BY created_at DESC").fetchall()]
            return self.json_response(200, {"samples": rows})
        if path == "/api/admin/samples" and method == "POST":
            return self.create_sample(admin_id)
        match = re.fullmatch(r"/api/admin/samples/(\d+)", path)
        if match and method == "PATCH":
            return self.update_sample(int(match.group(1)), admin_id)
        return self.json_response(404, {"error": "Administrator endpoint not found."})

    def update_inquiry(self, inquiry_id: int, admin_id: int) -> None:
        data = self.body_json()
        status = normalize_text(data.get("status"), "status", 30, True)
        notes = normalize_text(data.get("internal_notes", ""), "internal_notes", 5000)
        if status not in STATUSES:
            raise ApiError(400, "Choose a valid inquiry status.")
        with self.db() as db:
            changed = db.execute("UPDATE inquiries SET status=?,internal_notes=?,updated_at=? WHERE id=?", (status, notes, utc_iso(), inquiry_id)).rowcount
            if not changed:
                raise ApiError(404, "Inquiry not found.")
            audit(db, admin_id, "inquiry_updated", "inquiry", inquiry_id, {"status": status})
        return self.json_response(200, {"ok": True})

    def create_quote(self, admin_id: int) -> None:
        data = self.body_json()
        inquiry_id = data.get("inquiry_id")
        amount = data.get("amount_zmw")
        if isinstance(inquiry_id, bool) or not isinstance(inquiry_id, int) or inquiry_id < 1:
            raise ApiError(400, "Select a valid inquiry.")
        if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 100_000_000:
            raise ApiError(400, "Enter a whole ZMW quotation amount greater than zero.")
        scope = normalize_text(data.get("scope"), "scope", 3000, True)
        valid_until = normalize_text(data.get("valid_until", ""), "valid_until", 32)
        if valid_until:
            try:
                parsed_expiry = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ApiError(400, "Enter a valid quote expiry date.") from exc
            if parsed_expiry.tzinfo is None:
                parsed_expiry = parsed_expiry.replace(tzinfo=timezone.utc)
            if parsed_expiry <= utc_now():
                raise ApiError(400, "Quotation expiry must be in the future.")
        raw_token = secrets.token_urlsafe(36)
        token_digest = secret_hash(raw_token)
        with self.db() as db:
            inquiry = db.execute("SELECT * FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()
            if inquiry is None:
                raise ApiError(404, "Inquiry not found.")
            cancelled_count = db.execute("UPDATE quotations SET status='cancelled',updated_at=? WHERE inquiry_id=? AND status IN ('draft','sent')", (utc_iso(), inquiry_id)).rowcount
            if cancelled_count:
                audit(db, admin_id, "prior_quotations_superseded", "inquiry", inquiry_id, {"count": cancelled_count})
            pricing = config_latest(db)
            snapshot = {"version": pricing["version"], "configuration": json.loads(pricing["configuration_json"]), "approved": bool(pricing["approved"])}
            cursor = db.execute("INSERT INTO quotations(inquiry_id,amount_zmw,scope,valid_until,pricing_version,pricing_snapshot_json,acceptance_token_hash,created_by) VALUES(?,?,?,?,?,?,?,?)", (inquiry_id, amount, scope, valid_until or None, pricing["version"], json.dumps(snapshot, separators=(",", ":")), token_digest, admin_id))
            quote_id = cursor.lastrowid
            db.execute("INSERT INTO quotation_line_items(quotation_id,description,quantity,unit_amount_zmw,line_amount_zmw) VALUES(?,?,1,?,?)", (quote_id, scope[:240], amount, amount))
            db.execute("UPDATE inquiries SET status='quoted',updated_at=? WHERE id=?", (utc_iso(), inquiry_id))
            audit(db, admin_id, "quotation_created", "quotation", quote_id, {"inquiry_id": inquiry_id, "amount_zmw": amount, "pricing_version": pricing["version"]})
        return self.json_response(201, {"quote_id": quote_id, "status": "draft", "currency": "ZMW", "amount_zmw": amount, "acceptance_link": f"{APP_ORIGIN}/quote/accept?token={raw_token}", "link_warning": "Copy this private acceptance link now. It is not stored in readable form and must be shared with the client by an approved channel."})

    def issue_quote(self, quote_id: int, admin_id: int) -> None:
        expired = False
        with self.db() as db:
            row = db.execute("SELECT id,status,valid_until FROM quotations WHERE id=?", (quote_id,)).fetchone()
            if row is None:
                raise ApiError(404, "Quotation not found.")
            if row["status"] != "draft":
                raise ApiError(409, "Only a draft quotation can be marked as shared.")
            if row["valid_until"] and row["valid_until"] < utc_iso():
                db.execute("UPDATE quotations SET status='expired',updated_at=? WHERE id=?", (utc_iso(), quote_id))
                expired = True
            else:
                db.execute("UPDATE quotations SET status='sent',updated_at=? WHERE id=?", (utc_iso(), quote_id))
                audit(db, admin_id, "quotation_marked_shared", "quotation", quote_id)
        if expired:
            raise ApiError(409, "This draft quotation has expired. Create a new quotation before sharing it.")
        return self.json_response(200, {"ok": True, "status": "sent"})

    def get_public_quote(self, token: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,80}", token):
            raise ApiError(404, "This quotation link is invalid or unavailable.")
        with self.db() as db:
            row = db.execute("SELECT q.id,q.amount_zmw,q.currency,q.scope,q.status,q.valid_until,q.created_at,i.public_reference,i.project_type FROM quotations q JOIN inquiries i ON i.id=q.inquiry_id WHERE q.acceptance_token_hash=?", (secret_hash(token),)).fetchone()
        if row is None or row["status"] not in {"sent", "accepted", "declined", "expired"}:
            raise ApiError(404, "This quotation link is invalid or unavailable.")
        result = dict(row)
        if result["valid_until"] and result["valid_until"] < utc_iso():
            if result["status"] == "sent":
                with self.db() as db:
                    db.execute("UPDATE quotations SET status='expired',updated_at=? WHERE id=? AND status='sent'", (utc_iso(), result["id"]))
            result["status"] = "expired"
        result.pop("id")
        return self.json_response(200, {"quotation": result})

    def respond_public_quote(self, token: str) -> None:
        self.ensure_same_origin()
        data = self.body_json()
        response = data.get("response")
        if response not in {"accept", "decline"}:
            raise ApiError(400, "Choose accept or decline.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,80}", token):
            raise ApiError(404, "This quotation link is invalid or unavailable.")
        expired = False
        with self.db() as db:
            quote = db.execute("SELECT q.*,i.deadline_local,i.timezone,i.id AS inquiry_id FROM quotations q JOIN inquiries i ON i.id=q.inquiry_id WHERE q.acceptance_token_hash=?", (secret_hash(token),)).fetchone()
            if quote is None or quote["status"] != "sent":
                raise ApiError(409, "This quotation cannot be changed. Contact Academic Ace to discuss next steps.")
            if quote["valid_until"] and quote["valid_until"] < utc_iso():
                db.execute("UPDATE quotations SET status='expired',updated_at=? WHERE id=?", (utc_iso(), quote["id"]))
                expired = True
            else:
                next_status = "accepted" if response == "accept" else "declined"
                db.execute("UPDATE quotations SET status=?,updated_at=? WHERE id=?", (next_status, utc_iso(), quote["id"]))
                inquiry_status = "accepted" if response == "accept" else "declined"
                db.execute("UPDATE inquiries SET status=?,updated_at=? WHERE id=?", (inquiry_status, utc_iso(), quote["inquiry_id"]))
                project_id = None
                if response == "accept":
                    cursor = db.execute("INSERT INTO projects(inquiry_id,quotation_id,agreed_scope,deadline_local,timezone) VALUES(?,?,?,?,?)", (quote["inquiry_id"], quote["id"], quote["scope"], quote["deadline_local"], quote["timezone"]))
                    project_id = cursor.lastrowid
                    db.execute("INSERT INTO project_milestones(project_id,title,due_at) VALUES(?,?,?)", (project_id, "Agreed scope and kickoff", quote["deadline_local"]))
                audit(db, None, f"quotation_{next_status}", "quotation", quote["id"], {"project_created": bool(project_id)})
        if expired:
            raise ApiError(409, "This quotation has expired. Contact Academic Ace to request an updated quote.")
        return self.json_response(200, {"status": next_status, "project_created": bool(project_id), "message": "Your response has been recorded."})

    def update_project(self, project_id: int, admin_id: int) -> None:
        data = self.body_json()
        status = normalize_text(data.get("status"), "status", 30, True)
        if status not in PROJECT_STATUSES:
            raise ApiError(400, "Choose a valid project status.")
        with self.db() as db:
            changed = db.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?", (status, utc_iso(), project_id)).rowcount
            if not changed:
                raise ApiError(404, "Project not found.")
            audit(db, admin_id, "project_updated", "project", project_id, {"status": status})
        return self.json_response(200, {"ok": True})

    def create_milestone(self, project_id: int, admin_id: int) -> None:
        data = self.body_json()
        title = normalize_text(data.get("title"), "title", 180, True)
        due_at = normalize_text(data.get("due_at", ""), "due_at", 32)
        if not valid_local_deadline(due_at):
            raise ApiError(400, "Enter a valid milestone date and time.")
        with self.db() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ApiError(404, "Project not found.")
            cursor = db.execute("INSERT INTO project_milestones(project_id,title,due_at) VALUES(?,?,?)", (project_id, title, due_at))
            audit(db, admin_id, "milestone_created", "project_milestone", cursor.lastrowid, {"project_id": project_id})
        return self.json_response(201, {"milestone_id": cursor.lastrowid, "status": "pending"})

    def update_milestone(self, milestone_id: int, admin_id: int) -> None:
        data = self.body_json()
        status = normalize_text(data.get("status"), "status", 20, True)
        if status not in {"pending", "in_progress", "completed", "cancelled"}:
            raise ApiError(400, "Choose a valid milestone status.")
        with self.db() as db:
            changed = db.execute("UPDATE project_milestones SET status=? WHERE id=?", (status, milestone_id)).rowcount
            if not changed:
                raise ApiError(404, "Milestone not found.")
            audit(db, admin_id, "milestone_updated", "project_milestone", milestone_id, {"status": status})
        return self.json_response(200, {"ok": True})

    def record_payment(self, admin_id: int) -> None:
        data = self.body_json()
        project_id, quote_id, amount, status = data.get("project_id"), data.get("quotation_id"), data.get("amount_zmw"), data.get("status")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (project_id, quote_id, amount)) or amount > 100_000_000:
            raise ApiError(400, "Choose a project and quotation and enter a valid whole ZMW amount.")
        if status not in PAYMENT_STATUSES:
            raise ApiError(400, "Choose a valid payment record status.")
        tx = normalize_text(data.get("transaction_reference", ""), "transaction_reference", 160)
        note = normalize_text(data.get("note", ""), "note", 1200)
        if status == "successful" and not tx:
            raise ApiError(400, "A verified transaction reference is required to record a successful payment.")
        with self.db() as db:
            project = db.execute("SELECT p.id,p.quotation_id,q.amount_zmw FROM projects p JOIN quotations q ON q.id=p.quotation_id WHERE p.id=?", (project_id,)).fetchone()
            if project is None or project["quotation_id"] != quote_id:
                raise ApiError(400, "The selected quotation does not belong to this project.")
            if status == "successful":
                settled = db.execute("SELECT COALESCE(SUM(amount_zmw),0) FROM payment_records WHERE quotation_id=? AND status='successful'", (quote_id,)).fetchone()[0]
                if settled + amount > project["amount_zmw"]:
                    raise ApiError(409, "Recorded successful payments would exceed the quotation total.")
            if tx and db.execute("SELECT 1 FROM payment_records WHERE transaction_reference=?", (tx,)).fetchone():
                raise ApiError(409, "That transaction reference has already been recorded.")
            cursor = db.execute("INSERT INTO payment_records(project_id,quotation_id,amount_zmw,status,transaction_reference,recorded_by,note) VALUES(?,?,?,?,?,?,?)", (project_id, quote_id, amount, status, tx, admin_id, note))
            audit(db, admin_id, "payment_recorded_manual", "payment_record", cursor.lastrowid, {"status": status, "amount_zmw": amount, "currency": "ZMW"})
        return self.json_response(201, {"payment_record_id": cursor.lastrowid, "status": status, "currency": "ZMW", "recording_method": "administrator_recorded"})

    def sample_values(self, data: dict, partial: bool = False) -> dict:
        values = {}
        fields = {"title": (140, True), "category": (40, True), "description": (600, True), "preview_text": (10000, True)}
        for field, (limit, required) in fields.items():
            if partial and field not in data:
                continue
            values[field] = normalize_text(data.get(field, ""), field, limit, required)
        if "category" in values and values["category"] not in SAMPLE_CATEGORIES:
            raise ApiError(400, "Choose a valid sample category.")
        if not partial and not values["preview_text"]:
            raise ApiError(400, "Add an illustrative, permission-cleared preview.")
        if "published" in data:
            if not isinstance(data["published"], bool):
                raise ApiError(400, "Publication status must be true or false.")
            values["published"] = int(data["published"])
        return values

    def create_sample(self, admin_id: int) -> None:
        data = self.body_json()
        values = self.sample_values(data)
        published = values.pop("published", 0)
        slug = re.sub(r"[^a-z0-9]+", "-", values["title"].lower()).strip("-")[:100] or secrets.token_hex(6)
        slug = f"{slug}-{secrets.token_hex(3)}"
        with self.db() as db:
            cursor = db.execute("INSERT INTO samples(slug,title,category,description,preview_text,published,created_by) VALUES(?,?,?,?,?,?,?)", (slug, values["title"], values["category"], values["description"], values["preview_text"], published, admin_id))
            audit(db, admin_id, "sample_created", "sample", cursor.lastrowid, {"published": bool(published)})
        return self.json_response(201, {"sample_id": cursor.lastrowid, "published": bool(published)})

    def update_sample(self, sample_id: int, admin_id: int) -> None:
        data = self.body_json()
        values = self.sample_values(data, partial=True)
        if not values:
            raise ApiError(400, "Provide fields to update.")
        with self.db() as db:
            assignments = ",".join(f"{key}=?" for key in values)
            changed = db.execute(f"UPDATE samples SET {assignments},updated_at=? WHERE id=?", [*values.values(), utc_iso(), sample_id]).rowcount
            if not changed:
                raise ApiError(404, "Sample not found.")
            audit(db, admin_id, "sample_updated", "sample", sample_id, {"published": bool(values.get("published", -1)) if "published" in values else None})
        return self.json_response(200, {"ok": True})


def create_admin(email: str) -> None:
    migrate()
    email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", email):
        raise SystemExit("Enter a valid administrator email address.")
    with connect_db() as db:
        if db.execute("SELECT 1 FROM administrators WHERE email=?", (email,)).fetchone():
            raise SystemExit("That administrator already exists; no account was changed.")
        account_count = db.execute("SELECT COUNT(*) FROM administrators").fetchone()[0]
        if account_count > 0:
            answer = input("An administrator already exists. Create another administrator? [y/N] ").strip().lower()
            if answer != "y":
                return
        import getpass

        password = getpass.getpass("New administrator password (minimum 14 characters): ")
        confirm = getpass.getpass("Confirm password: ")
        if len(password) < 14 or password != confirm:
            raise SystemExit("Passwords must match and contain at least 14 characters.")
        salt = secrets.token_bytes(32)
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
        role = "owner" if account_count == 0 else "administrator"
        cursor = db.execute("INSERT INTO administrators(email,role,password_hash,password_salt) VALUES(?,?,?,?)", (email, role, password_hash, salt))
        audit(db, cursor.lastrowid, "administrator_created", "administrator", cursor.lastrowid, {"role": role})
    print(f"{role.title()} account created. Store the credentials in an approved password manager.")


def reset_admin_password(email: str) -> None:
    email = email.strip().lower()
    with connect_db() as db:
        admin = db.execute("SELECT id,role FROM administrators WHERE email=?", (email,)).fetchone()
        if admin is None:
            raise SystemExit("No administrator with that email exists.")
        import getpass

        password = getpass.getpass("New administrator password (minimum 14 characters): ")
        confirm = getpass.getpass("Confirm password: ")
        if len(password) < 14 or password != confirm:
            raise SystemExit("Passwords must match and contain at least 14 characters.")
        salt = secrets.token_bytes(32)
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
        db.execute("UPDATE administrators SET password_hash=?,password_salt=? WHERE id=?", (password_hash, salt, admin["id"]))
        db.execute("DELETE FROM admin_sessions WHERE administrator_id=?", (admin["id"],))
        audit(db, admin["id"], "administrator_password_reset", "administrator", admin["id"])
    print("Administrator password reset and active sessions invalidated.")


def make_backup(destination: str | None = None) -> Path:
    migrate()
    target = Path(destination) if destination else ROOT / "backups" / ("database-" + utc_now().strftime("%Y%m%d-%H%M%S") + ".sqlite3")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_db = connect_db()
    backup_db = sqlite3.connect(target)
    try:
        source_db.backup(backup_db)
    finally:
        backup_db.close()
        source_db.close()
    return target


def purge_expired(dry_run: bool = False) -> dict[str, int]:
    try:
        retention_days = int(os.environ.get("RETENTION_DAYS", "365"))
    except ValueError as exc:
        raise SystemExit("RETENTION_DAYS must be a whole number between 30 and 3,650.") from exc
    if not 30 <= retention_days <= 3650:
        raise SystemExit("RETENTION_DAYS must be a whole number between 30 and 3,650.")
    cutoff = utc_iso(utc_now() - timedelta(days=retention_days))
    now = int(time.time())
    with connect_db() as db:
        completed_projects = [dict(row) for row in db.execute("SELECT id,inquiry_id,quotation_id FROM projects WHERE status IN ('completed','cancelled') AND updated_at<?", (cutoff,)).fetchall()]
        old_inquiries = [row[0] for row in db.execute("SELECT id FROM inquiries WHERE status IN ('declined','closed','quoted','accepted') AND updated_at<? AND id NOT IN (SELECT inquiry_id FROM projects)", (cutoff,)).fetchall()]
        completed_inquiry_ids = {row["inquiry_id"] for row in completed_projects}
        project_inquiry_candidates = [inquiry_id for inquiry_id in completed_inquiry_ids if db.execute("SELECT 1 FROM projects WHERE inquiry_id=? AND id NOT IN (SELECT id FROM projects WHERE status IN ('completed','cancelled') AND updated_at<?) LIMIT 1", (inquiry_id, cutoff)).fetchone() is None]
        delete_inquiry_ids = set(old_inquiries) | set(project_inquiry_candidates)
        current_time = utc_iso()
        counts = {"projects": len(completed_projects), "inquiries": len(delete_inquiry_ids), "expired_sessions": db.execute("SELECT COUNT(*) FROM admin_sessions WHERE expires_at<?", (current_time,)).fetchone()[0], "expired_rate_limits": db.execute("SELECT COUNT(*) FROM rate_limits WHERE expires_at<?", (now,)).fetchone()[0]}
        if dry_run:
            return counts
        for project in completed_projects:
            db.execute("DELETE FROM payment_records WHERE project_id=?", (project["id"],))
            db.execute("DELETE FROM project_milestones WHERE project_id=?", (project["id"],))
            db.execute("DELETE FROM projects WHERE id=?", (project["id"],))
            db.execute("DELETE FROM quotations WHERE id=?", (project["quotation_id"],))
        for inquiry_id in delete_inquiry_ids:
            if db.execute("SELECT 1 FROM projects WHERE inquiry_id=? LIMIT 1", (inquiry_id,)).fetchone():
                continue
            db.execute("DELETE FROM quotations WHERE inquiry_id=?", (inquiry_id,))
            db.execute("DELETE FROM inquiries WHERE id=?", (inquiry_id,))
        db.execute("DELETE FROM admin_sessions WHERE expires_at<?", (current_time,))
        db.execute("DELETE FROM rate_limits WHERE expires_at<?", (now,))
        return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Academic Ace application server")
    parser.add_argument("command", nargs="?", choices=["serve", "create-admin", "reset-admin-password", "migrate", "backup", "purge-expired"], default="serve")
    parser.add_argument("email", nargs="?", help="Email for create-admin")
    parser.add_argument("--destination", help="Optional database backup destination")
    parser.add_argument("--dry-run", action="store_true", help="Report retention candidates without deleting data")
    args = parser.parse_args()
    if args.command == "create-admin":
        email = args.email or input("Administrator email: ")
        create_admin(email)
        return
    if args.command == "reset-admin-password":
        email = args.email or input("Administrator email: ")
        reset_admin_password(email)
        return
    migrate()
    if args.command == "migrate":
        print(f"Database migrations applied: {DATABASE_PATH}")
        return
    if args.command == "backup":
        print(f"Database backup written: {make_backup(args.destination)}")
        return
    if args.command == "purge-expired":
        result = purge_expired(args.dry_run)
        print(json.dumps({"dry_run": args.dry_run, "retention_days": int(os.environ.get("RETENTION_DAYS", "365")), **result}))
        return
    if not APP_SECRET:
        raise SystemExit("Set APP_SECRET before starting the server.")
    print(f"Academic Ace server listening at {APP_ORIGIN} (environment: {APP_ENV})")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Academic Ace server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
