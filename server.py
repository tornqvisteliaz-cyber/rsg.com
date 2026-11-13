"""Rotaidem site for Python hosts.

Serves the static site in cloudflare/public and the same /api routes as
cloudflare/functions/api/[[path]].js, so one codebase runs on Cloudflare
Pages, on hosts that run `gunicorn main:app` (Render, Docker), or as a CGI
script on shared web hosting such as STRATO (see strato/).

Database: PostgreSQL when DATABASE_URL is set, MySQL/MariaDB when MYSQL_HOST
is set, otherwise SQLite at DATA_DIR/rotaidem.db (put DATA_DIR on persistent storage).
"""
import base64
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from html import escape, unescape as html_unescape
from pathlib import Path

from flask import Flask, abort, g, has_request_context, jsonify, redirect, request, send_from_directory

PUBLIC = Path(__file__).resolve().parent / "cloudflare" / "public"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_PG = DATABASE_URL.startswith("postgresql://")
USE_MYSQL = not USE_PG and bool(os.environ.get("MYSQL_HOST"))
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent / "instance"))
STAFF = ("Owner", "Admin", "Support")
SECRET_KEY = os.environ.get("SECRET_KEY") or os.environ.get("ADMIN_PASSWORD") or "rotaidem-dev-secret"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_API_URL = os.environ.get("RESEND_API_URL", "https://api.resend.com").rstrip("/")  # override only for tests
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip() or "Rotaidem <no-reply@rotaidem.com>"
# Payments through Stripe Checkout (setup.py stripe writes these to config.env).
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_API_URL = os.environ.get("STRIPE_API_URL", "https://api.stripe.com").rstrip("/")  # override only for tests
# PayPal Payouts (Admin -> Affiliates): pays affiliates their commissions by PayPal email. From setup.py paypal.
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
PAYPAL_SECRET = os.environ.get("PAYPAL_SECRET", "").strip()
PAYPAL_API_URL = os.environ.get("PAYPAL_API_URL", "https://api-m.paypal.com").rstrip("/")  # sandbox: https://api-m.sandbox.paypal.com; override only for tests
# Discord (Admin -> Discord): the application and its bot, from setup.py discord
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "").strip()
DISCORD_API_URL = os.environ.get("DISCORD_API_URL", "https://discord.com/api/v10").rstrip("/")  # override only for tests
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
AUDIENCES = ("accounts", "buyers", "subscribers", "contacts")
LICENSE_API_KEY = os.environ.get("LICENSE_API_KEY", "").strip()
# License keys: PREFIX-XXXX-XXXX-XXXX. 11 random characters plus 1 check character from a 32-letter
# alphabet without look-alikes (no I, O, 0, 1). Must match licensing/src/Rotaidem.Licensing/LicenseKey.cs.
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
KEY_RE = re.compile(r"^([A-Z0-9]{2,8})-([A-Z2-9]{4})-([A-Z2-9]{4})-([A-Z2-9]{4})$")
ROLES = ("Customer", "Support", "Admin", "Owner")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # image uploads are resized in the browser first


# ---------- database ----------

def pg_options():
    # External (public) database URLs need TLS; internal hosts and localhost may not support it.
    opts = {"connect_timeout": 10}
    if "sslmode=" not in DATABASE_URL:
        host = re.sub(r"^postgresql://([^@]*@)?", "", DATABASE_URL).split("/")[0].split(":")[0]
        if host not in ("localhost", "127.0.0.1") and "." in host:
            opts["sslmode"] = "require"
    return opts


class DB:
    def __init__(self):
        if USE_PG:
            import psycopg
            from psycopg.rows import dict_row
            # Retry briefly: managed databases can drop the first connection while waking up.
            for attempt in range(5):
                try:
                    self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True, **pg_options())
                    break
                except psycopg.OperationalError:
                    if attempt == 4:
                        raise
                    time.sleep(2 ** attempt)
        elif USE_MYSQL:
            import pymysql
            options = dict(host=os.environ["MYSQL_HOST"], port=int(os.environ.get("MYSQL_PORT") or 3306),
                           user=os.environ.get("MYSQL_USER", ""), password=os.environ.get("MYSQL_PASSWORD", ""),
                           database=os.environ.get("MYSQL_DATABASE", ""), charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor, autocommit=True, connect_timeout=10)
            try:
                # TLS whenever the server offers it. It also lets MySQL 8 logins work without the
                # 'cryptography' package, which shared hosting may not have.
                self.conn = pymysql.connect(ssl={"check_hostname": False}, **options)
            except Exception:  # noqa: BLE001 - e.g. an old TLS setup on the server: try without
                self.conn = pymysql.connect(**options)
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(DATA_DIR / "rotaidem.db")
            self.conn.row_factory = sqlite3.Row
            self.conn.isolation_level = None

    def run(self, sql, *args):
        if USE_MYSQL:
            cur = self.conn.cursor()
            cur.execute(mysql_sql(sql), args or None)
            return cur
        if USE_PG:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, args)

    def first(self, sql, *args):
        row = self.run(sql, *args).fetchone()
        return plain_row(row) if row else None

    def all(self, sql, *args):
        return [plain_row(r) for r in self.run(sql, *args).fetchall()]

    def close(self):
        self.conn.close()


def mysql_sql(sql):
    """The queries are written for SQLite/PostgreSQL; adjust the few spots MySQL spells differently."""
    sql = sql.replace("?", "%s")
    sql = re.sub(r"ON CONFLICT\s*\(\w+\)\s*DO UPDATE SET", "ON DUPLICATE KEY UPDATE", sql)
    sql = re.sub(r"\bexcluded\.(\w+)", r"VALUES(\1)", sql)
    return re.sub(r"(?<![\w.`'])key(?![\w`'])", "`key`", sql)  # meta.key is a reserved word in MySQL


def plain_row(row):
    # MySQL returns DATETIME columns as datetime objects; the site expects "YYYY-MM-DD HH:MM:SS" text.
    return {k: (v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v) for k, v in dict(row).items()}


def db():
    if "db" not in g:
        g.db = DB()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


# Bump when the tables below change. Lets each request skip the CREATE/ALTER statements, which
# matters on CGI hosting where every request starts a fresh process.
SCHEMA_VERSION = "2026-11-13-consents"


def ensure_schema():
    d = DB()
    try:
        row = d.first("SELECT value FROM meta WHERE key = 'schema_version'")
        if row and row["value"] == SCHEMA_VERSION:
            d.close()
            return
    except Exception:  # noqa: BLE001 - first run: the meta table does not exist yet
        pass
    if USE_MYSQL:
        # MySQL 8 allows no plain default on TEXT columns, so short values with a default are VARCHAR.
        # Keys need a length, defaults on timestamps need DATETIME, images LONGTEXT, money 2 decimals.
        pk, ts, ident, word = "INT AUTO_INCREMENT PRIMARY KEY", "DATETIME DEFAULT CURRENT_TIMESTAMP", "VARCHAR(191)", "VARCHAR(64)"
        big, money, tail = "LONGTEXT", "DECIMAL(10,2)", " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    else:
        pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        ts, ident, word, big, money, tail = "TEXT DEFAULT CURRENT_TIMESTAMP", "TEXT", "TEXT", "TEXT", "NUMERIC", ""
    for sql in (
        f"CREATE TABLE IF NOT EXISTS users (id {pk}, name TEXT, email {ident} UNIQUE, password_hash TEXT, role {word} DEFAULT 'Customer', token TEXT, created_at {ts})",
        f"CREATE TABLE IF NOT EXISTS products (id {ident} PRIMARY KEY, name TEXT, simulator TEXT, version TEXT, folder_name TEXT, download_url TEXT, image_url TEXT, price TEXT, description TEXT, buy_url TEXT, status TEXT, images TEXT, tagline TEXT, features TEXT, sort_order INTEGER DEFAULT 0)",
        f"CREATE TABLE IF NOT EXISTS liveries (id {ident} PRIMARY KEY, name TEXT, aircraft TEXT, folder_name TEXT, download_url TEXT, image_url TEXT)",
        f"CREATE TABLE IF NOT EXISTS posts (id {pk}, title TEXT, content TEXT, image_url TEXT, date TEXT)",
        f"CREATE TABLE IF NOT EXISTS messages (id {pk}, name TEXT, email TEXT, message TEXT, created_at {ts})",
        f"CREATE TABLE IF NOT EXISTS orders (id {pk}, customer_id INTEGER, product_id TEXT, status {word} DEFAULT 'Completed', payment_status {word} DEFAULT 'Paid', amount {money} DEFAULT 0, invoice_file TEXT, created_at {ts})",
        f"CREATE TABLE IF NOT EXISTS audit_logs (id {pk}, admin_id INTEGER, action TEXT, target TEXT, ip_address TEXT, created_at {ts})",
        f"CREATE TABLE IF NOT EXISTS meta (key {ident} PRIMARY KEY, value TEXT)",
        f"CREATE TABLE IF NOT EXISTS subscribers (id {pk}, email {ident} UNIQUE, source TEXT, unsubscribed INTEGER DEFAULT 0, created_at {ts})",
        f"CREATE TABLE IF NOT EXISTS licenses (id {pk}, license_key {ident} UNIQUE, product_id TEXT, email TEXT, customer_id INTEGER, order_id INTEGER, status {word} DEFAULT 'active', max_activations INTEGER DEFAULT 2, note TEXT, created_by TEXT, created_at {ts})",
        f"CREATE TABLE IF NOT EXISTS license_activations (id {pk}, license_id INTEGER, machine_id {ident}, machine_name TEXT, first_seen {ts}, last_seen {ts}, UNIQUE (license_id, machine_id))",
        f"CREATE TABLE IF NOT EXISTS media (id {pk}, filename TEXT, mime TEXT, data {big}, size INTEGER, width INTEGER, height INTEGER, created_at {ts})",
        f"CREATE TABLE IF NOT EXISTS campaigns (id {pk}, subject TEXT, product_id TEXT, audience TEXT, sent_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0, admin_id INTEGER, created_at {ts})",
        f"CREATE TABLE IF NOT EXISTS shared_profiles (id {pk}, name_key {ident} UNIQUE, name TEXT, data TEXT, license_id INTEGER, downloads INTEGER DEFAULT 0, created_at {ts}, updated_at {ts})",
        f"CREATE TABLE IF NOT EXISTS license_failures (id {pk}, ip_address {ident}, created_at {word})",
        # One row per new computer activated on a licence: limits activate/deactivate cycling.
        f"CREATE TABLE IF NOT EXISTS license_activation_log (id {pk}, license_id INTEGER, machine_id {ident}, created_at {word})",
        # One row per logged-in browser; only a hash of the token is stored.
        f"CREATE TABLE IF NOT EXISTS sessions (id {pk}, user_id INTEGER, token_hash {ident} UNIQUE, ip_address {word}, "
        f"user_agent TEXT, created_at {word}, last_seen {word})",
        f"CREATE TABLE IF NOT EXISTS downloads (id {pk}, product_id {ident}, user_id INTEGER, ip_address {word}, created_at {word})",
        # One row per paid Stripe Checkout session: claimed first, so an order is never made twice.
        f"CREATE TABLE IF NOT EXISTS stripe_payments (session_id {ident} PRIMARY KEY, product_id {word}, user_id INTEGER, email TEXT, "
        f"order_id INTEGER, license_key {word}, created_at {word})",
        # Affiliate program: people with a referral code earn a share of the sales they bring.
        f"CREATE TABLE IF NOT EXISTS affiliates (id {pk}, code {ident} UNIQUE, user_id INTEGER, email {ident}, name TEXT, "
        f"percent INTEGER DEFAULT 20, status {word} DEFAULT 'active', note TEXT, created_at {word})",
        # One row per referred paid order: what the affiliate earned, and whether you have paid it out.
        f"CREATE TABLE IF NOT EXISTS affiliate_commissions (id {pk}, affiliate_id INTEGER, order_id INTEGER UNIQUE, amount {money} DEFAULT 0, "
        f"currency {word}, rate INTEGER DEFAULT 0, status {word} DEFAULT 'pending', created_at {word})",
        # Discord bot data: tags, warnings, reminders, suggestions, reports, forms, applications, giveaways and
        # their entries, recurring posts. uniq stops doubles (one giveaway entry per member) without a race.
        f"CREATE TABLE IF NOT EXISTS discord_items (id {ident} PRIMARY KEY, kind {word}, ref {ident}, member {word}, status {word}, "
        f"due {word}, uniq {ident} UNIQUE, data TEXT, created_at {word})",
    ):
        d.run(sql + tail)
    for table, column in (("products", "images TEXT"), ("products", "tagline TEXT"), ("products", "features TEXT"),
                          ("products", "sort_order INTEGER DEFAULT 0"), ("orders", "product_id TEXT"),
                          ("posts", f"status {word} DEFAULT 'published'"), ("messages", "handled INTEGER DEFAULT 0"),
                          # accounts from before email confirmation count as confirmed; new sign-ups are stored as 0
                          ("users", "email_verified INTEGER DEFAULT 1"),
                          ("products", f"download_gate {word} DEFAULT 'public'"), ("products", "changelog TEXT"),
                          ("users", "admin_note TEXT"), ("posts", f"publish_at {word}"),
                          ("messages", "reply TEXT"), ("messages", f"replied_at {word}"), ("messages", "replied_by TEXT"),
                          # RotaHub updates: "everyone must update", and when the version or download last changed
                          ("products", "update_required INTEGER DEFAULT 0"), ("products", f"updated_at {word}"),
                          # Stripe: the price charged at checkout (smallest unit, e.g. ore/cents), and on orders the payment
                          ("products", "pay_amount INTEGER DEFAULT 0"), ("products", f"pay_currency {word}"),
                          ("orders", f"payment_ref {ident}"), ("orders", f"currency {word}"),
                          # a sale: a lower price at checkout until a date (Admin -> Products)
                          ("products", "sale_amount INTEGER DEFAULT 0"), ("products", f"sale_until {word}"),
                          # the Discord account linked on My account (for the verified buyer role)
                          ("users", f"discord_id {word}"), ("users", "discord_name TEXT"), ("users", f"discord_linked_at {word}"),
                          # what happened the last time the role was given or taken (Admin -> Discord -> Linked members)
                          ("users", "discord_result TEXT"),
                          # consents: when the terms (and the age line) were accepted, and at checkout the waiver of the
                          # 14-day withdrawal right for digital content (EU consumer law asks to keep this)
                          ("users", f"terms_accepted_at {word}"), ("users", f"terms_version {word}"), ("orders", "consent TEXT"),
                          # the affiliate code that referred the order (Admin -> Affiliates)
                          ("orders", f"affiliate_code {ident}"),
                          # affiliate accounts: where to send their money, and how often their link was opened
                          ("affiliates", "payout TEXT"), ("affiliates", "clicks INTEGER DEFAULT 0"),
                          # automatic payouts over Stripe Connect: the affiliate's connected account and whether Stripe can pay it
                          ("affiliates", f"stripe_account {word}"), ("affiliates", "payouts_ready INTEGER DEFAULT 0"),
                          # the Stripe transfer that settled a commission (empty when paid by hand)
                          ("affiliate_commissions", f"transfer_id {word}"),
                          # optional licence end date (empty = never ends); lost in the affiliate merge
                          ("licenses", f"expires_at {word}")):
        try:
            d.run(f"ALTER TABLE {table} ADD COLUMN {column}")
        except Exception:
            pass
    # Never delete data here: this runs on every install, update and migration.
    # Emptying the catalog is an explicit command (strato/setup.py reset-catalog).
    d.run("DELETE FROM meta WHERE key = 'schema_version'")
    d.run("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", SCHEMA_VERSION)
    d.close()


# ---------- helpers ----------

def today(d=None):
    d = d or datetime.now()
    return f"{d:%B} {d.day}, {d.year}"


def post_date(value):
    """The date shown on a news post: from the admin's date picker (YYYY-MM-DD), or today."""
    try:
        return today(datetime.strptime(str(value or "").strip(), "%Y-%m-%d"))
    except ValueError:
        return today()


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def err(message, status):
    return jsonify({"error": message}), status


def bearer():
    # X-Rotaidem-Token is a copy for web servers that drop the Authorization header before CGI scripts.
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    return token or request.headers.get("X-Rotaidem-Token", "").strip()


SESSION_DAYS = 60    # a browser not used for this long is logged out
SESSIONS_MAX = 20    # newest logins kept per account


def token_hash(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def new_session(user_id):
    """Logs this browser in: a new token, of which only the hash is stored. Returns the token."""
    token = uuid.uuid4().hex
    now = utc_stamp()
    db().run("INSERT INTO sessions (user_id, token_hash, ip_address, user_agent, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
             user_id, token_hash(token), (request.remote_addr or "")[:64], request.headers.get("User-Agent", "")[:300], now, now)
    for old in db().all("SELECT id FROM sessions WHERE user_id = ? ORDER BY id DESC", user_id)[SESSIONS_MAX:]:
        db().run("DELETE FROM sessions WHERE id = ?", old["id"])
    return token


def end_sessions(user_id, keep=None):
    """Logs the account out everywhere (or everywhere but session `keep`)."""
    if keep:
        db().run("DELETE FROM sessions WHERE user_id = ? AND id <> ?", user_id, keep)
    else:
        db().run("DELETE FROM sessions WHERE user_id = ?", user_id)
    db().run("UPDATE users SET token = NULL WHERE id = ?", user_id)


def current_user():
    if "current_user" in g:
        return g.current_user
    token, user = bearer(), None
    if token:
        session = db().first("SELECT * FROM sessions WHERE token_hash = ?", token_hash(token))
        if session and str(session["last_seen"] or "") < utc_stamp(SESSION_DAYS * 86400):
            db().run("DELETE FROM sessions WHERE id = ?", session["id"])
            session = None
        if session:
            user = db().first("SELECT * FROM users WHERE id = ?", session["user_id"])
            if user and str(session["last_seen"] or "") < utc_stamp(300):  # at most one write per 5 minutes
                db().run("UPDATE sessions SET last_seen = ?, ip_address = ? WHERE id = ?", utc_stamp(), (request.remote_addr or "")[:64], session["id"])
        else:
            # Logged in before sessions existed: the token is still on the user. Move it into a session.
            user = db().first("SELECT * FROM users WHERE token = ?", token)
            if user:
                now = utc_stamp()
                db().run("INSERT INTO sessions (user_id, token_hash, ip_address, user_agent, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                         user["id"], token_hash(token), (request.remote_addr or "")[:64], request.headers.get("User-Agent", "")[:300], now, now)
                db().run("UPDATE users SET token = NULL WHERE id = ?", user["id"])
                session = db().first("SELECT * FROM sessions WHERE token_hash = ?", token_hash(token))
        if user:
            user["token"] = token  # the admin password unlock is tied to this browser's login
            user["session_id"] = session["id"] if session else None
    g.current_user = user
    return user


def device_label(user_agent):
    """"Chrome on Windows" and so on, from the browser's User-Agent."""
    ua = str(user_agent or "")
    if ua.startswith("RotaHub"):  # the desktop app logs in like a browser and says so in its User-Agent
        return "RotaHub on Windows"
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                           ("Safari/", "Safari")) if key in ua), "A browser")
    system = next((name for key, name in (("Windows", "Windows"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                          ("Mac OS X", "Mac"), ("CrOS", "Chromebook"), ("Linux", "Linux")) if key in ua), "")
    return f"{browser} on {system}" if system else browser


def session_rows(user_id, current_id=None):
    rows = db().all("SELECT id, ip_address, user_agent, created_at, last_seen FROM sessions WHERE user_id = ? ORDER BY last_seen DESC", user_id)
    return [{"id": r["id"], "device": device_label(r["user_agent"]), "ip_address": r["ip_address"] or "", "created_at": r["created_at"],
             "last_seen": r["last_seen"], "current": r["id"] == current_id} for r in rows]


def staff_user():
    user = current_user()
    return user if user and user["role"] in STAFF and admin_unlocked(user) else None


def audit(user, action, target=""):
    db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address) VALUES (?, ?, ?, ?)",
             user["id"], action, str(target)[:200], request.remote_addr or "")


def slug(value):
    s = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


def list_images(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
    except (ValueError, TypeError):
        pass
    return [s.strip() for s in str(value).split("\n") if s.strip()]


def with_images(item):
    item = dict(item)
    item["images"] = list_images(item.get("images"))
    item["features"] = list_images(item.get("features"))
    item["page"] = "/addons/view?id=" + item["id"]
    return item


# PBKDF2-SHA256, 100k iterations, "salt_b64.hash_b64": same format as the Cloudflare function.
def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return base64.b64encode(salt).decode() + "." + base64.b64encode(digest).decode()


def verify_password(password, stored):
    if not stored or "." not in stored:
        return False
    salt_b64, hash_b64 = stored.split(".", 1)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), 100000)
    return hmac.compare_digest(base64.b64encode(digest).decode(), hash_b64)


def session_payload(user, token):
    return {"token": token, "name": user["name"], "email": user["email"], "role": user["role"],
            "is_admin": user["role"] in STAFF}


# ---------- auth ----------

@app.post("/api/login")
def login():
    b = body()
    ident = str(b.get("email", "")).strip().lower()
    password = str(b.get("password", ""))
    user = db().first("SELECT * FROM users WHERE LOWER(email) = ? OR LOWER(name) = ?", ident, ident)
    admin_pw = os.environ.get("ADMIN_PASSWORD")
    admin_email = os.environ.get("ADMIN_EMAIL", "Eliaz@rotaidem.com")
    admin_name = os.environ.get("ADMIN_USERNAME", "eliaz")
    if not user and admin_pw and hmac.compare_digest(password, admin_pw) and ident in (admin_email.lower(), admin_name.lower()):
        db().run("INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'Owner')",
                 admin_name, admin_email, hash_password(password))
        owner = db().first("SELECT id FROM users WHERE LOWER(email) = ?", admin_email.lower())
        return jsonify(session_payload({"name": admin_name, "email": admin_email, "role": "Owner"}, new_session(owner["id"])))
    if not user or not verify_password(password, user["password_hash"]):
        return err("Invalid email or password", 401)
    if not email_verified(user):
        return jsonify({"error": "Confirm your email address first: click the link in the email we sent you when you signed up.",
                        "unverified": True, "email": user["email"]}), 403
    return jsonify(session_payload(user, new_session(user["id"])))


def email_verified(user):
    value = user.get("email_verified")
    return value is None or int(value) == 1


@app.post("/api/signup")
def signup():
    b = body()
    name = str(b.get("name", "")).strip()
    email = str(b.get("email", "")).strip().lower()
    password = str(b.get("password", ""))
    if not name or not email or len(password) < 6:
        return err("Name, email and a 6 character password are required", 400)
    if not EMAIL_RE.match(email):
        return err("Enter a valid email address", 400)
    if not b.get("terms") or not b.get("age_ok"):
        return err("Please accept the terms and privacy policy, and confirm your age", 400)
    if is_blocked(email):
        return err("Sign-ups from this address are not possible. Contact us if you think this is a mistake.", 403)
    if db().first("SELECT id FROM users WHERE LOWER(email) = ?", email):
        return err("Email already exists", 400)
    if not RESEND_API_KEY:
        # Without email there is no way to confirm an address: the account works at once, as before.
        app.logger.warning("Sign-up without email confirmation: RESEND_API_KEY is not set")
        db().run("INSERT INTO users (name, email, password_hash, role, email_verified, terms_accepted_at, terms_version) VALUES (?, ?, ?, 'Customer', 1, ?, ?)",
                 name, email, hash_password(password), utc_stamp(), TERMS_VERSION)
        new_user = db().first("SELECT id FROM users WHERE LOWER(email) = ?", email)
        discord_auto("team", {"text": f"🧑‍✈️ New account: **{name[:80]}**"})
        return jsonify({"token": new_session(new_user["id"]), "name": name, "email": email, "role": "Customer", "is_admin": False})
    db().run("INSERT INTO users (name, email, password_hash, role, email_verified, terms_accepted_at, terms_version) VALUES (?, ?, ?, 'Customer', 0, ?, ?)",
             name, email, hash_password(password), utc_stamp(), TERMS_VERSION)
    user = db().first("SELECT * FROM users WHERE LOWER(email) = ?", email)
    error = send_verify_email(user)
    if error:
        db().run("DELETE FROM users WHERE id = ?", user["id"])  # nothing half-made that would block the address
        return err("We could not send the confirmation email right now. Please try again in a few minutes.", 502)
    discord_auto("team", {"text": f"🧑‍✈️ New account: **{name[:80]}** (confirming their email)"})
    return jsonify({"verify": True, "email": email,
                    "message": f"Almost done: we sent a link to {email}. Click it to confirm your email and open your account."})


# ---------- email confirmation ----------

VERIFY_HOURS = 48


def verify_signature(user_id, expires, email):
    message = f"verify:{user_id}:{expires}:{(email or '').lower()}".encode()
    return hmac.new(SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()[:40]


def verify_token(user):
    expires = int(time.time()) + VERIFY_HOURS * 3600
    return f"{user['id']}.{expires}.{verify_signature(user['id'], expires, user['email'])}"


def user_for_verify_token(token):
    try:
        user_id, expires, signature = str(token or "").strip().split(".")
        user_id, expires = int(user_id), int(expires)
    except ValueError:
        return None
    if expires < time.time():
        return None
    user = db().first("SELECT * FROM users WHERE id = ?", user_id)
    if not user or not hmac.compare_digest(verify_signature(user_id, expires, user["email"]), signature):
        return None
    return user


def verify_email_html(name, link):
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#3a3c40">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff">
<tr><td style="background:#111315;padding:20px 28px;color:#ffffff;font-weight:bold;letter-spacing:3px;font-size:15px">ROTAIDEM</td></tr>
<tr><td style="padding:28px 28px 8px;font-size:16px;line-height:1.6">
<h1 style="margin:0 0 12px;font-size:22px;color:#111315">Confirm your email</h1>
<p style="margin:0 0 12px">Hi {escape(name or "there")}, welcome to Rotaidem.</p>
<p style="margin:0 0 20px">Click the button to confirm that this is your email address. Then your account opens, with your licence keys, downloads and orders. The link works for {VERIFY_HOURS} hours.</p></td></tr>
<tr><td style="padding:4px 28px 20px"><a href="{escape(link)}" style="display:inline-block;background:#111315;color:#ffffff;text-decoration:none;padding:13px 24px;font-weight:bold">Confirm my email</a></td></tr>
<tr><td style="padding:0 28px 24px;font-size:13px;line-height:1.6;color:#6d6f75">If the button does not work, copy this address into your browser:<br><a href="{escape(link)}" style="color:#6d6f75;word-break:break-all">{escape(link)}</a></td></tr>
<tr><td style="padding:20px 28px;border-top:1px solid #e4e4e7;font-size:12px;color:#6d6f75">Did not sign up? Then someone typed your address by mistake: ignore this email and no account is opened. Questions? Email <a href="mailto:Eliaz@rotaidem.com" style="color:#6d6f75">Eliaz@rotaidem.com</a>.</td></tr>
</table></td></tr></table></body></html>"""


def send_verify_email(user):
    """Sends the confirmation link; returns an error text, or "" when Resend took it."""
    link = f"{site_url()}/verify-email?token={urllib.parse.quote(verify_token(user))}"
    _sent, error = send_batch([{"from": MAIL_FROM, "to": [user["email"]], "subject": "Confirm your email for Rotaidem",
                                "html": verify_email_html(user["name"], link)}])
    if error:
        app.logger.error("Confirmation email to user %s failed: %s", user["id"], error)
    return error


@app.post("/api/verify-email")
def verify_email():
    """The link in the confirmation email: confirms the address and logs the person in."""
    user = user_for_verify_token(body().get("token"))
    if not user:
        return err("This confirmation link has expired or is not valid. Log in to get a new one.", 400)
    db().run("UPDATE users SET email_verified = 1 WHERE id = ?", user["id"])
    token = new_session(user["id"])
    if not email_verified(user):
        db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'email_verified', ?, ?, ?)",
                 user["id"], user["email"], request.remote_addr or "", utc_stamp())
    return jsonify({**session_payload(user, token), "verified": True})


@app.post("/api/verify-email/resend")
def resend_verify_email():
    """Always gives the same answer, so nobody can find out which addresses have an account."""
    ident = str(body().get("email", "")).strip().lower()
    if not ident:
        return err("Enter your email address", 400)
    if not RESEND_API_KEY:
        return err("Email is not available right now. Please email Eliaz@rotaidem.com.", 503)
    ip = request.remote_addr or ""
    hour_ago = utc_stamp(3600)
    recent = db().first("SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'verify_email_request' AND ip_address = ? AND created_at >= ?", ip, hour_ago)
    if recent and recent["n"] >= RESET_PER_IP:
        return err("Too many requests. Please wait an hour and try again.", 429)
    user = db().first("SELECT * FROM users WHERE LOWER(email) = ? OR LOWER(name) = ?", ident, ident)
    db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'verify_email_request', ?, ?, ?)",
             user["id"] if user else None, ident[:200], ip, utc_stamp())
    answer = jsonify({"ok": True, "message": "If that account still needs confirming, a new link is on its way. Check your inbox (and spam)."})
    if not user or email_verified(user):
        return answer
    sent_lately = db().first("SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'verify_email_request' AND admin_id = ? AND created_at >= ?",
                             user["id"], hour_ago)
    if sent_lately and sent_lately["n"] > RESET_PER_ACCOUNT:
        return answer
    if send_verify_email(user):
        return err("The email could not be sent right now. Please try again later.", 502)
    return answer


@app.post("/api/logout")
def logout():
    token = bearer()
    if token:
        db().run("DELETE FROM sessions WHERE token_hash = ?", token_hash(token))
        db().run("UPDATE users SET token = NULL WHERE token = ?", token)  # a login from before sessions
    return jsonify({"ok": True})


# ---------- where you are logged in ----------

@app.get("/api/account/sessions")
def account_sessions():
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    return jsonify({"sessions": session_rows(user["id"], user.get("session_id"))})


@app.delete("/api/account/sessions/<int:session_id>")
def account_end_session(session_id):
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    db().run("DELETE FROM sessions WHERE id = ? AND user_id = ?", session_id, user["id"])
    return jsonify({"ok": True, "current": session_id == user.get("session_id")})


@app.post("/api/account/sessions/others")
def account_end_other_sessions():
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    end_sessions(user["id"], keep=user.get("session_id"))
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    count = db().first("SELECT COUNT(*) AS n FROM orders WHERE customer_id = ?", user["id"])
    return jsonify({"user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"],
                             "is_admin": user["role"] in STAFF, "orders_count": count["n"] if count else 0,
                             "discord": {"available": discord_ready(), "linked": bool(user.get("discord_id")), "name": user.get("discord_name") or "",
                                         "buyer": bool(user.get("discord_id")) and has_buyer_licence(user)}}})


# ---------- admin password ----------
# An extra password in front of the admin pages, on top of each staff member's own login. Only
# its hash is stored (meta "admin.gate"). After entering it, the browser gets an unlock code
# signed over the staff member's session and the current admin password; every admin request
# sends it in X-Rotaidem-Admin. Logging out, logging in elsewhere or changing the admin password
# makes old unlock codes useless. Set or change it under Admin > Security, or over SSH with
# `strato/setup.py admin-password`.

GATE_KEY = "admin.gate"
GATE_HOURS = 12
GATE_ATTEMPTS, GATE_MINUTES = 5, 15


def gate_hash():
    if "gate_hash" not in g:
        try:
            g.gate_hash = str(get_setting(GATE_KEY, "") or "")
        except Exception:  # noqa: BLE001 - e.g. before the tables exist; the role checks still apply
            g.gate_hash = ""
    return g.gate_hash


def gate_signature(user, expires, stored):
    message = f"gate:{user['id']}:{user.get('token') or ''}:{expires}:{stored}".encode()
    return hmac.new(SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()[:40]


def admin_unlocked(user):
    stored = gate_hash()
    if not stored:
        return True
    try:
        expires, signature = request.headers.get("X-Rotaidem-Admin", "").strip().split(".")
        expires = int(expires)
    except ValueError:
        expires, signature = 0, ""
    if expires > time.time() and hmac.compare_digest(gate_signature(user, expires, stored), signature):
        return True
    g.admin_locked = True  # turns this request's "Admin only" answer into "enter the admin password"
    return False


def staff_role_user():
    """A staff member by role, whether or not the admin password was entered yet."""
    user = current_user()
    return user if user and user["role"] in STAFF else None


@app.after_request
def admin_locked_answer(resp):
    if getattr(g, "admin_locked", False) and resp.status_code in (401, 403):
        resp.set_data(json.dumps({"error": "Enter the admin password to continue.", "locked": True}))
        resp.status_code, resp.mimetype = 403, "application/json"
    return resp


@app.get("/api/admin/gate")
def admin_gate_state():
    user = staff_role_user()
    if not user:
        return err("Admin only", 403)
    enabled = bool(gate_hash())
    return jsonify({"enabled": enabled, "unlocked": admin_unlocked(user), "can_change": user["role"] == "Owner"})


@app.post("/api/admin/unlock")
def admin_unlock():
    user = staff_role_user()
    if not user:
        return err("Admin only", 403)
    stored = gate_hash()
    if not stored:
        return jsonify({"unlock": "", "enabled": False})
    cutoff = utc_stamp(GATE_MINUTES * 60)
    failed = db().first("SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'admin_unlock_failed' AND admin_id = ? AND created_at >= ?",
                        user["id"], cutoff)
    if failed and failed["n"] >= GATE_ATTEMPTS:
        return err(f"Too many wrong attempts. Wait {GATE_MINUTES} minutes and try again.", 429)
    if not verify_password(str(body().get("password", "")), stored):
        db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'admin_unlock_failed', '', ?, ?)",
                 user["id"], request.remote_addr or "", utc_stamp())
        return err("Wrong admin password", 401)
    db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'admin_unlock', '', ?, ?)",
             user["id"], request.remote_addr or "", utc_stamp())
    expires = int(time.time()) + GATE_HOURS * 3600
    return jsonify({"unlock": f"{expires}.{gate_signature(user, expires, stored)}", "enabled": True})


@app.post("/api/admin/gate")
def admin_set_gate():
    """Owner only. Turning it on needs no current admin password; changing or removing it does."""
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    if user["role"] != "Owner":
        return err("Only the Owner can change the admin password", 403)
    b = body()
    stored = gate_hash()
    if stored and not verify_password(str(b.get("current_password", "")), stored):
        return err("The current admin password is wrong", 400)
    new = str(b.get("new_password", ""))
    if b.get("disable"):
        db().run("DELETE FROM meta WHERE key = ?", GATE_KEY)
        audit(user, "admin_password", "removed")
        return jsonify({"ok": True, "enabled": False})
    if len(new) < 8:
        return err("The admin password needs at least 8 characters", 400)
    if verify_password(new, user["password_hash"]):
        return err("Use a different password than your own login password", 400)
    g.gate_hash = hash_password(new)
    set_setting(GATE_KEY, g.gate_hash)
    audit(user, "admin_password", "changed" if stored else "turned on")
    expires = int(time.time()) + GATE_HOURS * 3600
    return jsonify({"ok": True, "enabled": True, "unlock": f"{expires}.{gate_signature(user, expires, g.gate_hash)}"})


# ---------- forgot password ----------
# The reset link carries "user id.expiry.signature". The signature covers the current password
# hash, so a link stops working as soon as the password changes (it can be used once) and nothing
# has to be stored. Same format as the Cloudflare function.

RESET_HOURS = 1
RESET_PER_ACCOUNT, RESET_PER_IP = 3, 10  # requests per hour


def utc_stamp(seconds_ago=0):
    return datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def reset_signature(user_id, expires, password_hash):
    message = f"reset:{user_id}:{expires}:{password_hash or ''}".encode()
    return hmac.new(SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()[:40]


def reset_token(user):
    expires = int(time.time()) + RESET_HOURS * 3600
    return f"{user['id']}.{expires}.{reset_signature(user['id'], expires, user['password_hash'])}"


def user_for_reset_token(token):
    """The user a reset link belongs to, or None when it is wrong, used or expired."""
    try:
        user_id, expires, signature = str(token or "").strip().split(".")
        user_id, expires = int(user_id), int(expires)
    except ValueError:
        return None
    if expires < time.time():
        return None
    user = db().first("SELECT * FROM users WHERE id = ?", user_id)
    if not user or not hmac.compare_digest(reset_signature(user_id, expires, user["password_hash"]), signature):
        return None
    return user


def mask_email(email):
    name, _, domain = str(email or "").partition("@")
    return (name[:1] + "•" * max(1, min(len(name) - 1, 6)) + "@" + domain) if domain else ""


def reset_email_html(name, link):
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#3a3c40">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff">
<tr><td style="background:#111315;padding:20px 28px;color:#ffffff;font-weight:bold;letter-spacing:3px;font-size:15px">ROTAIDEM</td></tr>
<tr><td style="padding:28px 28px 8px;font-size:16px;line-height:1.6">
<h1 style="margin:0 0 12px;font-size:22px;color:#111315">Reset your password</h1>
<p style="margin:0 0 12px">Hi {escape(name or "there")},</p>
<p style="margin:0 0 20px">Someone (hopefully you) asked to reset the password of your Rotaidem account. Click the button to choose a new one. The link works for {RESET_HOURS} hour and only once.</p></td></tr>
<tr><td style="padding:4px 28px 20px"><a href="{escape(link)}" style="display:inline-block;background:#111315;color:#ffffff;text-decoration:none;padding:13px 24px;font-weight:bold">Choose a new password</a></td></tr>
<tr><td style="padding:0 28px 24px;font-size:13px;line-height:1.6;color:#6d6f75">If the button does not work, copy this address into your browser:<br><a href="{escape(link)}" style="color:#6d6f75;word-break:break-all">{escape(link)}</a></td></tr>
<tr><td style="padding:20px 28px;border-top:1px solid #e4e4e7;font-size:12px;color:#6d6f75">Did not ask for this? Then you can ignore this email: your password stays the same. Questions? Email <a href="mailto:Eliaz@rotaidem.com" style="color:#6d6f75">Eliaz@rotaidem.com</a>.</td></tr>
</table></td></tr></table></body></html>"""


@app.post("/api/password/forgot")
def password_forgot():
    """Always gives the same answer, so nobody can find out which addresses have an account."""
    ident = str(body().get("email", "")).strip().lower()
    if not ident:
        return err("Enter the email address of your account", 400)
    if not RESEND_API_KEY:
        return err("Password reset by email is not available right now. Please email Eliaz@rotaidem.com.", 503)
    ip = request.remote_addr or ""
    hour_ago = utc_stamp(3600)
    recent = db().first("SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'password_reset_request' AND ip_address = ? AND created_at >= ?",
                        ip, hour_ago)
    if recent and recent["n"] >= RESET_PER_IP:
        return err("Too many requests. Please wait an hour and try again.", 429)
    user = db().first("SELECT * FROM users WHERE LOWER(email) = ? OR LOWER(name) = ?", ident, ident)
    db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'password_reset_request', ?, ?, ?)",
             user["id"] if user else None, ident[:200], ip, utc_stamp())
    answer = jsonify({"ok": True, "message": "If an account exists for that address, we have sent a link to reset the password. Check your inbox (and spam)."})
    if not user or not EMAIL_RE.match(user["email"] or ""):
        return answer
    sent_lately = db().first("SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'password_reset_request' AND admin_id = ? AND created_at >= ?",
                             user["id"], hour_ago)
    if sent_lately and sent_lately["n"] > RESET_PER_ACCOUNT:
        return answer  # enough emails for now; say the same thing
    link = f"{site_url()}/reset-password?token={urllib.parse.quote(reset_token(user))}"
    _sent, error = send_batch([{"from": MAIL_FROM, "to": [user["email"]], "subject": "Reset your Rotaidem password",
                                "html": reset_email_html(user["name"], link)}])
    if error:
        app.logger.error("Password reset email to user %s failed: %s", user["id"], error)
        return err("The email could not be sent right now. Please try again later or email Eliaz@rotaidem.com.", 502)
    return answer


@app.post("/api/admin/users/<int:user_id>/reset-link")
def admin_send_reset_link(user_id):
    """Staff send a user the same reset email as Forgot password; the link works for RESET_HOURS."""
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    if not RESEND_API_KEY:
        return err("Email is not set up. Add RESEND_API_KEY to the settings (on STRATO: rotaidem-data/config.env).", 400)
    target = db().first("SELECT * FROM users WHERE id = ?", user_id)
    if not target or not EMAIL_RE.match(target["email"] or ""):
        return err("That user has no valid email address", 400)
    link = f"{site_url()}/reset-password?token={urllib.parse.quote(reset_token(target))}"
    _sent, error = send_batch([{"from": MAIL_FROM, "to": [target["email"]], "subject": "Reset your Rotaidem password",
                                "html": reset_email_html(target["name"], link)}])
    if error:
        return err(error, 502)
    audit(user, "send_reset_link", target["email"])
    return jsonify({"ok": True, "sent_to": target["email"]})


@app.post("/api/password/check")
def password_check():
    user = user_for_reset_token(body().get("token"))
    if not user:
        return err("This link has expired or was already used. Ask for a new one.", 400)
    return jsonify({"ok": True, "email": mask_email(user["email"])})


@app.post("/api/password/reset")
def password_reset():
    b = body()
    user = user_for_reset_token(b.get("token"))
    if not user:
        return err("This link has expired or was already used. Ask for a new one.", 400)
    password = str(b.get("password", ""))
    if len(password) < 6:
        return err("The new password must be at least 6 characters", 400)
    db().run("UPDATE users SET password_hash = ?, email_verified = 1 WHERE id = ?", hash_password(password), user["id"])
    end_sessions(user["id"])  # a new password logs out every other browser
    token = new_session(user["id"])
    db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'password_reset', ?, ?, ?)",
             user["id"], user["email"] or "", request.remote_addr or "", utc_stamp())
    return jsonify(session_payload(user, token))


@app.post("/api/account/settings")
def account_settings():
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    b = body()
    name = str(b.get("name", "")).strip()
    current = str(b.get("current_password", ""))
    new = str(b.get("new_password", ""))
    if not name:
        return err("Name is required", 400)
    if new:
        if len(new) < 6:
            return err("New password must be at least 6 characters", 400)
        if not verify_password(current, user["password_hash"]):
            return err("Current password is required and must be correct", 400)
        db().run("UPDATE users SET name = ?, password_hash = ? WHERE id = ?", name, hash_password(new), user["id"])
        end_sessions(user["id"], keep=user.get("session_id"))  # a new password logs out the other browsers
    else:
        db().run("UPDATE users SET name = ? WHERE id = ?", name, user["id"])
    return jsonify({"ok": True, "name": name})


@app.get("/api/account/orders")
def account_orders():
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    return jsonify({"orders": db().all("SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC", user["id"])})


# ---------- catalog ----------

def public_product(row, staff=False):
    """A product for the website. Products are only downloaded through RotaHub, so the installer link
    stays hidden from everyone but staff; has_download says there is one."""
    p = with_images(row)
    try:
        p["changelog"] = json.loads(p.get("changelog") or "[]")
    except ValueError:
        p["changelog"] = []
    p["download_gate"] = p.get("download_gate") or "public"
    if not staff:
        p["has_download"] = bool(p.get("download_url"))
        p["download_url"] = ""
    # Bought on the website through Stripe Checkout (Admin -> Products -> Price at checkout)
    p["can_buy"] = bool(STRIPE_SECRET_KEY and (p.get("pay_amount") or 0) > 0 and p.get("status") == "published")
    p["sale_active"] = sale_active(p)
    p["price_now"] = price_now(p)
    return p


def sale_active(product):
    """A sale runs while its price is below the normal one, up to and including its last day (UTC)."""
    sale, full = int(product.get("sale_amount") or 0), int(product.get("pay_amount") or 0)
    until = str(product.get("sale_until") or "")
    return bool(0 < sale < full and (not until or until >= datetime.now(timezone.utc).strftime("%Y-%m-%d")))


def price_now(product):
    """What Stripe Checkout charges right now, in the smallest unit (ore, cents)."""
    return int(product.get("sale_amount") or 0) if sale_active(product) else int(product.get("pay_amount") or 0)


@app.get("/api/products")
def products():
    rows = db().all("SELECT * FROM products ORDER BY sort_order, name")
    viewer = current_user()
    staff = bool(viewer and viewer["role"] in STAFF)
    return jsonify({"products": [public_product(r, staff) for r in rows]})


def clean_changelog(items):
    """Version history of a product: [{version, date, notes}], newest first, at most 50."""
    out = []
    for i in (items if isinstance(items, list) else [])[:50]:
        version = str((i or {}).get("version") or "").strip()[:30]
        if not version:
            continue
        date = str(i.get("date") or "").strip()
        out.append({"version": version, "date": date if DATE_RE.match(date) else "", "notes": str(i.get("notes") or "").strip()[:3000]})
    return out


DOWNLOAD_REPEAT_MINUTES = 10  # the same address downloading again within this time is counted once


@app.post("/api/download/<pid>")
def download(pid):
    """Gives the download link and counts the download. "Licence holders only" products need a
    logged-in account with an active licence for the product."""
    product = db().first("SELECT * FROM products WHERE id = ?", pid)
    url = str((product or {}).get("download_url") or "")
    if not product or not (url.startswith("https://") or url.startswith("/files/")):
        return err("This product has no download yet", 404)
    user = current_user()
    # The website only hands out RotaHub; RotaHub itself (User-Agent RotaHub/...) gets the installer.
    via_hub = not request.headers.get("User-Agent", "").startswith("RotaHub")
    if (product.get("download_gate") or "public") == "licence" and not via_hub:
        if not user:
            return jsonify({"error": "Log in to download. The download is for licence holders.", "login": True}), 401
        if user["role"] not in STAFF:
            rows = db().all("SELECT product_id FROM licenses WHERE status = 'active' AND (customer_id = ? OR LOWER(email) = ?)",
                            user["id"], (user["email"] or "").lower())
            if not any(same_product(pid, r["product_id"]) for r in rows):
                return err("This download is for licence holders. Your account has no active licence for it yet.", 403)
    ip = (request.remote_addr or "")[:64]
    if not db().first("SELECT id FROM downloads WHERE product_id = ? AND ip_address = ? AND created_at >= ?",
                      pid, ip, utc_stamp(DOWNLOAD_REPEAT_MINUTES * 60)):
        db().run("INSERT INTO downloads (product_id, user_id, ip_address, created_at) VALUES (?, ?, ?, ?)",
                 pid, user["id"] if user else None, ip, utc_stamp())
    if via_hub:
        return jsonify({"hub": True, "url": hub_config()["downloads"]["rotahub_url"], "product": product["name"] or pid})
    return jsonify({"url": url})


# ---------- the fun part of the dashboard ----------

def day_counts(sql, since):
    """{"YYYY-MM-DD": n} from a query that returns one created_at per row."""
    out = {}
    for r in db().all(sql, since):
        day = str(r["created_at"] or "")[:10]
        if day:
            out[day] = out.get(day, 0) + 1
    return out


@app.get("/api/admin/insights")
def admin_insights():
    """This week against last week, and a year of activity per day for the heatmap."""
    if not staff_user():
        return err("Admin only", 403)
    week, two_weeks, year = utc_stamp(7 * 86400), utc_stamp(14 * 86400), utc_stamp(366 * 86400)
    sources = {"downloads": "SELECT created_at FROM downloads WHERE created_at >= ?",
               "accounts": "SELECT created_at FROM users WHERE created_at >= ?",
               "orders": "SELECT created_at FROM orders WHERE created_at >= ?",
               "messages": "SELECT created_at FROM messages WHERE created_at >= ?",
               "subscribers": "SELECT created_at FROM subscribers WHERE created_at >= ?"}
    recap, heatmap = {}, {}
    for name, sql in sources.items():
        days = day_counts(sql, year)
        recap[name] = {"this_week": sum(v for d, v in days.items() if d >= week[:10]),
                       "last_week": sum(v for d, v in days.items() if two_weeks[:10] <= d < week[:10])}
        if name in ("downloads", "accounts", "orders"):
            for d, v in days.items():
                heatmap[d] = heatmap.get(d, 0) + v
    return jsonify({"recap": recap, "heatmap": heatmap})


STAFF_NOTE_COLOURS = ("yellow", "blue", "green", "pink")


@app.get("/api/admin/notes")
def admin_notes():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"notes": get_setting("staff.notes", []) or []})


@app.post("/api/admin/notes")
def admin_add_note():
    """A sticky note on the dashboard, shared by all staff."""
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    b = body()
    text = str(b.get("text") or "").strip()[:500]
    if not text:
        return err("Write something on the note first", 400)
    notes = get_setting("staff.notes", []) or []
    if len(notes) >= 40:
        return err("The board is full. Remove an old note first.", 400)
    notes.insert(0, {"id": uuid.uuid4().hex[:12], "text": text, "author": user["name"], "created_at": utc_stamp(),
                     "colour": b.get("colour") if b.get("colour") in STAFF_NOTE_COLOURS else "yellow"})
    set_setting("staff.notes", notes)
    return jsonify({"ok": True, "notes": notes})


@app.delete("/api/admin/notes/<note_id>")
def admin_delete_note(note_id):
    if not staff_user():
        return err("Admin only", 403)
    notes = [n for n in (get_setting("staff.notes", []) or []) if n.get("id") != note_id]
    set_setting("staff.notes", notes)
    return jsonify({"ok": True, "notes": notes})


# ---------- short link clicks, goals, crew leaderboard ----------

@app.post("/api/link-hit/<slug>")
def link_hit(slug):
    """Counted by 404.html just before it forwards a short link. Only existing short links count."""
    slug = str(slug or "").strip().lower()
    if slug not in (get_setting("site.links", {}) or {}):
        return jsonify({"ok": True})
    hits = get_setting("links.hits", {}) or {}
    row = hits.get(slug) if isinstance(hits.get(slug), dict) else {}
    hits[slug] = {"total": int(row.get("total") or 0) + 1, "last": utc_stamp()}
    set_setting("links.hits", hits)
    return jsonify({"ok": True})


@app.get("/api/admin/link-hits")
def admin_link_hits():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"hits": get_setting("links.hits", {}) or {}})


GOAL_KEYS = ("revenue", "customers", "downloads", "subscribers")


@app.get("/api/admin/goals")
def admin_goals():
    if not staff_user():
        return err("Admin only", 403)
    saved = get_setting("dashboard.goals", {}) or {}
    return jsonify({"goals": {k: saved.get(k) or 0 for k in GOAL_KEYS}})


@app.post("/api/admin/goals")
def admin_save_goals():
    """Targets shown as progress bars on the dashboard; 0 hides one."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can set goals", 403)
    b, goals = body(), {}
    for k in GOAL_KEYS:
        try:
            v = float(b.get(k) or 0)
        except (TypeError, ValueError):
            return err(f"The {k} goal must be a number", 400)
        if v < 0 or v > 1e9:
            return err(f"The {k} goal is out of range", 400)
        goals[k] = round(v, 2) if k == "revenue" else int(v)
    set_setting("dashboard.goals", goals)
    audit(user, "save_goals", ", ".join(f"{k} {v}" for k, v in goals.items() if v))
    return jsonify({"ok": True, "goals": goals})


@app.get("/api/admin/leaderboard")
def admin_leaderboard():
    """What each staff member did this month, from the activity log."""
    if not staff_user():
        return err("Admin only", 403)
    since = datetime.now(timezone.utc).strftime("%Y-%m-01 00:00:00")
    rows = db().all("SELECT u.name AS name, a.action AS action, COUNT(*) AS n FROM audit_logs a JOIN users u ON u.id = a.admin_id "
                    "WHERE a.created_at >= ? AND u.role IN ('Owner', 'Admin', 'Support') GROUP BY u.name, a.action", since)
    return jsonify({"since": since[:10], "rows": [{"name": r["name"], "action": r["action"], "n": int(r["n"] or 0)} for r in rows]})


@app.get("/api/admin/downloads")
def admin_downloads():
    """Downloads per product (all time, 7 and 30 days) and per day for the last 30 days."""
    if not staff_user():
        return err("Admin only", 403)
    n = lambda sql, *args: (db().first(sql, *args) or {}).get("n") or 0  # noqa: E731
    per_product = [{"product_id": r["product_id"], "total": r["n"],
                    "last_7": n("SELECT COUNT(*) AS n FROM downloads WHERE product_id = ? AND created_at >= ?", r["product_id"], utc_stamp(7 * 86400)),
                    "last_30": n("SELECT COUNT(*) AS n FROM downloads WHERE product_id = ? AND created_at >= ?", r["product_id"], utc_stamp(30 * 86400))}
                   for r in db().all("SELECT product_id, COUNT(*) AS n FROM downloads WHERE product_id NOT LIKE ? GROUP BY product_id ORDER BY n DESC", "tester:%")]
    days = {}
    for r in db().all("SELECT created_at FROM downloads WHERE created_at >= ?", utc_stamp(30 * 86400)):
        day = str(r["created_at"])[:10]
        days[day] = days.get(day, 0) + 1
    return jsonify({"products": per_product, "days": days, "last_7": n("SELECT COUNT(*) AS n FROM downloads WHERE created_at >= ?", utc_stamp(7 * 86400))})


@app.get("/api/liveries")
def liveries():
    return jsonify({"liveries": db().all("SELECT * FROM liveries ORDER BY name")})


@app.post("/api/save-product")
def save_product():
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    b = body()
    name = str(b.get("name", "")).strip()
    if not name:
        return err("Name is required", 400)
    pid = slug(b.get("id") or name)
    try:
        sort_order = int(b.get("sort_order") or 0)
    except (TypeError, ValueError):
        sort_order = 0
    download_url = str(b.get("download_url") or "").strip()
    if download_url and not (download_url.startswith("https://") or FILE_LINK.match(download_url)):
        return err("The download link must start with https:// or be an uploaded file (/files/...)", 400)
    version = str(b.get("version") or "0.1.0").strip()[:30]
    try:
        pay_amount = int(round(float(str(b.get("pay_amount") or 0).replace(",", ".")) * 100))
    except (TypeError, ValueError):
        return err("The price at checkout must be a number, like 299 or 29.99", 400)
    pay_currency = str(b.get("pay_currency") or "sek").lower()
    if pay_amount < 0 or pay_amount > 10_000_000 or pay_currency not in PAY_CURRENCIES:
        return err("Check the price at checkout and its currency", 400)
    try:
        sale_amount = int(round(float(str(b.get("sale_amount") or 0).replace(",", ".")) * 100))
    except (TypeError, ValueError):
        return err("The sale price must be a number, like 5.99", 400)
    sale_until = str(b.get("sale_until") or "").strip()
    if sale_amount and not 0 < sale_amount < pay_amount:
        return err("The sale price must be lower than the price at checkout", 400)
    if sale_until and not DATE_RE.match(sale_until):
        return err("The sale's last day must look like 2026-12-24", 400)
    before = db().first("SELECT version, download_url, updated_at FROM products WHERE id = ?", pid)
    # When the version or the file changes, RotaHub sees a new release from this time on.
    changed = not before or before["version"] != version or (before["download_url"] or "") != download_url
    updated_at = utc_stamp() if changed or not before["updated_at"] else before["updated_at"]
    db().run(
        "INSERT INTO products (id, name, simulator, version, folder_name, download_url, image_url, images, price, description, buy_url, status, tagline, features, sort_order, download_gate, changelog, update_required, updated_at, pay_amount, pay_currency, sale_amount, sale_until) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, simulator=excluded.simulator, "
        "version=excluded.version, folder_name=excluded.folder_name, download_url=excluded.download_url, image_url=excluded.image_url, "
        "images=excluded.images, price=excluded.price, description=excluded.description, buy_url=excluded.buy_url, status=excluded.status, "
        "tagline=excluded.tagline, features=excluded.features, sort_order=excluded.sort_order, download_gate=excluded.download_gate, changelog=excluded.changelog, "
        "update_required=excluded.update_required, updated_at=excluded.updated_at, pay_amount=excluded.pay_amount, pay_currency=excluded.pay_currency, sale_amount=excluded.sale_amount, sale_until=excluded.sale_until",
        pid, name, b.get("simulator") or "MSFS 2024", version, b.get("folder_name") or f"rotaidem-{pid}",
        download_url, b.get("image_url") or "", json.dumps(list_images(b.get("images"))),
        b.get("price") or "", b.get("description") or "", b.get("buy_url") or "mailto:Eliaz@rotaidem.com",
        b.get("status") or "in_development", b.get("tagline") or "", json.dumps(list_images(b.get("features"))), sort_order,
        "licence" if b.get("download_gate") == "licence" else "public", json.dumps(clean_changelog(b.get("changelog"))),
        1 if b.get("update_required") else 0, updated_at, pay_amount, pay_currency, sale_amount, sale_until)
    audit(user, "save_product", pid + (f" (update {version})" if before and changed else ""))
    if before and before["version"] != version and (b.get("status") or "") == "published":
        entry = next((c for c in clean_changelog(b.get("changelog")) if c.get("version") == version), {})
        discord_auto("updates", {"ping": "buyers" if discord_settings()["updates_ping"] else "none",
                                 "embed": {"title": f"{name} {version} is out", "description": (entry.get("notes") or b.get("tagline") or "")[:1500],
                                           "url": "/addons/view?id=" + pid, "image": b.get("image_url") or ""},
                                 "buttons": [{"label": "Update in RotaHub", "url": "/get-rotahub?product=" + urllib.parse.quote(name)},
                                             {"label": "What's new", "url": "/addons/view?id=" + pid}]})
    return jsonify({"ok": True, "id": pid, "page": "/addons/view?id=" + pid})


@app.delete("/api/products/<pid>")
def delete_product(pid):
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    db().run("DELETE FROM products WHERE id = ?", pid)
    audit(user, "delete_product", pid)
    return jsonify({"ok": True})


@app.post("/api/save-livery")
def save_livery():
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    b = body()
    name = str(b.get("name", "")).strip()
    if not name:
        return err("Name is required", 400)
    lid = slug(b.get("id") or name)
    db().run("INSERT INTO liveries (id, name, aircraft, folder_name, download_url, image_url) VALUES (?, ?, ?, ?, ?, ?) "
             "ON CONFLICT(id) DO UPDATE SET name=excluded.name, aircraft=excluded.aircraft, folder_name=excluded.folder_name, "
             "download_url=excluded.download_url, image_url=excluded.image_url",
             lid, name, b.get("aircraft") or "", b.get("folder_name") or f"rotaidem-{lid}", b.get("download_url") or "", b.get("image_url") or "")
    return jsonify({"ok": True, "id": lid})


# ---------- newsletter & contact ----------

@app.get("/api/newsletter")
def newsletter():
    """Published posts only; drafts are listed in the admin (/api/admin/posts)."""
    return jsonify({"posts": db().all("SELECT * FROM posts WHERE status IS NULL OR status = 'published' OR "
                                      "(status = 'scheduled' AND publish_at <= ?) ORDER BY id DESC", utc_stamp())})


@app.get("/api/admin/posts")
def admin_posts():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"posts": db().all("SELECT * FROM posts ORDER BY id DESC")})


@app.post("/api/newsletter")
def save_newsletter():
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    b = body()
    if not b.get("title") or not b.get("content"):
        return err("Title and content are required", 400)
    status = b.get("status") if b.get("status") in ("draft", "scheduled") else "published"
    publish_at = ""
    if status == "scheduled":
        try:  # from the admin as an ISO time in UTC, e.g. 2026-11-01T17:00:00.000Z
            when = datetime.strptime(str(b.get("publish_at") or "")[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return err("Pick the date and time the post should appear", 400)
        publish_at = when.strftime("%Y-%m-%d %H:%M:%S")
        if publish_at <= utc_stamp():
            return err("Pick a time in the future, or publish it now", 400)
        b["date"] = when.strftime("%Y-%m-%d")  # the date shown is the day it appears
    if b.get("id"):
        old = db().first("SELECT date FROM posts WHERE id = ?", int(b["id"])) or {}
        date = post_date(b["date"]) if b.get("date") else old.get("date") or today()
        db().run("UPDATE posts SET title = ?, content = ?, image_url = ?, status = ?, date = ?, publish_at = ? WHERE id = ?",
                 b["title"], b["content"], b.get("image_url") or "", status, date, publish_at, int(b["id"]))
    else:
        db().run("INSERT INTO posts (title, content, image_url, date, status, publish_at) VALUES (?, ?, ?, ?, ?, ?)",
                 b["title"], b["content"], b.get("image_url") or "", post_date(b.get("date")), status, publish_at)
    audit(user, "save_post", ("draft: " if status == "draft" else "") + b["title"])
    discord_announce_news()
    return jsonify({"ok": True})


@app.delete("/api/newsletter/<int:post_id>")
def delete_newsletter(post_id):
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    db().run("DELETE FROM posts WHERE id = ?", post_id)
    audit(user, "delete_post", post_id)
    return jsonify({"ok": True})


@app.post("/api/contact")
def contact():
    b = body()
    if not str(b.get("message", "")).strip():
        return err("Message is required", 400)
    email = str(b.get("email", "")).strip()[:200]
    if is_blocked(email):
        return jsonify({"ok": True})  # dropped quietly: a spammer learns nothing
    db().run("INSERT INTO messages (name, email, message) VALUES (?, ?, ?)",
             str(b.get("name", ""))[:200], email, str(b.get("message", ""))[:5000])
    send_auto_reply(str(b.get("name", ""))[:200], email)
    discord_team_message(str(b.get("name", "")).strip() or email or "someone", b.get("message", ""), "contact form")
    return jsonify({"ok": True})


@app.post("/api/subscribe")
def subscribe():
    b = body()
    email = str(b.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        return err("Please enter a valid email address", 400)
    if is_blocked(email):
        return jsonify({"ok": True})
    existing = db().first("SELECT id FROM subscribers WHERE email = ?", email)
    if existing:
        db().run("UPDATE subscribers SET unsubscribed = 0 WHERE id = ?", existing["id"])
    else:
        db().run("INSERT INTO subscribers (email, source) VALUES (?, ?)", email, str(b.get("source", ""))[:100])
    return jsonify({"ok": True})


def unsubscribe_token(email):
    return hmac.new(SECRET_KEY.encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:32]


def unsubscribe_headers(email):
    """List-Unsubscribe headers: Gmail, Outlook and Apple Mail show an Unsubscribe button that works in one click."""
    url = f"{site_url()}/api/unsubscribe/one-click?email={urllib.parse.quote(email)}&token={unsubscribe_token(email)}"
    return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def unsubscribe_link(email):
    return f"{site_url()}/unsubscribe?email={urllib.parse.quote(email)}&token={unsubscribe_token(email)}"


@app.post("/api/unsubscribe/one-click")
def unsubscribe_one_click():
    email = str(request.args.get("email", "")).strip().lower()
    if not email or not hmac.compare_digest(unsubscribe_token(email), str(request.args.get("token", ""))):
        return err("This unsubscribe link is not valid", 400)
    mark_unsubscribed(email)
    return jsonify({"ok": True})


def mark_unsubscribed(email):
    if db().first("SELECT id FROM subscribers WHERE email = ?", email):
        db().run("UPDATE subscribers SET unsubscribed = 1 WHERE email = ?", email)
    else:
        db().run("INSERT INTO subscribers (email, source, unsubscribed) VALUES (?, 'unsubscribe', 1)", email)


@app.post("/api/unsubscribe")
def unsubscribe():
    b = body()
    email = str(b.get("email", "")).strip().lower()
    if not email or not hmac.compare_digest(unsubscribe_token(email), str(b.get("token", ""))):
        return err("This unsubscribe link is not valid", 400)
    if db().first("SELECT id FROM subscribers WHERE email = ?", email):
        db().run("UPDATE subscribers SET unsubscribed = 1 WHERE email = ?", email)
    else:
        db().run("INSERT INTO subscribers (email, source, unsubscribed) VALUES (?, 'unsubscribe', 1)", email)
    return jsonify({"ok": True})


# ---------- admin ----------

@app.get("/api/admin/overview")
def admin_overview():
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    n = lambda sql: (db().first(sql) or {}).get("n") or 0
    revenue = db().first("SELECT COALESCE(SUM(amount), 0) AS n FROM orders WHERE payment_status = 'Paid'")
    return jsonify({"id": user["id"], "name": user["name"], "role": user["role"],
                    "products": n("SELECT COUNT(*) AS n FROM products"),
                    "customers": n("SELECT COUNT(*) AS n FROM users WHERE role = 'Customer'"),
                    "new_users_7d": n(f"SELECT COUNT(*) AS n FROM users WHERE created_at >= '{utc_stamp(7 * 86400)}'"),
                    "orders": n("SELECT COUNT(*) AS n FROM orders"),
                    "posts": n("SELECT COUNT(*) AS n FROM posts"),
                    "drafts": n("SELECT COUNT(*) AS n FROM posts WHERE status = 'draft'"),
                    "messages": n("SELECT COUNT(*) AS n FROM messages"),
                    "open_messages": n("SELECT COUNT(*) AS n FROM messages WHERE handled IS NULL OR handled = 0"),
                    "subscribers": n("SELECT COUNT(*) AS n FROM subscribers WHERE unsubscribed = 0"),
                    "licenses": n("SELECT COUNT(*) AS n FROM licenses WHERE status = 'active'"),
                    "downloads": n("SELECT COUNT(*) AS n FROM downloads"),
                    "emails_sent": n("SELECT COALESCE(SUM(sent_count), 0) AS n FROM campaigns"),
                    "media": n("SELECT COUNT(*) AS n FROM media"),
                    "staff_online": staff_online(),
                    "email_ready": bool(RESEND_API_KEY), "payments_ready": bool(STRIPE_SECRET_KEY),
                    "revenue": float(revenue["n"] if revenue else 0)})


@app.get("/api/admin/customers")
def admin_customers():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"customers": db().all("SELECT id, name, email, role, created_at, email_verified FROM users ORDER BY id DESC")})


@app.post("/api/admin/users/<int:user_id>/role")
def admin_set_role(user_id):
    user = staff_user()
    if not user or user["role"] != "Owner":
        return err("Owner only", 403)
    role = body().get("role")
    if role not in ROLES:
        return err("Unknown role", 400)
    if user_id == user["id"]:
        return err("You cannot change your own role", 400)
    db().run("UPDATE users SET role = ? WHERE id = ?", role, user_id)
    end_sessions(user_id)
    audit(user, "set_role", f"{user_id}:{role}")
    return jsonify({"ok": True})


@app.get("/api/admin/orders")
def admin_orders():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"orders": db().all(
        "SELECT orders.*, users.name AS customer_name, users.email AS customer_email FROM orders "
        "LEFT JOIN users ON orders.customer_id = users.id ORDER BY orders.id DESC")})


@app.post("/api/admin/orders")
def admin_save_order():
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    b = body()
    try:
        customer_id = int(b.get("customer_id"))
        amount = float(b.get("amount") or 0)
    except (TypeError, ValueError):
        return err("Customer and amount must be numbers", 400)
    db().run("INSERT INTO orders (customer_id, product_id, status, payment_status, amount) VALUES (?, ?, ?, ?, ?)",
             customer_id, b.get("product_id") or "", b.get("status") or "Completed", b.get("payment_status") or "Paid", amount)
    audit(user, "create_order", customer_id)
    key = ""
    if b.get("create_license") and b.get("product_id"):
        customer = db().first("SELECT id, email FROM users WHERE id = ?", customer_id)
        order = db().first("SELECT MAX(id) AS id FROM orders WHERE customer_id = ?", customer_id)
        while not key:
            candidate = generate_key(str(b.get("license_prefix") or "HPRO"))
            if insert_license(candidate, b["product_id"], customer["email"] if customer else "", customer_id, order["id"] if order else None,
                              2, "Created with order", user["name"]):
                key = candidate
        audit(user, "create_licenses", f"{key} for order")
        email = customer["email"] if customer else ""
        if b.get("send_email", True) and email:
            error = email_license_keys(email, [key], b["product_id"])
            if not error:
                audit(user, "email_licenses", email)
            return jsonify({"ok": True, "license_key": key, **email_reply(email, error)})
    return jsonify({"ok": True, "license_key": key})


ORDER_PAYMENTS = ("Paid", "Pending", "Refunded")


@app.post("/api/admin/orders/<int:order_id>")
def admin_update_order(order_id):
    """Change an order's payment (e.g. mark it refunded) or amount. Owner and Admins only."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change orders", 403)
    order = db().first("SELECT * FROM orders WHERE id = ?", order_id)
    if not order:
        return err("Order not found", 404)
    b = body()
    payment = b.get("payment_status", order["payment_status"])
    if payment not in ORDER_PAYMENTS:
        return err("Payment must be Paid, Pending or Refunded", 400)
    try:
        amount = round(float(b.get("amount", order["amount"]) or 0), 2)
    except (TypeError, ValueError):
        return err("The amount must be a number", 400)
    if amount < 0:
        return err("The amount cannot be negative", 400)
    db().run("UPDATE orders SET payment_status = ?, amount = ? WHERE id = ?", payment, amount, order_id)
    audit(user, "update_order", f"#{order_id} {payment} {amount:.2f}")
    return jsonify({"ok": True})


@app.delete("/api/admin/orders/<int:order_id>")
def admin_delete_order(order_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can delete orders", 403)
    if not db().first("SELECT id FROM orders WHERE id = ?", order_id):
        return err("Order not found", 404)
    db().run("DELETE FROM orders WHERE id = ?", order_id)
    audit(user, "delete_order", f"#{order_id}")
    return jsonify({"ok": True})


@app.get("/api/admin/messages")
def admin_messages():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"messages": db().all("SELECT * FROM messages ORDER BY id DESC")})


@app.post("/api/admin/messages/<int:msg_id>")
def admin_mark_message(msg_id):
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    handled = 1 if body().get("handled") else 0
    db().run("UPDATE messages SET handled = ? WHERE id = ?", handled, msg_id)
    audit(user, "message_handled" if handled else "message_reopened", msg_id)
    return jsonify({"ok": True})


@app.delete("/api/admin/messages/<int:msg_id>")
def admin_delete_message(msg_id):
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    db().run("DELETE FROM messages WHERE id = ?", msg_id)
    return jsonify({"ok": True})


def staff_online():
    """Staff seen in the last 15 minutes (a session's last_seen moves at most every 5)."""
    rows = db().all("SELECT u.id, u.name, u.role, MAX(s.last_seen) AS last_seen FROM sessions s JOIN users u ON u.id = s.user_id "
                    "WHERE u.role IN ('Owner', 'Admin', 'Support') AND s.last_seen >= ? GROUP BY u.id, u.name, u.role ORDER BY last_seen DESC",
                    utc_stamp(15 * 60))
    return [{"name": r["name"], "role": r["role"], "last_seen": str(r["last_seen"] or "")} for r in rows]


# ---------- inbox: replies by email, saved answers, automatic reply ----------

AUTO_REPLY_DEFAULT = {"enabled": False, "subject": "We got your message",
                      "text": "Thanks for writing to Rotaidem. We read every message and usually answer within a day or two.\n\nIf it is about a licence, "
                              "reply to this email with your licence key and we will help you."}
SAVED_REPLIES_MAX = 30


def inbox_settings():
    saved = get_setting("inbox.settings", {}) or {}
    a = saved.get("autoreply") if isinstance(saved.get("autoreply"), dict) else {}
    return {"autoreply": {"enabled": bool(a.get("enabled")),
                          "subject": str(a.get("subject") or "").strip()[:150] or AUTO_REPLY_DEFAULT["subject"],
                          "text": str(a.get("text") or "").strip()[:3000] or AUTO_REPLY_DEFAULT["text"]},
            "replies": [r for r in (saved.get("replies") or []) if isinstance(r, dict)][:SAVED_REPLIES_MAX]}


def clean_inbox(b):
    """Inbox settings from the admin, checked; raises ValueError with a message for the admin."""
    a = b.get("autoreply") or {}
    subject, text = str(a.get("subject") or "").strip(), str(a.get("text") or "").strip()
    if len(subject) > 150 or len(text) > 3000:
        raise ValueError("The automatic reply is too long (subject 150, text 3000 characters)")
    if a.get("enabled") and not text:
        raise ValueError("Write the automatic reply before switching it on")
    replies = []
    for r in b.get("replies") or []:
        title, body_text = str((r or {}).get("title") or "").strip(), str((r or {}).get("text") or "").strip()
        if not title and not body_text:
            continue
        if not title or not body_text:
            raise ValueError("Each saved answer needs a name and a text")
        if len(title) > 80 or len(body_text) > 3000:
            raise ValueError(f'The saved answer "{title[:40]}" is too long (name 80, text 3000 characters)')
        replies.append({"title": title, "text": body_text})
    if len(replies) > SAVED_REPLIES_MAX:
        raise ValueError(f"At most {SAVED_REPLIES_MAX} saved answers")
    return {"autoreply": {"enabled": bool(a.get("enabled")), "subject": subject, "text": text}, "replies": replies}


def reply_email_html(name, text, signer, original=""):
    paragraphs = "".join(f'<p style="margin:0 0 14px">{escape(p).replace(chr(10), "<br>")}</p>' for p in re.split(r"\n\s*\n", text.strip()) if p.strip())
    quoted = (f'<tr><td style="padding:0 28px 24px"><div style="border-left:3px solid #e4e4e7;padding:4px 0 4px 14px;color:#6d6f75;font-size:14px;line-height:1.5">'
              f'<div style="font-size:12px;margin-bottom:6px">You wrote:</div>{escape(original[:2000]).replace(chr(10), "<br>")}</div></td></tr>') if original else ""
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#3a3c40">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff">
<tr><td style="background:#111315;padding:20px 28px;color:#ffffff;font-weight:bold;letter-spacing:3px;font-size:15px">ROTAIDEM</td></tr>
<tr><td style="padding:28px 28px 6px;font-size:16px;line-height:1.6"><p style="margin:0 0 14px">Hi {escape(name or "there")},</p>{paragraphs}
<p style="margin:0 0 4px">{escape(signer)}<br><span style="color:#6d6f75">Rotaidem</span></p></td></tr>{quoted}
<tr><td style="padding:20px 28px;border-top:1px solid #e4e4e7;font-size:12px;color:#6d6f75">You can answer this email directly.</td></tr>
</table></td></tr></table></body></html>"""


def send_one(to, subject, html):
    """One email, with replies going to the contact address. Returns an error text, or ''."""
    _sent, error = send_batch([{"from": MAIL_FROM, "to": [to], "subject": subject, "html": html, "reply_to": site_config()["contact_email"]}])
    return error


def send_auto_reply(name, email):
    """The automatic "we got your message" email, at most once a day per address."""
    a = inbox_settings()["autoreply"]
    if not (a["enabled"] and RESEND_API_KEY and EMAIL_RE.match(email or "")):
        return
    recent = db().first("SELECT COUNT(*) AS n FROM messages WHERE LOWER(email) = ? AND created_at >= ?", email.lower(), utc_stamp(86400))
    if recent and (recent["n"] or 0) > 1:
        return
    error = send_one(email, a["subject"], reply_email_html(name, a["text"], "The Rotaidem team"))
    if error:
        app.logger.error("Automatic reply failed: %s", error)


@app.get("/api/admin/inbox")
def admin_inbox():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({**inbox_settings(), "email_ready": bool(RESEND_API_KEY)})


@app.post("/api/admin/inbox")
def admin_save_inbox():
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    try:
        cleaned = clean_inbox(body())
    except ValueError as exc:
        return err(str(exc), 400)
    set_setting("inbox.settings", cleaned)
    audit(user, "save_inbox", "automatic reply " + ("on" if cleaned["autoreply"]["enabled"] else "off"))
    return jsonify({"ok": True, **inbox_settings()})


@app.post("/api/admin/messages/<int:msg_id>/reply")
def admin_reply_message(msg_id):
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    msg = db().first("SELECT * FROM messages WHERE id = ?", msg_id)
    if not msg:
        return err("Message not found", 404)
    b = body()
    text = str(b.get("text") or "").strip()
    if not text:
        return err("Write your reply first", 400)
    if len(text) > 10000:
        return err("The reply is too long", 400)
    if not EMAIL_RE.match(str(msg["email"] or "")):
        return err("This message has no valid email address to reply to", 400)
    if not RESEND_API_KEY:
        return err("Email is not set up (RESEND_API_KEY is missing). Use the Email button to answer from your own mail program.", 400)
    subject = str(b.get("subject") or "").strip()[:150] or "Re: your message to Rotaidem"
    error = send_one(msg["email"], subject, reply_email_html(msg["name"], text, user["name"], str(msg["message"] or "")))
    if error:
        return err(error, 502)
    db().run("UPDATE messages SET handled = 1, reply = ?, replied_at = ?, replied_by = ? WHERE id = ?", text, utc_stamp(), user["name"], msg_id)
    audit(user, "reply_message", msg["email"])
    return jsonify({"ok": True})


# ---------- blocklist and system ----------

BLOCK_IP = re.compile(r"^[0-9a-f.:]{3,45}$")


def blocklist():
    return [str(x).strip().lower() for x in (get_setting("security.blocklist", []) or []) if str(x).strip()]


def is_blocked(email=""):
    """True when the email, its domain (@example.com) or the visitor's IP address (or prefix like 203.0.113.) is on the blocklist."""
    email, ip = str(email or "").strip().lower(), (request.remote_addr or "").lower()
    for entry in blocklist():
        if entry.startswith("@"):
            if email.endswith(entry):
                return True
        elif "@" in entry:
            if email == entry:
                return True
        elif ip and (ip == entry or (entry[-1] in ".:" and ip.startswith(entry))):
            return True
    return False


def clean_blocklist(entries):
    out = []
    for raw in entries if isinstance(entries, list) else []:
        entry = str(raw or "").strip().lower()
        if not entry or entry in out:
            continue
        ok = (entry.startswith("@") and "." in entry and EMAIL_RE.match("x" + entry)) or EMAIL_RE.match(entry) or BLOCK_IP.match(entry)
        if not ok or len(entry) > 200:
            raise ValueError(f'"{entry[:60]}" is not an email, @domain or IP address')
        out.append(entry)
    if len(out) > 500:
        raise ValueError("At most 500 blocked entries")
    return out


@app.get("/api/admin/blocklist")
def admin_blocklist():
    if not manager_user():
        return err("Only the Owner or an Admin can see the blocklist", 403)
    return jsonify({"entries": blocklist()})


@app.post("/api/admin/blocklist")
def admin_save_blocklist():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change the blocklist", 403)
    try:
        entries = clean_blocklist(body().get("entries"))
    except ValueError as exc:
        return err(str(exc), 400)
    set_setting("security.blocklist", entries)
    audit(user, "save_blocklist", f"{len(entries)} entries")
    return jsonify({"ok": True, "entries": entries})


@app.get("/api/admin/system")
def admin_system():
    """What the site runs on, for the Owner and Admins. No secrets."""
    if not manager_user():
        return err("Only the Owner or an Admin can see this", 403)
    n = lambda sql: (db().first(sql) or {}).get("n") or 0  # noqa: E731
    tables = {}
    for table in BACKUP_TABLES + ("sessions", "downloads"):
        try:
            tables[table] = int(n(f"SELECT COUNT(*) AS n FROM {table}"))
        except Exception:  # noqa: BLE001 - a table that does not exist yet
            tables[table] = 0
    last_backup = db().first("SELECT MAX(created_at) AS at FROM audit_logs WHERE action = 'download_backup'")
    return jsonify({"database": "MySQL / MariaDB" if USE_MYSQL else ("PostgreSQL" if USE_PG else "SQLite"),
                    "schema": SCHEMA_VERSION, "python": platform.python_version(), "server_time": utc_stamp(),
                    "site_url": site_url(), "email_ready": bool(RESEND_API_KEY), "mail_from": MAIL_FROM,
                    "admin_password": bool(gate_hash()), "tables": tables,
                    "media_bytes": int(n("SELECT COALESCE(SUM(size), 0) AS n FROM media")),
                    "last_backup": str((last_backup or {}).get("at") or ""), "blocked": len(blocklist()),
                    "stripe": stripe_mode(), "stripe_webhook": bool(STRIPE_WEBHOOK_SECRET)})


@app.get("/api/admin/audit")
def admin_audit():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"logs": db().all(
        "SELECT audit_logs.*, users.name AS admin_name FROM audit_logs LEFT JOIN users ON audit_logs.admin_id = users.id "
        "ORDER BY audit_logs.id DESC LIMIT 100")})


# ---------- licences ----------

def key_checksum(prefix, body11):
    total = sum(ord(ch) for ch in prefix)
    for i, ch in enumerate(body11):
        total += (2 * i + 1) * KEY_ALPHABET.index(ch)
    return KEY_ALPHABET[total % 32]


def generate_key(prefix="HPRO"):
    body = "".join(secrets.choice(KEY_ALPHABET) for _ in range(11))
    full = body + key_checksum(prefix, body)
    return f"{prefix}-{full[0:4]}-{full[4:8]}-{full[8:12]}"


def normalize_key(value):
    """Upper-case, strip spaces, and return the key if its format and check character are right, else ''."""
    key = re.sub(r"\s+", "", str(value or "")).upper()
    m = KEY_RE.match(key)
    if not m:
        return ""
    chars = m.group(2) + m.group(3) + m.group(4)
    if any(ch not in KEY_ALPHABET for ch in chars) or key_checksum(m.group(1), chars[:11]) != chars[11]:
        return ""
    return key


def same_product(requested, stored):
    """Products match if their ids agree ignoring case and punctuation, or one id is the other plus
    extra words ("heavypro" matches "heavy-pro" and "heavypro-for-msfs-2024", not "heavypro2"),
    so a catalogue entry with a longer name does not lock customers out."""
    def prefixes(value):
        words = re.findall(r"[a-z0-9]+", str(value or "").lower())
        return {"".join(words[:i]) for i in range(1, len(words) + 1)}, "".join(words)
    a_all, a = prefixes(requested)
    b_all, b = prefixes(stored)
    return bool(a) and bool(b) and (a in b_all or b in a_all)


def license_tool_user():
    """Staff session (Owner/Admin) or the LICENSE_API_KEY used by the generator program."""
    supplied = request.headers.get("X-License-Api-Key", "")
    if LICENSE_API_KEY and len(LICENSE_API_KEY) >= 16 and supplied and hmac.compare_digest(supplied, LICENSE_API_KEY):
        return {"id": None, "name": "License generator", "role": "Tool"}
    return manager_user()


def insert_license(key, product_id, email="", customer_id=None, order_id=None, max_activations=2, note="", created_by=""):
    if db().first("SELECT id FROM licenses WHERE license_key = ?", key):
        return False
    db().run("INSERT INTO licenses (license_key, product_id, email, customer_id, order_id, max_activations, note, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
             key, product_id, (email or "").strip().lower(), customer_id, order_id, max_activations, note, created_by)
    return True


def clamp_int(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def license_email_html(keys, product_name, max_activations, bought=False):
    base = site_url()
    rows = "".join(f'<tr><td style="padding:14px 18px;background:#f4f4f5;border:1px solid #e4e4e7;font:bold 20px/1.2 Consolas,Menlo,monospace;letter-spacing:2px;color:#111315;text-align:center">{escape(k)}</td></tr><tr><td style="height:8px"></td></tr>' for k in keys)
    plural = len(keys) > 1
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#3a3c40">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff">
<tr><td style="background:#111315;padding:20px 28px;color:#ffffff;font-weight:bold;letter-spacing:3px;font-size:15px">ROTAIDEM</td></tr>
<tr><td style="padding:28px 28px 6px;font-size:16px;line-height:1.6">
<h1 style="margin:0 0 12px;font-size:22px;color:#111315">Your {escape(product_name)} licence key{"s" if plural else ""}</h1>
<p style="margin:0 0 20px">Thank you for choosing {escape(product_name)}. Here {"are your keys" if plural else "is your key"}:</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{rows}</table></td></tr>
<tr><td style="padding:12px 28px 8px;font-size:15px;line-height:1.6">
<h2 style="margin:0 0 8px;font-size:17px;color:#111315">How to activate</h2>
<ol style="margin:0 0 16px;padding-left:20px">
<li>Download the installer from <a href="{escape(base)}/downloads" style="color:#111315">{escape(base.split("://")[-1])}/downloads</a>.</li>
<li>Run it and enter your key when asked.</li>
<li>Each key works on {max_activations} computer{"s" if max_activations != 1 else ""}. To move to a new PC, deactivate it in {escape(product_name)} first.</li>
</ol></td></tr>
<tr><td style="padding:4px 28px 28px"><a href="{escape(base)}/account" style="display:inline-block;background:#111315;color:#ffffff;text-decoration:none;padding:13px 24px;font-weight:bold">View my keys</a></td></tr>
{bought_note(bought)}<tr><td style="padding:20px 28px;border-top:1px solid #e4e4e7;font-size:12px;color:#6d6f75">Keep this email somewhere safe. Questions? Email <a href="mailto:Eliaz@rotaidem.com" style="color:#6d6f75">Eliaz@rotaidem.com</a>.</td></tr>
</table></td></tr></table></body></html>"""


def bought_note(bought):
    """The confirmation EU law asks for after selling digital content: what was agreed at checkout."""
    if not bought:
        return ""
    base = site_url()
    return (f'<tr><td style="padding:0 28px 20px;font-size:13px;line-height:1.55;color:#6d6f75">At checkout you asked to get your licence right away and agreed that '
            f'your 14-day right of withdrawal ends once you activate it. Before you activate, you can still cancel for a full refund. '
            f'If the product does not work as described, you always keep your rights: see our <a href="{escape(base)}/refunds" style="color:#6d6f75">refund policy</a> '
            f'and <a href="{escape(base)}/terms" style="color:#6d6f75">terms</a>.</td></tr>')


def email_license_keys(email, keys, product_id, bought=False):
    """Email licence keys to a customer. Returns an error message, or '' when sent."""
    if not keys or not email:
        return ""
    if not RESEND_API_KEY:
        return "Email is not set up (RESEND_API_KEY is missing), so the key was not emailed."
    product = db().first("SELECT name FROM products WHERE id = ?", product_id)
    name = product["name"] if product else ("HeavyPro" if product_id == "heavypro" else product_id)
    lic = db().first("SELECT max_activations FROM licenses WHERE license_key = ?", keys[0])
    subject = f"Your {name} licence key" + ("s" if len(keys) > 1 else "")
    sent, error = send_batch([{"from": MAIL_FROM, "to": [email], "subject": subject,
                               "html": license_email_html(keys, name, lic["max_activations"] if lic else 2, bought)}])
    return "" if sent else error


def email_reply(email, error):
    return {"emailed": [email] if email and not error else [], "email_error": error}


@app.post("/api/licenses/import")
def licenses_import():
    """Used by the License Generator program to register keys it created."""
    user = license_tool_user()
    if not user:
        return err("Not allowed. Check the generator's API key.", 403)
    items = body().get("licenses") or []
    if not isinstance(items, list) or len(items) > 1000:
        return err("Send a list of at most 1000 licences", 400)
    created, skipped, invalid = [], [], []
    by_email = {}
    for item in items:
        key = normalize_key(item.get("key"))
        if not key:
            invalid.append(str(item.get("key", ""))[:40])
            continue
        email = str(item.get("email") or "").strip().lower()
        customer = db().first("SELECT id FROM users WHERE LOWER(email) = ?", email) if email else None
        ok = insert_license(key, str(item.get("product_id") or "heavypro")[:60], email, customer["id"] if customer else None, None,
                            clamp_int(item.get("max_activations"), 2, 1, 100), str(item.get("note") or "")[:200], user["name"])
        (created if ok else skipped).append(key)
        if ok and email and EMAIL_RE.match(email):
            by_email.setdefault((email, str(item.get("product_id") or "heavypro")[:60]), []).append(key)
    if created:
        audit(user, "import_licenses", f"{len(created)} keys")
    emailed, errors = [], []
    if body().get("send_email"):
        for (email, product_id), keys in by_email.items():
            error = email_license_keys(email, keys, product_id)
            (errors.append(f"{email}: {error}") if error else emailed.append(email))
        if emailed:
            audit(user, "email_licenses", ", ".join(emailed))
    return jsonify({"ok": True, "created": created, "skipped": skipped, "invalid": invalid, "emailed": emailed, "email_error": "; ".join(errors)})


@app.post("/api/admin/licenses/generate")
def admin_generate_licenses():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can create licences", 403)
    b = body()
    count = clamp_int(b.get("count"), 1, 1, 500)
    prefix = re.sub(r"[^A-Z0-9]", "", str(b.get("prefix") or "HPRO").upper())[:8] or "HPRO"
    email = str(b.get("email") or "").strip().lower()
    if email and not EMAIL_RE.match(email):
        return err("Enter a valid email address or leave it empty", 400)
    customer = db().first("SELECT id FROM users WHERE LOWER(email) = ?", email) if email else None
    keys = []
    while len(keys) < count:
        key = generate_key(prefix)
        if insert_license(key, str(b.get("product_id") or "heavypro")[:60], email, customer["id"] if customer else None, None,
                          clamp_int(b.get("max_activations"), 2, 1, 100), str(b.get("note") or "")[:200], user["name"]):
            keys.append(key)
    audit(user, "create_licenses", f"{count} x {prefix}" + (f" for {email}" if email else ""))
    if email:
        discord_sync_owners([(customer["id"] if customer else None, email)])
    error = email_license_keys(email, keys, str(b.get("product_id") or "heavypro")[:60]) if email and b.get("send_email", True) else ""
    if email and b.get("send_email", True) and not error:
        audit(user, "email_licenses", email)
    return jsonify({"ok": True, "keys": keys, **email_reply(email if b.get("send_email", True) else "", error)})


@app.get("/api/admin/licenses")
def admin_licenses():
    if not staff_user():
        return err("Admin only", 403)
    rows = db().all("SELECT licenses.*, (SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS activations "
                    "FROM licenses ORDER BY licenses.id DESC LIMIT 2000")
    return jsonify({"licenses": rows})


@app.get("/api/admin/licenses/<int:license_id>")
def admin_license_detail(license_id):
    if not staff_user():
        return err("Admin only", 403)
    lic = db().first("SELECT * FROM licenses WHERE id = ?", license_id)
    if not lic:
        return err("Licence not found", 404)
    lic["machines"] = db().all("SELECT * FROM license_activations WHERE license_id = ? ORDER BY id", license_id)
    return jsonify({"license": lic})


@app.post("/api/admin/licenses/<int:license_id>")
def admin_update_license(license_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change licences", 403)
    lic = db().first("SELECT * FROM licenses WHERE id = ?", license_id)
    if not lic:
        return err("Licence not found", 404)
    b = body()
    status = b.get("status", lic["status"])
    if status not in ("active", "revoked"):
        return err("Status must be active or revoked", 400)
    email = str(b.get("email", lic["email"]) or "").strip().lower()
    customer = db().first("SELECT id FROM users WHERE LOWER(email) = ?", email) if email else None
    product_id = str(b.get("product_id") or lic["product_id"] or "heavypro").strip()[:60]
    expires = str(b.get("expires_at", lic.get("expires_at")) or "").strip()
    if expires and not license_expiry({"expires_at": expires}):
        return err("expires_at must be a date like 2027-12-31 (or empty for a licence that never ends)", 400)
    expires = license_expiry({"expires_at": expires}).strftime("%Y-%m-%d %H:%M:%S") if expires else None
    db().run("UPDATE licenses SET status = ?, email = ?, customer_id = ?, max_activations = ?, note = ?, product_id = ?, expires_at = ? WHERE id = ?",
             status, email, customer["id"] if customer else None, clamp_int(b.get("max_activations"), lic["max_activations"], 1, 100),
             str(b.get("note", lic["note"]) or "")[:200], product_id, expires, license_id)
    audit(user, "update_license", f"{lic['license_key']} ({status})")
    discord_sync_owners([(lic["customer_id"], lic["email"]), (customer["id"] if customer else None, email)])
    return jsonify({"ok": True})


@app.post("/api/admin/licenses/bulk")
def admin_bulk_licenses():
    """Revoke or restore many licences at once."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change licences", 403)
    b = body()
    status = {"revoke": "revoked", "activate": "active"}.get(b.get("action"))
    if not status:
        return err("Choose revoke or activate", 400)
    ids = sorted({int(i) for i in (b.get("ids") or []) if str(i).isdigit()})[:1000]
    if not ids:
        return err("Select at least one licence", 400)
    changed, owners = 0, []
    for license_id in ids:
        lic = db().first("SELECT id, status, customer_id, email FROM licenses WHERE id = ?", license_id)
        if lic and lic["status"] != status:
            db().run("UPDATE licenses SET status = ? WHERE id = ?", status, license_id)
            changed += 1
            owners.append((lic["customer_id"], lic["email"]))
    audit(user, "bulk_licenses", f"{changed} {status}")
    discord_sync_owners(owners)
    return jsonify({"ok": True, "changed": changed})


@app.post("/api/admin/licenses/<int:license_id>/email")
def admin_email_license(license_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can email licences", 403)
    lic = db().first("SELECT * FROM licenses WHERE id = ?", license_id)
    if not lic:
        return err("Licence not found", 404)
    email = str(body().get("email") or lic["email"] or "").strip().lower()
    if not EMAIL_RE.match(email):
        return err("This licence has no valid customer email. Add one first.", 400)
    error = email_license_keys(email, [lic["license_key"]], lic["product_id"])
    if error:
        return err(error, 502)
    audit(user, "email_licenses", f"{lic['license_key']} to {email}")
    return jsonify({"ok": True, "emailed": [email]})


@app.delete("/api/admin/licenses/<int:license_id>/activations/<int:activation_id>")
def admin_reset_activation(license_id, activation_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change licences", 403)
    db().run("DELETE FROM license_activations WHERE id = ? AND license_id = ?", activation_id, license_id)
    audit(user, "reset_activation", license_id)
    return jsonify({"ok": True})


# ---------- signed licence answers ----------
# Every valid activate/validate answer carries a token (an RS256 JSON Web Token) signed with this
# server's private key. HeavyPro checks it with the public key built into it, so a fake licence
# server, an edited answer or an activation copied to another PC is refused. The token also repeats
# the random "nonce" the app sent (no replays) and runs out after LICENSE_LEASE_DAYS, after which
# the app must reach this server again. The key pair is made once and kept in the database (meta
# "license_signing_key"), so it moves with backups. Same scheme as KeyForge.

LICENSE_LEASE_DAYS = 14
LICENSE_FAILURES_PER_HOUR = 30
# A computer that has not checked in for this long gives its slot to a new one when the key is at its
# limit (Windows reinstalled, PC replaced). Longer than any lease, so that computer has already stopped.
LICENSE_STALE_DAYS = 30
# New computers a key may activate per 30 days beyond its own limit. Stops one key serving many PCs by
# activating and deactivating in turn (each keeps a signed lease for LICENSE_LEASE_DAYS).
LICENSE_EXTRA_ACTIVATIONS_PER_30_DAYS = 3
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text):
    return base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))


def _probably_prime(n, rounds=40):
    for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def generate_signing_key(bits=2048):
    def prime(size):
        while True:
            c = secrets.randbits(size) | (3 << (size - 2)) | 1
            if c % 65537 != 1 and _probably_prime(c):
                return c
    while True:
        p, q = prime(bits // 2), prime(bits // 2)
        if p != q and (p * q).bit_length() == bits:
            return {"n": p * q, "e": 65537, "d": pow(65537, -1, (p - 1) * (q - 1)), "p": p, "q": q}


def _pkcs1(message, length):
    digest = SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    return b"\x00\x01" + b"\xff" * (length - len(digest) - 3) + b"\x00" + digest


def rsa_sign(key, message):
    length = (key["n"].bit_length() + 7) // 8
    m = int.from_bytes(_pkcs1(message, length), "big")
    p, q, d = key["p"], key["q"], key["d"]
    m1, m2 = pow(m, d % (p - 1), p), pow(m, d % (q - 1), q)
    s = m2 + ((pow(q, -1, p) * (m1 - m2)) % p) * q
    if pow(s, key["e"], key["n"]) != m:
        raise ValueError("signature check failed")
    return s.to_bytes(length, "big")


def rsa_verify(key, message, signature):
    length = (key["n"].bit_length() + 7) // 8
    if len(signature) != length or int.from_bytes(signature, "big") >= key["n"]:
        return False
    return hmac.compare_digest(pow(int.from_bytes(signature, "big"), key["e"], key["n"]).to_bytes(length, "big"), _pkcs1(message, length))


def signing_key_id(key):
    return hashlib.sha256(key["n"].to_bytes((key["n"].bit_length() + 7) // 8, "big")).hexdigest()[:16]


def signing_jwk(key):
    return {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": signing_key_id(key),
            "n": b64url(key["n"].to_bytes((key["n"].bit_length() + 7) // 8, "big")), "e": b64url((65537).to_bytes(3, "big"))}


def signing_pem(key):
    def der(tag, body):
        n = len(body)
        size = bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
        return bytes([tag]) + size + body

    def der_int(v):
        raw = v.to_bytes((v.bit_length() + 7) // 8, "big")
        return der(0x02, b"\x00" + raw if raw[0] & 0x80 else raw)
    rsa_key = der(0x30, der_int(key["n"]) + der_int(key["e"]))
    spki = der(0x30, der(0x30, der(0x06, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00") + der(0x03, b"\x00" + rsa_key))
    text = base64.b64encode(spki).decode()
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(text[i:i + 64] for i in range(0, len(text), 64)) + "\n-----END PUBLIC KEY-----\n"


_SIGNING_KEY = {}


def signing_key():
    """The RSA key for licence tokens: made on first use (normally by setup.py deploy), kept in the database."""
    if "key" in _SIGNING_KEY:
        return _SIGNING_KEY["key"]
    d = DB()
    try:
        row = d.first("SELECT value FROM meta WHERE key = 'license_signing_key'")
        if not row:
            made = json.dumps({k: format(v, "x") for k, v in generate_signing_key().items()})
            try:
                d.run("INSERT INTO meta (key, value) VALUES ('license_signing_key', ?)", made)
            except Exception:  # noqa: BLE001 - made at the same moment by another request: use that one
                pass
            row = d.first("SELECT value FROM meta WHERE key = 'license_signing_key'")
    finally:
        d.close()
    _SIGNING_KEY["key"] = {k: int(v, 16) for k, v in json.loads(row["value"]).items()}
    return _SIGNING_KEY["key"]


def make_license_token(claims):
    key = signing_key()
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": signing_key_id(key)}, separators=(",", ":")).encode())
    data = head + "." + b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    return data + "." + b64url(rsa_sign(key, data.encode()))


def read_license_token(token):
    """The claims of a token this server signed, or None (signature only)."""
    try:
        head, claims, sig = str(token).split(".")
        if not rsa_verify(signing_key(), f"{head}.{claims}".encode(), b64url_decode(sig)):
            return None
        return json.loads(b64url_decode(claims))
    except (ValueError, TypeError):
        return None


def lease_days():
    try:
        return max(1, min(365, int(os.environ.get("LICENSE_LEASE_DAYS") or LICENSE_LEASE_DAYS)))
    except ValueError:
        return LICENSE_LEASE_DAYS


def stale_days():
    try:
        return max(lease_days() + 1, int(os.environ.get("LICENSE_STALE_DAYS") or LICENSE_STALE_DAYS))
    except ValueError:
        return max(lease_days() + 1, LICENSE_STALE_DAYS)


def license_expiry(lic):
    """The licence's end as an aware datetime, or None when it never ends."""
    text = str(lic.get("expires_at") or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def license_failed(reply, status):
    db().run("DELETE FROM license_failures WHERE created_at < ?", utc_stamp(86400))
    db().run("INSERT INTO license_failures (ip_address, created_at) VALUES (?, ?)", request.remote_addr or "",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    return reply, status


def too_many_license_failures():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    row = db().first("SELECT COUNT(*) AS n FROM license_failures WHERE ip_address = ? AND created_at >= ?", request.remote_addr or "", cutoff)
    return (row or {}).get("n", 0) >= LICENSE_FAILURES_PER_HOUR


@app.get("/api/licenses/jwks.json")
def license_jwks():
    resp = jsonify({"keys": [signing_jwk(signing_key())]})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.get("/api/licenses/public-key")
def license_public_key():
    return app.response_class(signing_pem(signing_key()), mimetype="application/x-pem-file")


def license_for_request():
    """Shared checks for the public activation endpoints. Returns (licence, machine_id, error_response)."""
    b = body()
    if too_many_license_failures():
        return None, "", (jsonify({"valid": False, "reason": "rate_limited",
                                   "error": "Too many wrong licence keys from this address. Try again in an hour."}), 429)
    key = normalize_key(b.get("key"))
    if not key:
        return None, "", license_failed(jsonify({"valid": False, "reason": "invalid_format", "error": "That is not a valid licence key."}), 400)
    lic = db().first("SELECT * FROM licenses WHERE license_key = ?", key)
    if not lic:
        return None, "", license_failed(jsonify({"valid": False, "reason": "not_found", "error": "This licence key does not exist."}), 404)
    if lic["status"] != "active":
        return None, "", (jsonify({"valid": False, "reason": "revoked", "error": "This licence key has been revoked."}), 403)
    ends = license_expiry(lic)
    if ends and datetime.now(timezone.utc) >= ends:
        return None, "", (jsonify({"valid": False, "reason": "expired", "expires_at": ends.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "error": f"This licence ended on {ends.strftime('%Y-%m-%d')}. Renew it at rotaidem.com."}), 403)
    product = str(b.get("product") or "").strip()
    if product and not same_product(product, lic["product_id"]):
        return None, "", (jsonify({"valid": False, "reason": "wrong_product", "error": "This licence key is for another product."}), 403)
    machine = str(b.get("machine_id") or "").strip()[:128]
    if not machine:
        return None, "", (jsonify({"valid": False, "reason": "missing_machine", "error": "machine_id is required."}), 400)
    return lic, machine, None


def license_reply(lic, used, machine):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    until = now + timedelta(days=lease_days())
    ends = license_expiry(lic)
    if ends and ends < until:
        until = ends
    claims = {"iss": site_url(), "sub": f"license:{lic['id']}", "aud": lic["product_id"], "key": lic["license_key"], "mid": machine,
              "iat": int(now.timestamp()), "exp": int(until.timestamp()), "activations_used": used, "max_activations": lic["max_activations"]}
    nonce = str(body().get("nonce") or "")[:128]
    if nonce:
        claims["nonce"] = nonce
    return jsonify({"valid": True, "product_id": lic["product_id"], "activations_used": used, "max_activations": lic["max_activations"],
                    "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "lease_until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "token": make_license_token(claims)})


@app.post("/api/licenses/activate")
def license_activate():
    lic, machine, error = license_for_request()
    if error:
        return error
    name = str(body().get("machine_name") or "")[:100]
    existing = db().first("SELECT id FROM license_activations WHERE license_id = ? AND machine_id = ?", lic["id"], machine)
    if existing:
        db().run("UPDATE license_activations SET last_seen = CURRENT_TIMESTAMP, machine_name = ? WHERE id = ?", name, existing["id"])
        return license_reply(lic, activations_used(lic), machine)

    limit = lic["max_activations"]
    recent = (db().first("SELECT COUNT(*) AS n FROM license_activation_log WHERE license_id = ? AND created_at >= ?",
                         lic["id"], utc_stamp(30 * 86400)) or {}).get("n", 0)
    if recent >= limit + LICENSE_EXTRA_ACTIVATIONS_PER_30_DAYS:
        return jsonify({"valid": False, "reason": "activation_churn", "activations_used": activations_used(lic), "max_activations": limit,
                        "error": "This key has been moved to new computers too often this month. Contact support@rotaidem.com "
                                 "and we will sort it out."}), 429
    if activations_used(lic) >= limit:
        # At the limit: a computer that has not checked in for LICENSE_STALE_DAYS (Windows reinstalled,
        # PC replaced) gives up its slot. Its lease ran out long ago, so it is no longer running.
        stale = db().first("SELECT id, machine_name FROM license_activations WHERE license_id = ? AND last_seen < ? "
                           "ORDER BY last_seen LIMIT 1", lic["id"], utc_stamp(stale_days() * 86400))
        if not stale:
            used = activations_used(lic)
            return jsonify({"valid": False, "reason": "activation_limit", "activations_used": used, "max_activations": limit,
                            "error": f"This key is already active on {used} computer(s). In HeavyPro on the computer you no longer use, "
                                     "choose Deactivate this PC, or contact support@rotaidem.com."}), 403
        db().run("DELETE FROM license_activations WHERE id = ?", stale["id"])
        db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'license_stale_slot_reused', ?, ?, ?)",
                 None, f"{lic['license_key']} {stale['machine_name'] or ''}"[:200], request.remote_addr or "", utc_stamp())
    try:
        db().run("INSERT INTO license_activations (license_id, machine_id, machine_name) VALUES (?, ?, ?)", lic["id"], machine, name)
    except Exception:  # noqa: BLE001 - the same computer activated at the same moment: that one counts
        return license_reply(lic, activations_used(lic), machine)
    if activations_used(lic) > limit:
        # Two new computers activated at the same moment: the later one gives its slot back.
        db().run("DELETE FROM license_activations WHERE license_id = ? AND machine_id = ?", lic["id"], machine)
        used = activations_used(lic)
        return jsonify({"valid": False, "reason": "activation_limit", "activations_used": used, "max_activations": limit,
                        "error": f"This key is already active on {used} computer(s)."}), 403
    db().run("INSERT INTO license_activation_log (license_id, machine_id, created_at) VALUES (?, ?, ?)", lic["id"], machine, utc_stamp())
    return license_reply(lic, activations_used(lic), machine)


def activations_used(lic):
    return (db().first("SELECT COUNT(*) AS n FROM license_activations WHERE license_id = ?", lic["id"]) or {}).get("n", 0)


@app.post("/api/licenses/validate")
def license_validate():
    lic, machine, error = license_for_request()
    if error:
        return error
    existing = db().first("SELECT id FROM license_activations WHERE license_id = ? AND machine_id = ?", lic["id"], machine)
    if not existing:
        return jsonify({"valid": False, "reason": "not_activated", "error": "This computer has not been activated with this key."}), 403
    db().run("UPDATE license_activations SET last_seen = CURRENT_TIMESTAMP WHERE id = ?", existing["id"])
    return license_reply(lic, activations_used(lic), machine)


@app.post("/api/licenses/deactivate")
def license_deactivate():
    lic, machine, error = license_for_request()
    if error:
        return error
    db().run("DELETE FROM license_activations WHERE license_id = ? AND machine_id = ?", lic["id"], machine)
    return jsonify({"ok": True})


# ---------- shared feel profiles (HeavyPro) ----------
# HeavyPro owners can publish a feel profile under a name and anyone else with the program can fetch
# it by that name. Both need a licence key activated on the calling computer. A name belongs to the
# licence that published it first; only that licence can update or delete it.

PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{1,39}$")
PROFILE_MAX_BYTES = 4000


def profile_name():
    return " ".join(str(body().get("name") or "").split())


def profile_key(name):
    return name.lower()


def licensed_computer():
    lic, machine, error = license_for_request()
    if error:
        return None, error
    if not db().first("SELECT id FROM license_activations WHERE license_id = ? AND machine_id = ?", lic["id"], machine):
        return None, (jsonify({"ok": False, "reason": "not_activated", "error": "This computer has not been activated with this key."}), 403)
    return lic, None


@app.post("/api/profiles/publish")
def profile_publish():
    lic, error = licensed_computer()
    if error:
        return error
    name = profile_name()
    if not PROFILE_NAME.match(name):
        return jsonify({"ok": False, "reason": "invalid_name",
                        "error": "Use 2 to 40 letters, digits, spaces, dots, dashes or underscores for the name."}), 400
    data = body().get("profile")
    if not isinstance(data, dict):
        return jsonify({"ok": False, "reason": "invalid_profile", "error": "The profile is missing."}), 400
    text = json.dumps(data, separators=(",", ":"))
    if len(text) > PROFILE_MAX_BYTES:
        return jsonify({"ok": False, "reason": "too_large", "error": "That profile is too large to share."}), 400
    existing = db().first("SELECT id, license_id FROM shared_profiles WHERE name_key = ?", profile_key(name))
    if existing and existing["license_id"] != lic["id"]:
        return jsonify({"ok": False, "reason": "name_taken",
                        "error": f'Someone else already shares a profile called "{name}". Pick another name.'}), 409
    if existing:
        db().run("UPDATE shared_profiles SET name = ?, data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", name, text, existing["id"])
    else:
        db().run("INSERT INTO shared_profiles (name_key, name, data, license_id) VALUES (?, ?, ?, ?)",
                 profile_key(name), name, text, lic["id"])
    return jsonify({"ok": True, "name": name, "updated": bool(existing)})


@app.post("/api/profiles/get")
def profile_get():
    _lic, error = licensed_computer()
    if error:
        return error
    name = profile_name()
    row = db().first("SELECT id, name, data FROM shared_profiles WHERE name_key = ?", profile_key(name))
    if not row:
        return jsonify({"ok": False, "reason": "not_found", "error": f'No shared profile is called "{name}". Check the spelling.'}), 404
    db().run("UPDATE shared_profiles SET downloads = downloads + 1 WHERE id = ?", row["id"])
    return jsonify({"ok": True, "name": row["name"], "profile": json.loads(row["data"])})


@app.post("/api/profiles/list")
def profile_list():
    """Community list: every shared profile, most downloaded first. Optional "search" filters by name."""
    _lic, error = licensed_computer()
    if error:
        return error
    search = " ".join(str(body().get("search") or "").split()).lower()[:40]
    rows = db().all("SELECT name, downloads, updated_at FROM shared_profiles ORDER BY downloads DESC, name_key")
    if search:
        rows = [r for r in rows if search in str(r["name"]).lower()]
    return jsonify({"ok": True, "profiles": [{"name": r["name"], "downloads": int(r["downloads"] or 0),
                                               "updated_at": str(r["updated_at"] or "")} for r in rows[:500]]})


@app.post("/api/profiles/delete")
def profile_delete():
    lic, error = licensed_computer()
    if error:
        return error
    name = profile_name()
    row = db().first("SELECT id, license_id FROM shared_profiles WHERE name_key = ?", profile_key(name))
    if not row:
        return jsonify({"ok": False, "reason": "not_found", "error": f'No shared profile is called "{name}".'}), 404
    if row["license_id"] != lic["id"]:
        return jsonify({"ok": False, "reason": "not_owner", "error": "Only whoever shared this profile can remove it."}), 403
    db().run("DELETE FROM shared_profiles WHERE id = ?", row["id"])
    return jsonify({"ok": True})


@app.get("/api/account/licenses")
def account_licenses():
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    rows = db().all("SELECT licenses.id, license_key, product_id, status, max_activations, licenses.created_at, expires_at, "
                    "(SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS activations "
                    "FROM licenses WHERE customer_id = ? OR LOWER(email) = ? ORDER BY licenses.id DESC", user["id"], (user["email"] or "").lower())
    return jsonify({"licenses": rows})


@app.post("/api/account/licenses/claim")
def account_claim_license():
    """Adds a key the customer has (bought with another address, a gift) to their account, so it
    shows here and in RotaHub on every PC. A key already on another account stays there."""
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    if too_many_license_failures():
        return err("Too many wrong licence keys from this address. Try again in an hour.", 429)
    key = normalize_key(body().get("key"))
    if not key:
        return license_failed(jsonify({"error": "That is not a valid licence key."}), 400)
    lic = db().first("SELECT * FROM licenses WHERE license_key = ?", key)
    if not lic:
        return license_failed(jsonify({"error": "This licence key does not exist."}), 404)
    if lic["status"] != "active":
        return err("This licence key has been revoked.", 403)
    if lic["customer_id"] and int(lic["customer_id"]) != int(user["id"]):
        return err("This key is on another Rotaidem account. It can still be activated on this PC.", 409)
    if not lic["customer_id"]:
        db().run("UPDATE licenses SET customer_id = ? WHERE id = ?", user["id"], lic["id"])
        db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'license_claimed', ?, ?, ?)",
                 user["id"], key, request.remote_addr or "", utc_stamp())
        discord_sync_owners([(user["id"], user["email"])])
    row = db().first("SELECT licenses.id, license_key, product_id, status, max_activations, licenses.created_at, "
                     "(SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS activations "
                     "FROM licenses WHERE id = ?", lic["id"])
    return jsonify({"license": row})


# ---------- payments: Stripe Checkout ----------
# Buy -> /api/checkout/<id> makes a Checkout session -> the customer pays on Stripe's page ->
# /checkout-done asks /api/checkout/status (and Stripe calls /api/stripe/webhook): one order and
# one licence key, emailed. The price comes from the product (Admin -> Products -> Price at checkout).

PAY_CURRENCIES = ("sek", "eur", "usd", "gbp", "nok", "dkk")
CHECKOUT_SESSION = re.compile(r"^cs_[A-Za-z0-9_]{10,250}$")
REFERRAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")  # window.endorsely_referral from the buyer's browser
AFFILIATE_DEFAULT_PERCENT = 20  # a new affiliate earns this share of each referred sale (Admin -> Affiliates)


def stripe_mode():
    return "" if not STRIPE_SECRET_KEY else ("test" if "_test_" in STRIPE_SECRET_KEY else "live")


def stripe_call(method, path, fields=None, idempotency_key=""):
    """Stripe's API with the secret key from config.env. Returns (answer, error text).
    Pass idempotency_key on money-moving POSTs so a retry cannot pay twice."""
    data = urllib.parse.urlencode(fields or {}).encode() if method == "POST" else None
    headers = {"Authorization": f"Bearer {STRIPE_SECRET_KEY}", "Content-Type": "application/x-www-form-urlencoded",
               "User-Agent": "Rotaidem-Website/1.0"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(f"{STRIPE_API_URL}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()), ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            message = json.loads(raw)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = raw[:200]
        app.logger.error("Stripe %s %s -> %s: %s", method, path, exc.code, message)
        return None, f"Stripe said ({exc.code}): {message}"
    except (OSError, ValueError) as exc:
        app.logger.error("Stripe %s %s: %s", method, path, exc)
        return None, "Could not reach Stripe. Try again in a minute."


def key_prefix_for(product_id):
    """The prefix of a product's licence keys: Admin -> RotaHub -> Install settings, HPRO for HeavyPro, else from the id."""
    settings = next((p for p in hub_config()["products"] if p["id"] == product_id), None)
    if settings and settings.get("key_prefix"):
        return settings["key_prefix"]
    if product_id in ("heavypro", "rotaidem-heavypro"):
        return "HPRO"
    return (re.sub(r"[^A-Z0-9]", "", product_id.upper()) or "ROTA")[:4].ljust(2, "X")


def fulfil_checkout(session):
    """A paid Checkout session -> one order and one licence key, emailed. Safe to call twice
    (from the success page and the webhook): the stripe_payments row is claimed first."""
    if session.get("payment_status") != "paid" or session.get("mode") != "payment" or not CHECKOUT_SESSION.match(str(session.get("id") or "")):
        return None
    sid = session["id"]
    done = db().first("SELECT * FROM stripe_payments WHERE session_id = ?", sid)
    if done:
        return done
    meta = session.get("metadata") or {}
    product_id = str(meta.get("product_id") or "")
    try:
        user_id = int(meta.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    user = db().first("SELECT * FROM users WHERE id = ?", user_id) if user_id else None
    email = ((user or {}).get("email") or (session.get("customer_details") or {}).get("email") or session.get("customer_email") or "").lower()
    try:
        db().run("INSERT INTO stripe_payments (session_id, product_id, user_id, email, created_at) VALUES (?, ?, ?, ?, ?)",
                 sid, product_id, user_id or None, email, utc_stamp())
    except Exception:  # noqa: BLE001 - the other request (page or webhook) claimed it a moment ago
        return db().first("SELECT * FROM stripe_payments WHERE session_id = ?", sid)
    amount = (session.get("amount_total") or 0) / 100
    currency = str(session.get("currency") or "").lower()
    affiliate_code = str(meta.get("affiliate_code") or "")
    consent = str(meta.get("consent") or "")[:300]
    db().run("INSERT INTO orders (customer_id, product_id, status, payment_status, amount, payment_ref, currency, affiliate_code, consent) VALUES (?, ?, 'Completed', 'Paid', ?, ?, ?, ?, ?)",
             user_id or None, product_id, amount, sid, currency, affiliate_code, consent)
    order = db().first("SELECT id FROM orders WHERE payment_ref = ?", sid)
    referred_by = credit_affiliate(order["id"], amount, currency, affiliate_code, user_id, email) if order else None
    if referred_by:
        affiliate_autopay(referred_by)  # pay their cut straight away if automatic payouts are on and they are set up
    key = ""
    while not key:
        candidate = generate_key(key_prefix_for(product_id))
        if insert_license(candidate, product_id, email, user_id or None, order["id"] if order else None, 2, "Bought on rotaidem.com (Stripe)", "Stripe"):
            key = candidate
    db().run("UPDATE stripe_payments SET order_id = ?, license_key = ? WHERE session_id = ?", order["id"] if order else None, key, sid)
    discord_sync_owners([(user_id or None, email)])
    error = email_license_keys(email, [key], product_id, bought=True)
    if error:
        app.logger.error("Licence e-mail after payment %s failed: %s", sid, error)
    if user:
        audit(user, "stripe_order", f"{product_id} {amount:.2f} {str(session.get('currency') or '').upper()}")
    product = db().first("SELECT name FROM products WHERE id = ?", product_id) or {}
    discord_buyer_thanks(db().first("SELECT * FROM users WHERE id = ?", user_id) if user_id else None, product.get("name") or product_id)
    today_count = db().first("SELECT COUNT(*) AS n FROM stripe_payments WHERE created_at >= ?", utc_stamp()[:10] + " 00:00:00") or {}
    discord_auto("sales", {"text": f"🎉 New sale: **{product.get('name') or product_id}** for {amount:.2f} {str(session.get('currency') or '').upper()}"
                                   f" ({today_count.get('n') or 1} today)" + (f" · referred by {referred_by['code']}" if referred_by else "")})
    return db().first("SELECT * FROM stripe_payments WHERE session_id = ?", sid)


@app.post("/api/checkout/<pid>")
def checkout(pid):
    user = current_user()
    if not user:
        return jsonify({"error": "Log in to buy. Your licence key is linked to your account.", "login": True}), 401
    product = db().first("SELECT * FROM products WHERE id = ?", pid)
    if not product or not (product.get("pay_amount") or 0) > 0 or product.get("status") != "published":
        return err("This product cannot be bought on the website yet", 404)
    if not STRIPE_SECRET_KEY:
        return err("Payments are not set up yet. Please try again later or contact us.", 503)
    owned = db().all("SELECT product_id FROM licenses WHERE status = 'active' AND (customer_id = ? OR LOWER(email) = ?)", user["id"], (user["email"] or "").lower())
    if any(same_product(pid, r["product_id"]) for r in owned):
        return jsonify({"error": f"You already have a licence for {product['name']}. It is under My account.", "owned": True}), 409
    if not body().get("withdrawal_consent"):
        return jsonify({"error": "Please tick the box: you get your licence key right away, so the 14-day right of withdrawal ends once you activate it.",
                        "consent_needed": True}), 400
    base = site_url()
    fields = {"mode": "payment", "line_items[0][quantity]": "1",
              "line_items[0][price_data][currency]": product.get("pay_currency") or "sek",
              "line_items[0][price_data][unit_amount]": str(price_now(product)),
              "line_items[0][price_data][product_data][name]": product["name"] or pid,
              "customer_email": user["email"], "client_reference_id": str(user["id"]),
              "metadata[product_id]": pid, "metadata[user_id]": str(user["id"]),
              "metadata[consent]": f"{utc_stamp()} UTC: immediate delivery, withdrawal right ends on activation ({TERMS_VERSION})",
              "payment_intent_data[metadata][product_id]": pid, "payment_intent_data[metadata][user_id]": str(user["id"]),
              "success_url": f"{base}/checkout-done?session_id={{CHECKOUT_SESSION_ID}}",
              "cancel_url": f"{base}/addons/view?id={urllib.parse.quote(pid)}&cancelled=1"}
    if product.get("tagline"):
        fields["line_items[0][price_data][product_data][description]"] = product["tagline"][:300]
    if shop_settings()["promo_codes"]:
        fields["allow_promotion_codes"] = "true"  # codes made in the Stripe dashboard
    referral = str(body().get("referral") or "").strip()
    if REFERRAL_ID.match(referral):
        fields["metadata[endorsely_referral]"] = referral  # Endorsely (affiliates) credits the sale from this
    if affiliate_by_code(body().get("ref")):  # our own affiliate program
        fields["metadata[affiliate_code]"] = str(body().get("ref")).strip()
    session, error = stripe_call("POST", "/v1/checkout/sessions", fields)
    if error or not (session or {}).get("url"):
        return err(error or "Stripe did not open a checkout page", 502)
    return jsonify({"url": session["url"]})


@app.get("/api/checkout/status")
def checkout_status():
    """The page after paying asks this: makes the order and key at once if Stripe says it is paid."""
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    sid = request.args.get("session_id", "")
    if not CHECKOUT_SESSION.match(sid):
        return err("Order not found", 404)
    if not STRIPE_SECRET_KEY:
        return err("Payments are not set up yet", 503)
    session, error = stripe_call("GET", f"/v1/checkout/sessions/{urllib.parse.quote(sid)}")
    if error:
        return err(error, 502)
    meta = session.get("metadata") or {}
    if str(meta.get("user_id")) != str(user["id"]):
        return err("Order not found", 404)
    product = db().first("SELECT name FROM products WHERE id = ?", meta.get("product_id") or "")
    answer = {"paid": session.get("payment_status") == "paid", "status": session.get("status") or "",
              "product_id": meta.get("product_id") or "", "product": (product or {}).get("name") or meta.get("product_id") or ""}
    if answer["paid"]:
        done = fulfil_checkout(session) or {}
        answer.update({"license_key": done.get("license_key") or "", "email": done.get("email") or ""})
    return jsonify(answer)


@app.post("/api/stripe/webhook")
def stripe_webhook():
    """Stripe tells about paid checkouts here, also when the customer closed the page after paying."""
    if not STRIPE_WEBHOOK_SECRET:
        return err("The webhook is not set up", 503)
    payload = request.get_data()
    parts = [p.split("=", 1) for p in request.headers.get("Stripe-Signature", "").split(",") if "=" in p]
    stamp = next((v for k, v in parts if k == "t"), "")
    signatures = [v for k, v in parts if k == "v1"]
    try:
        fresh = abs(time.time() - int(stamp)) <= 300
    except ValueError:
        fresh = False
    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if not fresh or not any(hmac.compare_digest(expected, v) for v in signatures):
        return err("Bad signature", 400)
    try:
        event = json.loads(payload)
    except ValueError:
        return err("Bad payload", 400)
    if event.get("type") in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        fulfil_checkout((event.get("data") or {}).get("object") or {})
    return jsonify({"received": True})


# ---------- Admin -> Shop: payments set-up, discount codes, sales per product ----------

SHOP_DEFAULT = {"promo_codes": False}


def shop_settings():
    saved = get_setting("shop.settings", {}) or {}
    return {**SHOP_DEFAULT, **{k: saved[k] for k in SHOP_DEFAULT if k in saved}}


@app.get("/api/admin/shop")
def admin_shop():
    if not staff_user():
        return err("Admin only", 403)
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    sales = {}
    for row in db().all("SELECT product_id, currency, COUNT(*) AS n, SUM(amount) AS total, "
                        "SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS recent FROM orders "
                        "WHERE payment_status = 'Paid' GROUP BY product_id, currency", since):
        sales.setdefault(row["product_id"] or "", []).append({"currency": (row["currency"] or "").lower(), "orders": int(row["n"] or 0),
                                                              "total": round(float(row["total"] or 0), 2), "last_30_days": int(row["recent"] or 0)})
    items = []
    for p in db().all("SELECT * FROM products ORDER BY sort_order, name"):
        items.append({"id": p["id"], "name": p["name"], "status": p["status"], "pay_amount": int(p.get("pay_amount") or 0),
                      "pay_currency": p.get("pay_currency") or "sek", "sale_amount": int(p.get("sale_amount") or 0),
                      "sale_until": p.get("sale_until") or "", "sale_active": sale_active(p), "price_now": price_now(p),
                      "can_buy": bool(STRIPE_SECRET_KEY and (p.get("pay_amount") or 0) > 0 and p.get("status") == "published"),
                      "sales": sales.pop(p["id"], [])})
    return jsonify({"stripe": {"connected": bool(STRIPE_SECRET_KEY), "mode": stripe_mode(), "webhook": bool(STRIPE_WEBHOOK_SECRET),
                               "webhook_url": site_url() + "/api/stripe/webhook"},
                    "settings": shop_settings(), "products": items,
                    "other_sales": [{"product_id": k, **v} for k, rows in sales.items() for v in rows]})


@app.post("/api/admin/shop")
def admin_save_shop():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change the shop", 403)
    b = body()
    settings = {**shop_settings(), "promo_codes": bool(b.get("promo_codes"))}
    set_setting("shop.settings", settings)
    audit(user, "save_shop", "discount codes " + ("on" if settings["promo_codes"] else "off"))
    return jsonify({"ok": True, "settings": settings})


# ---------- affiliate program ----------
# People with a referral code earn a share of the sales they bring. A visitor arriving with ?ref=CODE
# keeps the code (auth.js), it travels to Stripe Checkout as metadata, and a paid order credits the
# affiliate a commission (their percent of the amount). You mark commissions paid in Admin -> Affiliates.

AFFILIATE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,30}$")


def affiliate_by_code(code):
    code = str(code or "").strip()
    if not AFFILIATE_CODE.match(code):
        return None
    return db().first("SELECT * FROM affiliates WHERE LOWER(code) = ? AND status = 'active'", code.lower())


def affiliate_for_user(user):
    """The affiliate account tied to a logged-in user, by account id or by email."""
    return db().first("SELECT * FROM affiliates WHERE user_id = ? OR LOWER(email) = ?", user["id"], (user.get("email") or "").lower())


def credit_affiliate(order_id, amount, currency, affiliate_code, buyer_id, buyer_email):
    """One commission for a referred order. Skips self-referral and does nothing without a valid code."""
    aff = affiliate_by_code(affiliate_code)
    if not aff:
        return None
    if (aff["user_id"] and buyer_id and int(aff["user_id"]) == int(buyer_id)) or (aff["email"] and aff["email"].lower() == (buyer_email or "").lower()):
        return None  # you cannot refer yourself
    commission = round((float(amount or 0) * int(aff["percent"] or 0)) / 100, 2)
    try:
        db().run("INSERT INTO affiliate_commissions (affiliate_id, order_id, amount, currency, rate, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                 aff["id"], order_id, commission, (currency or "").lower(), int(aff["percent"] or 0), utc_stamp())
    except Exception:  # noqa: BLE001 - already credited (order_id is unique)
        return None
    return aff


def affiliate_totals(affiliate_id):
    rows = db().all("SELECT status, COUNT(*) AS n, SUM(amount) AS total FROM affiliate_commissions WHERE affiliate_id = ? GROUP BY status", affiliate_id)
    by = {r["status"]: {"orders": int(r["n"] or 0), "amount": round(float(r["total"] or 0), 2)} for r in rows}
    return {"pending": by.get("pending", {"orders": 0, "amount": 0}), "paid": by.get("paid", {"orders": 0, "amount": 0}),
            "orders": sum(v["orders"] for v in by.values())}


def affiliate_orders(affiliate_id, with_buyer=False):
    """The referred orders behind an affiliate's commissions, newest first. Buyers are only named for staff."""
    rows = db().all("SELECT c.id, c.amount AS commission, c.currency, c.rate, c.status, c.created_at, o.id AS order_id, o.amount AS sale, "
                    "o.product_id, o.customer_id, o.payment_ref, p.name AS product FROM affiliate_commissions c "
                    "LEFT JOIN orders o ON o.id = c.order_id LEFT JOIN products p ON p.id = o.product_id "
                    "WHERE c.affiliate_id = ? ORDER BY c.id DESC LIMIT 500", affiliate_id)
    out = []
    for r in rows:
        item = {"order_id": r["order_id"], "date": str(r["created_at"] or "")[:16], "product": r["product"] or r["product_id"] or "",
                "sale": round(float(r["sale"] or 0), 2), "commission": round(float(r["commission"] or 0), 2),
                "currency": (r["currency"] or "").upper(), "rate": r["rate"], "status": r["status"]}
        if with_buyer:
            buyer = db().first("SELECT name, email FROM users WHERE id = ?", r["customer_id"]) if r["customer_id"] else None
            if not buyer and r["payment_ref"]:
                buyer = db().first("SELECT email FROM stripe_payments WHERE session_id = ?", r["payment_ref"])
            item["buyer"] = (buyer or {}).get("email") or ""
        out.append(item)
    return out


def affiliate_public(aff, with_buyer=False):
    return {"id": aff["id"], "code": aff["code"], "name": aff["name"] or "", "email": aff["email"] or "", "percent": aff["percent"],
            "status": aff["status"], "note": aff["note"] or "", "payout": aff.get("payout") or "", "clicks": int(aff.get("clicks") or 0),
            "payouts_ready": bool(affiliate_paypal_email(aff)),  # has a valid PayPal email, so auto-payouts can reach them
            "created_at": aff["created_at"], "link": site_url() + "/?ref=" + urllib.parse.quote(aff["code"]),
            "orders_list": affiliate_orders(aff["id"], with_buyer), **affiliate_totals(aff["id"])}


@app.get("/api/account/affiliate")
def account_affiliate():
    """The logged-in user's own affiliate dashboard, if they are an affiliate (or have applied)."""
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    aff = affiliate_for_user(user)
    if not aff:
        return jsonify({"affiliate": None, "percent": AFFILIATE_DEFAULT_PERCENT})
    if affiliate_paypal_email(aff):
        affiliate_autopay(aff)      # pay anything still pending now they have a PayPal email saved
        aff = affiliate_for_user(user) or aff
    data = affiliate_public(aff)
    data.pop("note", None)  # the admin's own note stays private
    return jsonify({"affiliate": data, "percent": AFFILIATE_DEFAULT_PERCENT, "payouts": affiliate_payout_settings()})


@app.post("/api/account/affiliate/payout")
def account_affiliate_payout():
    """Where the affiliate wants their money sent (PayPal email, IBAN, ...), shown to staff when paying."""
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    aff = affiliate_for_user(user)
    if not aff:
        return err("You are not an affiliate", 404)
    payout = str(body().get("payout") or "").strip()[:300]
    db().run("UPDATE affiliates SET payout = ? WHERE id = ?", payout, aff["id"])
    return jsonify({"ok": True, "payout": payout})


@app.post("/api/affiliate/click")
def affiliate_click():
    """Counts an arrival through an affiliate link (the browser sends it once per code)."""
    aff = affiliate_by_code(body().get("code"))
    if aff:
        db().run("UPDATE affiliates SET clicks = COALESCE(clicks, 0) + 1 WHERE id = ?", aff["id"])
    return jsonify({"ok": True})


@app.get("/api/admin/affiliates")
def admin_affiliates():
    if not staff_user():
        return err("Admin only", 403)
    out = []
    for aff in db().all("SELECT * FROM affiliates ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, id DESC"):
        item = affiliate_public(aff)
        item.pop("orders_list")
        out.append(item)
    return jsonify({"affiliates": out, "default_percent": AFFILIATE_DEFAULT_PERCENT, "payouts": affiliate_payout_settings(), "paypal_ready": paypal_ready(),
                    "pending_total": round(float((db().first("SELECT SUM(amount) AS t FROM affiliate_commissions WHERE status = 'pending'") or {}).get("t") or 0), 2)})


@app.get("/api/admin/affiliates/<int:affiliate_id>")
def admin_affiliate_detail(affiliate_id):
    """One affiliate with every referred order, including who bought."""
    if not staff_user():
        return err("Admin only", 403)
    aff = db().first("SELECT * FROM affiliates WHERE id = ?", affiliate_id)
    if not aff:
        return err("Affiliate not found", 404)
    return jsonify({"affiliate": affiliate_public(aff, with_buyer=True)})


@app.post("/api/admin/affiliates")
def admin_save_affiliate():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change affiliates", 403)
    b = body()
    code = str(b.get("code") or "").strip()
    if not AFFILIATE_CODE.match(code):
        return err("The code is 3 to 31 letters, digits, dashes or underscores, starting with a letter or digit", 400)
    email = str(b.get("email") or "").strip().lower()
    if email and not EMAIL_RE.match(email):
        return err("Enter a valid email address or leave it empty", 400)
    percent = clamp_int(b.get("percent"), AFFILIATE_DEFAULT_PERCENT, 1, 90)
    status = b.get("status") if b.get("status") in ("active", "disabled", "pending") else "active"
    clash = db().first("SELECT id FROM affiliates WHERE LOWER(code) = ?", code.lower())
    edit_id = b.get("id")
    if clash and str(clash["id"]) != str(edit_id or ""):
        return err(f'The code "{code}" is already used by another affiliate', 409)
    customer = db().first("SELECT id FROM users WHERE LOWER(email) = ?", email) if email else None
    if edit_id:
        if not db().first("SELECT id FROM affiliates WHERE id = ?", int(edit_id)):
            return err("Affiliate not found", 404)
        db().run("UPDATE affiliates SET code = ?, email = ?, user_id = ?, name = ?, percent = ?, status = ?, note = ? WHERE id = ?",
                 code, email, customer["id"] if customer else None, str(b.get("name") or "")[:100], percent, status, str(b.get("note") or "")[:300], int(edit_id))
        audit(user, "save_affiliate", f"{code} ({percent}%, {status})")
    else:
        db().run("INSERT INTO affiliates (code, email, user_id, name, percent, status, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 code, email, customer["id"] if customer else None, str(b.get("name") or "")[:100], percent, status, str(b.get("note") or "")[:300], utc_stamp())
        audit(user, "add_affiliate", f"{code} ({percent}%)")
    return jsonify({"ok": True})


@app.post("/api/admin/affiliates/<int:affiliate_id>/pay")
def admin_pay_affiliate(affiliate_id):
    """Mark this affiliate's pending commissions as paid (after you have sent the money)."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change affiliates", 403)
    aff = db().first("SELECT * FROM affiliates WHERE id = ?", affiliate_id)
    if not aff:
        return err("Affiliate not found", 404)
    pending = affiliate_totals(affiliate_id)["pending"]
    db().run("UPDATE affiliate_commissions SET status = 'paid' WHERE affiliate_id = ? AND status = 'pending'", affiliate_id)
    audit(user, "pay_affiliate", f"{aff['code']} ({pending['orders']} orders)")
    return jsonify({"ok": True, "paid": pending})


@app.delete("/api/admin/affiliates/<int:affiliate_id>")
def admin_delete_affiliate(affiliate_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change affiliates", 403)
    aff = db().first("SELECT code FROM affiliates WHERE id = ?", affiliate_id)
    if not aff:
        return err("Affiliate not found", 404)
    db().run("DELETE FROM affiliate_commissions WHERE affiliate_id = ?", affiliate_id)
    db().run("DELETE FROM affiliates WHERE id = ?", affiliate_id)
    audit(user, "delete_affiliate", aff["code"])
    return jsonify({"ok": True})


# ---------- automatic affiliate payouts (Stripe Connect) ----------
# Affiliates connect a Stripe account once (Express onboarding). When payouts are on, the website
# transfers each affiliate's pending commissions to them, grouped by currency, above a minimum. STRATO
# has no cron, so the weekly batch runs at the first request after it is due (like the scheduled posts).

AFFILIATE_PAYOUT_MIN = 50       # dollars: the smallest pending total that triggers an automatic payout
AFFILIATE_PAYOUT_DAYS = 7       # how often the automatic batch runs


def money_or(value, default):
    try:
        return max(0.0, round(float(value), 2))
    except (TypeError, ValueError):
        return default


def affiliate_payout_settings():
    return {"on": bool(get_setting("affiliate.payouts_on", False)),
            "min": money_or(get_setting("affiliate.payout_min", AFFILIATE_PAYOUT_MIN), AFFILIATE_PAYOUT_MIN),
            "last": get_setting("affiliate.payout_last", "") or "", "every_days": AFFILIATE_PAYOUT_DAYS}


def paypal_ready():
    """True when PayPal Payouts is configured (config.env), so the site can pay affiliates."""
    return bool(PAYPAL_CLIENT_ID and PAYPAL_SECRET)


def affiliate_paypal_email(aff):
    """The affiliate's PayPal email (where their money is sent), or '' when they have not given a valid one."""
    email = str(aff.get("payout") or "").strip()
    return email if EMAIL_RE.match(email) else ""


def paypal_token():
    """A short-lived OAuth2 access token for PayPal (client-credentials). Returns (token, error)."""
    auth = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_SECRET}".encode()).decode()
    req = urllib.request.Request(f"{PAYPAL_API_URL}/v1/oauth2/token", data=b"grant_type=client_credentials",
                                 headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode()).get("access_token") or "", ""
    except urllib.error.HTTPError as exc:
        app.logger.error("PayPal token -> %s", exc.code)
        return "", f"PayPal did not accept the keys ({exc.code}). Run setup.py paypal again."
    except (OSError, ValueError) as exc:
        app.logger.error("PayPal token: %s", exc)
        return "", "Could not reach PayPal. Try again in a minute."


def paypal_payout(email, amount, currency, note, batch_id):
    """Send one payout to a PayPal email. Returns (payout batch id, error). sender_batch_id makes it
    idempotent: the same id never pays twice."""
    token, error = paypal_token()
    if error or not token:
        return None, error or "No PayPal token"
    payload = {"sender_batch_header": {"sender_batch_id": batch_id, "email_subject": "Your Rotaidem affiliate payout",
                                       "email_message": "Thank you for sending buyers our way."},
               "items": [{"recipient_type": "EMAIL", "amount": {"value": f"{amount:.2f}", "currency": currency.upper()},
                          "receiver": email, "note": str(note)[:200], "sender_item_id": batch_id}]}
    req = urllib.request.Request(f"{PAYPAL_API_URL}/v1/payments/payouts", data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode())
            return (out.get("batch_header") or {}).get("payout_batch_id") or batch_id, ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            message = json.loads(raw).get("message") or raw[:200]
        except (ValueError, AttributeError):
            message = raw[:200]
        app.logger.error("PayPal payout -> %s: %s", exc.code, message)
        return None, f"PayPal said ({exc.code}): {message}"
    except (OSError, ValueError) as exc:
        app.logger.error("PayPal payout: %s", exc)
        return None, "Could not reach PayPal. Try again in a minute."


def affiliate_pay_one(aff, minimum):
    """PayPal an affiliate's pending commissions (grouped by currency) when a group clears the minimum.
    Returns a list of {amount, currency, transfer}. sender_batch_id covers the exact set, so a retry
    after a crash cannot pay twice."""
    paid = []
    email = affiliate_paypal_email(aff)
    if not email:
        return paid
    for g in db().all("SELECT currency, SUM(amount) AS total FROM affiliate_commissions "
                      "WHERE affiliate_id = ? AND status = 'pending' GROUP BY currency", aff["id"]):
        total = round(float(g["total"] or 0), 2)
        if total < minimum or total <= 0:
            continue
        currency = (g["currency"] or "usd").lower()
        ids = [r["id"] for r in db().all("SELECT id FROM affiliate_commissions WHERE affiliate_id = ? AND status = 'pending' AND currency = ?",
                                         aff["id"], g["currency"])]
        if not ids:
            continue
        batch_id = f"aff-{aff['id']}-{currency}-{max(ids)}-{len(ids)}"
        batch, error = paypal_payout(email, total, currency, f"Rotaidem affiliate {aff['code']}", batch_id)
        if error or not batch:
            app.logger.error("Affiliate payout %s (%s %s): %s", aff["code"], total, currency, error)
            continue
        marks = ",".join("?" for _ in ids)
        db().run(f"UPDATE affiliate_commissions SET status = 'paid', transfer_id = ? WHERE id IN ({marks})", batch, *ids)
        paid.append({"amount": total, "currency": currency.upper(), "transfer": batch})
    return paid


def affiliate_payout_run():
    """Pay every active affiliate who has a PayPal email and whose pending total clears the minimum."""
    minimum = affiliate_payout_settings()["min"]
    out = []
    if paypal_ready():
        for aff in db().all("SELECT * FROM affiliates WHERE status = 'active' AND payout IS NOT NULL AND payout <> ''"):
            if affiliate_paypal_email(aff):
                out += [{"code": aff["code"], **row} for row in affiliate_pay_one(aff, minimum)]
    set_setting("affiliate.payout_last", utc_stamp())
    set_setting("affiliate.payout_last_ts", time.time())
    if out:
        lines = ", ".join(f"{p['amount']:.2f} {p['currency']} to {p['code']}" for p in out)
        discord_auto("sales", {"text": f"💸 Affiliate payouts sent by PayPal: {lines}."})
    return {"paid": out, "min": minimum}


def affiliate_autopay(aff):
    """PayPal an affiliate their pending commissions right now, the moment a referred sale is paid (and
    when they save their PayPal email), so money is auto and direct. Ignores the minimum — that only
    guards the weekly catch-up. Never raises: on failure the commission stays pending for the next run."""
    if not paypal_ready() or not aff or not get_setting("affiliate.payouts_on", False):
        return []
    try:
        if not affiliate_paypal_email(aff):
            return []
        return affiliate_pay_one(aff, 0)
    except Exception:  # noqa: BLE001 - a buyer's success page never fails over a payout
        app.logger.exception("Instant affiliate payout failed for %s", (aff or {}).get("code"))
        return []


def affiliate_payouts_due():
    """Run the weekly catch-up at the first request after it is due (STRATO has no cron)."""
    if not paypal_ready() or not get_setting("affiliate.payouts_on", False):
        return
    now = time.time()
    if now - float(get_setting("affiliate.payout_checked", 0) or 0) < 3600:   # look at most once an hour
        return
    set_setting("affiliate.payout_checked", now)
    if now - float(get_setting("affiliate.payout_last_ts", 0) or 0) < AFFILIATE_PAYOUT_DAYS * 86400:
        return
    try:
        affiliate_payout_run()
    except Exception:  # noqa: BLE001 - visitors never see a payout problem
        app.logger.exception("Automatic affiliate payouts failed")


@app.post("/api/admin/affiliates/settings")
def admin_affiliate_settings():
    """Turn automatic payouts on or off and set the minimum (Admin -> Affiliates)."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change affiliates", 403)
    b = body()
    set_setting("affiliate.payouts_on", bool(b.get("payouts_on")))
    set_setting("affiliate.payout_min", money_or(b.get("payout_min"), AFFILIATE_PAYOUT_MIN))
    audit(user, "affiliate_payout_settings", f"on={bool(b.get('payouts_on'))}, min={b.get('payout_min')}")
    return jsonify({"ok": True, **affiliate_payout_settings()})


@app.post("/api/admin/affiliates/payout-run")
def admin_affiliate_payout_run():
    """Send every due PayPal payout now, instead of waiting for the weekly run."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change affiliates", 403)
    if not paypal_ready():
        return err("PayPal is not set up yet. Run setup.py paypal over SSH.", 503)
    result = affiliate_payout_run()
    audit(user, "affiliate_payout_run", f"{len(result['paid'])} payout(s)")
    return jsonify({"ok": True, **result})


# ---------- Discord: a "Verified buyer" role for licence holders ----------
# No bot runs all the time (STRATO cannot keep one running): everything is a call to Discord's API.
# On My account a customer links Discord (OAuth2, scopes identify + guilds.join): the website adds
# them to the server and gives the buyer role when they have an active licence. The role follows
# the licence (bought, revoked, restored, claimed). Channels are locked to the role in Discord.
# /verify in Discord reaches /api/discord/interactions (signed by Discord, checked with Ed25519).

DISCORD_DEFAULT = {"guild_id": "", "role_id": "", "products": [], "updates_channel": "", "updates_ping": False, "news_channel": "", "sales_channel": "", "status_channel": "",
                   "welcome_channel": "", "team_channel": "", "tickets_channel": "", "invite_url": ""}
SNOWFLAKE = re.compile(r"^\d{15,22}$")
DISCORD_STATE_MINUTES = 15
# Manage Roles, Create Invite, View Channels, Send Messages, Embed Links, Mention Everyone,
# Add Reactions, Manage Messages (pins), Read Message History, Send Polls, Manage Events
DISCORD_BOT_PERMISSIONS = str(268435456 | 1 | 1024 | 2048 | 16384 | 131072 | 64 | 8192 | 65536 | (1 << 49) | (1 << 33) | (1 << 36) | (1 << 38) | (1 << 34)  # + create private threads, send in threads, manage threads (support tickets)
                              | (1 << 1) | (1 << 2) | (1 << 4) | (1 << 26) | (1 << 40)  # + kick, ban, manage channels (slowmode, lock), change nickname, timeout members
                              | (1 << 5) | (1 << 7) | (1 << 30)  # + manage server (invite list, AutoMod), view audit log, manage emojis
                              | (1 << 29) | (1 << 35))  # + manage webhooks (post as another name), create public threads (news discussions)


def discord_settings():
    saved = get_setting("discord.settings", {}) or {}
    return {**DISCORD_DEFAULT, **{k: saved[k] for k in DISCORD_DEFAULT if k in saved}}


def discord_ready():
    cfg = discord_settings()
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and DISCORD_BOT_TOKEN and cfg["guild_id"] and cfg["role_id"])


def discord_call(method, path, payload=None, form=None, bearer=None, timeout=20, reason=""):
    """Discord's API with the bot token (or a member's OAuth token). Returns (answer, error text, HTTP status)."""
    if form is not None:
        data, kind = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    elif payload is not None:
        data, kind = json.dumps(payload).encode(), "application/json"
    else:
        data, kind = (b"" if method in ("PUT", "POST") else None), "application/json"
    auth = f"Bearer {bearer}" if bearer else f"Bot {DISCORD_BOT_TOKEN}"
    headers = {"Authorization": auth, "Content-Type": kind, "User-Agent": "DiscordBot (https://rotaidem.com, 1.0)"}
    if reason:  # shown in the server's Audit Log in Discord
        headers["X-Audit-Log-Reason"] = urllib.parse.quote(str(reason)[:400])
    for attempt in (1, 2):
        req = urllib.request.Request(DISCORD_API_URL + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return (json.loads(raw) if raw else {}), "", resp.status
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 1:  # rate limited: Discord says how long to wait
                try:
                    wait = float(json.loads(exc.read().decode(errors="replace")).get("retry_after") or 1)
                except (ValueError, AttributeError):
                    wait = 1.0
                if wait <= 5:
                    time.sleep(wait)
                    continue
            return discord_http_error(method, path, exc)
        except (OSError, ValueError) as exc:
            app.logger.warning("Discord %s %s: %s", method, path.split("?")[0], exc)
            return None, "Could not reach Discord. Try again in a minute.", 0
    return None, "Discord is busy. Try again in a minute.", 429


def discord_http_error(method, path, exc):
    """(None, a readable error, the HTTP status) from Discord's error answer."""
    raw = exc.read().decode(errors="replace")
    try:
        message = json.loads(raw).get("message") or raw[:200]
    except ValueError:
        message = raw[:200]
    app.logger.warning("Discord %s %s -> %s: %s", method, path.split("?")[0], exc.code, message)
    return None, f"Discord said ({exc.code}): {message}", exc.code


def has_buyer_licence(user):
    """An active licence on this account (by account or email) for one of the products that count (none chosen: any)."""
    rows = db().all("SELECT product_id FROM licenses WHERE status = 'active' AND (customer_id = ? OR LOWER(email) = ?)",
                    user["id"], (user.get("email") or "").lower())
    products = discord_settings()["products"]
    return any(not products or any(same_product(p, r["product_id"]) for p in products) for r in rows)


def discord_sync_user(user):
    """Gives or takes the buyer role to match the licences: 'given', 'removed', 'not in server', 'off' or an error."""
    if not discord_ready() or not user.get("discord_id"):
        return "off"
    cfg = discord_settings()
    wanted = has_buyer_licence(user)
    _, error, status = discord_call("PUT" if wanted else "DELETE", f"/guilds/{cfg['guild_id']}/members/{user['discord_id']}/roles/{cfg['role_id']}")
    result = "not in server" if status == 404 else error or ("given" if wanted else "removed")
    if status != 404:
        try:
            discord_sync_product_roles(user)
        except Exception:  # noqa: BLE001 - the buyer role result is what counts here
            app.logger.exception("Product roles failed")
    shown = {"given": "Role given", "removed": "No licence on this account: no role", "not in server": "Not in the server (left it, or could not be added)"}
    if status == 403:
        shown[result] = "Discord refused: the bot's role must be ABOVE the buyer role, with Manage Roles (press Check set-up)"
    try:
        db().run("UPDATE users SET discord_result = ? WHERE id = ?", f"{utc_stamp()[:16]} UTC: {shown.get(result, result)}"[:300], user["id"])
    except Exception:  # noqa: BLE001 - only a note for the admin
        pass
    return result


def discord_sync_owners(owners):
    """After a licence changed: (customer_id, email) pairs -> the linked accounts get the right role. Never fails the caller."""
    if not discord_ready():
        return
    seen = set()
    for customer_id, email in owners:
        try:
            for user in db().all("SELECT * FROM users WHERE discord_id IS NOT NULL AND discord_id <> '' AND (id = ? OR LOWER(email) = ?)",
                                 customer_id or 0, (email or "").lower() or "-"):
                if user["id"] not in seen:
                    seen.add(user["id"])
                    discord_sync_user(user)
        except Exception:  # noqa: BLE001 - a licence change must never fail because of Discord
            app.logger.exception("Discord role update failed")


def discord_state(user_id, expires=None, nonce=None):
    expires = expires or int(time.time()) + DISCORD_STATE_MINUTES * 60
    nonce = nonce or secrets.token_hex(8)
    sig = hmac.new(SECRET_KEY.encode(), f"discord:{user_id}:{expires}:{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{expires}.{nonce}.{sig}"


def discord_state_user(state):
    try:
        user_id, expires, nonce, _sig = str(state).split(".")
        if int(expires) < time.time() or not hmac.compare_digest(discord_state(int(user_id), int(expires), nonce), str(state)):
            return None
        return db().first("SELECT * FROM users WHERE id = ?", int(user_id))
    except ValueError:
        return None


def discord_redirect_uri():
    return site_url() + "/api/discord/callback"


@app.post("/api/discord/start")
def discord_start():
    user = current_user()
    if not user:
        return err("Log in first", 401)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    query = urllib.parse.urlencode({"client_id": DISCORD_CLIENT_ID, "response_type": "code", "scope": "identify guilds.join",
                                    "redirect_uri": discord_redirect_uri(), "state": discord_state(user["id"]), "prompt": "consent"})
    return jsonify({"url": "https://discord.com/oauth2/authorize?" + query})


@app.get("/api/discord/callback")
def discord_callback():
    """Discord sends the customer back here after they agreed: link the account, join the server, the role."""
    back = lambda result: redirect(site_url() + "/account?discord=" + result + "#discord")  # noqa: E731
    if request.args.get("error"):
        return back("cancelled")
    user = discord_state_user(request.args.get("state", ""))
    if not user or not discord_ready():
        return back("expired")
    token, error, _ = discord_call("POST", "/oauth2/token", form={
        "client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET, "grant_type": "authorization_code",
        "code": request.args.get("code", ""), "redirect_uri": discord_redirect_uri()})
    access = (token or {}).get("access_token")
    me, error2, _ = discord_call("GET", "/users/@me", bearer=access) if access else (None, error, 0)
    if not access or not me or not SNOWFLAKE.match(str(me.get("id") or "")):
        app.logger.warning("Discord link failed: %s", error or error2)
        return back("error")
    discord_id, name = str(me["id"]), str(me.get("global_name") or me.get("username") or "")[:100]
    first_link = not db().first("SELECT id FROM users WHERE discord_id = ?", discord_id)
    db().run("UPDATE users SET discord_id = NULL, discord_name = NULL, discord_linked_at = NULL WHERE discord_id = ? AND id <> ?", discord_id, user["id"])
    db().run("UPDATE users SET discord_id = ?, discord_name = ?, discord_linked_at = ? WHERE id = ?", discord_id, name, utc_stamp(), user["id"])
    user = db().first("SELECT * FROM users WHERE id = ?", user["id"])
    cfg, buyer = discord_settings(), has_buyer_licence(user)
    # into the server (already in: nothing changes), then the role to match the licence
    _, join_error, _ = discord_call("PUT", f"/guilds/{cfg['guild_id']}/members/{discord_id}", payload={"access_token": access, "roles": [cfg["role_id"]] if buyer else []})
    result = discord_sync_user(user)
    if result == "not in server" and join_error:
        db().run("UPDATE users SET discord_result = ? WHERE id = ?", f"{utc_stamp()[:16]} UTC: could not add them to the server: {join_error}"[:300], user["id"])
    db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'discord_linked', ?, ?, ?)",
             user["id"], f"{name} ({result})", request.remote_addr or "", utc_stamp())
    if result != "not in server":
        discord_link_role(discord_id, True)
    if first_link and result != "not in server":
        discord_welcome(discord_id, result == "given")
        discord_welcome_dm(discord_id, user.get("name") or name)
    return back("linked" if buyer else "linked-no-licence")


@app.post("/api/discord/unlink")
def discord_unlink():
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    if user.get("discord_id") and discord_ready():
        cfg = discord_settings()
        discord_call("DELETE", f"/guilds/{cfg['guild_id']}/members/{user['discord_id']}/roles/{cfg['role_id']}")
        discord_link_role(user["discord_id"], False)
    db().run("UPDATE users SET discord_id = NULL, discord_name = NULL, discord_linked_at = NULL WHERE id = ?", user["id"])
    return jsonify({"ok": True})


_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


def _add(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % _P
    B = (a[1] + a[0]) * (b[1] + b[0]) % _P
    C = 2 * a[3] * b[3] * _D % _P
    D = 2 * a[2] * b[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s, point):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, point)
        point, s = _add(point, point), s >> 1
    return q


def _point(raw):
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = (_recover_x(_GY, 0), _GY, 1, _recover_x(_GY, 0) * _GY % _P)


def ed25519_verify(public_key, message, signature):
    """RFC 8032 Ed25519 signature check."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a, r = _point(public_key), _point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
    left, right = _mul(s, _G), _add(r, _mul(h, a))
    return (left[0] * right[2] - right[0] * left[2]) % _P == 0 and (left[1] * right[2] - right[1] * left[2]) % _P == 0


@app.post("/api/discord/interactions")
def discord_interactions():
    """Discord's calls for slash commands and the /support form. /verify: link button, or the role at once when already linked."""
    try:
        key, sig = bytes.fromhex(DISCORD_PUBLIC_KEY), bytes.fromhex(request.headers.get("X-Signature-Ed25519", ""))
    except ValueError:
        key, sig = b"", b""
    raw = request.get_data()
    if not ed25519_verify(key, request.headers.get("X-Signature-Timestamp", "").encode() + raw, sig):
        return err("invalid request signature", 401)
    try:
        event = json.loads(raw)
    except ValueError:
        return err("Bad request", 400)
    if event.get("type") == 1:  # Discord checking the address
        return jsonify({"type": 1})
    data = event.get("data") or {}
    if event.get("type") in (3, 4, 5) or (event.get("type") == 2 and data.get("name") != "verify"):
        return jsonify(discord_interaction(event))
    if event.get("type") != 2:
        return jsonify({"type": 4, "data": {"content": "I do not know that one. Try /help.", "flags": 64}})
    discord_id = str(((event.get("member") or {}).get("user") or event.get("user") or {}).get("id") or "")
    user = db().first("SELECT * FROM users WHERE discord_id = ?", discord_id) if SNOWFLAKE.match(discord_id) else None
    link = {"type": 1, "components": [{"type": 2, "style": 5, "label": "Verify on rotaidem.com", "url": site_url() + "/account#discord"}]}
    if not user:
        text = "Link your Discord on your Rotaidem account (My account, Discord) to get the buyer channels."
        return jsonify({"type": 4, "data": {"content": text, "flags": 64, "components": [link]}})
    result = discord_sync_user(user)
    if result == "given":
        text = "You are verified: the buyer channels are open. Welcome aboard!"
    elif result == "removed":
        text = f"Your Discord is linked to {user['email']}, but that account has no active licence. Bought with another email? Add the key under My account."
    else:
        text = "Something went wrong giving you the role. Please try again in a minute or ask the team."
    return jsonify({"type": 4, "data": {"content": text, "flags": 64, "components": [] if result == "given" else [link]}})


@app.get("/api/admin/discord")
def admin_discord():
    if not staff_user():
        return err("Admin only", 403)
    members = []
    for u in db().all("SELECT id, name, email, discord_id, discord_name, discord_linked_at, discord_result FROM users WHERE discord_id IS NOT NULL AND discord_id <> '' ORDER BY discord_linked_at DESC"):
        members.append({**u, "buyer": has_buyer_licence(u)})
    invite = ("https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(
        {"client_id": DISCORD_CLIENT_ID, "scope": "bot applications.commands", "permissions": DISCORD_BOT_PERMISSIONS})) if DISCORD_CLIENT_ID else ""
    server = {}
    if discord_ready():
        guild, _, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}?with_counts=true", timeout=8)
        if guild:
            server = {"name": guild.get("name", ""), "members": guild.get("approximate_member_count"), "online": guild.get("approximate_presence_count")}
    return jsonify({"server": server, "setup": {"client_id": bool(DISCORD_CLIENT_ID), "client_secret": bool(DISCORD_CLIENT_SECRET),
                              "bot_token": bool(DISCORD_BOT_TOKEN), "public_key": bool(DISCORD_PUBLIC_KEY)},
                    "ready": discord_ready(), "settings": discord_settings(), "invite_url": invite,
                    "redirect_url": discord_redirect_uri(), "interactions_url": site_url() + "/api/discord/interactions", "members": members})


DISCORD_MANAGE_ROLES, DISCORD_ADMINISTRATOR = 1 << 28, 1 << 3


@app.get("/api/admin/discord/check")
def admin_discord_check():
    """Asks Discord why roles might not be given: keys, bot in the server, Manage Roles, role order."""
    if not staff_user():
        return err("Admin only", 403)
    checks = []
    def add(ok, text):
        checks.append({"ok": bool(ok), "text": text})
    cfg = discord_settings()
    missing = [n for n, v in (("Application ID", DISCORD_CLIENT_ID), ("Client Secret", DISCORD_CLIENT_SECRET), ("Bot token", DISCORD_BOT_TOKEN)) if not v]
    add(not missing, "Application keys are set" if not missing else "Missing: " + ", ".join(missing) + ". Run python3 rotaidem/strato/setup.py discord over SSH.")
    # The Public Key is what makes slash commands work: without it every command shows "This interaction failed".
    pub = DISCORD_PUBLIC_KEY or ""
    pub_ok = len(pub) == 64 and all(c in "0123456789abcdefABCDEF" for c in pub)
    add(pub_ok, "The Public Key for slash commands is set" if pub_ok else
        'The Public Key is missing or malformed, so every slash command shows "This interaction failed". '
        "Copy it from the Discord app (General Information, Public Key) and run setup.py discord.")
    add(cfg["guild_id"] and cfg["role_id"], "Server ID and buyer role ID are saved" if cfg["guild_id"] and cfg["role_id"] else "Paste the Server ID and the Buyer role ID below and save.")
    if missing or not (cfg["guild_id"] and cfg["role_id"]):
        return jsonify({"checks": checks, "ok": False})
    bot, error, _ = discord_call("GET", "/users/@me", timeout=10)
    add(bot, f"The bot token works ({bot.get('username')})" if bot else f"Discord does not accept the bot token: {error}. Run setup.py discord again with a new token (Bot, Reset Token).")
    if not bot:
        return jsonify({"checks": checks, "ok": False})
    roles, error, status = discord_call("GET", f"/guilds/{cfg['guild_id']}/roles", timeout=10)
    if not roles:
        add(False, "The bot cannot see the server: invite it with Invite the bot (and check the Server ID)." if status in (403, 404) else f"Could not read the server's roles: {error}")
        return jsonify({"checks": checks, "ok": False})
    by_id = {r["id"]: r for r in roles}
    buyer = by_id.get(cfg["role_id"])
    add(buyer, f"The buyer role is @{buyer['name']}" if buyer else "The Buyer role ID is not a role in this server: copy it again (right-click the role, Copy Role ID).")
    me, error, status = discord_call("GET", f"/guilds/{cfg['guild_id']}/members/{bot['id']}", timeout=10)
    add(me, "The bot is in the server" if me else "The bot is not in the server: press Invite the bot.")
    if not (buyer and me):
        return jsonify({"checks": checks, "ok": False})
    mine = [by_id[r] for r in me.get("roles", []) if r in by_id] + [by_id[cfg["guild_id"]]] if cfg["guild_id"] in by_id else [by_id[r] for r in me.get("roles", []) if r in by_id]
    perms = 0
    for r in mine:
        perms |= int(r.get("permissions") or 0)
    add(perms & (DISCORD_MANAGE_ROLES | DISCORD_ADMINISTRATOR), "The bot has Manage Roles" if perms & (DISCORD_MANAGE_ROLES | DISCORD_ADMINISTRATOR)
        else "The bot's role does not have Manage Roles: Server Settings, Roles, the bot's role, turn on Manage Roles (or invite it again).")
    top = max([r.get("position", 0) for r in mine] or [0])
    top_name = next((r["name"] for r in mine if r.get("position", 0) == top), "its role")
    add(top > buyer.get("position", 0), f"The bot's role @{top_name} is above @{buyer['name']}" if top > buyer.get("position", 0)
        else f"The bot's role @{top_name} is BELOW (or level with) @{buyer['name']}: in Server Settings, Roles, drag @{top_name} above @{buyer['name']}. This is the usual reason the role is not given.")
    add(not buyer.get("managed"), "The buyer role can be given by bots" if not buyer.get("managed") else f"@{buyer['name']} belongs to an integration and cannot be given: make a normal role.")
    add(perms & (1 | DISCORD_ADMINISTRATOR), "The bot may add people to the server (Create Invite)" if perms & (1 | DISCORD_ADMINISTRATOR)
        else "The bot lacks Create Invite, so it cannot add people who are not in the server yet: turn it on for the bot's role.")
    # Are the slash commands actually registered with Discord? (the usual reason /commands "fail" or do not show up)
    registered, cmd_error, _ = discord_call("GET", f"/applications/{DISCORD_CLIENT_ID}/guilds/{cfg['guild_id']}/commands", timeout=10)
    if registered is None:
        add(False, f"Could not read the registered slash commands: {cmd_error}")
    else:
        have, want = {c.get("name") for c in registered}, {c["name"] for c in discord_commands()}
        add(want <= have, f"All {len(want)} slash commands are registered" if want <= have
            else ('No slash commands are registered yet: press "Add slash commands".' if not (want & have)
                  else 'Slash commands are out of date: press "Add slash commands" to update them.'))
    return jsonify({"checks": checks, "ok": all(c["ok"] for c in checks)})


@app.post("/api/admin/discord")
def admin_save_discord():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change Discord settings", 403)
    b = body()
    guild, role = str(b.get("guild_id") or "").strip(), str(b.get("role_id") or "").strip()
    if (guild and not SNOWFLAKE.match(guild)) or (role and not SNOWFLAKE.match(role)):
        return err("Server and role IDs are long numbers: in Discord, turn on Developer Mode, then right-click and Copy ID", 400)
    products = [str(p).strip()[:60] for p in (b.get("products") or []) if str(p).strip()][:20]
    old = discord_settings()
    channels = {k: str(b.get(k, old[k]) or "").strip() for k in ("updates_channel", "news_channel", "sales_channel", "status_channel", "welcome_channel", "team_channel", "tickets_channel")}
    if any(v and not SNOWFLAKE.match(v) for v in channels.values()):
        return err("Choose the channels from the list", 400)
    if channels["news_channel"] and channels["news_channel"] != old["news_channel"]:
        # from now on: news posts that are already up are not announced again
        set_setting("discord.announced_posts", [r["id"] for r in visible_posts()])
    invite = str(b.get("invite_url", old["invite_url"]) or "").strip()[:200]
    if invite and not re.match(r"^https://(discord\.gg/|(www\.)?discord\.com/invite/)[A-Za-z0-9-]+$", invite):
        return err("The invite link must look like https://discord.gg/yourcode", 400)
    set_setting("discord.settings", {"guild_id": guild, "role_id": role, "products": products, **channels,
                                     "invite_url": invite, "updates_ping": bool(b.get("updates_ping", old["updates_ping"]))})
    audit(user, "save_discord", f"server {guild or '-'}, role {role or '-'}")
    commands_registered = False
    if guild and discord_ready():  # keep the slash commands in sync so they never silently go stale
        try:
            _, cmd_error, _ = discord_call("PUT", f"/applications/{DISCORD_CLIENT_ID}/guilds/{guild}/commands", payload=discord_commands())
            commands_registered = not cmd_error
        except Exception:  # noqa: BLE001 - saving settings must not fail over command registration
            app.logger.exception("Auto-registering Discord commands failed")
    return jsonify({"ok": True, "settings": discord_settings(), "ready": discord_ready(), "commands_registered": commands_registered})


@app.get("/api/discord/presence")
def discord_presence_config():
    """Public: what the HeavyPro app needs to show 'Playing HeavyPro' on Discord (Rich Presence) with
    two buttons. The app id is the Discord application's (public), the invite and link are the buttons."""
    return jsonify({"app_id": DISCORD_CLIENT_ID, "invite": discord_settings().get("invite_url") or "",
                    "url": site_url() + "/heavypro", "name": "HeavyPro"})


@app.post("/api/admin/discord/sync")
def admin_discord_sync():
    """Check every linked member again: role on for licence holders, off for the rest."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    counts, problems = {}, []
    for member in db().all("SELECT * FROM users WHERE discord_id IS NOT NULL AND discord_id <> ''"):
        result = discord_sync_user(member)
        key = result if result in ("given", "removed", "not in server") else "failed"
        counts[key] = counts.get(key, 0) + 1
        if key == "failed" and len(problems) < 5:
            problems.append(f"{member['discord_name'] or member['discord_id']}: {result}")
    audit(user, "discord_sync", ", ".join(f"{n} {k}" for k, n in counts.items()) or "nobody linked")
    return jsonify({"ok": True, "counts": counts, "problems": problems})


@app.post("/api/admin/discord/command")
def admin_discord_command():
    """Adds the slash commands to the server (Discord then sends them to /api/discord/interactions)."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    _, error, _ = discord_call("PUT", f"/applications/{DISCORD_CLIENT_ID}/guilds/{discord_settings()['guild_id']}/commands", payload=discord_commands())
    if error:
        return err(error, 502)
    audit(user, "discord_command", " ".join("/" + c["name"] for c in discord_commands()))
    return jsonify({"ok": True})


@app.post("/api/admin/discord/ticket-panel")
def admin_discord_ticket_panel():
    """Posts a message with an 'Open a ticket' button, so people start a ticket with one click."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    cfg = discord_settings()
    channel = str(body().get("channel_id") or cfg.get("tickets_channel") or "")
    if not SNOWFLAKE.match(channel):
        return err("Pick a channel to post the button in (or set a Support tickets channel first).", 400)
    text = str(body().get("text") or "").strip()[:1500] or "Need a hand from the Rotaidem team? Click below to open a private support ticket. Only you and our team will see it."
    cats = discord_extra()["ticket_categories"]
    components = ([{"type": 1, "components": [{"type": 3, "custom_id": "ticket_cat", "placeholder": "🎫 Open a ticket: pick a topic",
                                               "options": [{"label": c[:100], "value": str(i)} for i, c in enumerate(cats)]}]}] if cats else
                  [{"type": 1, "components": [{"type": 2, "style": 1, "label": "Open a ticket", "emoji": {"name": "🎫"}, "custom_id": "open_ticket"}]}])
    _, error, _ = discord_call("POST", f"/channels/{channel}/messages", payload={"content": text, "components": components, "allowed_mentions": {"parse": []}})
    if error:
        return err(error, 502)
    audit(user, "discord_ticket_panel", channel)
    return jsonify({"ok": True})


@app.post("/api/admin/discord/post")
def admin_discord_post():
    """The bot posts a message with a Verify button (to rotaidem.com/account) in a channel."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    b = body()
    channel, text = str(b.get("channel_id") or "").strip(), str(b.get("text") or "").strip()[:1800]
    if not SNOWFLAKE.match(channel):
        return err("Paste the channel's ID: right-click the channel in Discord, Copy Channel ID", 400)
    if not text:
        return err("Write the message", 400)
    message = {"content": text, "components": [{"type": 1, "components": [
        {"type": 2, "style": 5, "label": "Verify on rotaidem.com", "url": site_url() + "/account#discord"}]}]}
    _, error, _ = discord_call("POST", f"/channels/{channel}/messages", payload=message)
    if error:
        return err(error, 502)
    audit(user, "discord_post", channel)
    return jsonify({"ok": True})


# Posting with the bot (Admin -> Discord -> Post to Discord): a message, an optional card (embed) with
# a picture, link buttons, and who to ping. What the bot posted is listed so it can be edited or deleted.

DISCORD_PINGS = ("none", "buyers", "here", "everyone")


def discord_absolute(url):
    url = str(url or "").strip()
    return site_url() + url if url.startswith("/") and not url.startswith("//") else url


def discord_channel_names():
    """{channel name: id} for the server's text channels, so #name in a post becomes a channel link."""
    rows, error, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}/channels", timeout=8)
    return {} if error else {c.get("name", "").lower(): c["id"] for c in rows or [] if c.get("type") in (0, 2, 5, 13, 15)}


def link_channels(text, names):
    return re.sub(r"(?<![<\w&])#([a-z0-9_\-]{1,100})", lambda m: f"<#{names[m.group(1).lower()]}>" if m.group(1).lower() in names else m.group(0), text)


def discord_poll(b):
    """A poll from the admin: question, 2-10 answers, how many hours it stays open."""
    p = b.get("poll") if isinstance(b.get("poll"), dict) else {}
    question = str(p.get("question") or "").strip()
    if not question:
        return None
    answers = [str(a).strip() for a in (p.get("answers") or []) if str(a).strip()]
    if len(question) > 300 or not 2 <= len(answers) <= 10 or any(len(a) > 55 for a in answers):
        raise ValueError("A poll needs a question (300 characters) and 2 to 10 answers (55 characters each)")
    try:
        hours = max(1, min(768, int(p.get("hours") or 24)))
    except (TypeError, ValueError):
        hours = 24
    return {"question": {"text": question}, "answers": [{"poll_media": {"text": a}} for a in answers], "duration": hours,
            "allow_multiselect": bool(p.get("multiple")), "layout_type": 1}


def discord_message(b, names=None):
    """The message Discord gets from what the admin wrote. Raises ValueError with a message for the admin."""
    names = names or {}
    text = link_channels(str(b.get("text") or "").replace("\r\n", "\n").strip(), names)
    embed_in = b.get("embed") if isinstance(b.get("embed"), dict) else {}
    embed = {}
    title, desc = str(embed_in.get("title") or "").strip(), link_channels(str(embed_in.get("description") or "").replace("\r\n", "\n").strip(), names)
    link, image = str(embed_in.get("url") or "").strip(), str(embed_in.get("image") or "").strip()
    if len(title) > 256 or len(desc) > 4000:
        raise ValueError("The card's title can have 256 characters and its text 4000")
    if link and not (link.startswith("https://") or (link.startswith("/") and not link.startswith("//"))):
        raise ValueError("The card's link must start with https:// or be a page like /heavypro")
    if image and not valid_image_url(image):
        raise ValueError("The card's picture must be an uploaded picture or an https:// link")
    if title or desc or image:
        embed = {k: v for k, v in {"title": title, "description": desc, "url": discord_absolute(link)}.items() if v}
        color = str(embed_in.get("color") or "#aab6c2").strip()
        embed["color"] = int(color[1:], 16) if re.match(r"^#[0-9a-fA-F]{6}$", color) else 0xAAB6C2
        if image:
            embed["image"] = {"url": discord_absolute(image)}
    buttons = []
    for btn in (b.get("buttons") or [])[:5]:
        label, url = str((btn or {}).get("label") or "").strip(), str((btn or {}).get("url") or "").strip()
        if not label and not url:
            continue
        if not label or len(label) > 80 or not (url.startswith("https://") or (url.startswith("/") and not url.startswith("//"))):
            raise ValueError("Each button needs a short label and a link (https:// or a page like /downloads)")
        buttons.append({"type": 2, "style": 5, "label": label, "url": discord_absolute(url)})
    poll = discord_poll(b)
    if not text and not embed and not poll:
        raise ValueError("Write a message, fill in the card or add a poll")
    ping = b.get("ping") if b.get("ping") in DISCORD_PINGS else "none"
    role = discord_settings()["role_id"]
    prefix = {"buyers": f"<@&{role}>" if role else "", "here": "@here", "everyone": "@everyone"}.get(ping, "")
    content = (prefix + "\n" + text).strip() if prefix else text
    if len(content) > 2000:
        raise ValueError("A Discord message can have 2000 characters")
    allowed = {"parse": ["everyone"] if ping in ("here", "everyone") else [], "roles": [role] if ping == "buyers" and role else []}
    message = {"content": content, "embeds": [embed] if embed else [], "components": [{"type": 1, "components": buttons}] if buttons else [],
               "allowed_mentions": allowed}
    if poll:
        message["poll"] = poll
    return message


DISCORD_EMOJI = re.compile(r"^[^\s<>:/\\]{1,16}$")


def discord_send(channel, b, by="Rotaidem"):
    """Posts one message (and its reactions, pin and publishing). Returns (message id, error, published)."""
    message = discord_message(b, discord_channel_names())
    sent, error, hooked = discord_post_message(channel, message, discord_persona(b))
    if error:
        return None, error, False
    mid = sent["id"]
    for emoji in [e for e in str(b.get("reactions") or "").split() if DISCORD_EMOJI.match(e)][:5]:
        discord_call("PUT", f"/channels/{channel}/messages/{mid}/reactions/{urllib.parse.quote(emoji)}/@me")
    if b.get("pin"):
        discord_call("PUT", f"/channels/{channel}/pins/{mid}")
    published = False
    if b.get("publish"):
        _, perror, _ = discord_call("POST", f"/channels/{channel}/messages/{mid}/crosspost")
        published = not perror
    summary = ((message["embeds"][0].get("title") if message["embeds"] else "") or (message.get("poll") or {}).get("question", {}).get("text")
               or b.get("text") or "")[:120]
    posts = [{"id": mid, "channel_id": channel, "channel": str(b.get("channel_name") or "")[:100], "summary": summary, "at": utc_stamp(), "by": by,
              "hook": hooked, "draft": {k: b.get(k) for k in ("text", "embed", "buttons", "ping", "persona")}}] + discord_posts()
    set_setting("discord.posts", posts[:50])
    return mid, "", published


def discord_posts():
    return get_setting("discord.posts", []) or []


DISCORD_POST_FIELDS = ("channel_name", "text", "embed", "buttons", "ping", "poll", "reactions", "pin", "publish", "persona")


def discord_scheduled():
    return get_setting("discord.scheduled", []) or []


def discord_claim(key):
    """True for exactly one caller per key (two visits at the same moment must not post twice)."""
    try:
        db().run("INSERT INTO meta (key, value) VALUES (?, ?)", "discord.claim." + key, utc_stamp())
        return True
    except Exception:  # noqa: BLE001 - the key exists: someone else took it
        return False


def visible_posts():
    return db().all("SELECT * FROM posts WHERE status IS NULL OR status = 'published' OR (status = 'scheduled' AND publish_at <= ?) ORDER BY id", utc_stamp())


def discord_auto(kind, b):
    """An automatic post (product update, news, sale) in its chosen channel. Never fails the caller."""
    channel = discord_settings().get(kind + "_channel") if discord_ready() else ""
    if not channel:
        return
    try:
        mid, error, _ = discord_send(channel, b, "Automatic")
        if error:
            app.logger.warning("Automatic Discord post (%s): %s", kind, error)
        return mid
    except Exception:  # noqa: BLE001
        app.logger.exception("Automatic Discord post (%s) failed", kind)


def discord_announce_news():
    """News posts that are up and not announced yet go to the news channel (a few at a time)."""
    if not discord_ready() or not discord_settings()["news_channel"]:
        return
    done = set(get_setting("discord.announced_posts", []) or [])
    for post in [p for p in visible_posts() if p["id"] not in done][:3]:
        if not discord_claim(f"news.{post['id']}"):
            continue
        set_setting("discord.announced_posts", sorted(done | {post["id"]}))
        done.add(post["id"])
        mid = discord_auto("news", {"embed": {"title": post["title"], "description": str(post["content"] or "")[:600], "url": "/newsletter",
                                              "image": post.get("image_url") or ""}, "buttons": [{"label": "Read on rotaidem.com", "url": "/newsletter"}]})
        if mid and discord_extra()["news_threads"]:  # a thread under the post to talk about it
            discord_call("POST", f"/channels/{discord_settings()['news_channel']}/messages/{mid}/threads",
                         payload={"name": ("💬 " + str(post["title"] or "News"))[:100], "auto_archive_duration": 4320})


def discord_run_due():
    """Scheduled Discord posts and scheduled news run at the first request after their time (no cron on STRATO)."""
    if not DISCORD_BOT_TOKEN:
        return
    now = time.time()
    last = get_setting("discord.checked", 0) or 0
    if now - float(last) < 60:
        return
    set_setting("discord.checked", now)
    if not discord_ready():
        return
    stamp = utc_stamp()
    for item in [i for i in discord_scheduled() if i["at"] <= stamp]:
        set_setting("discord.scheduled", [i for i in discord_scheduled() if i["id"] != item["id"]])
        if not discord_claim("post." + item["id"]):
            continue
        try:
            post = item["post"]
            _, error, _ = discord_send(post["channel_id"], post, item.get("by") or "Scheduled")
            if error:
                app.logger.warning("Scheduled Discord post %s: %s", item["id"], error)
        except Exception:  # noqa: BLE001
            app.logger.exception("Scheduled Discord post %s failed", item["id"])
    discord_announce_news()
    discord_weekly_report()
    discord_daily()
    discord_jobs()


@app.get("/api/admin/discord/channels")
def admin_discord_channels():
    """The server's text and announcement channels, for the channel list when posting."""
    if not staff_user():
        return err("Admin only", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    rows, error, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}/channels")
    if error:
        return err(error, 502)
    categories = {c["id"]: c.get("name", "") for c in rows or [] if c.get("type") == 4}
    guild = discord_settings()["guild_id"]
    channels = [{"id": c["id"], "name": c.get("name", ""), "announcement": c.get("type") == 5, "category": categories.get(c.get("parent_id"), ""),
                 "position": c.get("position", 0), "slowmode": int(c.get("rate_limit_per_user") or 0),
                 "locked": any(o.get("id") == guild and int(o.get("deny") or 0) & DISCORD_SEND for o in c.get("permission_overwrites") or [])}
                for c in rows or [] if c.get("type") in (0, 5)]
    channels.sort(key=lambda c: (c["category"], c["position"]))
    voice = [{"id": c["id"], "name": c.get("name", "")} for c in rows or [] if c.get("type") in (2, 13)]
    return jsonify({"channels": channels, "voice": voice, "categories": [{"id": k, "name": v} for k, v in categories.items()]})


@app.get("/api/admin/discord/messages")
def admin_discord_messages():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"messages": discord_posts(), "scheduled": discord_scheduled()})


@app.post("/api/admin/discord/scheduled/<item_id>/now")
def admin_discord_post_now(item_id):
    """A scheduled post, sent straight away."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can post in Discord", 403)
    item = next((i for i in discord_scheduled() if i["id"] == item_id), None)
    if not item or not discord_claim("post." + item_id):
        return err("That post is not waiting any more", 404)
    set_setting("discord.scheduled", [i for i in discord_scheduled() if i["id"] != item_id])
    try:
        mid, error, _ = discord_send(item["post"]["channel_id"], item["post"], user["name"])
    except ValueError as exc:
        return err(str(exc), 400)
    if error:
        return err(error, 502)
    audit(user, "discord_message", "scheduled post sent now")
    return jsonify({"ok": True, "id": mid})


@app.delete("/api/admin/discord/scheduled/<item_id>")
def admin_discord_unschedule(item_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    set_setting("discord.scheduled", [i for i in discord_scheduled() if i["id"] != item_id])
    audit(user, "discord_unschedule", item_id)
    return jsonify({"ok": True})


@app.post("/api/admin/discord/message")
def admin_discord_send():
    """Post a message with the bot (and publish it to following servers in an announcement channel)."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can post in Discord", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    b = body()
    channel = str(b.get("channel_id") or "").strip()
    if not SNOWFLAKE.match(channel):
        return err("Choose the channel to post in", 400)
    try:
        discord_message(b)  # check it now, also when it is sent later
    except ValueError as exc:
        return err(str(exc), 400)
    at = str(b.get("at") or "").strip()
    if at:  # post later: from the admin as an ISO time in UTC
        try:
            when = datetime.strptime(at[:16], "%Y-%m-%dT%H:%M").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return err("Pick the date and time to post it", 400)
        if when <= utc_stamp():
            return err("Pick a time in the future, or post it now", 400)
        item = {"id": secrets.token_hex(6), "at": when, "by": user["name"], "post": {**{k: b.get(k) for k in DISCORD_POST_FIELDS}, "channel_id": channel}}
        set_setting("discord.scheduled", sorted(discord_scheduled() + [item], key=lambda i: i["at"])[:50])
        audit(user, "discord_schedule", f"#{b.get('channel_name') or channel} at {when} UTC")
        return jsonify({"ok": True, "scheduled": item["id"], "at": when})
    try:
        mid, error, published = discord_send(channel, b, user["name"])
    except ValueError as exc:
        return err(str(exc), 400)
    if error:
        return err(error, 502)
    audit(user, "discord_message", f"#{b.get('channel_name') or channel}: {discord_posts()[0]['summary'][:60]}")
    return jsonify({"ok": True, "id": mid, "published": published})


@app.post("/api/admin/discord/message/<channel>/<message_id>")
def admin_discord_edit(channel, message_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change Discord posts", 403)
    if not discord_ready() or not SNOWFLAKE.match(channel) or not SNOWFLAKE.match(message_id):
        return err("Message not found", 404)
    b = body()
    try:
        message = discord_message(b, discord_channel_names())
    except ValueError as exc:
        return err(str(exc), 400)
    message.pop("poll", None)  # a poll cannot be changed once it is posted
    old = next((p for p in discord_posts() if p["id"] == message_id), {})
    path = discord_hook_path(channel, message_id) if old.get("hook") else f"/channels/{channel}/messages/{message_id}"
    _, error, status = discord_call("PATCH", path or f"/channels/{channel}/messages/{message_id}", payload=message)
    if error:
        return err("That message is gone from Discord" if status == 404 else error, 404 if status == 404 else 502)
    summary = ((message["embeds"][0].get("title") if message["embeds"] else "") or b.get("text") or "")[:120]
    set_setting("discord.posts", [{**p, "summary": summary, "draft": {k: b.get(k) for k in ("text", "embed", "buttons", "ping")}}
                                  if p["id"] == message_id else p for p in discord_posts()])
    audit(user, "discord_edit", summary[:60])
    return jsonify({"ok": True})


@app.delete("/api/admin/discord/message/<channel>/<message_id>")
def admin_discord_delete(channel, message_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can delete Discord posts", 403)
    if not discord_ready() or not SNOWFLAKE.match(channel) or not SNOWFLAKE.match(message_id):
        return err("Message not found", 404)
    old = next((p for p in discord_posts() if p["id"] == message_id), {})
    path = (discord_hook_path(channel, message_id) if old.get("hook") else "") or f"/channels/{channel}/messages/{message_id}"
    _, error, status = discord_call("DELETE", path)
    if error and status != 404:  # already gone from Discord: just forget it here
        return err(error, 502)
    set_setting("discord.posts", [p for p in discord_posts() if p["id"] != message_id])
    audit(user, "discord_delete", message_id)
    return jsonify({"ok": True})


# ---------- more Discord: slash commands, team feed, weekly report, invites, events, DMs, giveaways ----------
# Still no bot running all the time: slash commands and the /support form reach /api/discord/interactions,
# the team feed and weekly report are posts made when something happens on the site.

DISCORD_COMMANDS = [
    {"name": "verify", "type": 1, "description": "Get the buyer channels: checks your Rotaidem licence"},
    {"name": "status", "type": 1, "description": "Is everything working? Website, licences, downloads"},
    {"name": "version", "type": 1, "description": "The latest version of each Rotaidem product"},
    {"name": "licence", "type": 1, "description": "Your licence keys and how many computers use them (only you see it)"},
    {"name": "download", "type": 1, "description": "Where to download Rotaidem products"},
    {"name": "profile", "type": 1, "description": "Look up a shared HeavyPro feel profile",
     "options": [{"type": 3, "name": "name", "description": "The profile's name", "required": True, "max_length": 60}]},
    {"name": "support", "type": 1, "description": "Send a message to Rotaidem support"},
    {"name": "ticket", "type": 1, "description": "Open a private support ticket with the team"},
    {"name": "account", "type": 1, "description": "Your Rotaidem account: orders, licences and settings"},
    {"name": "store", "type": 1, "description": "Where to buy Rotaidem products"},
    {"name": "roadmap", "type": 1, "description": "What Rotaidem is working on next"},
    {"name": "faq", "type": 1, "description": "Answers to common questions, and the install guide",
     "options": [{"type": 3, "name": "question", "description": "Pick a question to show its answer", "required": False, "autocomplete": True, "max_length": 100}]},
    {"name": "socials", "type": 1, "description": "Rotaidem's website, reviews and social links"},
    {"name": "help", "type": 1, "description": "What the Rotaidem bot can do"},
]


def discord_link_row(*buttons):
    return [{"type": 1, "components": [{"type": 2, "style": 5, "label": label, "url": site_url() + path} for label, path in buttons]}]


def discord_reply(content="", embeds=None, components=None, public=False):
    data = {"content": content[:2000], "embeds": embeds or [], "components": components or [], "allowed_mentions": {"parse": []}}
    if not public:
        data["flags"] = 64  # only the member who asked sees it
    return {"type": 4, "data": data}


def discord_member(event):
    who = (event.get("member") or {}).get("user") or event.get("user") or {}
    discord_id = str(who.get("id") or "")
    user = db().first("SELECT * FROM users WHERE discord_id = ?", discord_id) if SNOWFLAKE.match(discord_id) else None
    return discord_id, str(who.get("global_name") or who.get("username") or "Discord member")[:100], user


def discord_cmd_status(event):
    last, history = get_setting("status.last", None), get_setting("status.history", []) or []
    if not last:
        return discord_reply("Not checked yet. Open the live status page to run the checks.", components=discord_link_row(("Live status", "/status")))
    a = status_answer(last, history)
    head = {"ok": "🟢 All systems working", "degraded": "🟠 Some services have problems", "down": "🔴 Major outage"}[a["overall"]]
    lines = [f"{'🟢' if c['state'] == 'ok' else '🔴' if c['state'] == 'down' else '⚪'} {c['name']}" for c in a["checks"] if c["state"] != "off"]
    minutes = max(0, int((time.time() - float(last["at"])) // 60))
    return discord_reply(embeds=[{"title": head, "description": "\n".join(lines), "color": 0x4CC38A if a["overall"] == "ok" else 0xE5484D,
                                  "footer": {"text": f"Checked {minutes} min ago"}}], components=discord_link_row(("Live status", "/status")))


def discord_cmd_version(event):
    rows = db().all("SELECT name, version, changelog FROM products WHERE status = 'published' AND version IS NOT NULL AND version <> '' ORDER BY sort_order, name")[:5]
    if not rows:
        return discord_reply("No product versions are published yet.")
    fields = []
    for r in rows:
        try:
            notes = (json.loads(r["changelog"] or "[]") or [{}])[0].get("notes") or ""
        except (ValueError, TypeError, AttributeError):
            notes = ""
        fields.append({"name": f"{r['name']} {r['version']}", "value": (str(notes)[:300] or "No notes for this version.")})
    return discord_reply(embeds=[{"title": "Latest versions", "fields": fields, "color": 0xAAB6C2}],
                         components=discord_link_row(("Downloads", "/downloads"), ("Get RotaHub", "/get-rotahub")))


def discord_cmd_licence(event):
    _, _, user = discord_member(event)
    if not user:
        return discord_reply("Link your Discord on your Rotaidem account first (My account, Discord).", components=discord_link_row(("My account", "/account#discord")))
    rows = db().all("SELECT id, license_key, product_id, status, max_activations, (SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS used "
                    "FROM licenses WHERE customer_id = ? OR LOWER(email) = ? ORDER BY id DESC", user["id"], (user.get("email") or "").lower())
    if not rows:
        return discord_reply("Your account has no licence keys yet. Bought with another email? Add the key under My account.",
                             components=discord_link_row(("My account", "/account")))
    lines = [f"`{r['license_key'][:5]}••••-••••-{r['license_key'][-4:]}` {r['product_id']}: {r['used']}/{r['max_activations']} computers"
             f"{'' if r['status'] == 'active' else ' (revoked)'}" for r in rows[:10]]
    return discord_reply("Your licences (only you can see this):\n" + "\n".join(lines) + "\nFull keys are under My account, or press the button.",
                         components=discord_link_row(("My account", "/account")) + [{"type": 1, "components": [
                             {"type": 2, "style": 2, "label": "Send me the full keys by DM", "emoji": {"name": "🔑"}, "custom_id": "keys_dm"}]}])


def discord_cmd_download(event):
    return discord_reply("Rotaidem products install and update through RotaHub.",
                         components=discord_link_row(("Get RotaHub", "/get-rotahub"), ("Downloads", "/downloads"), ("Installation guide", "/installation")))


def discord_cmd_profile(event):
    opts = {o.get("name"): o.get("value") for o in (event.get("data") or {}).get("options") or []}
    name = str(opts.get("name") or "").strip()[:60]
    row = db().first("SELECT name, downloads, updated_at FROM shared_profiles WHERE name_key = ?", profile_key(name)) if name else None
    if not row:
        return discord_reply(f"No shared profile is called **{name or '?'}**. Names are exact, ignoring capitals.")
    return discord_reply(embeds=[{"title": f"Feel profile: {row['name']}", "color": 0xAAB6C2,
                                  "description": f"Fetched {row['downloads'] or 0} times, last changed {str(row['updated_at'] or '')[:10]}.\n"
                                                 f"In HeavyPro: Profiles, Get a shared profile, type **{row['name']}**."}], public=True)


def discord_cmd_support(event):
    field = lambda cid, label, style, most: {"type": 1, "components": [  # noqa: E731
        {"type": 4, "custom_id": cid, "label": label, "style": style, "max_length": most, "required": True}]}
    return {"type": 9, "data": {"custom_id": "support", "title": "Message Rotaidem support",
                                "components": [field("subject", "Subject", 1, 100), field("message", "How can we help?", 2, 2000)]}}


def discord_cmd_help(event):
    lines = [f"`/{c['name']}` {c['description']}" for c in discord_commands() if c.get("type", 1) == 1 and "default_member_permissions" not in c]
    return discord_reply("\n".join(lines), components=discord_link_row(("Help and FAQ", "/support"), ("Contact", "/contact")))


def discord_cmd_ticket(event):
    return discord_cmd_support(event)  # /ticket opens the same private-ticket form as /support


def discord_cmd_account(event):
    return discord_reply("Your Rotaidem account — orders, licence keys and settings:", components=discord_link_row(("My account", "/account")))


def discord_cmd_store(event):
    return discord_reply("Get Rotaidem add-ons for Microsoft Flight Simulator:", components=discord_link_row(("All products", "/addons/"), ("HeavyPro", "/heavypro")))


def discord_cmd_roadmap(event):
    return discord_reply("What we're building next:", components=discord_link_row(("Roadmap", "/roadmap"), ("Latest news", "/newsletter")))


def discord_cmd_socials(event):
    labels = {"discord": "Discord", "youtube": "YouTube", "instagram": "Instagram", "x": "X", "facebook": "Facebook", "twitch": "Twitch", "tiktok": "TikTok"}
    buttons = [{"type": 2, "style": 5, "label": labels.get(k, k), "url": u} for k, u in list((site_config().get("socials") or {}).items())[:5]]
    rows = ([{"type": 1, "components": buttons}] if buttons else []) + discord_link_row(("Website", "/"))
    return discord_reply("Find Rotaidem here:", components=rows)


def discord_support_submit(event):
    discord_id, name, user = discord_member(event)
    values = {c["custom_id"]: str(c.get("value") or "").strip() for row in (event.get("data") or {}).get("components") or []
              for c in row.get("components") or []}
    if not values.get("message"):
        return discord_reply("The message was empty, so nothing was sent.")
    limit = int(discord_extra()["ticket_limit"] or 0)
    if limit and discord_settings().get("tickets_channel"):
        mine = ditems("ticket", status="open", member=discord_id, limit=10)
        if len(mine) >= limit:
            return discord_reply(f"You already have an open ticket: <#{mine[0]['ref']}>. Please write there, and we will answer as soon as we can.")
    if not discord_claim(f"support.{discord_id}.{int(time.time()) // 600}"):
        return discord_reply("You sent a message a moment ago. We will answer it; please wait a little before sending another.")
    email = (user or {}).get("email") or f"Discord: {name}"
    text = f"[Discord /support] {values.get('subject', '')}\n\n{values['message']}"[:5000]
    db().run("INSERT INTO messages (name, email, message) VALUES (?, ?, ?)", (user or {}).get("name") or name, email, text)
    cfg, topic = discord_settings(), ""
    cid = str((event.get("data") or {}).get("custom_id") or "")
    if cid.startswith("support:"):
        try:
            topic = discord_extra()["ticket_categories"][int(cid[8:])]
        except (ValueError, IndexError):
            topic = ""
    if cfg.get("tickets_channel") and discord_ready() and SNOWFLAKE.match(discord_id) and discord_open_ticket(cfg["tickets_channel"], discord_id, name, values.get("subject", ""), values["message"], topic):
        return discord_reply("Thanks — we opened a private ticket thread for you here. Only you and the Rotaidem team can see it, and you'll be pinged when we reply.")
    discord_team_message(name, values.get("subject") or values["message"], "Discord /support")
    return discord_reply("Thanks, we got your message." + ("" if user else " Link Discord on your Rotaidem account so we can answer by email too."))


def discord_open_ticket(channel, discord_id, name, subject, message, topic=""):
    """Open a private support thread, add the member and post their message. Staff with Manage Threads
    see every private thread. Returns True when the thread was created. Never raises."""
    try:
        title = ("🎫 " + (f"{topic} · " if topic else "") + (str(subject).strip() or str(name).strip() or "Support"))[:90]
        thread, error, _ = discord_call("POST", f"/channels/{channel}/threads", payload={"name": title, "type": 12, "invitable": False})
        if error or not thread:
            app.logger.warning("Ticket thread for %s: %s", name, error)
            return False
        body = (f"New support ticket from <@{discord_id}>.\n" + (f"**Topic:** {topic}\n" if topic else "")
                + f"**Subject:** {str(subject).strip() or '(none)'}\n\n{str(message)[:1700]}")
        close = [{"type": 1, "components": [{"type": 2, "style": 4, "label": "Close ticket", "emoji": {"name": "🔒"}, "custom_id": "close_ticket"},
                                            {"type": 2, "style": 1, "label": "Claim", "emoji": {"name": "🙋"}, "custom_id": "claim_ticket"}]}]
        discord_call("POST", f"/channels/{thread['id']}/messages", payload={"content": body[:2000], "components": close, "allowed_mentions": {"users": [discord_id]}})
        ditem_add("ticket", {"title": title, "name": str(name)[:80], "topic": topic}, ref=thread["id"], member=discord_id, status="open")
        return True
    except Exception:  # noqa: BLE001 - a member never sees a ticketing problem; the inbox already has the message
        app.logger.exception("Opening ticket thread failed for %s", name)
        return False


def discord_close_ticket(event):
    """A member or staff pressed Close ticket in a thread: post a note, then archive and lock it."""
    channel = str(event.get("channel_id") or "")
    _, name, _ = discord_member(event)
    if not SNOWFLAKE.match(channel):
        return discord_reply("This button only works inside a ticket.")
    discord_ticket_close(channel, (event.get("channel") or {}).get("name") or "", name)
    return {"type": 6}  # acknowledge the button without posting another message (keeps the thread archived)


DISCORD_HANDLERS = {"status": discord_cmd_status, "version": discord_cmd_version, "licence": discord_cmd_licence, "download": discord_cmd_download,
                    "profile": discord_cmd_profile, "support": discord_cmd_support, "ticket": discord_cmd_ticket, "account": discord_cmd_account,
                    "store": discord_cmd_store, "roadmap": discord_cmd_roadmap, "faq": lambda e: discord_cmd_faq(e), "socials": discord_cmd_socials, "help": discord_cmd_help,
                    "tag": lambda e: discord_cmd_tag(e), "remind": lambda e: discord_cmd_remind(e), "suggest": lambda e: discord_cmd_suggest(e),
                    "Report to staff": lambda e: discord_cmd_report(e), "orders": lambda e: discord_cmd_orders(e)}


def discord_interaction(event):
    """Slash commands (except /verify), the /support form, and button clicks. Fast: no slow calls except
    the ticket buttons, which do one or two Discord calls like /support already does."""
    data = event.get("data") or {}
    discord_count_use(event)
    try:
        cid = str(data.get("custom_id") or "")
        if event.get("type") == 4:  # Discord asks for suggestions while someone types /tag or /faq
            return discord_faq_autocomplete(event) if data.get("name") == "faq" else discord_tag_autocomplete(event)
        if event.get("type") == 5:  # a form was sent
            if cid == "support" or cid.startswith("support:"):
                return discord_support_submit(event)
            if cid == "suggest":
                return discord_suggest_submit(event)
            if cid.startswith("form:"):
                return discord_form_submit(event, cid[5:])
            return discord_reply("That form is not active any more.")
        if event.get("type") == 3:  # a button was clicked or a menu choice made
            if cid == "open_ticket":
                return discord_cmd_support(event)   # show the ticket form
            if cid == "ticket_cat":
                return discord_ticket_category(event, (data.get("values") or [""])[0])
            if cid == "close_ticket":
                return discord_close_ticket(event)
            if cid == "claim_ticket":
                return discord_claim_ticket(event)
            if cid.startswith("role:"):
                return discord_role_button(event, cid[5:])
            if cid.startswith("gw:"):
                return discord_giveaway_enter(event, cid[3:])
            if cid.startswith("form:"):
                return discord_form_open(event, cid[5:])
            if cid == "rules_accept":
                return discord_rules_accept(event)
            if cid == "keys_dm":
                return discord_keys_dm(event)
            if cid.startswith("rate:") and cid.count(":") == 2:
                return discord_ticket_rate(event, *cid[5:].split(":"))
            return discord_reply("That button is not active any more.")
        if data.get("name") in DISCORD_STAFF_BITS:
            return discord_cmd_staff(event)
        handler = DISCORD_HANDLERS.get(data.get("name"))
        if handler:
            return handler(event)
        custom = next((c for c in discord_custom_commands() if c["name"] == data.get("name")), None)
        return discord_custom_reply(custom) if custom else discord_reply("I do not know that one. Try /help.")
    except Exception:  # noqa: BLE001 - the member gets an answer, the log gets the reason
        app.logger.exception("Discord command %s failed", data.get("name") or data.get("custom_id"))
        return discord_reply("Something went wrong on our side. Please try again in a minute.")


def discord_team_message(who, text, source):
    """A short note in the team channel about a new message. Never fails the caller."""
    first = " ".join(str(text or "").split())[:180]
    discord_auto("team", {"text": f"📨 New message from **{str(who)[:80]}** ({source}): {first}", "buttons": [{"label": "Open inbox", "url": "/admin#messages"}]})


def discord_welcome(discord_id, buyer):
    note = " You have the Verified buyer role: the buyer channels are open." if buyer else " Type /help to see what the Rotaidem bot can do."
    template = discord_extra()["welcome_text"] or "👋 Welcome {member}!{buyer}"
    discord_auto("welcome", {"text": discord_render(template, member=f"<@{discord_id}>", buyer=note)[:1900]})


def discord_weekly_report():
    """Monday from 08:00 UTC: last week's numbers in the team channel, once per week."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 0 or now.hour < 8 or not discord_settings()["team_channel"]:
        return
    year, week, _ = now.isocalendar()
    if not discord_claim(f"weekly.{year}.{week}"):
        return
    since = utc_stamp(7 * 86400)
    n = lambda sql, *args: int((db().first(sql, *args) or {}).get("n") or 0)  # noqa: E731
    money = db().all("SELECT COALESCE(currency, '') AS currency, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM orders "
                     "WHERE created_at >= ? AND payment_status = 'Paid' GROUP BY currency", since)
    sales = ", ".join(f"{float(r['total']):.2f} {(r['currency'] or 'usd').upper()}" for r in money) or "none"
    accounts = n("SELECT COUNT(*) AS n FROM users WHERE created_at >= ?", since)
    downloads = n("SELECT COUNT(*) AS n FROM downloads WHERE created_at >= ?", since)
    activations = n("SELECT COUNT(*) AS n FROM license_activation_log WHERE created_at >= ?", since)
    open_messages = n("SELECT COUNT(*) AS n FROM messages WHERE handled = 0 OR handled IS NULL")
    linked = n("SELECT COUNT(*) AS n FROM users WHERE discord_id IS NOT NULL AND discord_id <> ''")
    lines = [f"🛒 Orders: **{sum(int(r['n']) for r in money)}** ({sales})", f"🧑‍✈️ New accounts: **{accounts}**",
             f"⬇️ Downloads: **{downloads}**", f"🔑 New computers activated: **{activations}**",
             f"📨 Open messages: **{open_messages}**", f"🔗 Discord accounts linked: **{linked}**"]
    discord_auto("team", {"embed": {"title": f"Weekly report, week {week}", "description": "\n".join(lines), "color": "#aab6c2"},
                          "buttons": [{"label": "Open the admin", "url": "/admin"}]})


@app.post("/api/admin/discord/invite")
def admin_discord_invite():
    """An invite link to a channel: how long it works and how many people may use it."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    b = body()
    channel = str(b.get("channel_id") or "")
    if not SNOWFLAKE.match(channel):
        return err("Choose a channel", 400)
    age, uses = clamp_int(b.get("max_age"), 86400, 0, 604800), clamp_int(b.get("max_uses"), 0, 0, 100)
    invite, error, _ = discord_call("POST", f"/channels/{channel}/invites", payload={"max_age": age, "max_uses": uses, "unique": True})
    if error:
        return err(error, 502)
    audit(user, "discord_invite", f"{invite.get('code')} ({age // 3600} h, {uses or 'any'} uses)")
    return jsonify({"ok": True, "url": "https://discord.gg/" + str(invite.get("code") or "")})


@app.get("/api/admin/discord/events")
def admin_discord_events():
    if not staff_user():
        return err("Admin only", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    events, error, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}/scheduled-events?with_user_count=true", timeout=10)
    if error:
        return err(error, 502)
    return jsonify({"events": [{k: e.get(k) for k in ("id", "name", "description", "scheduled_start_time", "scheduled_end_time", "user_count")}
                               | {"location": (e.get("entity_metadata") or {}).get("location", "")} for e in events or []]})


@app.post("/api/admin/discord/events")
def admin_discord_event_create():
    """A server event (for example a group flight): shows at the top of the server and members can say they are interested."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    b = body()
    name, location = str(b.get("name") or "").strip()[:100], str(b.get("location") or "").strip()[:100]
    try:
        start = datetime.fromisoformat(str(b.get("start") or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(b.get("end") or "").replace("Z", "+00:00"))
    except ValueError:
        return err("Choose when it starts and ends", 400)
    if not name or not location:
        return err("Give the event a name and a place (e.g. EGLL to KJFK, or VATSIM)", 400)
    if start.tzinfo is None or start <= datetime.now(timezone.utc) or end <= start:
        return err("The start must be in the future and the end after the start", 400)
    payload = {"name": name, "description": str(b.get("description") or "")[:1000], "privacy_level": 2, "entity_type": 3,
               "entity_metadata": {"location": location}, "scheduled_start_time": start.isoformat(), "scheduled_end_time": end.isoformat()}
    event, error, _ = discord_call("POST", f"/guilds/{discord_settings()['guild_id']}/scheduled-events", payload=payload)
    if error:
        return err(error + (" The bot needs Manage Events: invite it again with the button under Set-up." if "Missing Permissions" in error else ""), 502)
    audit(user, "discord_event", name)
    return jsonify({"ok": True, "id": event.get("id")})


@app.delete("/api/admin/discord/events/<event_id>")
def admin_discord_event_delete(event_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not SNOWFLAKE.match(event_id) or not discord_ready():
        return err("Unknown event", 400)
    _, error, _ = discord_call("DELETE", f"/guilds/{discord_settings()['guild_id']}/scheduled-events/{event_id}")
    if error:
        return err(error, 502)
    audit(user, "discord_event_delete", event_id)
    return jsonify({"ok": True})


@app.post("/api/admin/discord/dm")
def admin_discord_dm():
    """A direct message from the bot to a member who linked Discord (they must share the server with the bot)."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    b = body()
    member = db().first("SELECT id, name, discord_id, discord_name FROM users WHERE id = ?", clamp_int(b.get("user_id"), 0, 0, 2 ** 31))
    text = str(b.get("text") or "").strip()[:1900]
    if not member or not member.get("discord_id"):
        return err("That account has not linked Discord", 400)
    if not text:
        return err("Write the message", 400)
    dm, error, _ = discord_call("POST", "/users/@me/channels", payload={"recipient_id": member["discord_id"]})
    if not error:
        _, error, _ = discord_call("POST", f"/channels/{dm['id']}/messages", payload={"content": text, "allowed_mentions": {"parse": []}})
    if error:
        return err(error + (" They may have DMs from server members turned off." if "50007" in error or "Cannot send" in error else ""), 502)
    audit(user, "discord_dm", member["discord_name"] or member["discord_id"])
    return jsonify({"ok": True})


DISCORD_MESSAGE_LINK = re.compile(r"/channels/\d+/(\d{15,22})/(\d{15,22})")


@app.post("/api/admin/discord/giveaway")
def admin_discord_giveaway():
    """Fair random winners among everyone who reacted to a message with an emoji; the bot can announce them."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    b = body()
    m = DISCORD_MESSAGE_LINK.search(str(b.get("link") or ""))
    emoji = str(b.get("emoji") or "").strip()
    if not m:
        return err("Paste the message link: right-click the message in Discord, Copy Message Link", 400)
    if not emoji or len(emoji) > 64:
        return err("Which reaction counts? Paste the emoji, e.g. 🎉", 400)
    channel, message = m.groups()
    people, after = {}, "0"
    for _ in range(10):  # up to 1000 entries
        page, error, _ = discord_call("GET", f"/channels/{channel}/messages/{message}/reactions/{urllib.parse.quote(emoji)}?limit=100&after={after}")
        if error:
            return err(error, 502)
        for u in page or []:
            if not u.get("bot"):
                people[u["id"]] = u.get("global_name") or u.get("username") or u["id"]
        if len(page or []) < 100:
            break
        after = page[-1]["id"]
    count = clamp_int(b.get("winners"), 1, 1, 20)
    if not people:
        return err("Nobody (except bots) reacted with that emoji yet", 400)
    winners = secrets.SystemRandom().sample(sorted(people), min(count, len(people)))
    if b.get("announce"):
        prize = str(b.get("prize") or "").strip()[:200]
        text = f"🎉 Congratulations {', '.join(f'<@{w}>' for w in winners)}!" + (f" You won **{prize}**." if prize else "") + f" ({len(people)} entered)"
        _, error, _ = discord_call("POST", f"/channels/{channel}/messages", payload={
            "content": text, "message_reference": {"message_id": message, "fail_if_not_exists": False}, "allowed_mentions": {"users": winners}})
        if error:
            return err("Winners picked but not announced: " + error, 502)
    audit(user, "discord_giveaway", f"{len(winners)} of {len(people)}: " + ", ".join(people[w] for w in winners))
    return jsonify({"ok": True, "entered": len(people), "winners": [{"id": w, "name": people[w]} for w in winners]})


# ---------- Discord tools: tickets, role buttons, own commands, moderation, clean-up, channels, automation ----------
# All through Discord's API, like the rest: nothing has to keep running on STRATO.

DISCORD_SEND = 2048 | (1 << 38)  # Send Messages, Send Messages in Threads: what a locked channel takes away from @everyone
DISCORD_EPOCH_MS = 1420070400000
# Roles with any of these are never handed out by a role button: Administrator, Manage Server, Manage Roles, Kick, Ban,
# Manage Channels, Manage Messages, Timeout members, Manage Webhooks, Manage Nicknames, Manage Threads, Mention @everyone.
DISCORD_RISKY = 8 | 32 | (1 << 28) | 2 | 4 | 16 | 8192 | (1 << 40) | (1 << 29) | (1 << 27) | (1 << 34) | (1 << 17)
DISCORD_MILESTONES = (25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000)
DISCORD_CMD_NAME = re.compile(r"^[a-z0-9_-]{1,32}$")


def discord_perm_hint(error):
    """A Missing Permissions answer: the bot was invited before it needed this. Say how to fix it."""
    return error + (" The bot needs more permissions for this: press Invite the bot (under Set-up) and accept, or give its role the permission in Server Settings."
                    if "Missing Permissions" in error or "50013" in error else "")


def snowflake_time(sid):
    try:
        return datetime.fromtimestamp(((int(sid) >> 22) + DISCORD_EPOCH_MS) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


# 1-2. Support tickets from the admin: list, read, reply, close (with a transcript in the inbox), reopen

def discord_ticket_transcript(thread_id, title, closed_by):
    """Keep a closed ticket's conversation in the website inbox (marked handled). Returns the text ('' if none). Never raises."""
    try:
        rows, error, _ = discord_call("GET", f"/channels/{thread_id}/messages?limit=100", timeout=8)
        if error or not rows or not discord_claim(f"transcript.{thread_id}.{rows[0]['id']}"):
            return ""
        lines = []
        for m in reversed(rows):
            who = (m.get("author") or {}).get("global_name") or (m.get("author") or {}).get("username") or "?"
            text = (m.get("content") or " ".join((e.get("title") or "") for e in m.get("embeds") or [])).strip()
            if text:
                lines.append(f"[{snowflake_time(m['id'])[:16]}] {who}: {text}")
        body = f"[Discord ticket, closed by {closed_by}] {title}\n\n" + "\n".join(lines)
        db().run("INSERT INTO messages (name, email, message, handled) VALUES (?, ?, ?, 1)", (title or "Discord ticket")[:100], "Discord ticket", body[:15000])
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - closing a ticket never fails over the copy
        app.logger.exception("Saving the ticket transcript failed")
        return ""


def discord_ticket_close(thread_id, title, by_name):
    """Close note, archive and lock, and the transcript into the inbox."""
    try:
        if not title:
            info, _, _ = discord_call("GET", f"/channels/{thread_id}", timeout=8)
            title = (info or {}).get("name") or "Discord ticket"
        discord_call("POST", f"/channels/{thread_id}/messages", payload={
            "content": f"🔒 Ticket closed by **{by_name}**. Open a new one any time with **/support** or the ticket button.", "allowed_mentions": {"parse": []}})
        transcript = discord_ticket_transcript(thread_id, title, by_name)
        _, error, _ = discord_call("PATCH", f"/channels/{thread_id}", payload={"archived": True, "locked": True})
        t = ditem_row(db().first("SELECT * FROM discord_items WHERE kind = 'ticket' AND ref = ? AND status = 'open'", thread_id))
        if t:  # the rating and the email go out at the next visit (a button answer must be quick)
            ditem_save(t, status="followup", transcript=transcript[:12000], closed_by=by_name)
        return error
    except Exception:  # noqa: BLE001
        app.logger.exception("Closing ticket failed")
        return "Could not close the ticket"


def discord_ticket_thread(thread_id):
    """The thread, only when it is a ticket (a private thread in the tickets channel)."""
    channel = discord_settings().get("tickets_channel")
    if not channel or not SNOWFLAKE.match(str(thread_id)):
        return None
    info, _, _ = discord_call("GET", f"/channels/{thread_id}", timeout=8)
    return info if info and info.get("parent_id") == channel and info.get("type") == 12 else None


def discord_ticket_row(t, is_open):
    meta = t.get("thread_metadata") or {}
    return {"id": t["id"], "name": t.get("name", ""), "open": is_open, "messages": t.get("message_count") or 0,
            "opened": (meta.get("create_timestamp") or snowflake_time(t["id"]))[:19].replace("T", " "),
            "closed": "" if is_open else str(meta.get("archive_timestamp") or "")[:19].replace("T", " ")}


@app.get("/api/admin/discord/tickets")
def admin_discord_tickets():
    if not staff_user():
        return err("Admin only", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    cfg = discord_settings()
    channel = cfg.get("tickets_channel")
    if not channel:
        return jsonify({"tickets": [], "channel": ""})
    active, error, _ = discord_call("GET", f"/guilds/{cfg['guild_id']}/threads/active", timeout=10)
    if error:
        return err(discord_perm_hint(error), 502)
    tickets = [discord_ticket_row(t, True) for t in (active or {}).get("threads") or [] if t.get("parent_id") == channel and t.get("type") == 12]
    closed, _, _ = discord_call("GET", f"/channels/{channel}/threads/archived/private?limit=25", timeout=10)
    tickets += [discord_ticket_row(t, False) for t in (closed or {}).get("threads") or []]
    return jsonify({"tickets": tickets, "channel": channel})


@app.get("/api/admin/discord/tickets/<thread_id>")
def admin_discord_ticket(thread_id):
    if not staff_user():
        return err("Admin only", 403)
    if not discord_ready() or not discord_ticket_thread(thread_id):
        return err("Ticket not found", 404)
    rows, error, _ = discord_call("GET", f"/channels/{thread_id}/messages?limit=50", timeout=10)
    if error:
        return err(error, 502)
    out = [{"id": m["id"], "at": snowflake_time(m["id"]), "bot": bool((m.get("author") or {}).get("bot")),
            "who": (m.get("author") or {}).get("global_name") or (m.get("author") or {}).get("username") or "?",
            "text": m.get("content") or " ".join((e.get("title") or "") + " " + (e.get("description") or "") for e in m.get("embeds") or []).strip()}
           for m in reversed(rows or [])]
    return jsonify({"messages": out})


@app.post("/api/admin/discord/tickets/<thread_id>/<action>")
def admin_discord_ticket_action(thread_id, action):
    """reply (the bot posts the staff member's answer), close or reopen."""
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    if action not in ("reply", "close", "reopen"):
        return err("Not found", 404)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    thread = discord_ticket_thread(thread_id)
    if not thread:
        return err("Ticket not found", 404)
    closed = bool((thread.get("thread_metadata") or {}).get("archived"))
    if action == "reply":
        text = str(body().get("text") or "").strip()[:1800]
        if not text:
            return err("Write your answer", 400)
        if closed:
            return err("This ticket is closed. Reopen it first.", 400)
        _, error, _ = discord_call("POST", f"/channels/{thread_id}/messages", payload={
            "content": f"**{str(user.get('name') or 'Rotaidem')[:60]}** (Rotaidem team):\n{text}", "allowed_mentions": {"parse": []}})
    elif action == "close":
        if closed:
            return jsonify({"ok": True})
        error = discord_ticket_close(thread_id, thread.get("name", ""), user.get("name") or "the team")
    else:
        _, error, _ = discord_call("PATCH", f"/channels/{thread_id}", payload={"archived": False, "locked": False})
        if not error:
            db().run("UPDATE discord_items SET status = 'open' WHERE kind = 'ticket' AND ref = ?", thread_id)
            discord_call("POST", f"/channels/{thread_id}/messages", payload={
                "content": f"🔓 Ticket reopened by **{str(user.get('name') or 'the team')[:60]}**.", "allowed_mentions": {"parse": []}})
    if error:
        return err(discord_perm_hint(error), 502)
    audit(user, "discord_ticket_" + action, thread.get("name", thread_id)[:80])
    return jsonify({"ok": True})


# 3. Role buttons: members click to give themselves (or remove) a role

def discord_role_panel():
    return get_setting("discord.role_panel", []) or []


def discord_role_button(event, role_id):
    roles = {r["role_id"]: r for r in discord_role_panel()}
    member = event.get("member") or {}
    discord_id = str((member.get("user") or {}).get("id") or "")
    if role_id not in roles or not SNOWFLAKE.match(discord_id):
        return discord_reply("This role button is not in use any more.")
    has = role_id in (member.get("roles") or [])
    _, error, status = discord_call("DELETE" if has else "PUT", f"/guilds/{discord_settings()['guild_id']}/members/{discord_id}/roles/{role_id}",
                                    reason="Role button")
    label = roles[role_id].get("label") or "that role"
    if error:
        app.logger.warning("Role button %s: %s", role_id, error)
        return discord_reply("I could not change your roles. The team has to move the bot's role above this one." if status == 403 else
                             "Something went wrong. Please try again in a minute.")
    return discord_reply(f"Removed **{label}**." if has else f"You now have **{label}**. Click again to remove it.")


@app.get("/api/admin/discord/roles")
def admin_discord_roles():
    """The server's roles that a button may hand out (no buyer role, no roles with moderator powers)."""
    if not staff_user():
        return err("Admin only", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    cfg = discord_settings()
    rows, error, _ = discord_call("GET", f"/guilds/{cfg['guild_id']}/roles", timeout=10)
    if error:
        return err(error, 502)
    roles = [{"id": r["id"], "name": r.get("name", ""), "color": "#%06x" % int(r.get("color") or 0) if r.get("color") else "", "position": r.get("position", 0)}
             for r in rows or [] if discord_role_ok(r, cfg)]
    roles.sort(key=lambda r: -r["position"])
    return jsonify({"roles": roles, "panel": discord_role_panel()})


def discord_role_ok(r, cfg):
    return (r.get("id") not in (cfg["guild_id"], cfg["role_id"]) and not r.get("managed")
            and not int(r.get("permissions") or 0) & DISCORD_RISKY)


@app.post("/api/admin/discord/role-panel")
def admin_discord_role_panel():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    b, cfg = body(), discord_settings()
    channel = str(b.get("channel_id") or "")
    if not SNOWFLAKE.match(channel):
        return err("Choose the channel for the role buttons", 400)
    rows, error, _ = discord_call("GET", f"/guilds/{cfg['guild_id']}/roles", timeout=10)
    if error:
        return err(error, 502)
    allowed = {r["id"]: r for r in rows or [] if discord_role_ok(r, cfg)}
    panel = []
    for item in (b.get("roles") or [])[:10]:
        rid = str((item or {}).get("role_id") or "")
        if rid not in allowed:
            return err("Pick roles from the list. The buyer role and roles with moderator powers cannot be given by a button.", 400)
        if rid in [p["role_id"] for p in panel]:
            continue
        label = str(item.get("label") or allowed[rid].get("name") or "Role").strip()[:80]
        emoji = str(item.get("emoji") or "").strip()
        panel.append({"role_id": rid, "label": label, "emoji": emoji if DISCORD_EMOJI.match(emoji) else ""})
    if not panel:
        return err("Pick at least one role", 400)
    buttons = [{"type": 2, "style": 2, "label": p["label"], "custom_id": "role:" + p["role_id"], **({"emoji": {"name": p["emoji"]}} if p["emoji"] else {})} for p in panel]
    text = str(b.get("text") or "").strip()[:1500] or "Pick your roles: click a button to get it, click again to remove it."
    _, error, _ = discord_call("POST", f"/channels/{channel}/messages", payload={
        "content": text, "allowed_mentions": {"parse": []}, "components": [{"type": 1, "components": buttons[i:i + 5]} for i in range(0, len(buttons), 5)]})
    if error:
        return err(discord_perm_hint(error), 502)
    set_setting("discord.role_panel", panel)
    audit(user, "discord_role_panel", ", ".join(p["label"] for p in panel)[:200])
    return jsonify({"ok": True, "panel": panel})


# 4. The admin's own slash commands: a name, a reply and an optional link button

def discord_custom_commands():
    return get_setting("discord.custom_commands", []) or []


def discord_commands():
    ex = discord_extra()
    return (DISCORD_COMMANDS + DISCORD_MEMBER_COMMANDS + ([DISCORD_SUGGEST] if ex["suggestions_channel"] else [])
            + ([DISCORD_REPORT] if ex["modlog_channel"] else []) + (DISCORD_STAFF_COMMANDS if ex["staff_commands"] else [])
            + [{"name": c["name"], "type": 1, "description": c["description"]} for c in discord_custom_commands()])


def discord_reserved_names():
    return {c["name"] for c in DISCORD_COMMANDS + DISCORD_MEMBER_COMMANDS + DISCORD_STAFF_COMMANDS + [DISCORD_SUGGEST]}


def discord_custom_reply(c):
    rows = [{"type": 1, "components": [{"type": 2, "style": 5, "label": c["button_label"], "url": discord_absolute(c["button_url"])}]}] if c.get("button_url") else []
    return discord_reply(c["reply"], components=rows, public=bool(c.get("public")))


@app.get("/api/admin/discord/custom-commands")
def admin_discord_custom_commands():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"commands": discord_custom_commands(), "builtin": sorted(discord_reserved_names())})


@app.post("/api/admin/discord/custom-commands")
def admin_save_discord_custom_commands():
    """Saves the whole list and updates the commands in the server."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    builtin, out = discord_reserved_names(), []
    for c in (body().get("commands") or [])[:15]:
        name = str((c or {}).get("name") or "").strip().lstrip("/").lower()
        desc, reply = str(c.get("description") or "").strip()[:100], str(c.get("reply") or "").strip()
        label, url = str(c.get("button_label") or "").strip()[:80], str(c.get("button_url") or "").strip()
        if not DISCORD_CMD_NAME.match(name):
            return err(f"/{name or '?'}: a command name is 1 to 32 small letters, numbers, - or _", 400)
        if name in builtin or name in [o["name"] for o in out]:
            return err(f"/{name} is already a command", 400)
        if not desc or not reply or len(reply) > 2000:
            return err(f"/{name} needs a short description and a reply (up to 2000 characters)", 400)
        if url and (not label or not (url.startswith("https://") or (url.startswith("/") and not url.startswith("//")))):
            return err(f"/{name}: the button needs a label and a link (https:// or a page like /downloads)", 400)
        out.append({"name": name, "description": desc, "reply": reply, "button_label": label if url else "", "button_url": url, "public": bool(c.get("public"))})
    set_setting("discord.custom_commands", out)
    registered = False
    if discord_ready():
        _, error, _ = discord_call("PUT", f"/applications/{DISCORD_CLIENT_ID}/guilds/{discord_settings()['guild_id']}/commands", payload=discord_commands())
        registered = not error
    audit(user, "discord_custom_commands", " ".join("/" + c["name"] for c in out) or "none")
    return jsonify({"ok": True, "commands": out, "registered": registered})


# 5. Members: look someone up, timeout, kick, ban, unban

def discord_member_row(m, linked):
    u = m.get("user") or {}
    acc = linked.get(str(u.get("id")))
    return {"id": u.get("id"), "username": u.get("username", ""), "name": m.get("nick") or u.get("global_name") or u.get("username", ""),
            "joined": str(m.get("joined_at") or "")[:10], "bot": bool(u.get("bot")), "roles": len(m.get("roles") or []),
            "timeout_until": m.get("communication_disabled_until") or "",
            "account": {"name": acc["name"], "email": acc["email"], "buyer": has_buyer_licence(acc)} if acc else None}


@app.get("/api/admin/discord/members")
def admin_discord_members():
    if not manager_user():
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    q = str(request.args.get("q") or "").strip()[:100]
    if not q:
        return jsonify({"members": []})
    gid = discord_settings()["guild_id"]
    if SNOWFLAKE.match(q):
        one, error, status = discord_call("GET", f"/guilds/{gid}/members/{q}", timeout=10)
        rows = [one] if one else []
        if error and status != 404:
            return err(error, 502)
    else:
        rows, error, status = discord_call("GET", f"/guilds/{gid}/members/search?" + urllib.parse.urlencode({"query": q, "limit": 10}), timeout=10)
        if error:
            return err(discord_perm_hint(error) + (" If it keeps failing: Discord Developer Portal, your application, Bot, turn on Server Members Intent."
                                                   if status == 403 else ""), 502)
    ids = [str((m.get("user") or {}).get("id")) for m in rows or []]
    linked = {}
    for uid in ids:
        acc = db().first("SELECT * FROM users WHERE discord_id = ?", uid) if SNOWFLAKE.match(uid) else None
        if acc:
            linked[uid] = acc
    return jsonify({"members": [discord_member_row(m, linked) for m in rows or []]})


@app.get("/api/admin/discord/bans")
def admin_discord_bans():
    if not manager_user():
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    rows, error, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}/bans?limit=50", timeout=10)
    if error:
        return err(discord_perm_hint(error), 502)
    return jsonify({"bans": [{"id": (b.get("user") or {}).get("id"), "name": (b.get("user") or {}).get("global_name") or (b.get("user") or {}).get("username", ""),
                              "reason": b.get("reason") or ""} for b in rows or []]})


@app.post("/api/admin/discord/moderate")
def admin_discord_moderate():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    b = body()
    member, action = str(b.get("member_id") or ""), str(b.get("action") or "")
    if not SNOWFLAKE.match(member):
        return err("Pick a member", 400)
    reason = f"{user.get('name') or 'Admin'} on rotaidem.com: " + (str(b.get("reason") or "").strip()[:300] or "no reason given")
    gid = discord_settings()["guild_id"]
    if action == "timeout":
        minutes = clamp_int(b.get("minutes"), 60, 1, 40320)  # Discord allows up to 28 days
        error = discord_timeout(member, minutes, reason)
        what = f"timeout {minutes} min"
    elif action == "untimeout":
        error = discord_timeout(member, 0, reason)
        what = "timeout removed"
    elif action == "kick":
        _, error, _ = discord_call("DELETE", f"/guilds/{gid}/members/{member}", reason=reason)
        what = "kicked"
    elif action == "ban":
        _, error, _ = discord_call("PUT", f"/guilds/{gid}/bans/{member}", payload={"delete_message_seconds": 86400 if b.get("delete_messages") else 0}, reason=reason)
        what = "banned"
    elif action == "unban":
        _, error, _ = discord_call("DELETE", f"/guilds/{gid}/bans/{member}", reason=reason)
        what = "unbanned"
    else:
        return err("Unknown action", 400)
    if error:
        return err(discord_perm_hint(error) + (" (You cannot do this to the server owner or someone whose role is above the bot's.)" if "Missing Permissions" in error else ""), 502)
    audit(user, "discord_moderate", f"{member} {what}: {reason}"[:300])
    discord_modlog(f"🛡️ <@{member}> {what}", [reason], "#e5484d" if action in ("kick", "ban") else "#f5a524")
    return jsonify({"ok": True})


# 6. Clean up a channel: delete the last messages (all, or one member's)

@app.post("/api/admin/discord/purge")
def admin_discord_purge():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    b = body()
    channel, member = str(b.get("channel_id") or ""), str(b.get("member_id") or "").strip()
    count = clamp_int(b.get("count"), 10, 1, 100)
    if not SNOWFLAKE.match(channel):
        return err("Choose a channel", 400)
    if member and not SNOWFLAKE.match(member):
        return err("The member must be a Discord user ID (or leave it empty for everyone)", 400)
    deleted, too_old, error = discord_purge(channel, count, member, user.get("name") or "Admin")
    if error:
        return err(discord_perm_hint(error), 502)
    discord_modlog(f"🧹 {deleted} message(s) deleted in <#{channel}>", [f"By {user.get('name')}" + (f", only <@{member}>" if member else "")])
    audit(user, "discord_purge", f"{deleted} message(s) in {channel}" + (f" from {member}" if member else ""))
    return jsonify({"ok": True, "deleted": deleted, "too_old": too_old})


# 7. Channel controls: slowmode, lock and unlock for @everyone

@app.post("/api/admin/discord/channel/<channel>")
def admin_discord_channel(channel):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready() or not SNOWFLAKE.match(channel):
        return err("Choose a channel", 400)
    b, gid, by = body(), discord_settings()["guild_id"], user.get("name") or "Admin"
    info, error, _ = discord_call("GET", f"/channels/{channel}", timeout=10)
    if error or not info or info.get("guild_id") != gid:
        return err(error or "That channel is not in your server", 404)
    done = []
    if "slowmode" in b:
        seconds = clamp_int(b.get("slowmode"), 0, 0, 21600)
        error = discord_slowmode(channel, seconds, by)
        if error:
            return err(discord_perm_hint(error), 502)
        done.append(f"slowmode {seconds}s")
    if "locked" in b:
        _, error = discord_lock(channel, bool(b.get("locked")), by, str(b.get("note") or "").strip()[:500])
        if error:
            return err(discord_perm_hint(error), 502)
        done.append("locked" if b.get("locked") else "unlocked")
    audit(user, "discord_channel", f"#{info.get('name', channel)}: " + ", ".join(done))
    return jsonify({"ok": True})


# 8-10. Automation (daily role check, member milestones) and the bot's nickname

DISCORD_AUTOMATION = {"auto_sync": False, "milestones": False, "milestone_channel": "", "nickname": ""}


def discord_automation():
    saved = get_setting("discord.automation", {}) or {}
    return {**DISCORD_AUTOMATION, **{k: saved[k] for k in DISCORD_AUTOMATION if k in saved}}


def discord_member_count():
    guild, _, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}?with_counts=true", timeout=8)
    return int((guild or {}).get("approximate_member_count") or 0)


def discord_daily():
    """Once a day: the buyer role checked again for every linked member, 25 per visit. Once an hour: member milestones."""
    auto = discord_automation()
    if auto["auto_sync"]:
        today, state = utc_stamp()[:10], get_setting("discord.autosync", {}) or {}
        if state.get("day") != today and discord_claim("autosync." + today):
            state = {"day": today, "after": 0, "done": False}
            set_setting("discord.autosync", state)
        if state.get("day") == today and not state.get("done"):
            batch = db().all("SELECT * FROM users WHERE discord_id IS NOT NULL AND discord_id <> '' AND id > ? ORDER BY id LIMIT 25", int(state.get("after") or 0))
            for member in batch:
                try:
                    discord_sync_user(member)
                except Exception:  # noqa: BLE001
                    app.logger.exception("Daily Discord role check failed for %s", member["id"])
            set_setting("discord.autosync", {"day": today, "after": batch[-1]["id"] if batch else state.get("after", 0), "done": len(batch) < 25})
    if auto["milestones"] and time.time() - float(get_setting("discord.milestone_checked", 0) or 0) >= 3600:
        set_setting("discord.milestone_checked", time.time())
        count = discord_member_count()
        reached = max([m for m in DISCORD_MILESTONES if m <= count] or [0])
        if reached > int(get_setting("discord.milestone_last", 0) or 0):
            set_setting("discord.milestone_last", reached)
            channel = auto["milestone_channel"] or discord_settings()["welcome_channel"]
            if channel and discord_claim(f"milestone.{reached}"):
                try:
                    discord_send(channel, {"embed": {"title": f"🎉 {reached:,} members!", "color": "#f5c542",
                                                     "description": f"Our Discord just passed **{reached:,} members**. Thank you all for flying with Rotaidem ✈️"}}, "Automatic")
                except Exception:  # noqa: BLE001
                    app.logger.exception("Milestone post failed")


@app.get("/api/admin/discord/automation")
def admin_discord_automation():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({**discord_automation(), "autosync": get_setting("discord.autosync", {}) or {},
                    "milestone_last": int(get_setting("discord.milestone_last", 0) or 0)})


@app.post("/api/admin/discord/automation")
def admin_save_discord_automation():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    b, old = body(), discord_automation()
    channel = str(b.get("milestone_channel", old["milestone_channel"]) or "").strip()
    if channel and not SNOWFLAKE.match(channel):
        return err("Choose the channel from the list", 400)
    new = {**old, "auto_sync": bool(b.get("auto_sync", old["auto_sync"])), "milestones": bool(b.get("milestones", old["milestones"])), "milestone_channel": channel}
    if new["milestones"] and not old["milestones"] and discord_ready():
        # from now on: milestones already passed are not celebrated
        count = discord_member_count()
        set_setting("discord.milestone_last", max([m for m in DISCORD_MILESTONES if m <= count] or [0]))
    set_setting("discord.automation", new)
    audit(user, "discord_automation", f"daily check {'on' if new['auto_sync'] else 'off'}, milestones {'on' if new['milestones'] else 'off'}")
    return jsonify({"ok": True, **new})


@app.post("/api/admin/discord/nickname")
def admin_discord_nickname():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    nick = " ".join(str(body().get("nickname") or "").split())[:32]
    _, error, _ = discord_call("PATCH", f"/guilds/{discord_settings()['guild_id']}/members/@me", payload={"nick": nick or None})
    if error:
        return err(discord_perm_hint(error), 502)
    set_setting("discord.automation", {**discord_automation(), "nickname": nick})
    audit(user, "discord_nickname", nick or "(reset)")
    return jsonify({"ok": True, "nickname": nick})


# ---------- Discord, part 3: a bot that does a lot ----------
# Tags, reminders, suggestions, reports, forms, button giveaways, ticket categories and claiming, staff
# commands and warnings, a mod log, product roles, role/channel/emoji/invite managers, the audit log,
# live stat channels, recurring posts, bulk roles and usage numbers. Still no program running on STRATO:
# Discord calls the website, and timed things run at the first visit after they are due.

DISCORD_EXTRA = {"modlog_channel": "", "suggestions_channel": "", "warn_limit": 3, "warn_timeout_minutes": 60,
                 "ticket_categories": ["Technical help", "Licence & activation", "Billing", "Something else"],
                 "ticket_autoclose_days": 0, "product_roles": {}, "stat_channels": [], "staff_commands": True}
DISCORD_STAFF_BITS = {"warn": 1 << 40, "warnings": 1 << 40, "timeout": 1 << 40, "clear": 8192, "slowmode": 16, "lock": 16, "unlock": 16,
                      "User info": 1 << 40}
DISCORD_STAFF_COMMANDS = [
    {"name": "warn", "type": 1, "description": "Staff: warn a member (they get a DM; too many warnings = timeout)",
     "default_member_permissions": str(1 << 40), "options": [{"type": 6, "name": "member", "description": "Who", "required": True},
                                                             {"type": 3, "name": "reason", "description": "Why", "required": True, "max_length": 300}]},
    {"name": "warnings", "type": 1, "description": "Staff: a member's warnings", "default_member_permissions": str(1 << 40),
     "options": [{"type": 6, "name": "member", "description": "Who", "required": True}]},
    {"name": "timeout", "type": 1, "description": "Staff: mute a member for a while", "default_member_permissions": str(1 << 40),
     "options": [{"type": 6, "name": "member", "description": "Who", "required": True},
                 {"type": 4, "name": "minutes", "description": "How long (1 to 40320)", "required": True, "min_value": 1, "max_value": 40320},
                 {"type": 3, "name": "reason", "description": "Why", "required": False, "max_length": 300}]},
    {"name": "clear", "type": 1, "description": "Staff: delete the last messages in this channel", "default_member_permissions": "8192",
     "options": [{"type": 4, "name": "count", "description": "How many (1 to 100)", "required": True, "min_value": 1, "max_value": 100},
                 {"type": 6, "name": "member", "description": "Only this member's", "required": False}]},
    {"name": "slowmode", "type": 1, "description": "Staff: slowmode in this channel", "default_member_permissions": "16",
     "options": [{"type": 4, "name": "seconds", "description": "0 = off, up to 21600", "required": True, "min_value": 0, "max_value": 21600}]},
    {"name": "lock", "type": 1, "description": "Staff: stop members writing in this channel", "default_member_permissions": "16",
     "options": [{"type": 3, "name": "note", "description": "A note posted in the channel", "required": False, "max_length": 300}]},
    {"name": "unlock", "type": 1, "description": "Staff: let members write in this channel again", "default_member_permissions": "16"},
    {"name": "User info", "type": 2, "default_member_permissions": str(1 << 40)},  # right-click a member, Apps
]
DISCORD_STAT_VARS = ("members", "online", "linked", "buyers", "licences")
DISCORD_AUDIT = {1: "changed the server", 10: "created a channel", 11: "changed a channel", 12: "deleted a channel", 13: "changed channel permissions",
                 14: "changed channel permissions", 15: "changed channel permissions", 20: "kicked", 22: "banned", 23: "unbanned",
                 24: "changed a member", 25: "changed a member's roles", 26: "moved a member", 27: "disconnected a member", 28: "added a bot",
                 30: "created a role", 31: "changed a role", 32: "deleted a role", 40: "created an invite", 41: "changed an invite", 42: "deleted an invite",
                 60: "added an emoji", 61: "changed an emoji", 62: "deleted an emoji", 72: "deleted a message", 73: "deleted messages",
                 74: "pinned a message", 75: "unpinned a message", 80: "added an integration", 100: "created an event", 101: "changed an event",
                 102: "cancelled an event", 110: "created a thread", 111: "changed a thread", 112: "deleted a thread", 121: "changed a command's permissions",
                 140: "created an AutoMod rule", 143: "AutoMod blocked a message"}


def discord_extra():
    saved = get_setting("discord.extra", {}) or {}
    return {**DISCORD_EXTRA, **{k: saved[k] for k in DISCORD_EXTRA if k in saved}}


# --- small storage for the bot (table discord_items) ---

def ditem_add(kind, data, ref="", member="", status="", due="", uniq=None):
    """A new row; returns its id, or None when uniq is taken (e.g. a second giveaway entry)."""
    item_id = secrets.token_hex(6)
    try:
        db().run("INSERT INTO discord_items (id, kind, ref, member, status, due, uniq, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 item_id, kind, str(ref)[:190], str(member)[:60], status, due, uniq, json.dumps(data), utc_stamp())
    except Exception:  # noqa: BLE001 - the unique key exists already
        if uniq:
            return None
        raise
    return item_id


def ditem_row(row):
    if not row:
        return None
    try:
        data = json.loads(row.get("data") or "{}")
    except ValueError:
        data = {}
    return {**{k: row[k] for k in ("id", "kind", "ref", "member", "status", "due", "created_at")}, **data, "id": row["id"]}


def ditem(item_id, kind):
    return ditem_row(db().first("SELECT * FROM discord_items WHERE id = ? AND kind = ?", str(item_id), kind))


def ditems(kind, status=None, ref=None, member=None, limit=200):
    sql, args = "SELECT * FROM discord_items WHERE kind = ?", [kind]
    for col, value in (("status", status), ("ref", ref), ("member", member)):
        if value is not None:
            sql += f" AND {col} = ?"
            args.append(value)
    return [ditem_row(r) for r in db().all(sql + f" ORDER BY created_at DESC LIMIT {int(limit)}", *args)]


def ditem_save(item, **changes):
    """Write back changed data fields and/or status/due. Returns the new item."""
    item = {**item, **changes}
    data = {k: v for k, v in item.items() if k not in ("id", "kind", "ref", "member", "status", "due", "created_at")}
    db().run("UPDATE discord_items SET status = ?, due = ?, data = ? WHERE id = ?", item.get("status") or "", item.get("due") or "", json.dumps(data), item["id"])
    return item


def ditem_take(item_id, from_status, to_status):
    """Moves an item from one status to another for exactly one caller (two visits at once: one wins)."""
    return db().run("UPDATE discord_items SET status = ? WHERE id = ? AND status = ?", to_status, item_id, from_status).rowcount == 1


def discord_ts(stamp):
    """'2026-11-12 18:00:00' (UTC) -> <t:...:R>, which Discord shows in each reader's own time ("in 2 hours")."""
    try:
        return f"<t:{int(datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp())}:R>"
    except (TypeError, ValueError):
        return stamp


def discord_dm(discord_id, payload):
    """A direct message from the bot. Returns the error text ('' when sent)."""
    dm, error, _ = discord_call("POST", "/users/@me/channels", payload={"recipient_id": str(discord_id)})
    if error:
        return error
    _, error, _ = discord_call("POST", f"/channels/{dm['id']}/messages", payload={"allowed_mentions": {"parse": []}, **payload})
    return error


def discord_modlog(title, lines=(), color="#aab6c2", link=""):
    """A line in the mod log channel. Never fails the caller."""
    channel = discord_extra().get("modlog_channel")
    if not channel or not discord_ready():
        return
    try:
        embed = {"title": title[:256], "description": "\n".join(str(x) for x in lines if x)[:3500], "color": int(color[1:], 16),
                 "timestamp": datetime.now(timezone.utc).isoformat()}
        message = {"embeds": [embed], "allowed_mentions": {"parse": []}}
        if link:
            message["components"] = [{"type": 1, "components": [{"type": 2, "style": 5, "label": "Open in Discord", "url": link}]}]
        discord_call("POST", f"/channels/{channel}/messages", payload=message)
    except Exception:  # noqa: BLE001
        app.logger.exception("Mod log post failed")


def discord_has(event, bit):
    """The member who used a command has this permission (or is an Administrator) in that channel."""
    try:
        return bool(int((event.get("member") or {}).get("permissions") or 0) & (bit | 8))
    except (TypeError, ValueError):
        return False


def discord_opts(event):
    return {o.get("name"): o.get("value") for o in (event.get("data") or {}).get("options") or []}


def discord_resolved_name(event, user_id):
    res = (event.get("data") or {}).get("resolved") or {}
    member, user = (res.get("members") or {}).get(str(user_id)) or {}, (res.get("users") or {}).get(str(user_id)) or {}
    return str(member.get("nick") or user.get("global_name") or user.get("username") or user_id)[:80]


# --- moderation, shared by the admin page and the staff commands in Discord ---

def discord_timeout(member, minutes, reason):
    until = (datetime.now(timezone.utc) + timedelta(minutes=int(minutes))).isoformat() if minutes else None
    _, error, _ = discord_call("PATCH", f"/guilds/{discord_settings()['guild_id']}/members/{member}", payload={"communication_disabled_until": until}, reason=reason)
    return error


def discord_purge(channel, count, member, by):
    """(deleted, too old to delete, error)."""
    rows, error, _ = discord_call("GET", f"/channels/{channel}/messages?limit={100 if member else count}", timeout=10)
    if error:
        return 0, 0, error
    picked = [m for m in rows or [] if not member or str((m.get("author") or {}).get("id")) == member][:count]
    cutoff = utc_stamp(14 * 86400 - 3600)  # Discord deletes in bulk only messages younger than 14 days
    ids = [m["id"] for m in picked if snowflake_time(m["id"]) > cutoff]
    if len(ids) >= 2:
        _, error, _ = discord_call("POST", f"/channels/{channel}/messages/bulk-delete", payload={"messages": ids}, reason=f"Clean-up by {by}")
    elif ids:
        _, error, _ = discord_call("DELETE", f"/channels/{channel}/messages/{ids[0]}", reason=f"Clean-up by {by}")
    if error:
        return 0, 0, error
    gone = set(ids)
    set_setting("discord.posts", [p for p in discord_posts() if p["id"] not in gone])
    return len(ids), len(picked) - len(ids), ""


def discord_lock(channel, locked, by, note=""):
    """Lock or unlock a channel for @everyone, keeping its other permission overwrites. Returns (channel name, error)."""
    gid = discord_settings()["guild_id"]
    info, error, _ = discord_call("GET", f"/channels/{channel}", timeout=10)
    if error or not info or info.get("guild_id") != gid:
        return "", error or "That channel is not in your server"
    old = next((o for o in info.get("permission_overwrites") or [] if o.get("id") == gid), {})
    allow, deny = int(old.get("allow") or 0), int(old.get("deny") or 0)
    deny = deny | DISCORD_SEND if locked else deny & ~DISCORD_SEND
    allow &= ~DISCORD_SEND if locked else ~0
    _, error, _ = discord_call("PUT", f"/channels/{channel}/permissions/{gid}", payload={"type": 0, "allow": str(allow), "deny": str(deny)},
                               reason=f"{'Locked' if locked else 'Unlocked'} by {by}")
    if error:
        return info.get("name", ""), error
    if note:
        discord_call("POST", f"/channels/{channel}/messages", payload={"content": ("🔒 " if locked else "🔓 ") + note[:500], "allowed_mentions": {"parse": []}})
    discord_modlog(f"{'🔒 Locked' if locked else '🔓 Unlocked'} #{info.get('name', '')}", [f"By {by}", note])
    return info.get("name", ""), ""


def discord_slowmode(channel, seconds, by):
    _, error, _ = discord_call("PATCH", f"/channels/{channel}", payload={"rate_limit_per_user": int(seconds)}, reason=f"Slowmode by {by}")
    return error


def discord_warn(member, name, reason, by_name, by_id=""):
    """Saves a warning, DMs the member, and times them out when they reach the limit. Returns (warnings now, timed out)."""
    ex = discord_extra()
    ditem_add("warning", {"reason": reason[:300], "by": by_name[:80], "by_id": by_id, "name": name[:80]}, member=member, status="active")
    count = len(ditems("warning", status="active", member=member))
    discord_dm(member, {"content": f"⚠️ You got a warning in the Rotaidem Discord: **{reason[:300]}**\nPlease read the rules. "
                                   f"({count} of {ex['warn_limit']} warnings)"})
    timed_out = False
    if ex["warn_limit"] and count >= int(ex["warn_limit"]) and ex["warn_timeout_minutes"]:
        timed_out = not discord_timeout(member, ex["warn_timeout_minutes"], f"{count} warnings (automatic)")
    discord_modlog(f"⚠️ Warning for {name}", [f"<@{member}> · {reason}", f"By {by_name} · {count} active warning(s)",
                                              f"Timed out for {ex['warn_timeout_minutes']} min (automatic)" if timed_out else ""], "#f5a524")
    return count, timed_out


# --- member commands: /tag, /remind, /suggest, Report to staff ---

DISCORD_MEMBER_COMMANDS = [
    {"name": "tag", "type": 1, "description": "Show a saved answer from the Rotaidem team",
     "options": [{"type": 3, "name": "name", "description": "Which answer", "required": True, "autocomplete": True, "max_length": 60},
                 {"type": 6, "name": "for", "description": "Mention someone with it", "required": False}]},
    {"name": "orders", "type": 1, "description": "Your latest Rotaidem orders (only you see them)"},
    {"name": "remind", "type": 1, "description": "The bot reminds you (by DM) later",
     "options": [{"type": 3, "name": "in", "description": "When: 10m, 2h, 3d or 1w", "required": True, "max_length": 10},
                 {"type": 3, "name": "about", "description": "What about", "required": True, "max_length": 300}]},
]
DISCORD_SUGGEST = {"name": "suggest", "type": 1, "description": "Suggest an idea to the Rotaidem team"}
DISCORD_REPORT = {"name": "Report to staff", "type": 3}  # right-click a message, Apps
REMIND_UNITS = {"m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800}
DISCORD_TAG_NAME = re.compile(r"^[a-z0-9][a-z0-9 _.-]{0,59}$")


def discord_tags():
    return sorted(ditems("tag", limit=1000), key=lambda t: t["ref"])


def discord_tag_autocomplete(event):
    typed = str(next((o.get("value") for o in (event.get("data") or {}).get("options") or [] if o.get("focused")), "") or "").lower().strip()
    names = [t["ref"] for t in discord_tags()]
    hits = [n for n in names if n.startswith(typed)] + [n for n in names if typed in n and not n.startswith(typed)]
    return {"type": 8, "data": {"choices": [{"name": n, "value": n} for n in hits[:25]]}}


def discord_cmd_tag(event):
    o = discord_opts(event)
    name = str(o.get("name") or "").lower().strip()
    row = db().first("SELECT * FROM discord_items WHERE kind = 'tag' AND ref = ?", name)
    tag = ditem_row(row)
    if not tag:
        return discord_reply(f"There is no saved answer called **{name}**. Start typing after /tag to see them.")
    ditem_save(tag, uses=int(tag.get("uses") or 0) + 1)
    target = str(o.get("for") or "")
    content = (f"<@{target}> " if SNOWFLAKE.match(target) else "") + tag.get("text", "")
    rows = [{"type": 1, "components": [{"type": 2, "style": 5, "label": tag["button_label"], "url": discord_absolute(tag["button_url"])}]}] if tag.get("button_url") else []
    reply = discord_reply(content, components=rows, public=True)
    reply["data"]["allowed_mentions"] = {"parse": [], "users": [target] if SNOWFLAKE.match(target) else []}
    return reply


def discord_cmd_remind(event):
    discord_id, _, _ = discord_member(event)
    o = discord_opts(event)
    m = re.match(r"^\s*(\d{1,4})\s*(m|min|h|d|w)\s*$", str(o.get("in") or "").lower())
    seconds = int(m.group(1)) * REMIND_UNITS[m.group(2)] if m else 0
    if not 60 <= seconds <= 30 * 86400:
        return discord_reply("Say when like **10m**, **2h**, **3d** or **1w** (1 minute to 30 days).")
    if len(ditems("reminder", status="pending", member=discord_id)) >= 10:
        return discord_reply("You have 10 reminders waiting already.")
    due = utc_stamp(-seconds)
    ditem_add("reminder", {"text": str(o.get("about") or "")[:300], "channel": str(event.get("channel_id") or "")}, member=discord_id, status="pending", due=due)
    return discord_reply(f"⏰ Got it. I will remind you {discord_ts(due)}. (Allow DMs from server members so it reaches you.)")


def discord_cmd_suggest(event):
    if not discord_extra()["suggestions_channel"]:
        return discord_reply("Suggestions are not open right now.")
    return {"type": 9, "data": {"custom_id": "suggest", "title": "Suggest an idea", "components": [{"type": 1, "components": [
        {"type": 4, "custom_id": "idea", "label": "Your idea", "style": 2, "max_length": 1500, "required": True,
         "placeholder": "What should Rotaidem add or change?"}]}]}}


DISCORD_SUGGEST_STATUS = {"open": ("💡 Open", 0xAAB6C2), "planned": ("🗺️ Planned", 0x5865F2), "accepted": ("✅ Accepted", 0x4CC38A),
                          "declined": ("❌ Declined", 0xE5484D), "done": ("🚀 Done", 0xF5C542)}


def discord_suggestion_embed(s):
    label, color = DISCORD_SUGGEST_STATUS.get(s.get("status") or "open", DISCORD_SUGGEST_STATUS["open"])
    fields = [{"name": "Status", "value": label + (f": {s['note']}" if s.get("note") else ""), "inline": False}]
    return {"author": {"name": f"Suggestion from {s.get('name') or 'a member'}"}, "description": s.get("text", "")[:4000], "color": color, "fields": fields}


def discord_suggest_submit(event):
    discord_id, name, _ = discord_member(event)
    channel = discord_extra()["suggestions_channel"]
    idea = next((c.get("value") for row in (event.get("data") or {}).get("components") or [] for c in row.get("components") or [] if c.get("custom_id") == "idea"), "")
    idea = str(idea or "").strip()[:1500]
    if not channel or not idea:
        return discord_reply("Nothing was sent.")
    if not discord_claim(f"suggest.{discord_id}.{int(time.time()) // 120}"):
        return discord_reply("You just sent one. Give it a couple of minutes before the next.")
    item = {"text": idea, "name": name, "channel": channel, "status": "open"}
    sent, error, _ = discord_call("POST", f"/channels/{channel}/messages", payload={"embeds": [discord_suggestion_embed(item)], "allowed_mentions": {"parse": []}})
    if error:
        return discord_reply("Sorry, the suggestion could not be posted. Please try again later.")
    for emoji in ("👍", "👎"):
        discord_call("PUT", f"/channels/{channel}/messages/{sent['id']}/reactions/{urllib.parse.quote(emoji)}/@me")
    ditem_add("suggestion", {"text": idea, "name": name, "channel": channel, "message": sent["id"]}, member=discord_id, status="open")
    return discord_reply("💡 Thanks! Your idea is posted for everyone to vote on.")


def discord_cmd_report(event):
    discord_id, name, _ = discord_member(event)
    data = event.get("data") or {}
    target = data.get("target_id")
    msg = ((data.get("resolved") or {}).get("messages") or {}).get(str(target)) or {}
    if not discord_extra()["modlog_channel"]:
        return discord_reply("Reports are not set up yet. Please contact the team with /support.")
    if not discord_claim(f"report.{discord_id}.{target}"):
        return discord_reply("You reported this message already. Thanks!")
    author = msg.get("author") or {}
    link = f"https://discord.com/channels/{event.get('guild_id') or discord_settings()['guild_id']}/{msg.get('channel_id') or event.get('channel_id')}/{target}"
    ditem_add("report", {"by": name, "author": author.get("global_name") or author.get("username") or "?", "author_id": author.get("id", ""),
                         "text": str(msg.get("content") or "")[:1000], "link": link}, member=discord_id, status="open")
    discord_modlog(f"🚩 Message reported by {name}", [f"Written by <@{author.get('id', '')}>", "> " + str(msg.get("content") or "(no text)")[:900]], "#e5484d", link)
    return discord_reply("🚩 Thanks, the team got your report. Only staff can see it.")


def discord_cmd_staff(event):
    """warn, warnings, timeout, clear, slowmode, lock, unlock and User info, for members with the right permission."""
    data = event.get("data") or {}
    name, o = data.get("name"), discord_opts(event)
    if not discord_has(event, DISCORD_STAFF_BITS.get(name, 8)):
        return discord_reply("That is a staff command.")
    _, by, _ = discord_member(event)
    by_id, channel = discord_member(event)[0], str(event.get("channel_id") or "")
    if name == "User info":
        return discord_user_info(event, str(data.get("target_id") or ""))
    if name in ("warn", "warnings", "timeout"):
        target = str(o.get("member") or "")
        if not SNOWFLAKE.match(target):
            return discord_reply("Pick a member.")
        who = discord_resolved_name(event, target)
        if name == "warn":
            count, timed_out = discord_warn(target, who, str(o.get("reason") or "")[:300], by, by_id)
            return discord_reply(f"⚠️ Warned **{who}** ({count} warning(s))." + (" They reached the limit and are timed out." if timed_out else ""))
        if name == "warnings":
            rows = ditems("warning", status="active", member=target, limit=20)
            return discord_reply(f"**{who}** has {len(rows)} active warning(s)." + "".join(f"\n• {w.get('reason')} ({w.get('by')}, {str(w['created_at'])[:10]})" for w in rows))
        minutes = clamp_int(o.get("minutes"), 60, 1, 40320)
        error = discord_timeout(target, minutes, f"{by}: {o.get('reason') or 'no reason given'}")
        if error:
            return discord_reply("Could not time them out: " + discord_perm_hint(error))
        discord_modlog(f"⏳ Timeout for {who}", [f"{minutes} min by {by}", str(o.get("reason") or "")], "#f5a524")
        return discord_reply(f"⏳ **{who}** is timed out for {minutes} minutes.")
    if name == "clear":
        member = str(o.get("member") or "")
        deleted, old, error = discord_purge(channel, clamp_int(o.get("count"), 10, 1, 100), member if SNOWFLAKE.match(member) else "", by)
        if error:
            return discord_reply("Could not delete: " + discord_perm_hint(error))
        discord_modlog(f"🧹 {deleted} message(s) deleted in <#{channel}>", [f"By {by}"])
        return discord_reply(f"🧹 Deleted {deleted} message(s)." + (f" {old} were older than 14 days and stay." if old else ""))
    if name == "slowmode":
        seconds = clamp_int(o.get("seconds"), 0, 0, 21600)
        error = discord_slowmode(channel, seconds, by)
        return discord_reply(("Could not change it: " + discord_perm_hint(error)) if error else (f"🐢 Slowmode: {seconds} seconds." if seconds else "Slowmode is off."))
    _, error = discord_lock(channel, name == "lock", by, str(o.get("note") or ""))
    return discord_reply(("Could not change it: " + discord_perm_hint(error)) if error else ("🔒 Locked." if name == "lock" else "🔓 Unlocked."))


def discord_user_info(event, target):
    res = (event.get("data") or {}).get("resolved") or {}
    user, member = (res.get("users") or {}).get(target) or {}, (res.get("members") or {}).get(target) or {}
    acc = db().first("SELECT * FROM users WHERE discord_id = ?", target) if SNOWFLAKE.match(target) else None
    warnings = len(ditems("warning", status="active", member=target)) if SNOWFLAKE.match(target) else 0
    lines = [f"Discord account made: {snowflake_time(target)[:10]}", f"Joined the server: {str(member.get('joined_at') or '')[:10] or '?'}",
             f"Active warnings: {warnings}"]
    if acc:
        email = acc.get("email") or ""
        masked = (email[:2] + "•••" + email[email.index("@"):]) if "@" in email else email
        licences = db().first("SELECT COUNT(*) AS n FROM licenses WHERE status = 'active' AND (customer_id = ? OR LOWER(email) = ?)", acc["id"], email.lower())
        lines += [f"Website account: **{acc.get('name') or '?'}** ({masked})", f"Active licences: {int((licences or {}).get('n') or 0)}"]
    else:
        lines.append("Website account: not linked")
    name = user.get("global_name") or user.get("username") or target
    return discord_reply(embeds=[{"title": f"👤 {name}", "description": "\n".join(lines), "color": 0xAAB6C2}])


# --- forms (applications), giveaways with a button, ticket categories and claiming ---

def discord_form_open(event, form_id):
    form = ditem(form_id, "form")
    if not form or not form.get("questions"):
        return discord_reply("This form is closed.")
    discord_id = discord_member(event)[0]
    if db().first("SELECT id FROM discord_items WHERE kind = 'application' AND ref = ? AND member = ? AND status = 'pending'", form_id, discord_id):
        return discord_reply("You already sent this form. The team will get back to you.")
    rows = [{"type": 1, "components": [{"type": 4, "custom_id": f"q{i}", "label": q["label"][:45], "style": 2 if q.get("long") else 1,
                                        "max_length": 1000 if q.get("long") else 200, "required": q.get("required", True)}]}
            for i, q in enumerate(form["questions"][:5])]
    return {"type": 9, "data": {"custom_id": f"form:{form_id}", "title": form["title"][:45], "components": rows}}


def discord_form_submit(event, form_id):
    form = ditem(form_id, "form")
    discord_id, name, _ = discord_member(event)
    if not form:
        return discord_reply("This form is closed.")
    values = {c["custom_id"]: str(c.get("value") or "").strip() for row in (event.get("data") or {}).get("components") or [] for c in row.get("components") or []}
    answers = [{"q": q["label"], "a": values.get(f"q{i}", "")[:1000]} for i, q in enumerate(form["questions"][:5])]
    if not ditem_add("application", {"form": form["title"], "name": name, "answers": answers}, ref=form_id, member=discord_id, status="pending",
                     uniq=f"app:{form_id}:{discord_id}:{int(time.time()) // 60}"):
        return discord_reply("Already sent.")
    discord_modlog(f"📝 New {form['title']} from {name}", [f"**{a['q']}**\n{a['a'][:300]}" for a in answers] + ["Review it in Admin → Discord → Support."])
    return discord_reply(form.get("thanks") or "📝 Thanks! The team will look at it and get back to you.")


def discord_giveaway_message(gw, entries, ended=False):
    lines = [gw.get("description") or "", f"**Winners:** {gw.get('winners', 1)}", f"**Entries:** {entries}"]
    if gw.get("buyers_only"):
        lines.append("**Who can enter:** verified buyers")
    if ended:
        won = gw.get("winner_ids") or []
        lines.append("**Won by:** " + (", ".join(f"<@{w}>" for w in won) if won else "nobody entered"))
    else:
        lines.append(f"**Ends:** {discord_ts(gw['due'])}")
    button = {"type": 2, "style": 2 if ended else 1, "label": "Giveaway ended" if ended else "Enter", "emoji": {"name": "🎉"}, "custom_id": f"gw:{gw['id']}", "disabled": ended}
    return {"embeds": [{"title": f"🎁 {gw['prize']}", "description": "\n".join(x for x in lines if x), "color": 0x8E8E93 if ended else 0xF5C542}],
            "components": [{"type": 1, "components": [button]}], "allowed_mentions": {"parse": []}}


def discord_giveaway_entries(gw_id):
    return [r["member"] for r in db().all("SELECT member FROM discord_items WHERE kind = 'gw_entry' AND ref = ?", gw_id)]


def discord_giveaway_enter(event, gw_id):
    gw = ditem(gw_id, "giveaway")
    if not gw or gw.get("status") != "running" or gw.get("due", "") <= utc_stamp():
        return discord_reply("This giveaway has ended.")
    discord_id = discord_member(event)[0]
    role = discord_settings()["role_id"]
    if gw.get("buyers_only") and role not in ((event.get("member") or {}).get("roles") or []):
        return discord_reply("This giveaway is for verified buyers. Link your Rotaidem account with /verify first.")
    if not ditem_add("gw_entry", {}, ref=gw_id, member=discord_id, uniq=f"gw:{gw_id}:{discord_id}"):
        return discord_reply("You are already in. Good luck! 🍀")
    count = len(discord_giveaway_entries(gw_id))
    if count <= 20 or count % 10 == 0:  # keep the entry count on the post fresh without editing it for every click
        discord_call("PATCH", f"/channels/{gw['channel']}/messages/{gw['message']}", payload=discord_giveaway_message(gw, count))
    return discord_reply(f"🎉 You are in! Winners are drawn {discord_ts(gw['due'])}.")


def discord_giveaway_end(gw, reroll=False):
    """Draws the winners (new ones on a reroll), updates the post and announces them."""
    entries = discord_giveaway_entries(gw["id"])
    pool = [e for e in entries if e not in (gw.get("winner_ids") or [])] if reroll else entries
    winners = secrets.SystemRandom().sample(pool, min(int(gw.get("winners") or 1), len(pool))) if pool else []
    gw = ditem_save(gw, status="ended", winner_ids=winners if not reroll else winners or gw.get("winner_ids") or [])
    discord_call("PATCH", f"/channels/{gw['channel']}/messages/{gw['message']}", payload=discord_giveaway_message(gw, len(entries), ended=True))
    text = (f"🎉 Congratulations {', '.join(f'<@{w}>' for w in winners)}! You won **{gw['prize']}**." if winners else
            f"Nobody {'else ' if reroll else ''}entered the giveaway for **{gw['prize']}**.")
    discord_call("POST", f"/channels/{gw['channel']}/messages", payload={
        "content": text + (" (re-drawn)" if reroll and winners else ""), "allowed_mentions": {"users": winners},
        "message_reference": {"message_id": gw["message"], "fail_if_not_exists": False}})
    discord_modlog(f"🎁 Giveaway {'re-drawn' if reroll else 'ended'}: {gw['prize']}", [f"{len(entries)} entries", "Winners: " + (", ".join(f"<@{w}>" for w in winners) or "none")])
    return gw, winners


def discord_ticket_category(event, value):
    cats = discord_extra()["ticket_categories"]
    try:
        cat = cats[int(value)]
    except (ValueError, IndexError, TypeError):
        return discord_reply("Pick a topic again.")
    modal = discord_cmd_support(event)
    modal["data"]["custom_id"] = f"support:{cats.index(cat)}"
    modal["data"]["title"] = f"Ticket: {cat}"[:45]
    return modal


def discord_claim_ticket(event):
    if not discord_has(event, (1 << 34) | (1 << 40) | 8192):
        return discord_reply("Only the team can claim a ticket.")
    _, name, _ = discord_member(event)
    discord_call("POST", f"/channels/{event.get('channel_id')}/messages", payload={
        "content": f"🙋 **{name}** from the Rotaidem team is taking care of this ticket.", "allowed_mentions": {"parse": []}})
    close = [{"type": 1, "components": [{"type": 2, "style": 4, "label": "Close ticket", "emoji": {"name": "🔒"}, "custom_id": "close_ticket"}]}]
    return {"type": 7, "data": {"components": close}}  # the same message, without the Claim button


# --- timed jobs: reminders, giveaways, recurring posts, stat channels, ticket auto-close ---

def discord_next_run(r):
    """The next time a recurring post is due after now: every day, or on a weekday, at HH:MM UTC."""
    now = datetime.now(timezone.utc)
    hour, minute = (int(x) for x in str(r.get("time") or "09:00").split(":"))
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while nxt <= now or (r.get("every") == "week" and nxt.weekday() != int(r.get("weekday") or 0)):
        nxt += timedelta(days=1)
    return nxt.strftime("%Y-%m-%d %H:%M:%S")


def discord_stat_values():
    n = lambda sql: int((db().first(sql) or {}).get("n") or 0)  # noqa: E731
    guild, _, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}?with_counts=true", timeout=8)
    linked_buyers = sum(1 for u in db().all("SELECT * FROM users WHERE discord_id IS NOT NULL AND discord_id <> ''") if has_buyer_licence(u))
    return {"members": int((guild or {}).get("approximate_member_count") or 0), "online": int((guild or {}).get("approximate_presence_count") or 0),
            "linked": n("SELECT COUNT(*) AS n FROM users WHERE discord_id IS NOT NULL AND discord_id <> ''"), "buyers": linked_buyers,
            "licences": n("SELECT COUNT(*) AS n FROM licenses WHERE status = 'active'")}


def discord_update_stats(force=False):
    """Renames the stat channels ("👥 Members: 128"). Discord allows few renames, so at most every 30 minutes."""
    stats = discord_extra()["stat_channels"]
    if not stats or (not force and time.time() - float(get_setting("discord.stats_at", 0) or 0) < 1800):
        return []
    set_setting("discord.stats_at", time.time())
    values, done = discord_stat_values(), []
    for s in stats:
        name = s["template"]
        for k, v in values.items():
            name = name.replace("{" + k + "}", f"{v:,}")
        _, error, _ = discord_call("PATCH", f"/channels/{s['channel_id']}", payload={"name": name[:100]}, reason="Live stats")
        done.append({"channel_id": s["channel_id"], "name": name[:100], "error": error})
    return done


def discord_autoclose_tickets():
    days, channel = int(discord_extra()["ticket_autoclose_days"] or 0), discord_settings()["tickets_channel"]
    if not days or not channel:
        return
    active, error, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}/threads/active", timeout=10)
    cutoff = utc_stamp(days * 86400)
    for t in [t for t in (active or {}).get("threads") or [] if t.get("parent_id") == channel and t.get("type") == 12][:5]:
        if snowflake_time(t.get("last_message_id") or t["id"]) < cutoff and discord_claim(f"autoclose.{t['id']}.{t.get('last_message_id')}"):
            discord_call("POST", f"/channels/{t['id']}/messages", payload={"content": f"⏲️ No reply for {days} day(s), so this ticket closes now. Open a new one any time."})
            discord_ticket_close(t["id"], t.get("name", ""), f"auto-close after {days} day(s)")


def discord_jobs():
    """Every visit (at most once a minute, from discord_run_due)."""
    now = utc_stamp()
    for r in [ditem_row(x) for x in db().all("SELECT * FROM discord_items WHERE kind = 'reminder' AND status = 'pending' AND due <= ? LIMIT 20", now)]:
        if ditem_take(r["id"], "pending", "sent"):
            text = {"content": f"⏰ Reminder: **{r.get('text', '')}**"}
            if discord_dm(r["member"], text) and r.get("channel"):  # DMs closed: in the channel where they asked
                discord_call("POST", f"/channels/{r['channel']}/messages", payload={"content": f"<@{r['member']}> ⏰ Reminder: **{r.get('text', '')}**",
                                                                                   "allowed_mentions": {"users": [r["member"]]}})
    for gw in [ditem_row(x) for x in db().all("SELECT * FROM discord_items WHERE kind = 'giveaway' AND status = 'running' AND due <= ? LIMIT 5", now)]:
        if ditem_take(gw["id"], "running", "drawing"):
            discord_giveaway_end({**gw, "status": "drawing"})
    for rp in [ditem_row(x) for x in db().all("SELECT * FROM discord_items WHERE kind = 'recurring' AND status = 'active' AND due <= ? LIMIT 5", now)]:
        if discord_claim(f"recur.{rp['id']}.{rp['due']}"):
            try:
                discord_send(rp["post"]["channel_id"], rp["post"], "Repeating post")
            except Exception:  # noqa: BLE001
                app.logger.exception("Repeating Discord post %s failed", rp["id"])
            ditem_save(rp, due=discord_next_run(rp), sent=int(rp.get("sent") or 0) + 1)  # from now: no catch-up burst after a quiet spell
    for t in [ditem_row(x) for x in db().all("SELECT * FROM discord_items WHERE kind = 'ticket' AND status = 'followup' LIMIT 5")]:
        if ditem_take(t["id"], "followup", "closed"):
            try:
                discord_ticket_followup(t)
            except Exception:  # noqa: BLE001
                app.logger.exception("Ticket follow-up failed")
    if time.time() - float(get_setting("discord.hourly_at", 0) or 0) >= 1800:
        set_setting("discord.hourly_at", time.time())
        discord_update_stats()
        discord_autoclose_tickets()


def discord_count_use(event):
    """How often each command and button is used (Admin -> Discord -> Insights). Never fails the caller."""
    try:
        data = event.get("data") or {}
        name = ("/" + data["name"]) if data.get("name") else str(data.get("custom_id") or "?").split(":")[0]
        if event.get("type") == 4:
            return
        day = utc_stamp()[:10]
        usage = get_setting("discord.usage", {}) or {}
        usage[name] = int(usage.get(name) or 0) + 1
        set_setting("discord.usage", usage)
        daily = get_setting("discord.usage_days", {}) or {}
        daily[day] = int(daily.get(day) or 0) + 1
        set_setting("discord.usage_days", {k: daily[k] for k in sorted(daily)[-30:]})
    except Exception:  # noqa: BLE001
        pass


# --- product roles: an extra role per product, besides the buyer role ---

def discord_sync_product_roles(user):
    mapping = discord_extra()["product_roles"]
    if not mapping or not user.get("discord_id"):
        return
    rows = db().all("SELECT product_id FROM licenses WHERE status = 'active' AND (customer_id = ? OR LOWER(email) = ?)", user["id"], (user.get("email") or "").lower())
    gid = discord_settings()["guild_id"]
    for product, role in mapping.items():
        owns = any(same_product(product, r["product_id"]) for r in rows)
        discord_call("PUT" if owns else "DELETE", f"/guilds/{gid}/members/{user['discord_id']}/roles/{role}", reason="Product role")


# --- admin endpoints ---

def discord_admin(manager=True):
    """(user, error response). Manager actions need the Owner or an Admin; reading needs staff."""
    user = manager_user() if manager else staff_user()
    if not user:
        return None, err("Only the Owner or an Admin can do this" if manager else "Admin only", 403)
    if not discord_ready():
        return None, err("Discord is not set up yet", 503)
    return user, None


def discord_register_commands():
    _, error, _ = discord_call("PUT", f"/applications/{DISCORD_CLIENT_ID}/guilds/{discord_settings()['guild_id']}/commands", payload=discord_commands())
    return not error


@app.get("/api/admin/discord/extra")
def admin_discord_extra():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({**discord_extra(), "stat_vars": list(DISCORD_STAT_VARS)})


@app.post("/api/admin/discord/extra")
def admin_save_discord_extra():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    b, old = body(), discord_extra()
    new = dict(old)
    for k in ("modlog_channel", "suggestions_channel"):
        if k in b:
            v = str(b.get(k) or "").strip()
            if v and not SNOWFLAKE.match(v):
                return err("Choose the channels from the list", 400)
            new[k] = v
    if "warn_limit" in b:
        new["warn_limit"] = clamp_int(b.get("warn_limit"), 3, 0, 20)
    if "warn_timeout_minutes" in b:
        new["warn_timeout_minutes"] = clamp_int(b.get("warn_timeout_minutes"), 60, 0, 40320)
    if "ticket_autoclose_days" in b:
        new["ticket_autoclose_days"] = clamp_int(b.get("ticket_autoclose_days"), 0, 0, 60)
    if "staff_commands" in b:
        new["staff_commands"] = bool(b.get("staff_commands"))
    for k in ("ticket_rating", "ticket_email", "news_threads"):
        if k in b:
            new[k] = bool(b.get(k))
    if "ticket_limit" in b:
        new["ticket_limit"] = clamp_int(b.get("ticket_limit"), 1, 0, 5)
    for k, most in (("welcome_text", 1500), ("welcome_dm", 1500), ("buyer_dm", 1500)):
        if k in b:
            new[k] = str(b.get(k) or "").strip()[:most]
    if "link_role" in b:
        role = str(b.get("link_role") or "").strip()
        if role and (not SNOWFLAKE.match(role) or role == discord_settings()["role_id"]):
            return err("Choose the role from the list (not the buyer role)", 400)
        new["link_role"] = role
    if "ticket_categories" in b:
        cats = [" ".join(str(c).split())[:80] for c in (b.get("ticket_categories") or []) if str(c).strip()][:25]
        new["ticket_categories"] = list(dict.fromkeys(cats))
    if "product_roles" in b:
        roles = {}
        for product, role in (b.get("product_roles") or {}).items():
            role = str(role or "").strip()
            if role:
                if not SNOWFLAKE.match(role) or role == discord_settings()["role_id"]:
                    return err("Pick a role for each product (not the buyer role)", 400)
                roles[str(product)[:60]] = role
        new["product_roles"] = roles
    if "stat_channels" in b:
        stats = []
        for s in (b.get("stat_channels") or [])[:5]:
            channel, template = str((s or {}).get("channel_id") or ""), " ".join(str((s or {}).get("template") or "").split())[:100]
            if not SNOWFLAKE.match(channel) or not template:
                return err("Each stat channel needs a channel and a name like 👥 Members: {members}", 400)
            if not any("{" + v + "}" in template for v in DISCORD_STAT_VARS):
                return err("Put a number in the name: {members}, {online}, {linked}, {buyers} or {licences}", 400)
            stats.append({"channel_id": channel, "template": template})
        new["stat_channels"] = stats
    set_setting("discord.extra", new)
    registered = discord_register_commands() if discord_ready() else False
    audit(user, "discord_extra", ", ".join(sorted(k for k in b if k in DISCORD_EXTRA)))
    return jsonify({"ok": True, **new, "registered": registered})


# tags
@app.get("/api/admin/discord/tags")
def admin_discord_tags():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"tags": [{k: t.get(k) for k in ("id", "ref", "text", "button_label", "button_url", "uses")} for t in discord_tags()]})


@app.post("/api/admin/discord/tags")
def admin_save_discord_tag():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    b = body()
    name = " ".join(str(b.get("name") or "").lower().split())
    text, label, url = str(b.get("text") or "").strip(), str(b.get("button_label") or "").strip()[:80], str(b.get("button_url") or "").strip()
    if not DISCORD_TAG_NAME.match(name):
        return err("A tag name is 1 to 60 small letters, numbers, spaces, - _ or .", 400)
    if not text or len(text) > 2000:
        return err("Write the answer (up to 2000 characters)", 400)
    if url and (not label or not (url.startswith("https://") or (url.startswith("/") and not url.startswith("//")))):
        return err("The button needs a label and a link (https:// or a page like /downloads)", 400)
    data = {"text": text, "button_label": label if url else "", "button_url": url}
    old = ditem(b.get("id"), "tag") if b.get("id") else None
    clash = db().first("SELECT id FROM discord_items WHERE kind = 'tag' AND ref = ?", name)
    if clash and (not old or clash["id"] != old["id"]):
        return err(f"There is already a tag called {name}", 400)
    if old:
        db().run("UPDATE discord_items SET ref = ?, uniq = ? WHERE id = ?", name, "tag:" + name, old["id"])
        ditem_save({**old, "ref": name}, **data)
    else:
        if len(discord_tags()) >= 1000:
            return err("Up to 1000 tags", 400)
        ditem_add("tag", {**data, "uses": 0}, ref=name, uniq="tag:" + name)
    audit(user, "discord_tag", name)
    return jsonify({"ok": True})


@app.delete("/api/admin/discord/tags/<item_id>")
def admin_delete_discord_tag(item_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    db().run("DELETE FROM discord_items WHERE id = ? AND kind = 'tag'", item_id)
    audit(user, "discord_tag_delete", item_id)
    return jsonify({"ok": True})


# suggestions and reports
@app.get("/api/admin/discord/suggestions")
def admin_discord_suggestions():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"suggestions": ditems("suggestion", limit=100), "reports": ditems("report", limit=100)})


@app.post("/api/admin/discord/suggestions/<item_id>")
def admin_discord_suggestion(item_id):
    user, problem = discord_admin(manager=False)
    if problem:
        return problem
    s = ditem(item_id, "suggestion")
    status = str(body().get("status") or "")
    if not s or status not in DISCORD_SUGGEST_STATUS:
        return err("Not found", 404)
    s = ditem_save(s, status=status, note=str(body().get("note") or "").strip()[:300])
    _, error, _ = discord_call("PATCH", f"/channels/{s['channel']}/messages/{s['message']}", payload={"embeds": [discord_suggestion_embed(s)]})
    audit(user, "discord_suggestion", f"{status}: {s.get('text', '')[:60]}")
    return jsonify({"ok": True, "warning": error})


@app.post("/api/admin/discord/reports/<item_id>")
def admin_discord_report(item_id):
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    r = ditem(item_id, "report")
    if not r:
        return err("Not found", 404)
    ditem_save(r, status="resolved" if body().get("resolved", True) else "open", handled_by=user.get("name"))
    return jsonify({"ok": True})


# forms and applications
@app.get("/api/admin/discord/forms")
def admin_discord_forms():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"forms": ditems("form", limit=50), "applications": ditems("application", limit=200)})


@app.post("/api/admin/discord/forms")
def admin_save_discord_form():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    b = body()
    title = " ".join(str(b.get("title") or "").split())[:45]
    questions = [{"label": " ".join(str(q.get("label") or "").split())[:45], "long": bool(q.get("long")), "required": q.get("required", True) is not False}
                 for q in (b.get("questions") or [])[:5] if str((q or {}).get("label") or "").strip()]
    role = str(b.get("role_id") or "").strip()
    if not title or not questions:
        return err("A form needs a title and 1 to 5 questions (45 characters each)", 400)
    if role and (not SNOWFLAKE.match(role) or role == discord_settings()["role_id"]):
        return err("Choose the role from the list (not the buyer role)", 400)
    data = {"title": title, "questions": questions, "role_id": role, "thanks": str(b.get("thanks") or "").strip()[:300]}
    old = ditem(b.get("id"), "form") if b.get("id") else None
    if old:
        ditem_save(old, **data)
    else:
        ditem_add("form", data, status="open")
    audit(user, "discord_form", title)
    return jsonify({"ok": True})


@app.delete("/api/admin/discord/forms/<item_id>")
def admin_delete_discord_form(item_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    db().run("DELETE FROM discord_items WHERE id = ? AND kind = 'form'", item_id)
    audit(user, "discord_form_delete", item_id)
    return jsonify({"ok": True})


@app.post("/api/admin/discord/forms/<item_id>/post")
def admin_post_discord_form(item_id):
    user, problem = discord_admin()
    if problem:
        return problem
    form, b = ditem(item_id, "form"), body()
    channel = str(b.get("channel_id") or "")
    if not form:
        return err("Not found", 404)
    if not SNOWFLAKE.match(channel):
        return err("Choose a channel", 400)
    text = str(b.get("text") or "").strip()[:1500] or f"**{form['title']}**\nPress the button to fill it in. Only the team sees your answers."
    _, error, _ = discord_call("POST", f"/channels/{channel}/messages", payload={"content": text, "allowed_mentions": {"parse": []}, "components": [
        {"type": 1, "components": [{"type": 2, "style": 1, "label": str(b.get("label") or "Apply")[:80], "emoji": {"name": "📝"}, "custom_id": f"form:{item_id}"}]}]})
    if error:
        return err(discord_perm_hint(error), 502)
    audit(user, "discord_form_post", form["title"])
    return jsonify({"ok": True})


@app.post("/api/admin/discord/applications/<item_id>")
def admin_discord_application(item_id):
    user, problem = discord_admin()
    if problem:
        return problem
    a, b = ditem(item_id, "application"), body()
    action = str(b.get("action") or "")
    if not a or action not in ("approve", "decline"):
        return err("Not found", 404)
    if not ditem_take(item_id, "pending", "approved" if action == "approve" else "declined"):
        return err("Already handled", 400)
    form = ditem(a["ref"], "form") or {}
    note = str(b.get("note") or "").strip()[:500]
    role_error = ""
    if action == "approve" and form.get("role_id"):
        _, role_error, _ = discord_call("PUT", f"/guilds/{discord_settings()['guild_id']}/members/{a['member']}/roles/{form['role_id']}", reason=f"{a.get('form')} approved")
    text = (f"✅ Your **{a.get('form')}** was approved!" if action == "approve" else f"Your **{a.get('form')}** was not approved this time.") + (f"\n{note}" if note else "")
    dm_error = discord_dm(a["member"], {"content": text})
    discord_modlog(f"📝 {a.get('form')} from {a.get('name')} {'approved' if action == 'approve' else 'declined'}", [f"By {user.get('name')}", note])
    audit(user, "discord_application_" + action, f"{a.get('form')}: {a.get('name')}")
    return jsonify({"ok": True, "warning": " ".join(x for x in (role_error and "Role not given: " + role_error, dm_error and "They did not get the DM (DMs closed?).") if x)})


# giveaways with a button
@app.get("/api/admin/discord/giveaways")
def admin_discord_giveaways():
    if not staff_user():
        return err("Admin only", 403)
    out = []
    for gw in ditems("giveaway", limit=30):
        out.append({**gw, "entries": len(discord_giveaway_entries(gw["id"]))})
    return jsonify({"giveaways": out})


@app.post("/api/admin/discord/giveaways")
def admin_create_discord_giveaway():
    user, problem = discord_admin()
    if problem:
        return problem
    b = body()
    channel, prize = str(b.get("channel_id") or ""), " ".join(str(b.get("prize") or "").split())[:200]
    hours = clamp_int(b.get("hours"), 24, 1, 24 * 30)
    if not SNOWFLAKE.match(channel) or not prize:
        return err("Choose a channel and write the prize", 400)
    gw = {"id": "pending", "prize": prize, "winners": clamp_int(b.get("winners"), 1, 1, 20), "description": str(b.get("description") or "").strip()[:1000],
          "buyers_only": bool(b.get("buyers_only")), "channel": channel, "due": utc_stamp(-hours * 3600)}
    gw_id = ditem_add("giveaway", {k: gw[k] for k in ("prize", "winners", "description", "buyers_only", "channel")}, status="running", due=gw["due"])
    gw["id"] = gw_id
    sent, error, _ = discord_call("POST", f"/channels/{channel}/messages", payload=discord_giveaway_message(gw, 0))
    if error:
        db().run("DELETE FROM discord_items WHERE id = ?", gw_id)
        return err(discord_perm_hint(error), 502)
    ditem_save(ditem(gw_id, "giveaway"), message=sent["id"])
    audit(user, "discord_giveaway_start", prize)
    return jsonify({"ok": True, "id": gw_id})


@app.post("/api/admin/discord/giveaways/<item_id>/<action>")
def admin_discord_giveaway_action(item_id, action):
    user, problem = discord_admin()
    if problem:
        return problem
    gw = ditem(item_id, "giveaway")
    if not gw or action not in ("end", "reroll"):
        return err("Not found", 404)
    if action == "end":
        if not ditem_take(item_id, "running", "drawing"):
            return err("It has ended already", 400)
        gw, winners = discord_giveaway_end({**gw, "status": "drawing"})
    else:
        if gw.get("status") != "ended":
            return err("End it first", 400)
        gw, winners = discord_giveaway_end(gw, reroll=True)
    audit(user, "discord_giveaway_" + action, gw["prize"])
    return jsonify({"ok": True, "winners": winners})


# warnings
@app.get("/api/admin/discord/warnings")
def admin_discord_warnings():
    if not manager_user():
        return err("Only the Owner or an Admin can do this", 403)
    member = str(request.args.get("member") or "")
    return jsonify({"warnings": ditems("warning", status="active", member=member if SNOWFLAKE.match(member) else None, limit=200),
                    "limit": discord_extra()["warn_limit"]})


@app.post("/api/admin/discord/warnings")
def admin_add_discord_warning():
    user, problem = discord_admin()
    if problem:
        return problem
    b = body()
    member, reason = str(b.get("member_id") or ""), str(b.get("reason") or "").strip()[:300]
    if not SNOWFLAKE.match(member) or not reason:
        return err("Pick a member and write the reason", 400)
    count, timed_out = discord_warn(member, str(b.get("name") or member)[:80], reason, user.get("name") or "Admin")
    audit(user, "discord_warn", f"{member}: {reason}"[:200])
    return jsonify({"ok": True, "count": count, "timed_out": timed_out})


@app.delete("/api/admin/discord/warnings/<item_id>")
def admin_delete_discord_warning(item_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    db().run("UPDATE discord_items SET status = 'removed' WHERE id = ? AND kind = 'warning'", item_id)
    audit(user, "discord_warning_removed", item_id)
    return jsonify({"ok": True})


# roles, channels, emojis, invites, audit log
@app.get("/api/admin/discord/roles/all")
def admin_discord_all_roles():
    if not staff_user():
        return err("Admin only", 403)
    if not discord_ready():
        return err("Discord is not set up yet", 503)
    cfg = discord_settings()
    rows, error, _ = discord_call("GET", f"/guilds/{cfg['guild_id']}/roles", timeout=10)
    if error:
        return err(error, 502)
    roles = [{"id": r["id"], "name": r.get("name", ""), "color": "#%06x" % int(r.get("color") or 0) if r.get("color") else "", "position": r.get("position", 0),
              "hoist": bool(r.get("hoist")), "mentionable": bool(r.get("mentionable")), "managed": bool(r.get("managed")), "buyer": r["id"] == cfg["role_id"],
              "powers": bool(int(r.get("permissions") or 0) & DISCORD_RISKY), "safe": discord_role_ok(r, cfg)} for r in rows or [] if r["id"] != cfg["guild_id"]]
    return jsonify({"roles": sorted(roles, key=lambda r: -r["position"])})


def discord_role_fields(b):
    out = {}
    if "name" in b:
        name = " ".join(str(b.get("name") or "").split())[:100]
        if not name:
            raise ValueError("Give the role a name")
        out["name"] = name
    if "color" in b:
        color = str(b.get("color") or "")
        out["color"] = int(color[1:], 16) if re.match(r"^#[0-9a-fA-F]{6}$", color) else 0
    for k in ("hoist", "mentionable"):
        if k in b:
            out[k] = bool(b.get(k))
    return out


@app.post("/api/admin/discord/roles/create")
def admin_create_discord_role():
    user, problem = discord_admin()
    if problem:
        return problem
    try:
        fields = discord_role_fields({"name": "", **body()})
    except ValueError as exc:
        return err(str(exc), 400)
    role, error, _ = discord_call("POST", f"/guilds/{discord_settings()['guild_id']}/roles", payload={**fields, "permissions": "0"}, reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error), 502)
    audit(user, "discord_role_create", fields["name"])
    return jsonify({"ok": True, "id": role.get("id")})


@app.post("/api/admin/discord/roles/<role_id>")
def admin_edit_discord_role(role_id):
    user, problem = discord_admin()
    if problem:
        return problem
    if not SNOWFLAKE.match(role_id):
        return err("Unknown role", 404)
    try:
        fields = discord_role_fields(body())
    except ValueError as exc:
        return err(str(exc), 400)
    _, error, _ = discord_call("PATCH", f"/guilds/{discord_settings()['guild_id']}/roles/{role_id}", payload=fields, reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error), 502)
    audit(user, "discord_role_edit", fields.get("name") or role_id)
    return jsonify({"ok": True})


@app.delete("/api/admin/discord/roles/<role_id>")
def admin_delete_discord_role(role_id):
    user, problem = discord_admin()
    if problem:
        return problem
    cfg = discord_settings()
    if not SNOWFLAKE.match(role_id) or role_id in (cfg["role_id"], cfg["guild_id"]):
        return err("The buyer role cannot be deleted here", 400)
    _, error, _ = discord_call("DELETE", f"/guilds/{cfg['guild_id']}/roles/{role_id}", reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error), 502)
    audit(user, "discord_role_delete", role_id)
    return jsonify({"ok": True})


DISCORD_CHANNEL_TYPES = {"text": 0, "voice": 2, "category": 4, "announcement": 5, "forum": 15}


@app.post("/api/admin/discord/channels/create")
def admin_create_discord_channel():
    user, problem = discord_admin()
    if problem:
        return problem
    b, cfg = body(), discord_settings()
    kind = str(b.get("kind") or "text")
    name = re.sub(r"\s+", "-" if kind in ("text", "announcement", "forum") else " ", str(b.get("name") or "").strip().lower() if kind in ("text", "announcement", "forum")
                  else str(b.get("name") or "").strip())[:100]
    parent = str(b.get("category_id") or "")
    if kind not in DISCORD_CHANNEL_TYPES or not name:
        return err("Give the channel a name and a kind", 400)
    if parent and (not SNOWFLAKE.match(parent) or kind == "category"):
        return err("Choose the category from the list", 400)
    payload = {"name": name, "type": DISCORD_CHANNEL_TYPES[kind]}
    if parent:
        payload["parent_id"] = parent
    if str(b.get("topic") or "").strip() and kind in ("text", "announcement", "forum"):
        payload["topic"] = str(b.get("topic")).strip()[:1024]
    if b.get("buyers_only"):
        bot, _, _ = discord_call("GET", "/users/@me", timeout=8)
        view = 1024 | 2048 | 65536 | (1 << 20) | (1 << 21)  # see, write, history, connect, speak
        payload["permission_overwrites"] = [{"id": cfg["guild_id"], "type": 0, "deny": "1024", "allow": "0"},
                                            {"id": cfg["role_id"], "type": 0, "allow": str(view), "deny": "0"}] + \
            ([{"id": bot["id"], "type": 1, "allow": str(view), "deny": "0"}] if bot else [])
    channel, error, _ = discord_call("POST", f"/guilds/{cfg['guild_id']}/channels", payload=payload, reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error) + (" (Announcement channels need a Community server.)" if kind == "announcement" else ""), 502)
    audit(user, "discord_channel_create", f"{kind} {name}")
    return jsonify({"ok": True, "id": channel.get("id"), "name": channel.get("name")})


@app.delete("/api/admin/discord/channels/<channel_id>")
def admin_delete_discord_channel(channel_id):
    user, problem = discord_admin()
    if problem:
        return problem
    if not SNOWFLAKE.match(channel_id):
        return err("Unknown channel", 404)
    cfg = discord_settings()
    info, error, _ = discord_call("GET", f"/channels/{channel_id}", timeout=8)
    if error or not info or info.get("guild_id") != cfg["guild_id"]:
        return err(error or "That channel is not in your server", 404)
    if channel_id in [cfg.get(k) for k in DISCORD_DEFAULT if k.endswith("_channel")] + [discord_extra()[k] for k in ("modlog_channel", "suggestions_channel")]:
        return err("The website uses this channel (Automatic posts or Support). Choose another channel there first.", 400)
    _, error, _ = discord_call("DELETE", f"/channels/{channel_id}", reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error), 502)
    audit(user, "discord_channel_delete", info.get("name", channel_id))
    return jsonify({"ok": True})


@app.get("/api/admin/discord/emojis")
def admin_discord_emojis():
    user, problem = discord_admin(manager=False)
    if problem:
        return problem
    rows, error, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}/emojis", timeout=10)
    if error:
        return err(error, 502)
    return jsonify({"emojis": [{"id": e["id"], "name": e.get("name", ""), "animated": bool(e.get("animated")),
                                "url": f"https://cdn.discordapp.com/emojis/{e['id']}.{'gif' if e.get('animated') else 'png'}"} for e in rows or []]})


@app.post("/api/admin/discord/emojis")
def admin_add_discord_emoji():
    user, problem = discord_admin()
    if problem:
        return problem
    b = body()
    name, image = str(b.get("name") or "").strip(), str(b.get("image") or "")
    if not re.match(r"^[A-Za-z0-9_]{2,32}$", name):
        return err("An emoji name is 2 to 32 letters, numbers or _", 400)
    m = re.match(r"^data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=]+)$", image)
    if not m or len(m.group(2)) * 3 // 4 > 256 * 1024:
        return err("Choose a PNG, JPG, GIF or WebP picture of up to 256 KB", 400)
    emoji, error, _ = discord_call("POST", f"/guilds/{discord_settings()['guild_id']}/emojis", payload={"name": name, "image": image}, reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error), 502)
    audit(user, "discord_emoji_add", name)
    return jsonify({"ok": True, "id": emoji.get("id")})


@app.delete("/api/admin/discord/emojis/<emoji_id>")
def admin_delete_discord_emoji(emoji_id):
    user, problem = discord_admin()
    if problem:
        return problem
    if not SNOWFLAKE.match(emoji_id):
        return err("Unknown emoji", 404)
    _, error, _ = discord_call("DELETE", f"/guilds/{discord_settings()['guild_id']}/emojis/{emoji_id}", reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error), 502)
    audit(user, "discord_emoji_delete", emoji_id)
    return jsonify({"ok": True})


@app.get("/api/admin/discord/invites")
def admin_discord_invites():
    user, problem = discord_admin()
    if problem:
        return problem
    rows, error, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}/invites", timeout=10)
    if error:
        return err(discord_perm_hint(error), 502)
    out = [{"code": i.get("code"), "channel": (i.get("channel") or {}).get("name", ""), "by": (i.get("inviter") or {}).get("username", ""),
            "uses": i.get("uses", 0), "max_uses": i.get("max_uses", 0), "expires": str(i.get("expires_at") or "")[:16].replace("T", " ")} for i in rows or []]
    return jsonify({"invites": sorted(out, key=lambda i: -int(i["uses"] or 0))})


@app.delete("/api/admin/discord/invites/<code>")
def admin_delete_discord_invite(code):
    user, problem = discord_admin()
    if problem:
        return problem
    if not re.match(r"^[A-Za-z0-9-]{2,40}$", code):
        return err("Unknown invite", 404)
    _, error, _ = discord_call("DELETE", f"/invites/{code}", reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error), 502)
    audit(user, "discord_invite_delete", code)
    return jsonify({"ok": True})


@app.get("/api/admin/discord/audit-log")
def admin_discord_audit_log():
    user, problem = discord_admin()
    if problem:
        return problem
    data, error, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}/audit-logs?limit=50", timeout=10)
    if error:
        return err(discord_perm_hint(error), 502)
    users = {u["id"]: u.get("global_name") or u.get("username") or u["id"] for u in (data or {}).get("users") or []}
    out = [{"at": snowflake_time(e["id"]), "who": users.get(e.get("user_id"), e.get("user_id") or "?"),
            "what": DISCORD_AUDIT.get(e.get("action_type"), f"action {e.get('action_type')}"),
            "target": users.get(e.get("target_id"), "") or "", "reason": e.get("reason") or ""} for e in (data or {}).get("audit_log_entries") or []]
    return jsonify({"entries": out})


# stat channels, recurring posts, bulk roles, insights
@app.post("/api/admin/discord/stats/update")
def admin_discord_stats_update():
    user, problem = discord_admin()
    if problem:
        return problem
    done = discord_update_stats(force=True)
    return jsonify({"ok": True, "channels": done, "values": discord_stat_values() if not done else None})


@app.get("/api/admin/discord/recurring")
def admin_discord_recurring():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"recurring": ditems("recurring", limit=50)})


@app.post("/api/admin/discord/recurring")
def admin_create_discord_recurring():
    user, problem = discord_admin()
    if problem:
        return problem
    b = body()
    post = b.get("post") if isinstance(b.get("post"), dict) else {}
    channel = str(post.get("channel_id") or "")
    every, at = str(b.get("every") or "week"), str(b.get("time") or "")
    if not SNOWFLAKE.match(channel):
        return err("Choose the channel", 400)
    if every not in ("day", "week") or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", at):
        return err("Choose how often and the time (UTC)", 400)
    try:
        discord_message(post)
    except ValueError as exc:
        return err(str(exc), 400)
    rp = {"every": every, "time": at, "weekday": clamp_int(b.get("weekday"), 0, 0, 6),
          "post": {**{k: post.get(k) for k in DISCORD_POST_FIELDS}, "channel_id": channel}, "sent": 0, "by": user.get("name")}
    ditem_add("recurring", rp, status="active", due=discord_next_run(rp))
    audit(user, "discord_recurring", f"every {every} {at}")
    return jsonify({"ok": True})


@app.post("/api/admin/discord/recurring/<item_id>")
def admin_toggle_discord_recurring(item_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    rp = ditem(item_id, "recurring")
    if not rp:
        return err("Not found", 404)
    active = bool(body().get("active"))
    ditem_save(rp, status="active" if active else "paused", due=discord_next_run(rp) if active else rp.get("due"))
    return jsonify({"ok": True})


@app.delete("/api/admin/discord/recurring/<item_id>")
def admin_delete_discord_recurring(item_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    db().run("DELETE FROM discord_items WHERE id = ? AND kind = 'recurring'", item_id)
    audit(user, "discord_recurring_delete", item_id)
    return jsonify({"ok": True})


@app.post("/api/admin/discord/bulk-role")
def admin_discord_bulk_role():
    """Give or take a role for linked members (all, only licence holders, or only those without), 50 per press."""
    user, problem = discord_admin()
    if problem:
        return problem
    b, cfg = body(), discord_settings()
    role, action, who = str(b.get("role_id") or ""), str(b.get("action") or "add"), str(b.get("who") or "linked")
    roles, error, _ = discord_call("GET", f"/guilds/{cfg['guild_id']}/roles", timeout=10)
    if error:
        return err(error, 502)
    if not any(r["id"] == role and discord_role_ok(r, cfg) for r in roles or []):
        return err("Pick a role from the list (not the buyer role, no roles with moderator powers)", 400)
    if action not in ("add", "remove") or who not in ("linked", "buyers", "others"):
        return err("Choose what to do and for whom", 400)
    after = clamp_int(b.get("after"), 0, 0, 2 ** 31)
    batch = db().all("SELECT * FROM users WHERE discord_id IS NOT NULL AND discord_id <> '' AND id > ? ORDER BY id LIMIT 50", after)
    done = failed = 0
    for u in batch:
        if who != "linked" and has_buyer_licence(u) != (who == "buyers"):
            continue
        _, e, _ = discord_call("PUT" if action == "add" else "DELETE", f"/guilds/{cfg['guild_id']}/members/{u['discord_id']}/roles/{role}", reason=f"Bulk by {user.get('name')}")
        done, failed = done + (not e), failed + bool(e)
    audit(user, "discord_bulk_role", f"{action} {role} for {who}: {done} done, {failed} failed")
    return jsonify({"ok": True, "done": done, "failed": failed, "next": batch[-1]["id"] if len(batch) == 50 else None})


@app.get("/api/admin/discord/insights")
def admin_discord_insights():
    if not staff_user():
        return err("Admin only", 403)
    n = lambda sql, *a: int((db().first(sql, *a) or {}).get("n") or 0)  # noqa: E731
    count = lambda kind, status=None: n("SELECT COUNT(*) AS n FROM discord_items WHERE kind = ?" + (" AND status = ?" if status else ""), *([kind, status] if status else [kind]))  # noqa: E731
    return jsonify({"usage": get_setting("discord.usage", {}) or {}, "days": get_setting("discord.usage_days", {}) or {},
                    "totals": {"tags": count("tag"), "reminders_waiting": count("reminder", "pending"), "suggestions_open": count("suggestion", "open"),
                               "reports_open": count("report", "open"), "applications_waiting": count("application", "pending"),
                               "warnings": count("warning", "active"), "giveaways_running": count("giveaway", "running"),
                               "repeating_posts": count("recurring", "active")},
                    "ratings": [int(json.loads(r["data"] or "{}").get("rating") or 0) for r in db().all("SELECT data FROM discord_items WHERE kind = 'ticket'")
                                if json.loads(r["data"] or "{}").get("rating")]})


# ---------- Discord, part 4: AutoMod, safety, welcome, customers, ticket follow-up, personas, templates ----------

DISCORD_EXTRA4 = {"welcome_text": "", "welcome_dm": "", "link_role": "", "rules_role": "", "buyer_dm": "", "ticket_limit": 1,
                  "ticket_rating": True, "ticket_email": True, "news_threads": False, "personas": []}
DISCORD_EXTRA.update(DISCORD_EXTRA4)
AUTOMOD_PREFIX = "Rotaidem: "
AUTOMOD_KINDS = {"words": "Blocked words", "invites": "Invite links", "mentions": "Mention spam", "spam": "Spam", "presets": "Word lists"}
AUTOMOD_INVITES = ["*discord.gg/*", "*discord.com/invite/*", "*discordapp.com/invite/*", "*dsc.gg/*"]


def discord_render(template, **values):
    out = str(template or "")
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    return out


# --- AutoMod: Discord's own filters, which work all the time without anything running on the website ---

def automod_rules():
    rows, error, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}/auto-moderation/rules", timeout=10)
    return rows or [], error


def automod_view(r):
    meta = r.get("trigger_metadata") or {}
    kind = next((k for k, v in AUTOMOD_KINDS.items() if r.get("name") == AUTOMOD_PREFIX + v), "")
    actions = {a.get("type"): a.get("metadata") or {} for a in r.get("actions") or []}
    return {"id": r.get("id"), "name": r.get("name"), "kind": kind, "enabled": bool(r.get("enabled")), "trigger": r.get("trigger_type"),
            "words": [w for w in meta.get("keyword_filter") or [] if not (kind == "invites" and w in AUTOMOD_INVITES)],
            "allow": meta.get("allow_list") or [], "presets": meta.get("presets") or [], "limit": meta.get("mention_total_limit"),
            "alert": 2 in actions, "timeout": int((actions.get(3) or {}).get("duration_seconds") or 0) // 60, "ours": bool(kind)}


@app.get("/api/admin/discord/automod")
def admin_discord_automod():
    user, problem = discord_admin()
    if problem:
        return problem
    rows, error = automod_rules()
    if error:
        return err(discord_perm_hint(error), 502)
    return jsonify({"rules": [automod_view(r) for r in rows], "modlog": bool(discord_extra()["modlog_channel"])})


@app.post("/api/admin/discord/automod")
def admin_save_discord_automod():
    """Create or change one of the website's AutoMod rules (blocked words, invite links, mention spam, spam, word lists)."""
    user, problem = discord_admin()
    if problem:
        return problem
    b = body()
    kind = str(b.get("kind") or "")
    if kind not in AUTOMOD_KINDS:
        return err("Unknown filter", 400)
    clean = lambda items, most: [w for w in (" ".join(str(x).split())[:60] for x in (items or [])) if w][:most]  # noqa: E731
    words, allow = clean(b.get("words"), 1000), clean(b.get("allow"), 100)
    message = str(b.get("message") or "").strip()[:150] or "This message was blocked by the server's filters."
    actions = [{"type": 1, "metadata": {"custom_message": message}}]
    modlog = discord_extra()["modlog_channel"]
    if b.get("alert", True) and modlog:
        actions.append({"type": 2, "metadata": {"channel_id": modlog}})
    minutes = clamp_int(b.get("timeout"), 0, 0, 40320)
    if minutes and kind in ("words", "mentions"):
        actions.append({"type": 3, "metadata": {"duration_seconds": minutes * 60}})
    if kind == "words":
        if not words:
            return err("Write at least one word or phrase (one per line). Use * as a wildcard, e.g. *scam*", 400)
        trigger, meta = 1, {"keyword_filter": words, "allow_list": allow}
    elif kind == "invites":
        trigger, meta = 1, {"keyword_filter": AUTOMOD_INVITES, "allow_list": allow}
    elif kind == "mentions":
        trigger, meta = 5, {"mention_total_limit": clamp_int(b.get("limit"), 5, 1, 50), "mention_raid_protection_enabled": True}
    elif kind == "spam":
        trigger, meta = 3, {}
        actions = [a for a in actions if a["type"] != 3]
    else:
        presets = [p for p in (b.get("presets") or []) if p in (1, 2, 3)]
        if not presets:
            return err("Tick at least one list", 400)
        trigger, meta = 4, {"presets": presets, "allow_list": allow}
    payload = {"name": AUTOMOD_PREFIX + AUTOMOD_KINDS[kind], "event_type": 1, "trigger_type": trigger, "trigger_metadata": meta,
               "actions": actions, "enabled": b.get("enabled", True) is not False}
    rows, error = automod_rules()
    if error:
        return err(discord_perm_hint(error), 502)
    old = next((r for r in rows if r.get("name") == payload["name"]), None)
    gid = discord_settings()["guild_id"]
    if old:
        payload.pop("trigger_type")
        payload.pop("event_type")
        rule, error, _ = discord_call("PATCH", f"/guilds/{gid}/auto-moderation/rules/{old['id']}", payload=payload, reason=f"By {user.get('name')}")
    else:
        rule, error, _ = discord_call("POST", f"/guilds/{gid}/auto-moderation/rules", payload=payload, reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error) + (" Discord allows only one Spam, one Word lists and one Mention spam filter per server." if "50035" in error or "maximum" in error.lower() else ""), 502)
    audit(user, "discord_automod", f"{AUTOMOD_KINDS[kind]} {'on' if payload['enabled'] else 'off'}")
    return jsonify({"ok": True, "rule": automod_view(rule or {})})


@app.delete("/api/admin/discord/automod/<rule_id>")
def admin_delete_discord_automod(rule_id):
    user, problem = discord_admin()
    if problem:
        return problem
    if not SNOWFLAKE.match(rule_id):
        return err("Unknown filter", 404)
    _, error, _ = discord_call("DELETE", f"/guilds/{discord_settings()['guild_id']}/auto-moderation/rules/{rule_id}", reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error), 502)
    audit(user, "discord_automod_delete", rule_id)
    return jsonify({"ok": True})


# --- safety: lockdown, server safety settings, a snapshot of roles and channels ---

@app.get("/api/admin/discord/lockdown")
def admin_discord_lockdown_state():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify(get_setting("discord.lockdown", {}) or {})


@app.post("/api/admin/discord/lockdown")
def admin_discord_lockdown():
    """Lock every public text channel at once (a raid, a bad update), and unlock exactly those again."""
    user, problem = discord_admin()
    if problem:
        return problem
    b, gid, by = body(), discord_settings()["guild_id"], user.get("name") or "Admin"
    rows, error, _ = discord_call("GET", f"/guilds/{gid}/channels", timeout=10)
    if error:
        return err(error, 502)
    state = get_setting("discord.lockdown", {}) or {}
    on, done, failed = bool(b.get("on")), [], 0
    targets = [c for c in rows or [] if c.get("type") in (0, 5)] if on else [c for c in rows or [] if c["id"] in (state.get("channels") or [])]
    for c in targets:
        ow = next((o for o in c.get("permission_overwrites") or [] if o.get("id") == gid), {})
        allow, deny = int(ow.get("allow") or 0), int(ow.get("deny") or 0)
        if on and (deny & 1024 or deny & DISCORD_SEND == DISCORD_SEND):
            continue  # hidden from @everyone (staff or buyer channels), or locked already: leave it alone
        deny = deny | DISCORD_SEND if on else deny & ~DISCORD_SEND
        allow = allow & ~DISCORD_SEND if on else allow
        _, e, _ = discord_call("PUT", f"/channels/{c['id']}/permissions/{gid}", payload={"type": 0, "allow": str(allow), "deny": str(deny)},
                               reason=f"{'Lockdown' if on else 'Lockdown ended'} by {by}")
        if e:
            failed += 1
        else:
            done.append(c["id"])
    set_setting("discord.lockdown", {"on": True, "channels": done, "at": utc_stamp(), "by": by} if on else {})
    note, channel = str(b.get("note") or "").strip()[:500], str(b.get("channel_id") or "")
    if note and SNOWFLAKE.match(channel):
        discord_call("POST", f"/channels/{channel}/messages", payload={"content": ("🚨 " if on else "✅ ") + note, "allowed_mentions": {"parse": []}})
    discord_modlog(f"{'🚨 Lockdown' if on else '✅ Lockdown ended'}: {len(done)} channel(s)", [f"By {by}", note], "#e5484d" if on else "#4cc38a")
    audit(user, "discord_lockdown", f"{'on' if on else 'off'}: {len(done)} channels, {failed} failed")
    return jsonify({"ok": True, "channels": len(done), "failed": failed})


DISCORD_SAFETY = {"verification_level": (0, 4), "explicit_content_filter": (0, 2), "default_message_notifications": (0, 1)}


@app.get("/api/admin/discord/safety")
def admin_discord_safety():
    user, problem = discord_admin()
    if problem:
        return problem
    guild, error, _ = discord_call("GET", f"/guilds/{discord_settings()['guild_id']}", timeout=10)
    if error:
        return err(error, 502)
    return jsonify({k: guild.get(k) for k in DISCORD_SAFETY})


@app.post("/api/admin/discord/safety")
def admin_save_discord_safety():
    user, problem = discord_admin()
    if problem:
        return problem
    b = body()
    payload = {k: clamp_int(b.get(k), lo, lo, hi) for k, (lo, hi) in DISCORD_SAFETY.items() if k in b}
    if not payload:
        return err("Nothing to change", 400)
    _, error, _ = discord_call("PATCH", f"/guilds/{discord_settings()['guild_id']}", payload=payload, reason=f"By {user.get('name')}")
    if error:
        return err(discord_perm_hint(error) + (" (The highest levels may need 2FA on the owner's account.)" if "2FA" in error or "60003" in error else ""), 502)
    discord_modlog("🛡️ Server safety settings changed", [f"By {user.get('name')}", ", ".join(f"{k} = {v}" for k, v in payload.items())])
    audit(user, "discord_safety", json.dumps(payload))
    return jsonify({"ok": True, **payload})


@app.get("/api/admin/discord/snapshot")
def admin_discord_snapshot():
    """Roles, channels and their permissions as a file: a record to rebuild the server by hand if something goes wrong."""
    user, problem = discord_admin()
    if problem:
        return problem
    gid = discord_settings()["guild_id"]
    guild, e1, _ = discord_call("GET", f"/guilds/{gid}", timeout=10)
    roles, e2, _ = discord_call("GET", f"/guilds/{gid}/roles", timeout=10)
    channels, e3, _ = discord_call("GET", f"/guilds/{gid}/channels", timeout=10)
    if e1 or e2 or e3:
        return err(e1 or e2 or e3, 502)
    names = {r["id"]: r.get("name") for r in roles or []}
    cats = {c["id"]: c.get("name") for c in channels or [] if c.get("type") == 4}
    snap = {"server": (guild or {}).get("name"), "taken": utc_stamp() + " UTC", "by": user.get("name"),
            "roles": [{k: r.get(k) for k in ("name", "color", "position", "hoist", "mentionable", "permissions", "managed")} for r in sorted(roles or [], key=lambda r: -r.get("position", 0))],
            "channels": [{"name": c.get("name"), "type": {0: "text", 2: "voice", 4: "category", 5: "announcement", 13: "stage", 15: "forum"}.get(c.get("type"), c.get("type")),
                          "category": cats.get(c.get("parent_id"), ""), "topic": c.get("topic") or "", "position": c.get("position"),
                          "slowmode": c.get("rate_limit_per_user") or 0,
                          "permissions": [{"for": names.get(o["id"], "member " + o["id"]) if o.get("type") == 0 else "member " + o["id"], "allow": o.get("allow"), "deny": o.get("deny")}
                                          for o in c.get("permission_overwrites") or []]} for c in sorted(channels or [], key=lambda c: (c.get("parent_id") or "", c.get("position", 0)))]}
    audit(user, "discord_snapshot", f"{len(snap['roles'])} roles, {len(snap['channels'])} channels")
    return jsonify(snap)


# --- members: rules gate, a role for everyone who links, welcome text and DM, thank-you DM to buyers ---

@app.post("/api/admin/discord/rules-gate")
def admin_discord_rules_gate():
    user, problem = discord_admin()
    if problem:
        return problem
    b, cfg = body(), discord_settings()
    channel, role = str(b.get("channel_id") or ""), str(b.get("role_id") or "")
    rules = str(b.get("rules") or "").strip()[:4000]
    if not SNOWFLAKE.match(channel) or not rules:
        return err("Choose the rules channel and write the rules", 400)
    roles, error, _ = discord_call("GET", f"/guilds/{cfg['guild_id']}/roles", timeout=10)
    if error:
        return err(error, 502)
    if not any(r["id"] == role and discord_role_ok(r, cfg) for r in roles or []):
        return err("Choose the role people get after accepting (a normal role, not the buyer role)", 400)
    button = {"type": 2, "style": 3, "label": str(b.get("label") or "I accept the rules")[:80], "emoji": {"name": "✅"}, "custom_id": "rules_accept"}
    _, error, _ = discord_call("POST", f"/channels/{channel}/messages", payload={
        "embeds": [{"title": str(b.get("title") or "📜 Server rules")[:256], "description": rules, "color": 0x4CC38A}],
        "components": [{"type": 1, "components": [button]}], "allowed_mentions": {"parse": []}})
    if error:
        return err(discord_perm_hint(error), 502)
    set_setting("discord.extra", {**discord_extra(), "rules_role": role})
    audit(user, "discord_rules_gate", channel)
    return jsonify({"ok": True})


def discord_rules_accept(event):
    role, member = discord_extra()["rules_role"], event.get("member") or {}
    discord_id = str((member.get("user") or {}).get("id") or "")
    if not role or not SNOWFLAKE.match(discord_id):
        return discord_reply("This button is not in use any more.")
    if role in (member.get("roles") or []):
        return discord_reply("You accepted the rules already. Welcome! ✈️")
    _, error, status = discord_call("PUT", f"/guilds/{discord_settings()['guild_id']}/members/{discord_id}/roles/{role}", reason="Accepted the rules")
    if error:
        return discord_reply("I could not give you the role. The team has to move the bot's role above it." if status == 403 else "Something went wrong. Try again in a minute.")
    return discord_reply("✅ Thanks! You can now see the rest of the server. Have a look at /help.")


def discord_link_role(discord_id, give):
    role = discord_extra()["link_role"]
    if role and discord_ready() and SNOWFLAKE.match(str(discord_id or "")):
        discord_call("PUT" if give else "DELETE", f"/guilds/{discord_settings()['guild_id']}/members/{discord_id}/roles/{role}", reason="Linked a Rotaidem account")


def discord_welcome_dm(discord_id, name):
    text = discord_extra()["welcome_dm"]
    if text:
        discord_dm(discord_id, {"content": discord_render(text, name=name)[:2000], "components": discord_link_row(("My account", "/account"))})


def discord_buyer_thanks(user, product_name):
    """A thank-you DM in Discord after a purchase, for buyers who linked Discord. Never fails the caller."""
    try:
        text = discord_extra()["buyer_dm"]
        if text and user and user.get("discord_id") and discord_ready():
            discord_dm(user["discord_id"], {"content": discord_render(text, name=user.get("name") or "there", product=product_name)[:2000],
                                            "components": discord_link_row(("My licence keys", "/account"), ("Install guide", "/installation"))})
    except Exception:  # noqa: BLE001
        app.logger.exception("Buyer thank-you DM failed")


# --- customers in Discord: /orders, /faq answers, "Send me my keys" ---

def discord_cmd_orders(event):
    _, _, user = discord_member(event)
    if not user:
        return discord_reply("Link your Discord on your Rotaidem account first (My account, Discord).", components=discord_link_row(("My account", "/account#discord")))
    rows = db().all("SELECT o.amount, o.currency, o.payment_status, o.created_at, o.product_id, p.name FROM orders o LEFT JOIN products p ON p.id = o.product_id "
                    "WHERE o.customer_id = ? ORDER BY o.id DESC LIMIT 5", user["id"])
    if not rows:
        return discord_reply("You have no orders on this account yet.", components=discord_link_row(("Store", "/addons/")))
    lines = [f"• **{r['name'] or r['product_id']}**: {float(r['amount'] or 0):.2f} {str(r['currency'] or '').upper()}, {r['payment_status'] or ''}, {str(r['created_at'] or '')[:10]}" for r in rows]
    return discord_reply("Your latest orders (only you can see this):\n" + "\n".join(lines), components=discord_link_row(("All orders", "/account")))


def discord_faq_items():
    return get_setting("site.faq", []) or []


def discord_faq_autocomplete(event):
    typed = str(next((o.get("value") for o in (event.get("data") or {}).get("options") or [] if o.get("focused")), "") or "").lower().strip()
    items = list(enumerate(discord_faq_items()))
    hits = [(i, f) for i, f in items if typed in f["q"].lower()] if typed else items
    return {"type": 8, "data": {"choices": [{"name": f["q"][:100], "value": str(i)} for i, f in hits[:25]]}}


def discord_cmd_faq(event):
    q = str(discord_opts(event).get("question") or "")
    items = discord_faq_items()
    if q.isdigit() and int(q) < len(items):
        f = items[int(q)]
        return discord_reply(embeds=[{"title": "❓ " + f["q"][:250], "description": f["a"][:4000], "color": 0xAAB6C2}], public=True,
                             components=discord_link_row(("All questions", "/support")))
    return discord_reply("Answers to common questions (type /faq and pick a question to show it here):",
                         components=discord_link_row(("Help & FAQ", "/support"), ("Install guide", "/installation")))


def discord_keys_dm(event):
    discord_id, _, user = discord_member(event)
    if not user:
        return discord_reply("Link your Discord on your Rotaidem account first.", components=discord_link_row(("My account", "/account#discord")))
    if not discord_claim(f"keys.{discord_id}.{int(time.time()) // 600}"):
        return discord_reply("I sent them a moment ago. Look in your DMs.")
    rows = db().all("SELECT l.license_key, l.product_id, p.name FROM licenses l LEFT JOIN products p ON p.id = l.product_id "
                    "WHERE l.status = 'active' AND (l.customer_id = ? OR LOWER(l.email) = ?) ORDER BY l.id DESC LIMIT 20", user["id"], (user.get("email") or "").lower())
    if not rows:
        return discord_reply("There are no active licence keys on your account.")
    text = "🔑 Your Rotaidem licence keys. Keep them private:\n" + "\n".join(f"**{r['name'] or r['product_id']}**: `{r['license_key']}`" for r in rows)
    error = discord_dm(discord_id, {"content": text[:2000]})
    return discord_reply("Could not DM you. Allow DMs from server members (Privacy settings) and try again, or see My account." if error else "📬 Sent to your DMs.")


# --- ticket follow-up: one open ticket each, a rating, the conversation by email ---

def discord_ticket_followup(t):
    """After a ticket closed (run from discord_jobs): ask for a rating, and email the conversation to linked members."""
    ex = discord_extra()
    if ex["ticket_rating"]:
        buttons = [{"type": 2, "style": 2, "label": "⭐" * n, "custom_id": f"rate:{t['ref']}:{n}"} for n in range(1, 6)]
        discord_dm(t["member"], {"content": f"Your Rotaidem ticket **{t.get('title', '')}** is closed. How did we do?", "components": [{"type": 1, "components": buttons}]})
    acc = db().first("SELECT * FROM users WHERE discord_id = ?", t["member"]) if ex["ticket_email"] else None
    if acc and acc.get("email") and t.get("transcript") and RESEND_API_KEY:
        text = f"Here is your support conversation with us, for your records.\n\n{t['transcript'][:12000]}"
        send_one(acc["email"], "Your Rotaidem support ticket", reply_email_html(acc.get("name"), text, "The Rotaidem team"))


def discord_ticket_rate(event, thread_id, stars):
    discord_id = discord_member(event)[0]
    t = ditem_row(db().first("SELECT * FROM discord_items WHERE kind = 'ticket' AND ref = ? AND member = ?", thread_id, discord_id))
    if not t or not stars.isdigit() or not 1 <= int(stars) <= 5:
        return discord_reply("This rating is not active any more.")
    if not t.get("rating"):
        ditem_save(t, rating=int(stars))
        discord_modlog(f"{'⭐' * int(stars)} for ticket {t.get('title', '')}", [f"<@{discord_id}>"])
    return {"type": 7, "data": {"content": f"Thanks for your rating: {'⭐' * int(t.get('rating') or stars)}", "components": []}}


# --- posting as another name and picture (personas) through a channel webhook ---

def discord_persona(b):
    personas = discord_extra()["personas"]
    try:
        i = int(b.get("persona"))
    except (TypeError, ValueError):
        return None
    return personas[i] if 0 <= i < len(personas) else None


def discord_webhook(channel, fresh=False):
    hooks = get_setting("discord.webhooks", {}) or {}
    if channel in hooks and not fresh:
        return hooks[channel], ""
    made, error, _ = discord_call("POST", f"/channels/{channel}/webhooks", payload={"name": "Rotaidem"})
    if error:
        return None, error
    hooks[channel] = {"id": made["id"], "token": made["token"]}
    set_setting("discord.webhooks", hooks)
    return hooks[channel], ""


def discord_post_message(channel, message, persona):
    """(sent message, error, webhook used): through the bot, or through a webhook with a persona's name and picture."""
    if not persona:
        sent, error, _ = discord_call("POST", f"/channels/{channel}/messages", payload=message)
        return sent, error, False
    for fresh in (False, True):  # a webhook someone deleted in Discord: make a new one once
        hook, error = discord_webhook(channel, fresh)
        if error:
            return None, discord_perm_hint(error), True
        payload = {**message, "username": persona["name"][:80]}
        if persona.get("avatar"):
            payload["avatar_url"] = discord_absolute(persona["avatar"])
        sent, error, status = discord_call("POST", f"/webhooks/{hook['id']}/{hook['token']}?wait=true&with_components=true", payload=payload)
        if status != 404:
            return sent, error, True
    return None, error, True


def discord_hook_path(channel, message_id):
    hook = (get_setting("discord.webhooks", {}) or {}).get(channel)
    return f"/webhooks/{hook['id']}/{hook['token']}/messages/{message_id}" if hook else ""


@app.post("/api/admin/discord/personas")
def admin_save_discord_personas():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    out = []
    for p in (body().get("personas") or [])[:5]:
        name, avatar = " ".join(str((p or {}).get("name") or "").split())[:80], str((p or {}).get("avatar") or "").strip()
        if not name or re.search(r"discord|clyde", name, re.I):
            return err("Each name needs some text, and Discord does not allow the words Discord or Clyde in it", 400)
        if avatar and not valid_image_url(avatar):
            return err("The picture must be an uploaded picture or an https:// link", 400)
        out.append({"name": name, "avatar": avatar})
    set_setting("discord.extra", {**discord_extra(), "personas": out})
    audit(user, "discord_personas", ", ".join(p["name"] for p in out) or "none")
    return jsonify({"ok": True, "personas": out})


# --- message templates for the composer ---

@app.get("/api/admin/discord/templates")
def admin_discord_templates():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"templates": get_setting("discord.templates", []) or []})


@app.post("/api/admin/discord/templates")
def admin_save_discord_template():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    b = body()
    name = " ".join(str(b.get("name") or "").split())[:60]
    draft = {k: b.get("draft", {}).get(k) for k in ("text", "embed", "buttons", "ping", "poll", "reactions", "persona")} if isinstance(b.get("draft"), dict) else {}
    if not name or not draft:
        return err("Give the template a name", 400)
    try:
        discord_message({**draft, "text": draft.get("text") or "·"})  # the same checks as posting
    except ValueError as exc:
        return err(str(exc), 400)
    items = [t for t in get_setting("discord.templates", []) or [] if t["name"].lower() != name.lower()]
    if len(items) >= 30:
        return err("Up to 30 templates", 400)
    set_setting("discord.templates", sorted(items + [{"name": name, "draft": draft}], key=lambda t: t["name"].lower()))
    audit(user, "discord_template", name)
    return jsonify({"ok": True})


@app.delete("/api/admin/discord/templates")
def admin_delete_discord_template():
    name = str(request.args.get("name") or "")
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    set_setting("discord.templates", [t for t in get_setting("discord.templates", []) or [] if t["name"] != name])
    return jsonify({"ok": True})


# --- licence holders who have not linked Discord: invite them by email ---

def discord_unlinked_holders():
    linked = db().all("SELECT id, email FROM users WHERE discord_id IS NOT NULL AND discord_id <> ''")
    linked_ids, linked_emails = {u["id"] for u in linked}, {(u["email"] or "").lower() for u in linked}
    gone = {(r["email"] or "").lower() for r in db().all("SELECT email FROM subscribers WHERE unsubscribed = 1")}
    out = {}
    for r in db().all("SELECT l.email, l.customer_id, l.product_id, u.name FROM licenses l LEFT JOIN users u ON u.id = l.customer_id "
                      "WHERE l.status = 'active' AND l.email IS NOT NULL AND l.email <> ''"):
        email = r["email"].lower()
        if email in linked_emails or r["customer_id"] in linked_ids or email in gone or not EMAIL_RE.match(email):
            continue
        item = out.setdefault(email, {"email": email, "name": r.get("name") or "", "products": []})
        if r["product_id"] not in item["products"]:
            item["products"].append(r["product_id"])
    invited = {r["key"][len("discord.claim.invite."):] for r in db().all("SELECT key FROM meta WHERE key LIKE ?", "discord.claim.invite.%")}
    return [{**v, "invited": k in invited} for k, v in sorted(out.items())]


@app.get("/api/admin/discord/unlinked")
def admin_discord_unlinked():
    if not manager_user():
        return err("Only the Owner or an Admin can do this", 403)
    people = discord_unlinked_holders()
    return jsonify({"people": people[:300], "total": len(people), "waiting": sum(1 for p in people if not p["invited"])})


@app.post("/api/admin/discord/unlinked/email")
def admin_discord_unlinked_email():
    """Email up to 50 licence holders (not invited before) how to join the Discord and get the buyer role."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can do this", 403)
    if not RESEND_API_KEY:
        return err("Email is not set up (RESEND_API_KEY)", 503)
    b = body()
    subject = " ".join(str(b.get("subject") or "").split())[:120] or "Join the Rotaidem Discord"
    template = str(b.get("text") or "").strip()[:3000] or ("You have a Rotaidem licence, so you can join the buyer-only channels in our Discord: early news, "
                                                            "help from other pilots and from us.\n\nOpen My account and press Link Discord: {link}")
    batch, messages = [], []
    for p in [p for p in discord_unlinked_holders() if not p["invited"]][:50]:
        if discord_claim("invite." + p["email"]):
            batch.append(p)
            text = discord_render(template, link=site_url() + "/account#discord", invite=discord_settings().get("invite_url") or "", name=p["name"] or "there")
            text += f"\n\nDon't want emails like this? Unsubscribe: {unsubscribe_link(p['email'])}"
            messages.append({"from": MAIL_FROM, "to": [p["email"]], "subject": subject, "html": reply_email_html(p["name"], text, "The Rotaidem team"),
                             "reply_to": site_config()["contact_email"], "headers": unsubscribe_headers(p["email"])})
    if not messages:
        return jsonify({"ok": True, "sent": 0, "left": 0})
    sent, error = send_batch(messages)
    if not sent:
        for p in batch:  # not sent: they can be invited next time
            db().run("DELETE FROM meta WHERE key = ?", "discord.claim.invite." + p["email"])
        return err(error or "Sending failed", 502)
    audit(user, "discord_invite_emails", f"{len(messages)} sent")
    return jsonify({"ok": True, "sent": len(messages), "left": sum(1 for p in discord_unlinked_holders() if not p["invited"])})


# ---------- your data: download it, delete your account, and old data cleaned up automatically ----------

def account_export(user):
    """Everything the website keeps about one account (GDPR: the right of access and to take your data with you)."""
    uid, email = user["id"], (user.get("email") or "").lower()
    pick = lambda row, keys: {k: row.get(k) for k in keys}  # noqa: E731
    lic_rows = db().all("SELECT * FROM licenses WHERE customer_id = ? OR LOWER(email) = ?", uid, email)
    licences = []
    for lic in lic_rows:
        acts = db().all("SELECT machine_name, first_seen, last_seen FROM license_activations WHERE license_id = ?", lic["id"])
        licences.append({**pick(lic, ("license_key", "product_id", "status", "max_activations", "created_at")), "computers": acts})
    discord_rows = db().all("SELECT kind, status, data, created_at FROM discord_items WHERE member = ?", user.get("discord_id") or "-") if user.get("discord_id") else []
    aff = db().first("SELECT * FROM affiliates WHERE user_id = ? OR LOWER(email) = ?", uid, email)
    return {
        "exported_at": utc_stamp() + " UTC", "about": "All personal data rotaidem.com keeps about this account.",
        "account": pick(user, ("name", "email", "role", "created_at", "email_verified", "terms_accepted_at", "terms_version")),
        "discord": pick(user, ("discord_id", "discord_name", "discord_linked_at")) if user.get("discord_id") else None,
        "orders": db().all("SELECT o.product_id, o.amount, o.currency, o.payment_status, o.created_at, o.consent FROM orders o WHERE o.customer_id = ? ORDER BY o.id", uid),
        "licences": licences,
        "logged_in_devices": db().all("SELECT ip_address, user_agent, created_at, last_seen FROM sessions WHERE user_id = ?", uid),
        "downloads": db().all("SELECT product_id, created_at FROM downloads WHERE user_id = ?", uid),
        "messages_to_us": db().all("SELECT name, message, reply, created_at FROM messages WHERE LOWER(email) = ?", email),
        "newsletter": db().first("SELECT source, unsubscribed, created_at FROM subscribers WHERE LOWER(email) = ?", email),
        "affiliate": pick(aff, ("code", "percent", "status", "payout", "created_at")) if aff else None,
        "discord_bot": [{"kind": r["kind"], "status": r["status"], "created_at": r["created_at"], "data": json.loads(r["data"] or "{}")} for r in discord_rows],
    }


@app.get("/api/account/export")
def account_export_route():
    user = current_user()
    if not user:
        return err("Log in first", 401)
    audit(user, "data_export", user.get("email") or "")
    resp = jsonify(account_export(user))
    resp.headers["Content-Disposition"] = 'attachment; filename="rotaidem-my-data.json"'
    return resp


@app.post("/api/account/delete")
def account_delete():
    """Delete your account. Orders stay (Swedish bookkeeping law: 7 years) but no longer point to you by name or email."""
    user = current_user()
    if not user:
        return err("Log in first", 401)
    if user["role"] in STAFF:
        return err("Team accounts cannot be deleted here. Ask the Owner to remove you under Admin → Users first.", 400)
    if not verify_password(str(body().get("password") or ""), user["password_hash"]):
        return err("The password is not right", 403)
    uid, email = user["id"], (user.get("email") or "").lower()
    gone = f"deleted-{uid}@deleted.invalid"
    if user.get("discord_id"):
        try:
            if discord_ready():
                cfg = discord_settings()
                discord_call("DELETE", f"/guilds/{cfg['guild_id']}/members/{user['discord_id']}/roles/{cfg['role_id']}")
                discord_link_role(user["discord_id"], False)
        except Exception:  # noqa: BLE001 - deleting the account must not fail over Discord
            app.logger.exception("Removing Discord roles on account deletion failed")
        db().run("DELETE FROM discord_items WHERE member = ?", user["discord_id"])
    for lic in db().all("SELECT id FROM licenses WHERE customer_id = ? OR LOWER(email) = ?", uid, email):
        db().run("UPDATE license_activations SET machine_name = '' WHERE license_id = ?", lic["id"])
    db().run("UPDATE licenses SET email = ? WHERE customer_id = ? OR LOWER(email) = ?", gone, uid, email)  # keys keep working in the app
    db().run("UPDATE stripe_payments SET email = ? WHERE user_id = ? OR LOWER(email) = ?", gone, uid, email)
    db().run("UPDATE downloads SET user_id = NULL, ip_address = NULL WHERE user_id = ?", uid)
    db().run("DELETE FROM sessions WHERE user_id = ?", uid)
    db().run("DELETE FROM subscribers WHERE LOWER(email) = ?", email)
    db().run("DELETE FROM messages WHERE LOWER(email) = ?", email)
    db().run("UPDATE affiliates SET email = ?, name = 'Deleted user', payout = NULL, status = 'inactive' WHERE user_id = ? OR LOWER(email) = ?", gone, uid, email)
    db().run("UPDATE users SET name = 'Deleted user', email = ?, password_hash = ?, token = NULL, discord_id = NULL, discord_name = NULL, "
             "discord_linked_at = NULL, discord_result = NULL, admin_note = NULL, email_verified = 0 WHERE id = ?", gone, hash_password(secrets.token_hex(24)), uid)
    db().run("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'account_deleted', ?, '', ?)", uid, f"user {uid}", utc_stamp())
    return jsonify({"ok": True})


RETENTION = {"downloads_ip_days": 30, "audit_ip_days": 180, "login_failures_days": 2, "sent_reminders_days": 30, "giveaway_entries_days": 90,
             "handled_messages_days": 730}


def privacy_cleanup():
    """Once a day: personal data we no longer need is removed (what the privacy policy promises)."""
    now = time.time()
    if now - float(get_setting("privacy.cleaned_at", 0) or 0) < 86400:
        return
    set_setting("privacy.cleaned_at", now)
    r = RETENTION
    try:
        db().run("UPDATE downloads SET ip_address = NULL WHERE ip_address IS NOT NULL AND created_at < ?", utc_stamp(r["downloads_ip_days"] * 86400))
        db().run("UPDATE audit_logs SET ip_address = '' WHERE ip_address <> '' AND created_at < ?", utc_stamp(r["audit_ip_days"] * 86400))
        db().run("DELETE FROM license_failures WHERE created_at < ?", utc_stamp(r["login_failures_days"] * 86400))
        db().run("DELETE FROM sessions WHERE last_seen < ?", utc_stamp(SESSION_DAYS * 86400))
        db().run("DELETE FROM messages WHERE handled = 1 AND created_at < ?", utc_stamp(r["handled_messages_days"] * 86400))
        db().run("DELETE FROM discord_items WHERE kind = 'reminder' AND status = 'sent' AND created_at < ?", utc_stamp(r["sent_reminders_days"] * 86400))
        old = [g["id"] for g in db().all("SELECT id FROM discord_items WHERE kind = 'giveaway' AND status = 'ended' AND created_at < ?", utc_stamp(r["giveaway_entries_days"] * 86400))]
        for gw_id in old:
            db().run("DELETE FROM discord_items WHERE kind = 'gw_entry' AND ref = ?", gw_id)
    except Exception:  # noqa: BLE001 - visitors never see a clean-up problem
        app.logger.exception("Privacy clean-up failed")


# ---------- RotaHub: what the desktop app shows, set in Admin -> RotaHub ----------

HUB_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")
HUB_PREFIX = re.compile(r"^[A-Z0-9]{2,8}$")
HUB_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
HUB_LEVELS = ("info", "warning")
HUB_PRODUCT_FIELDS = ("hidden", "featured", "manifest_url", "licence_ids", "licence_store", "key_prefix", "image")
HUB_TEXTS = {"login_title": 60, "login_text": 300, "library_title": 40, "library_text": 300}
HUB_IMAGES = ("login_image", "library_image", "product_image")


def hub_image(value, what):
    v = str(value or "").strip()
    if v and not valid_image_url(v):
        raise ValueError(f"The {what} must be an uploaded picture or an https:// link")
    return v


def hub_link(value, what):
    """An https:// address or a page on the site (/downloads); '' for none."""
    v = str(value or "").strip()
    if v and (len(v) > 400 or not (v.startswith("https://") or (v.startswith("/") and not v.startswith("//")))):
        raise ValueError(f"The {what} must start with https:// or / (a page on the site)")
    return v


def clean_hub(b):
    """RotaHub settings from the admin, checked; raises ValueError with a message for the admin."""
    m = b.get("message") or {}
    text = str(m.get("text") or "").strip()
    if len(text) > 300:
        raise ValueError("The message in the app can be at most 300 characters")
    if m.get("enabled") and not text:
        raise ValueError("Write the message before switching it on")
    level = m.get("level") if m.get("level") in HUB_LEVELS else "info"
    min_version = str(b.get("min_version") or "").strip()
    if min_version and not HUB_VERSION.match(min_version):
        raise ValueError("The lowest version looks like 0.2.0")
    products, seen = [], set()
    for p in (b.get("products") or [])[:60]:
        pid = str((p or {}).get("id") or "").strip().lower()
        if not HUB_ID.match(pid) or pid in seen:
            continue
        seen.add(pid)
        ids = p.get("licence_ids") or []
        ids = [x.strip().lower() for x in (ids.split(",") if isinstance(ids, str) else ids) if str(x).strip()]
        if len(ids) > 5 or not all(HUB_ID.match(x) for x in ids):
            raise ValueError(f"{pid}: licence product ids are short ids like heavypro, separated by commas (at most 5)")
        store = str(p.get("licence_store") or "").strip().lower()
        if store and not HUB_ID.match(store):
            raise ValueError(f"{pid}: the licence folder is one short name like heavypro, no slashes")
        prefix = str(p.get("key_prefix") or "").strip().upper()
        if prefix and not HUB_PREFIX.match(prefix):
            raise ValueError(f"{pid}: the key prefix is 2 to 8 letters or digits, like HPRO")
        manifest = str(p.get("manifest_url") or "").strip()
        if manifest and (not manifest.startswith("https://") or len(manifest) > 400):
            raise ValueError(f"{pid}: the release file must be an https:// address")
        entry = {"id": pid, "hidden": bool(p.get("hidden")), "featured": bool(p.get("featured")), "manifest_url": manifest,
                 "licence_ids": ids, "licence_store": store, "key_prefix": prefix, "image": hub_image(p.get("image"), f"{pid} picture")}
        if any(entry[k] for k in HUB_PRODUCT_FIELDS):
            products.append(entry)
    look_in = b.get("look") or {}
    accent = look_in.get("accent") if look_in.get("accent") in ACCENTS else ""
    look = {"accent": accent, "accent_color": ACCENTS[accent][0] if accent else ""}
    for key in HUB_IMAGES:
        look[key] = hub_image(look_in.get(key), key.replace("_", " "))
    for key, limit in HUB_TEXTS.items():
        text_value = str(look_in.get(key) or "").strip()
        if len(text_value) > limit:
            raise ValueError(f"The {key.replace('_', ' ')} can be at most {limit} characters")
        look[key] = text_value
    d = b.get("downloads") or {}
    rotahub_url = str(d.get("rotahub_url") or "").strip()
    if rotahub_url and (len(rotahub_url) > 400 or not (rotahub_url.startswith("https://") or re.match(r"^/files/[A-Za-z0-9._-]+(\?v=[A-Za-z0-9]+)?$", rotahub_url))):
        raise ValueError("The RotaHub download link must start with https:// or be an uploaded file (/files/...)")
    return {"message": {"enabled": bool(m.get("enabled")), "text": text, "level": level, "link": hub_link(m.get("link"), "message link")},
            "min_version": min_version, "help_url": hub_link(b.get("help_url"), "help link"), "products": products, "look": look,
            "downloads": {"rotahub_url": rotahub_url},
            # RotaHub's own version history: shown in the app when it updates, and on the Downloads page
            "changelog": clean_changelog(b.get("changelog"))}


def hub_config():
    saved = get_setting("hub.config", {}) or {}
    try:
        return clean_hub(saved)
    except ValueError:  # saved by an older version with other rules: show nothing rather than fail
        return clean_hub({})


def version_key(v):
    return tuple(int(x) for x in v.split("."))


def hub_stats(min_version=""):
    """Who uses RotaHub, from the website logins it makes (User-Agent RotaHub/0.2.0 ...)."""
    rows = db().all("SELECT user_id, user_agent, last_seen FROM sessions WHERE user_agent LIKE ?", "RotaHub%")
    week = utc_stamp(7 * 86400)
    versions = {}
    for r in rows:
        m = re.match(r"RotaHub/(\d{1,4}\.\d{1,4}\.\d{1,4})", str(r["user_agent"] or ""))
        v = m.group(1) if m else "older"
        versions.setdefault(v, set()).add(r["user_id"])
    listed = sorted(versions.items(), key=lambda kv: version_key(kv[0]) if kv[0] != "older" else (-1,), reverse=True)
    outdated = {u for v, users in versions.items() if min_version and (v == "older" or version_key(v) < version_key(min_version)) for u in users}
    return {"people": len({r["user_id"] for r in rows}), "logins": len(rows),
            "active_7d": len({r["user_id"] for r in rows if str(r["last_seen"] or "") >= week}),
            "versions": [{"version": v, "people": len(users)} for v, users in listed], "outdated": len(outdated)}


# ---------- download files (Admin -> RotaHub -> Files) ----------
# Uploaded in pieces from the browser (the host refuses big requests) into the website's files/
# folder, where Apache sends them directly; elsewhere into the data folder, sent by /files/<name>.

FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(exe|msi|zip|7z|pdf)$")
FILE_MAGIC = {".exe": b"MZ", ".zip": b"PK", ".msi": bytes.fromhex("d0cf11e0"), ".7z": b"7z", ".pdf": b"%PDF"}
FILE_CHUNK_MAX = 1024 * 1024
FILE_MAX = 300 * 1024 * 1024


def files_dir():
    public = os.environ.get("ROTAIDEM_PUBLIC_DIR", "")
    folder = os.path.join(public, "files") if public and os.path.isdir(public) else os.path.join(str(DATA_DIR), "files")
    os.makedirs(folder, exist_ok=True)
    os.chmod(folder, 0o755)
    return folder


def file_row(name, folder=None):
    path = os.path.join(folder or files_dir(), name)
    st = os.stat(path)
    return {"name": name, "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "url": "" if folder else f"/files/{name}?v={int(st.st_mtime):x}"}


@app.get("/api/admin/files")
def admin_files():
    if not manager_user():
        return err("Only the Owner or an Admin can see the files", 403)
    names = sorted(n for n in os.listdir(files_dir()) if FILE_NAME.match(n))
    return jsonify({"files": [file_row(n) for n in names]})


@app.post("/api/admin/files")
def admin_upload_file():
    """One piece of a file: {name, upload_id, index, total, data (base64), private}. The last piece puts the file in
    place: the public files/ folder, or with "private" the testers' folder that only signed tester links reach."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can upload files", 403)
    b = body()
    name, upload_id = str(b.get("name") or "").strip(), str(b.get("upload_id") or "")
    if not FILE_NAME.match(name):
        return err("Use a file name without spaces that ends in .exe, .msi, .zip, .7z or .pdf, like MyProductSetup.exe", 400)
    if not re.match(r"^[a-f0-9]{16,40}$", upload_id):
        return err("Bad upload", 400)
    try:
        index, total = int(b.get("index")), int(b.get("total"))
        piece = base64.b64decode(str(b.get("data") or ""), validate=True)
    except (TypeError, ValueError):
        return err("Bad upload", 400)
    if not (0 <= index < total <= FILE_MAX // 1024) or len(piece) > FILE_CHUNK_MAX:
        return err("Bad upload", 400)
    uploads = os.path.join(str(DATA_DIR), "uploads")
    os.makedirs(uploads, exist_ok=True)
    part = os.path.join(uploads, upload_id + ".part")
    done = os.path.getsize(part) if index and os.path.exists(part) else 0
    if index and not os.path.exists(part):
        return err("The upload was interrupted. Start it again.", 409)
    if done + len(piece) > FILE_MAX:
        os.remove(part)
        return err("Files can be at most 300 MB", 400)
    with open(part, "ab" if index else "wb") as fh:
        fh.write(piece)
    if index < total - 1:
        return jsonify({"ok": True, "received": done + len(piece)})
    with open(part, "rb") as fh:
        head = fh.read(4)
    magic = FILE_MAGIC[os.path.splitext(name)[1].lower()]
    if not head.startswith(magic):
        os.remove(part)
        return err(f"That does not look like a real {os.path.splitext(name)[1]} file. Nothing was saved.", 400)
    folder = tester_dir() if b.get("private") else None
    target = os.path.join(folder or files_dir(), name)
    digest = hashlib.sha256()
    with open(part, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    os.chmod(part, 0o644)
    shutil.move(part, target)
    write_sha256(target, digest.hexdigest())  # RotaHub checks downloads against it
    audit(user, "upload_file", name + (" (tester app)" if folder else ""))
    return jsonify({"ok": True, "file": file_row(name, folder)})


@app.delete("/api/admin/files/<name>")
def admin_delete_file(name):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can delete files", 403)
    path = os.path.join(files_dir(), name)
    if not FILE_NAME.match(name) or not os.path.isfile(path):
        return err("File not found", 404)
    os.remove(path)
    if os.path.exists(path + ".sha256"):
        os.remove(path + ".sha256")
    audit(user, "delete_file", name)
    return jsonify({"ok": True})


@app.get("/files/<name>")
def download_file(name):
    """Uploaded files when no web server sends them (local copies; on STRATO, Apache does)."""
    if not FILE_NAME.match(name) or not os.path.isfile(os.path.join(files_dir(), name)):
        abort(404)
    return send_from_directory(files_dir(), name, as_attachment=True)


FILE_LINK = re.compile(r"^/files/[A-Za-z0-9._-]+(\?v=[A-Za-z0-9]+)?$")


def write_sha256(path, sha):
    """NAME.sha256 next to a download, in the format of sha256sum (as setup.py fetch-installer writes it)."""
    temp = f"{path}.sha256.{os.getpid()}.tmp"
    with open(temp, "w") as fh:
        fh.write(f"{sha}  {os.path.basename(path)}\n")
    os.chmod(temp, 0o644)
    os.replace(temp, path + ".sha256")


def download_facts(url):
    """SHA-256 and size of a product's installer when it is a file on this site (/files/...); else empty."""
    path = urllib.parse.urlparse(str(url or "")).path
    base = urllib.parse.urlparse(site_url()).netloc
    host = urllib.parse.urlparse(str(url or "")).netloc
    name = path[len("/files/"):] if path.startswith("/files/") and (not host or host == base) else ""
    if not FILE_NAME.match(name):
        return "", 0
    file = os.path.join(files_dir(), name)
    if not os.path.isfile(file):
        return "", 0
    sha = ""
    try:
        with open(file + ".sha256") as fh:
            m = re.search(r"\b[0-9a-f]{64}\b", fh.read(200))
            sha = m.group(0) if m else ""
    except OSError:
        pass
    return sha, os.path.getsize(file)


VERSION_NUMBER = re.compile(r"^v?(\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?")


def version_number(text):
    """"1.2" -> "1.2.0", "v0.6.1-beta" -> "0.6.1"; "" when the version is not a number."""
    m = VERSION_NUMBER.match(str(text or "").strip())
    return f"{int(m.group(1))}.{int(m.group(2))}.{int(m.group(3) or 0)}" if m else ""


@app.get("/api/hub/products")
def hub_products():
    """RotaHub's update feed: every product's current version and installer, as set in Admin -> Products.
    Ask with ?since=<revision> to get just {"revision", "unchanged": true} when nothing changed.
    The installer link itself comes from POST /api/download/<id> (RotaHub only)."""
    items = []
    for r in db().all("SELECT * FROM products ORDER BY sort_order, name"):
        p = public_product(r)
        url = str(r["download_url"] or "")
        available = url.startswith("https://") or bool(FILE_LINK.match(url))
        sha, size = download_facts(url) if available else ("", 0)
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower().lstrip(".")
        latest = (p["changelog"] or [{}])[0]
        items.append({"id": r["id"], "name": r["name"] or r["id"], "status": r["status"] or "", "version": r["version"] or "",
                      "version_number": version_number(r["version"]), "updated_at": str(r["updated_at"] or ""),
                      "required": bool(r["update_required"]), "licence_only": p["download_gate"] == "licence",
                      "notes": latest.get("notes", "") if latest.get("version") == r["version"] else "", "changelog": p["changelog"],
                      "download": {"available": available, "kind": ext if ext in ("exe", "msi", "zip", "7z") else "other",
                                   "id": hashlib.sha256(url.encode()).hexdigest()[:12] if available else "", "sha256": sha, "size": size}})
    revision = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()[:16]
    if request.args.get("since") == revision:
        return jsonify({"revision": revision, "unchanged": True})
    return jsonify({"revision": revision, "products": items})


@app.get("/api/hub")
def hub_public():
    """Read by RotaHub at start and on refresh. Nothing secret: the same for everyone."""
    return jsonify(hub_config())


@app.get("/api/admin/hub")
def admin_hub():
    if not staff_user():
        return err("Admin only", 403)
    config = hub_config()
    return jsonify({**config, "stats": hub_stats(config["min_version"])})


@app.post("/api/admin/hub")
def admin_save_hub():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change RotaHub", 403)
    try:
        cleaned = clean_hub(body())
    except ValueError as exc:
        return err(str(exc), 400)
    set_setting("hub.config", cleaned)
    publish_site_settings()  # the website's Download buttons follow "downloads"
    audit(user, "save_hub", "message " + ("on" if cleaned["message"]["enabled"] else "off") + (f", lowest version {cleaned['min_version']}" if cleaned["min_version"] else ""))
    return jsonify({**cleaned, "stats": hub_stats(cleaned["min_version"])})


# ---------- site settings & media ----------

# Image slots on the public pages. Defaults are the artwork in cloudflare/public/img.
IMAGE_SLOTS = {
    "hero": "/img/hero.jpg",          # home page welcome
    "feature": "/img/approach.jpg",   # home page "A joystick is not a yoke"
    "heavypro": "/img/clouds.jpg",    # HeavyPro page hero
    "header": "/img/night.jpg",       # header band on inner pages
    "login": "/img/clouds.jpg",       # log in / sign up side panel
    "about": "/img/approach.jpg",     # About page picture
    "product": "/img/approach.jpg",   # products without their own image
}
MEDIA_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def get_setting(key, default):
    row = db().first("SELECT value FROM meta WHERE key = ?", key)
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return default


def set_setting(key, value):
    raw = json.dumps(value)
    if db().first("SELECT key FROM meta WHERE key = ?", key):
        db().run("UPDATE meta SET value = ? WHERE key = ?", raw, key)
    else:
        db().run("INSERT INTO meta (key, value) VALUES (?, ?)", key, raw)


SOCIALS = ("discord", "youtube", "instagram", "x", "facebook", "twitch", "tiktok")
CONFIG_DEFAULT = {"contact_email": "Eliaz@rotaidem.com",
                  "footer_text": "An independent studio making software for Microsoft Flight Simulator 2024.",
                  "socials": {}, "maintenance": {"enabled": False, "message": ""},
                  # who sells: shown in the footer and on the legal pages (EU law asks for these on a web shop)
                  "business": {"name": "", "org_number": "", "vat_number": "", "address": "", "country": ""}}
BUSINESS_FIELDS = {"name": 120, "org_number": 40, "vat_number": 40, "address": 300, "country": 60}
TERMS_VERSION = "2026-11-13"


def site_config():
    saved = get_setting("site.config", {}) or {}
    config = {**CONFIG_DEFAULT, **{k: v for k, v in saved.items() if k in CONFIG_DEFAULT}}
    config["socials"] = {k: v for k, v in (config.get("socials") or {}).items() if k in SOCIALS and v}
    m = config.get("maintenance") or {}
    config["maintenance"] = {"enabled": bool(m.get("enabled")), "message": str(m.get("message") or "")}
    biz = config.get("business") if isinstance(config.get("business"), dict) else {}
    config["business"] = {k: str(biz.get(k) or "") for k in BUSINESS_FIELDS}
    return config


def clean_config(c):
    """The general settings from the admin, checked; raises ValueError with a message for the admin."""
    email = str(c.get("contact_email") or "").strip()
    if email and not EMAIL_RE.match(email):
        raise ValueError("The contact email is not a valid address")
    socials = {}
    for name, url in (c.get("socials") or {}).items():
        url = str(url or "").strip()
        if name in SOCIALS and url:
            if not url.startswith("https://") or len(url) > 300:
                raise ValueError(f"The {name} link must start with https://")
            socials[name] = url
    m = c.get("maintenance") or {}
    biz = c.get("business") if isinstance(c.get("business"), dict) else {}
    return {"contact_email": email or CONFIG_DEFAULT["contact_email"],
            "footer_text": str(c.get("footer_text") or "").strip()[:300] or CONFIG_DEFAULT["footer_text"],
            "socials": socials,
            "maintenance": {"enabled": bool(m.get("enabled")), "message": str(m.get("message") or "").strip()[:500]},
            "business": {k: " ".join(str(biz.get(k) or "").split())[:most] for k, most in BUSINESS_FIELDS.items()}}


ROADMAP_STAGES = ("Done", "In progress", "Next", "Later")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LINK_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def clean_roadmap(items):
    """The roadmap page from the admin: [{stage, title, text}], at most 40 steps."""
    if not isinstance(items, list) or len(items) > 40:
        raise ValueError("The roadmap can have at most 40 steps")
    out = []
    for i in items:
        title = str((i or {}).get("title") or "").strip()[:120]
        if not title:
            continue
        stage = i.get("stage") if i.get("stage") in ROADMAP_STAGES else "Next"
        out.append({"stage": stage, "title": title, "text": str(i.get("text") or "").strip()[:600]})
    return out


def clean_aircraft(items):
    """Supported aircraft (Admin -> Aircraft): the home page, Compatibility and HeavyPro pages.
    [{name, profile, note, home, home_text}], at most 30. Empty: the pages keep their own list."""
    if not isinstance(items, list) or len(items) > 30:
        raise ValueError("At most 30 aircraft")
    out = []
    for i in items:
        i = i if isinstance(i, dict) else {}
        name = str(i.get("name") or "").strip()[:80]
        if name:
            out.append({"name": name, "profile": str(i.get("profile") or "").strip()[:80], "note": str(i.get("note") or "").strip()[:200],
                        "home": bool(i.get("home")), "home_text": str(i.get("home_text") or "").strip()[:120]})
    return out


def clean_testimonials(items):
    """Pilot quotes (Admin -> Pilot quotes): [{name, about, text, image}], at most 20, shown under "What pilots say"."""
    if not isinstance(items, list) or len(items) > 20:
        raise ValueError("At most 20 quotes")
    out = []
    for i in items:
        i = i if isinstance(i, dict) else {}
        name, text = str(i.get("name") or "").strip()[:60], str(i.get("text") or "").replace("\r\n", "\n").strip()[:800]
        if not (name and text):
            continue
        raw = str(i.get("image") or "").strip()
        image = valid_image_url(raw)
        if raw and not image:
            raise ValueError(f"The picture for {name} must be an uploaded picture or an https:// link")
        out.append({"name": name, "about": str(i.get("about") or "").strip()[:80], "text": text, "image": image})
    return out


def clean_faq(items):
    """The questions on the Support page: [{q, a}], at most 60."""
    if not isinstance(items, list) or len(items) > 60:
        raise ValueError("The FAQ can have at most 60 questions")
    out = []
    for i in items:
        q, a = str((i or {}).get("q") or "").strip()[:200], str((i or {}).get("a") or "").strip()[:2000]
        if q and a:
            out.append({"q": q, "a": a})
    return out


def clean_links(links):
    """Short links like rotaidem.com/discord: {slug: target}. A slug may not be an existing page."""
    if not isinstance(links, dict) or len(links) > 100:
        raise ValueError("At most 100 short links")
    out = {}
    for slug, target in links.items():
        slug, target = str(slug).strip().strip("/").lower(), str(target or "").strip()
        if not slug and not target:
            continue
        if not LINK_SLUG.match(slug):
            raise ValueError(f"Short link /{slug}: use lowercase letters, digits and dashes")
        if (PUBLIC / f"{slug}.html").exists() or (PUBLIC / slug).is_dir() or slug in ("api", "media", "img", "admin", "health", "files"):
            raise ValueError(f"/{slug} is already a page on the site")
        if not (target.startswith("https://") or (target.startswith("/") and not target.startswith("//"))) or len(target) > 500:
            raise ValueError(f"Short link /{slug} must point to an https:// address or a page like /downloads")
        out[slug] = target
    return out


# "Look & media" in the admin: colour, logo, screenshots, video, launch countdown, home page sections.
ACCENTS = {"steel": ("#aab6c2", "#cfd7de", "170, 182, 194"), "amber": ("#f2a900", "#ffbd2e", "242, 169, 0"),
           "sky": ("#5ab0ff", "#8cc8ff", "90, 176, 255"), "mint": ("#4cc38a", "#7ad7aa", "76, 195, 138"),
           "rose": ("#ff6b81", "#ff94a5", "255, 107, 129"), "violet": ("#a78bfa", "#c4b5fd", "167, 139, 250")}
HOME_SECTIONS = ("profiles", "products", "news", "signup")
GALLERY_MAX = 24
YOUTUBE_ID = re.compile(r"(?:youtu\.be/|[?&]v=|/embed/|/shorts/|/live/)([\w-]{11})(?:[?&#/]|$)")
LAUNCH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?Z$")
EXTRAS_DEFAULT = {"accent": "steel", "logo": "", "gallery": [], "video": "", "launch": {"enabled": False, "at": ""}, "hide": []}


def youtube_id(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"[\w-]{11}", value):
        return value
    match = YOUTUBE_ID.search(value) if re.match(r"^https://(www\.|m\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/", value) else None
    if not match:
        raise ValueError("Paste a YouTube link, like https://www.youtube.com/watch?v=...")
    return match.group(1)


def clean_extras(e):
    """The Look & media settings from the admin, checked; raises ValueError with a message for the admin."""
    logo = str(e.get("logo") or "").strip()
    if logo and not valid_image_url(logo):
        raise ValueError("The logo must be an uploaded picture or an https:// link")
    gallery = [str(u).strip() for u in (e.get("gallery") or []) if str(u).strip()]
    if len(gallery) > GALLERY_MAX:
        raise ValueError(f"At most {GALLERY_MAX} screenshots")
    if any(not valid_image_url(u) for u in gallery):
        raise ValueError("Each screenshot must be an uploaded picture or an https:// link")
    launch = e.get("launch") or {}
    at = str(launch.get("at") or "").strip()
    if at and not LAUNCH_RE.match(at):
        raise ValueError("Pick a date and time for the launch countdown")
    if launch.get("enabled") and not at:
        raise ValueError("Pick a date and time for the launch countdown")
    return {"accent": e.get("accent") if e.get("accent") in ACCENTS else "steel", "logo": logo, "gallery": gallery,
            "video": youtube_id(e.get("video")), "launch": {"enabled": bool(launch.get("enabled")), "at": at},
            "hide": [h for h in HOME_SECTIONS if h in (e.get("hide") or [])]}


def site_extras():
    saved = get_setting("site.extras", {}) or {}
    return {**EXTRAS_DEFAULT, **{k: v for k, v in saved.items() if k in EXTRAS_DEFAULT}}


def site_settings():
    images = dict(IMAGE_SLOTS)
    images.update({k: v for k, v in (get_setting("site.images", {}) or {}).items() if k in IMAGE_SLOTS and v})
    banner = get_setting("site.announcement", {}) or {}
    texts = get_setting("site.texts", {}) or {}
    return {"images": images, "defaults": IMAGE_SLOTS, "texts": texts if isinstance(texts, dict) else {}, "config": site_config(),
            "announcement": {"enabled": bool(banner.get("enabled")), "text": str(banner.get("text") or ""), "link": str(banner.get("link") or ""),
                             "from": str(banner.get("from") or ""), "until": str(banner.get("until") or "")},
            "roadmap": get_setting("site.roadmap", []) or [], "faq": get_setting("site.faq", []) or [],
            "aircraft": get_setting("site.aircraft", []) or [], "share": share_settings(),
            "testimonials": get_setting("site.testimonials", []) or [],
            "links": get_setting("site.links", {}) or {}, "extras": site_extras(), "hub": hub_config()["downloads"]}


# Page texts: elements with data-text="page.key" in the HTML show the saved text instead of their
# own (auth.js). Only changed texts are stored; the HTML keeps the defaults.
TEXT_KEY = re.compile(r"^[a-z0-9-]{1,40}\.[a-z0-9-]{1,60}$")
TEXT_MAX, TEXT_COUNT = 5000, 1000


def valid_image_url(url):
    url = str(url or "").strip()
    return url if (url.startswith("https://") or url.startswith("/img/") or re.match(r"^/media/\d+\.(jpg|png|webp|gif)$", url)) else ""


@app.get("/api/site")
def public_site():
    return jsonify(site_settings())


@app.get("/api/admin/settings")
def admin_settings():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify(site_settings())


@app.post("/api/admin/settings")
def admin_save_settings():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can change site settings", 403)
    b = body()
    if "images" in b:
        images = {}
        for slot, url in (b.get("images") or {}).items():
            if slot in IMAGE_SLOTS and url:
                clean = valid_image_url(url)
                if not clean:
                    return err(f"The {slot} image must be an https:// link or an uploaded image", 400)
                if clean != IMAGE_SLOTS[slot]:
                    images[slot] = clean
        set_setting("site.images", images)
    if "announcement" in b:
        a = b.get("announcement") or {}
        link = str(a.get("link") or "").strip()
        if link and not (link.startswith("https://") or link.startswith("/")):
            return err("The banner link must start with https:// or /", 400)
        window = {k: str(a.get(k) or "").strip() for k in ("from", "until")}
        if any(v and not DATE_RE.match(v) for v in window.values()):
            return err("Banner dates must look like 2026-11-01", 400)
        set_setting("site.announcement", {"enabled": bool(a.get("enabled")), "text": str(a.get("text") or "")[:200], "link": link[:300], **window})
    try:
        if "config" in b:
            set_setting("site.config", clean_config(b.get("config") or {}))
        if "roadmap" in b:
            set_setting("site.roadmap", clean_roadmap(b.get("roadmap")))
        if "faq" in b:
            set_setting("site.faq", clean_faq(b.get("faq")))
        if "aircraft" in b:
            set_setting("site.aircraft", clean_aircraft(b.get("aircraft")))
        if "share" in b:
            set_setting("site.share", clean_share(b.get("share")))
        if "testimonials" in b:
            set_setting("site.testimonials", clean_testimonials(b.get("testimonials")))
        if "links" in b:
            set_setting("site.links", clean_links(b.get("links")))
        if "extras" in b:
            set_setting("site.extras", clean_extras(b.get("extras") or {}))
    except ValueError as exc:
        return err(str(exc), 400)
    if "texts" in b:
        # A patch: {"page.key": "new text"} changes a text, null or "" puts the original back.
        changes = b.get("texts")
        if not isinstance(changes, dict):
            return err("texts must be an object", 400)
        texts = dict(get_setting("site.texts", {}) or {})
        for key, value in changes.items():
            if not TEXT_KEY.match(str(key)):
                return err(f"Unknown text {key}", 400)
            value = "" if value is None else str(value).replace("\r\n", "\n").strip()
            if len(value) > TEXT_MAX:
                return err(f"A text can be at most {TEXT_MAX} characters", 400)
            if value:
                texts[key] = value
            else:
                texts.pop(key, None)
        if len(texts) > TEXT_COUNT:
            return err("Too many changed texts", 400)
        set_setting("site.texts", texts)
    publish_site_settings()
    audit(user, "save_settings", ", ".join(k for k in ("images", "announcement", "config", "roadmap", "faq", "aircraft", "share", "testimonials", "links", "extras", "texts") if k in b)
          + (": " + ", ".join(list(b["texts"])[:5]) if isinstance(b.get("texts"), dict) else ""))
    return jsonify({"ok": True, **site_settings()})


@app.get("/api/admin/media")
def admin_media():
    if not staff_user():
        return err("Admin only", 403)
    rows = db().all("SELECT id, filename, mime, size, width, height, created_at FROM media ORDER BY id DESC")
    for r in rows:
        r["url"] = f"/media/{r['id']}.{MEDIA_TYPES.get(r['mime'], 'jpg')}"
    return jsonify({"media": rows})


@app.post("/api/admin/media")
def admin_upload_media():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can upload images", 403)
    b = body()
    if not b and request.content_length:
        return err("The picture did not arrive in one piece (the upload was cut off). Try a smaller picture.", 400)
    mime = str(b.get("mime") or "")
    if mime not in MEDIA_TYPES:
        return err("Upload a JPG, PNG, WebP or GIF image", 400)
    data = str(b.get("data") or "")
    if "," in data and data.startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        return err("The image data could not be read", 400)
    if not raw or len(raw) > 5 * 1024 * 1024:
        return err("Images must be smaller than 5 MB", 400)
    name = re.sub(r"[^\w.\- ]", "", str(b.get("filename") or "image"))[:120] or "image"
    db().run("INSERT INTO media (filename, mime, data, size, width, height) VALUES (?, ?, ?, ?, ?, ?)",
             name, mime, base64.b64encode(raw).decode(), len(raw), clamp_int(b.get("width"), 0, 0, 20000), clamp_int(b.get("height"), 0, 0, 20000))
    row = db().first("SELECT MAX(id) AS id FROM media")
    audit(user, "upload_media", name)
    write_public(f"media/{row['id']}.{MEDIA_TYPES[mime]}", raw)
    return jsonify({"ok": True, "id": row["id"], "url": f"/media/{row['id']}.{MEDIA_TYPES[mime]}"})


@app.delete("/api/admin/media/<int:media_id>")
def admin_delete_media(media_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can delete images", 403)
    row = db().first("SELECT filename, mime FROM media WHERE id = ?", media_id)
    db().run("DELETE FROM media WHERE id = ?", media_id)
    if row:
        remove_public(f"media/{media_id}.{MEDIA_TYPES.get(row['mime'], 'jpg')}")
    audit(user, "delete_media", row["filename"] if row else media_id)
    return jsonify({"ok": True})


@app.get("/media/<int:media_id>.<ext>")
def media_file(media_id, ext):
    row = db().first("SELECT mime, data FROM media WHERE id = ?", media_id)
    if not row or MEDIA_TYPES.get(row["mime"]) != ext:
        abort(404)
    raw = base64.b64decode(row["data"])
    write_public(f"media/{media_id}.{ext}", raw)  # from now on the web server sends it as a plain file
    resp = app.response_class(raw, mimetype=row["mime"])
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------- plain-file copies for the web server ----------
# On STRATO the entry script sets ROTAIDEM_PUBLIC_DIR (the website folder). Uploaded images and
# the site settings are then also written there, so Apache sends them straight away instead of
# starting this program and reading the database for every picture. Elsewhere this does nothing.

def public_path(name):
    folder = os.environ.get("ROTAIDEM_PUBLIC_DIR", "")
    if not folder or not os.path.isdir(folder) or not (re.match(r"^(media/\d+\.(jpg|png|webp|gif)|site-settings\.js)$", name) or name in SHARE_FILES):
        return ""
    return os.path.join(folder, name)


def write_public(name, data):
    path = public_path(name)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.chmod(os.path.dirname(path), 0o755)  # the program runs with umask 077; pictures are public
        temp = f"{path}.{os.getpid()}.tmp"
        with open(temp, "wb") as fh:
            fh.write(data)
        os.chmod(temp, 0o644)
        os.replace(temp, path)  # never a half-written file
    except OSError as exc:
        app.logger.warning("Could not write %s: %s", path, exc)


def remove_public(name):
    path = public_path(name)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:
            app.logger.warning("Could not remove %s: %s", path, exc)


def css_url(url):
    return "".join(c if c.isalnum() or c in "/:.-_~?=&%#+,;@!" else "%{:02X}".format(ord(c)) for c in url)


def site_settings_js():
    """Loaded in <head> of every page: the custom pictures apply before anything is drawn (CSS),
    and auth.js puts in the page texts without waiting for /api/site."""
    site = site_settings()
    css = "".join(f'[data-image="{slot}"]:not(img){{background-image:url("{css_url(url)}")!important}}'
                  for slot, url in site["images"].items() if url and url != IMAGE_SLOTS.get(slot))
    accent = ACCENTS.get(site["extras"].get("accent"))
    if accent and site["extras"].get("accent") != "steel":
        css += f":root{{--accent:{accent[0]};--accent-hover:{accent[1]};--accent-rgb:{accent[2]}}}"
    data = json.dumps(site).replace("</", "<\\/")
    style = json.dumps(css).replace("</", "<\\/")
    return (f"window.RSG_SITE={data};\n"
            f"(function(){{var s=document.createElement('style');s.textContent={style};document.head.appendChild(s);}})();\n")


def publish_site_settings():
    write_public("site-settings.js", site_settings_js().encode())
    publish_share_tags()


# ---------- testers (Admin -> Testers) ----------
# Testers are email addresses added in the admin (an account can come later). Logged in with that
# email, My account shows Tester downloads. Tester apps are uploaded in pieces like other files, but
# into the private data folder, which the web server never serves: a download is a link signed for
# one tester that works for a few minutes, and the tester is checked again when it is used.
TESTER_LINK_SECONDS = 15 * 60


def tester_dir():
    folder = os.path.join(str(DATA_DIR), "tester-files")
    os.makedirs(folder, exist_ok=True)
    return folder


def tester_list():
    return get_setting("testers.list", []) or []


def tester_apps():
    return get_setting("testers.apps", []) or []


def is_tester(user):
    """On the tester list, or Owner/Admin (so they can check what testers get)."""
    if not user:
        return False
    return user.get("role") in ("Owner", "Admin") or (user.get("email") or "").lower() in {t["email"] for t in tester_list()}


def tester_signature(app_id, user_id, expires):
    return hmac.new(SECRET_KEY.encode(), f"tester:{app_id}:{user_id}:{expires}".encode(), hashlib.sha256).hexdigest()[:40]


def public_tester_app(a):
    return {k: a.get(k) for k in ("id", "name", "version", "notes", "size", "created_at")}


@app.get("/api/admin/testers")
def admin_testers():
    if not manager_user():
        return err("Only the Owner or an Admin can manage testers", 403)
    people = []
    for t in tester_list():
        account = db().first("SELECT id, name FROM users WHERE LOWER(email) = ?", t["email"])
        people.append({**t, "account": (account or {}).get("name") or "", "has_account": bool(account)})
    counts = {r["product_id"]: r for r in db().all(
        "SELECT product_id, COUNT(*) AS n, COUNT(DISTINCT user_id) AS people FROM downloads WHERE product_id LIKE ? GROUP BY product_id", "tester:%")}
    apps = [{**a, "downloads": int((counts.get("tester:" + a["id"]) or {}).get("n") or 0),
             "people": int((counts.get("tester:" + a["id"]) or {}).get("people") or 0)} for a in tester_apps()]
    return jsonify({"testers": people, "apps": apps})


@app.post("/api/admin/testers")
def admin_add_tester():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can manage testers", 403)
    b = body()
    email, note = str(b.get("email") or "").strip().lower(), str(b.get("note") or "").strip()[:120]
    if not EMAIL_RE.match(email) or len(email) > 200:
        return err("Enter the tester's email address", 400)
    people = [t for t in tester_list() if t["email"] != email]
    set_setting("testers.list", [{"email": email, "note": note, "added_at": utc_stamp(), "added_by": user["name"]}] + people)
    audit(user, "add_tester", email)
    return jsonify({"ok": True})


@app.post("/api/admin/testers/remove")
def admin_remove_tester():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can manage testers", 403)
    email = str(body().get("email") or "").strip().lower()
    set_setting("testers.list", [t for t in tester_list() if t["email"] != email])
    audit(user, "remove_tester", email)
    return jsonify({"ok": True})


@app.post("/api/admin/tester-apps")
def admin_add_tester_app():
    """A tester app from a file uploaded with "private": name, version and what to test."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can add tester apps", 403)
    b = body()
    name, version = str(b.get("name") or "").strip()[:80], str(b.get("version") or "").strip()[:40]
    notes, file = str(b.get("notes") or "").strip()[:2000], str(b.get("file") or "")
    if not name:
        return err("Give the app a name", 400)
    if not FILE_NAME.match(file) or not os.path.isfile(os.path.join(tester_dir(), file)):
        return err("Upload the file first", 400)
    item = {"id": secrets.token_hex(6), "name": name, "version": version, "notes": notes, "file": file,
            "size": os.path.getsize(os.path.join(tester_dir(), file)), "created_at": utc_stamp(), "created_by": user["name"]}
    set_setting("testers.apps", [item] + tester_apps())
    audit(user, "add_tester_app", f"{name} {version}".strip())
    return jsonify({"ok": True, "app": item})


@app.delete("/api/admin/tester-apps/<app_id>")
def admin_delete_tester_app(app_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can delete tester apps", 403)
    apps = tester_apps()
    gone = next((a for a in apps if a["id"] == app_id), None)
    if not gone:
        return err("Tester app not found", 404)
    left = [a for a in apps if a["id"] != app_id]
    set_setting("testers.apps", left)
    path = os.path.join(tester_dir(), gone["file"])
    if not any(a["file"] == gone["file"] for a in left) and os.path.isfile(path):
        os.remove(path)
        if os.path.exists(path + ".sha256"):
            os.remove(path + ".sha256")
    audit(user, "delete_tester_app", f"{gone['name']} {gone.get('version') or ''}".strip())
    return jsonify({"ok": True})


@app.get("/api/account/tester")
def account_tester():
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    if not is_tester(user):
        return jsonify({"tester": False, "apps": []})
    return jsonify({"tester": True, "apps": [public_tester_app(a) for a in tester_apps()]})


@app.post("/api/account/tester/<app_id>/link")
def account_tester_link(app_id):
    """A download link for this tester only, valid for a few minutes (a browser download cannot send the login)."""
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    if not is_tester(user):
        return err("Tester downloads are for testers", 403)
    if not any(a["id"] == app_id for a in tester_apps()):
        return err("That tester app is no longer available", 404)
    expires = int(time.time()) + TESTER_LINK_SECONDS
    query = urllib.parse.urlencode({"u": user["id"], "e": expires, "s": tester_signature(app_id, user["id"], expires)})
    return jsonify({"url": f"/api/tester/download/{app_id}?{query}"})


@app.get("/api/tester/download/<app_id>")
def tester_download(app_id):
    try:
        user_id, expires = int(request.args.get("u", "")), int(request.args.get("e", ""))
    except ValueError:
        return err("This download link is not valid", 403)
    if expires < time.time() or not hmac.compare_digest(tester_signature(app_id, user_id, expires), str(request.args.get("s", ""))):
        return err("This download link has expired. Open My account and press Download again.", 403)
    user = db().first("SELECT id, email, role FROM users WHERE id = ?", user_id)
    item = next((a for a in tester_apps() if a["id"] == app_id), None)
    if not is_tester(user) or not item or not os.path.isfile(os.path.join(tester_dir(), item["file"])):
        return err("This tester download is no longer available to you", 403)
    db().run("INSERT INTO downloads (product_id, user_id, ip_address, created_at) VALUES (?, ?, ?, ?)",
             "tester:" + app_id, user_id, request.remote_addr or "", utc_stamp())
    return send_from_directory(tester_dir(), item["file"], as_attachment=True)


# ---------- link previews (Admin -> Site settings -> Link previews) ----------
# The picture, title and text that Discord, Messenger, iMessage and others show for a link. Those
# apps never run the page's scripts, so the tags are written into the page itself: into the copies
# in the website folder on STRATO (on every save and deploy), and on the way out everywhere else.

SHARE_PAGES = (  # address, file, name in the admin, picture slot used when nothing else is set
    ("/", "index.html", "Home", "hero"), ("/heavypro", "heavypro.html", "HeavyPro", "heavypro"),
    ("/addons/", "addons/index.html", "Products", "product"), ("/addons/view", "addons/view.html", "Product page", "product"),
    ("/downloads", "downloads.html", "Downloads", "header"), ("/get-rotahub", "get-rotahub.html", "Get RotaHub", "header"),
    ("/compatibility", "compatibility.html", "Compatibility", "header"), ("/installation", "installation.html", "Installation guide", "header"),
    ("/support", "support.html", "Help and FAQ", "header"), ("/roadmap", "roadmap.html", "Roadmap", "header"),
    ("/newsletter", "newsletter.html", "News", "header"), ("/about", "about.html", "About", "about"),
    ("/contact", "contact.html", "Contact", "header"), ("/work-at-rsg", "work-at-rsg.html", "Careers", "header"),
    ("/status", "status.html", "Status", "header"))
SHARE_FILES = {f: (path, slot) for path, f, _name, slot in SHARE_PAGES}
SHARE_BLOCK = re.compile(r"\s*<!-- share -->.*?<!-- /share -->", re.S)


def clean_share(data):
    """{"default": {"image"}, "pages": {"/heavypro": {"image", "title", "description"}}}: only known pages, nothing empty."""
    if not isinstance(data, dict):
        raise ValueError("Link previews must be an object")
    def one(item, name):
        item = item if isinstance(item, dict) else {}
        raw = str(item.get("image") or "").strip()
        image = valid_image_url(raw)
        if raw and not image:
            raise ValueError(f"The {name} picture must be an uploaded picture or an https:// link")
        return {k: v for k, v in {"image": image, "title": str(item.get("title") or "").strip()[:90],
                                  "description": str(item.get("description") or "").strip()[:200]}.items() if v}
    pages = {}
    for path, _f, name, _slot in SHARE_PAGES:
        item = one((data.get("pages") or {}).get(path), name.lower() + " page")
        if item:
            pages[path] = item
    return {"default": {k: v for k, v in one(data.get("default"), "default").items() if k == "image"}, "pages": pages}


def share_settings():
    saved = get_setting("site.share", {}) or {}
    return {"default": saved.get("default") or {}, "pages": saved.get("pages") or {}}


def share_base():
    base = os.environ.get("SITE_URL", "").strip() or (request.host_url if has_request_context() else "")
    base = base.rstrip("/")
    return "" if not base else (base if "://" in base else "https://" + base.lower())


def with_share_tags(page_html, file):
    """The page with its link-preview tags (replacing any from before)."""
    path, slot = SHARE_FILES[file]
    share = share_settings()
    own = share["pages"].get(path) or {}
    title = re.search(r"<title>(.*?)</title>", page_html, re.S)
    description = re.search(r'<meta name="description" content="([^"]*)"', page_html)
    title = own.get("title") or html_unescape(title.group(1).strip() if title else "Rotaidem")
    description = own.get("description") or html_unescape(description.group(1) if description else "")
    image = own.get("image") or share["default"].get("image") or site_settings()["images"].get(slot) or IMAGE_SLOTS["hero"]
    base = share_base()
    absolute = lambda u: base + u if u.startswith("/") else u  # noqa: E731 - apps need full addresses
    tags = [("og:type", "website"), ("og:site_name", "Rotaidem"), ("og:title", title), ("og:description", description),
            ("og:url", absolute(path)), ("og:image", absolute(image))]
    block = "\n    <!-- share -->\n" + "".join(f'    <meta property="{k}" content="{escape(v, quote=True)}">\n' for k, v in tags if v) + \
        f'    <meta name="twitter:card" content="summary_large_image">\n    <meta name="twitter:image" content="{escape(absolute(image), quote=True)}">\n    <!-- /share -->'
    page_html = SHARE_BLOCK.sub("", page_html)
    return page_html.replace("\n</head>", block + "\n</head>", 1) if "\n</head>" in page_html else page_html.replace("</head>", block + "</head>", 1)


def publish_share_tags():
    for file in SHARE_FILES:
        target = public_path(file)
        if not target or not os.path.isfile(target):
            continue
        try:
            with open(target, encoding="utf-8") as fh:
                current = fh.read()
            updated = with_share_tags(current, file)
            if updated != current:
                write_public(file, updated.encode("utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            app.logger.warning("Link preview for %s: %s", file, exc)


@app.get("/site-settings.js")
def site_settings_script():
    js = site_settings_js()
    write_public("site-settings.js", js.encode())
    resp = app.response_class(js, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------- backup ----------

BACKUP_TABLES = ("users", "products", "liveries", "posts", "messages", "orders", "audit_logs", "meta", "subscribers",
                 "licenses", "license_activations", "media", "campaigns", "shared_profiles", "stripe_payments", "discord_items")


def table_columns(d, table):
    return [c[0] for c in d.run(f"SELECT * FROM {table} LIMIT 0").description]


@app.get("/api/admin/backup")
def admin_backup():
    """Everything in the database as one JSON file: for safekeeping, or to move to another host
    (strato/setup.py import loads it)."""
    user = staff_user()
    if not user or user["role"] != "Owner":
        return err("Only the Owner can download a backup", 403)
    audit(user, "download_backup", "")
    tables = {t: db().all(f"SELECT * FROM {t} ORDER BY 1") for t in BACKUP_TABLES}
    resp = app.response_class(json.dumps({"format": "rotaidem-backup-1", "created_at": datetime.now(timezone.utc).isoformat(),
                                          "tables": tables}, default=str), mimetype="application/json")
    resp.headers["Content-Disposition"] = f'attachment; filename="rotaidem-backup-{datetime.now():%Y%m%d-%H%M}.json"'
    return resp


# ---------- users ----------

@app.get("/api/admin/users/<int:user_id>")
def admin_user_detail(user_id):
    if not staff_user():
        return err("Admin only", 403)
    u = db().first("SELECT id, name, email, role, created_at, email_verified, admin_note FROM users WHERE id = ?", user_id)
    if not u:
        return err("User not found", 404)
    u["orders"] = db().all("SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC", user_id)
    u["licenses"] = db().all("SELECT licenses.*, (SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS activations "
                             "FROM licenses WHERE customer_id = ? OR LOWER(email) = ? ORDER BY id DESC", user_id, (u["email"] or "").lower())
    u["sessions"] = session_rows(user_id)
    u["messages"] = db().all("SELECT id, message, created_at, handled, reply, replied_at, replied_by FROM messages WHERE LOWER(email) = ? ORDER BY id DESC LIMIT 50",
                             (u["email"] or "").lower())
    u["downloads"] = db().all("SELECT product_id, created_at FROM downloads WHERE user_id = ? ORDER BY id DESC LIMIT 50", user_id)
    return jsonify({"user": u})


@app.delete("/api/admin/users/<int:user_id>/sessions/<int:session_id>")
def admin_end_session(user_id, session_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can log users out", 403)
    db().run("DELETE FROM sessions WHERE id = ? AND user_id = ?", session_id, user_id)
    audit(user, "end_session", f"user {user_id}")
    return jsonify({"ok": True})


@app.post("/api/admin/users/<int:user_id>/note")
def admin_user_note(user_id):
    """A private note about a user, only seen by staff."""
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    db().run("UPDATE users SET admin_note = ? WHERE id = ?", str(body().get("note") or "").strip()[:2000], user_id)
    audit(user, "user_note", user_id)
    return jsonify({"ok": True})


@app.post("/api/admin/users/<int:user_id>/verify")
def admin_verify_user(user_id):
    """Staff confirm an address by hand, e.g. for someone whose confirmation email never arrived."""
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    db().run("UPDATE users SET email_verified = 1 WHERE id = ?", user_id)
    audit(user, "verify_user", user_id)
    return jsonify({"ok": True})


@app.post("/api/admin/users/<int:user_id>/sign-out")
def admin_sign_out_user(user_id):
    """Ends the user's session on every device (e.g. a shared or lost computer)."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can sign users out", 403)
    if user_id == user["id"]:
        return err("Use Log out to sign yourself out", 400)
    end_sessions(user_id)
    audit(user, "sign_out_user", user_id)
    return jsonify({"ok": True})


@app.delete("/api/admin/users/<int:user_id>")
def admin_delete_user(user_id):
    user = staff_user()
    if not user or user["role"] != "Owner":
        return err("Only the Owner can delete users", 403)
    if user_id == user["id"]:
        return err("You cannot delete your own account", 400)
    target = db().first("SELECT email FROM users WHERE id = ?", user_id)
    if not target:
        return err("User not found", 404)
    # Licences and orders stay (they are records of a sale); they are just no longer linked to an account.
    db().run("UPDATE licenses SET customer_id = NULL WHERE customer_id = ?", user_id)
    db().run("DELETE FROM users WHERE id = ?", user_id)
    db().run("DELETE FROM sessions WHERE user_id = ?", user_id)
    audit(user, "delete_user", target["email"])
    return jsonify({"ok": True})


# ---------- email broadcasts ----------

def manager_user():
    """Owner or Admin: allowed to email people."""
    user = current_user()
    return user if user and user["role"] in ("Owner", "Admin") and admin_unlocked(user) else None


def collect_recipients(groups, extra):
    """Unique, valid addresses from the chosen groups plus pasted extras, minus anyone who unsubscribed."""
    found = []
    queries = {
        "accounts": "SELECT email FROM users",
        "buyers": "SELECT DISTINCT users.email FROM orders JOIN users ON users.id = orders.customer_id",
        "subscribers": "SELECT email FROM subscribers WHERE unsubscribed = 0",
        "contacts": "SELECT DISTINCT email FROM messages",
    }
    for group in groups:
        if group in queries:
            found += [r["email"] for r in db().all(queries[group])]
    found += re.split(r"[\s,;]+", extra or "")
    blocked = {r["email"] for r in db().all("SELECT email FROM subscribers WHERE unsubscribed = 1")}
    seen, result = set(), []
    for email in found:
        email = str(email or "").strip().lower()
        if EMAIL_RE.match(email) and email not in seen and email not in blocked:
            seen.add(email)
            result.append(email)
    return result


def site_url():
    base = (os.environ.get("SITE_URL") or request.host_url).strip().rstrip("/")
    return base if "://" in base else "https://" + base.lower()  # "Rotaidem.com" still gives working links


EMAIL_IMAGES_MAX = 12


def email_image_tag(base, url):
    if url.startswith("/media/") or url.startswith("/img/"):
        url = base + url  # uploaded images need a full address inside an email
    if not url.startswith("https://"):
        return ""
    return f'<img src="{escape(url)}" alt="" width="560" style="display:block;width:100%;max-width:560px;height:auto;margin:0 0 20px">'


EMAIL_LAYOUTS = ("classic", "update", "launch", "plain")
BULLET = re.compile(r"^[-*\u2022]\s+")
BUTTON_STYLE = "display:inline-block;background:#141517;color:#ffffff;text-decoration:none;padding:13px 24px;font-weight:bold"


def email_paragraphs(message, bullets=False):
    """The message as HTML: blank lines split paragraphs; with bullets, lines starting with - or * become a list."""
    out, items = [], []
    for block in re.split(r"\n\s*\n", (message or "").strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        for line in lines:
            if bullets and BULLET.match(line):
                item = escape(BULLET.sub("", line))
                items.append(f'<li style="margin:0 0 6px">{item}</li>')
                continue
            if items:
                out.append(f'<ul style="margin:0 0 16px;padding-left:22px">{"".join(items)}</ul>')
                items = []
            out.append(f'<p style="margin:0 0 14px">{escape(line)}</p>')
        if items:
            out.append(f'<ul style="margin:0 0 16px;padding-left:22px">{"".join(items)}</ul>')
            items = []
    return "".join(out)


def email_button(base, button, product, layout):
    """The button: the admin's own text and link, or a sensible default for the layout."""
    button = button or {}
    url = str(button.get("url") or "").strip()
    if url.startswith("/"):
        url = base + url
    if not url:
        url = f"{base}/downloads" if layout == "update" else (f"{base}/addons/view?id={product['id']}" if product else base)
    text = str(button.get("text") or "").strip() or ("Download the update" if layout == "update" else
                                                     f"View {product['name']}" if product else "Visit rotaidem.com")
    return url, text


def product_email_html(product, message, email, images=(), layout="classic", subject="", button=None):
    """The email of "Email a product", in one of four layouts."""
    base = site_url()
    layout = layout if layout in EMAIL_LAYOUTS else "classic"
    unsub = f"{base}/unsubscribe?email={urllib.parse.quote(email)}&token={unsubscribe_token(email)}"
    url, label = email_button(base, button, product, layout)
    pictures = [email_image_tag(base, u) for u in images]
    footer = (f'<tr><td style="padding:20px 28px;border-top:1px solid #e2e5e9;font-size:12px;color:#6b7480">You are receiving this because you have '
              f'an account with Rotaidem, contacted us or subscribed to our news.\n<a href="{escape(unsub)}" style="color:#6b7480">Unsubscribe</a></td></tr>')
    head = '<tr><td style="background:#141517;padding:20px 28px;color:#ffffff;font-weight:bold;letter-spacing:3px;font-size:15px">ROTAIDEM</td></tr>'
    product_block = ""
    if product:
        product_block = (f'{email_image_tag(base, product.get("image_url") or "")}<h2 style="margin:0 0 6px;font-size:22px;color:#141517">{escape(product["name"])}</h2>'
                         f'<p style="margin:0 0 6px;color:#6b7480">{escape(product.get("tagline") or "")}</p>'
                         f'<p style="margin:0 0 22px;font-weight:600;color:#141517">{escape(product.get("price") or "")}</p>')
    if layout == "update":
        rows = (f'{head}<tr><td style="padding:28px 28px 0"><div style="font-size:12px;font-weight:bold;letter-spacing:2px;color:#6b7480">UPDATE</div>'
                f'<h1 style="margin:6px 0 18px;font-size:24px;color:#141517">{escape(subject)}</h1></td></tr>'
                f'<tr><td style="padding:0 28px 8px;font-size:16px;line-height:1.6">{email_paragraphs(message, bullets=True)}</td></tr>'
                f'<tr><td style="padding:8px 28px 28px">{"".join(pictures)}<a href="{escape(url)}" style="{BUTTON_STYLE}">{escape(label)}</a></td></tr>')
    elif layout == "launch":
        hero, rest = (pictures[0], pictures[1:]) if pictures else ("", [])
        hero = hero.replace("margin:0 0 20px", "margin:0")
        rows = (f'{head}<tr><td style="padding:0">{hero}</td></tr>'
                f'<tr><td align="center" style="padding:34px 32px 6px;text-align:center"><h1 style="margin:0 0 14px;font-size:30px;line-height:1.2;color:#141517">{escape(subject)}</h1>'
                f'<div style="font-size:17px;line-height:1.6;color:#39424e">{email_paragraphs(message)}</div></td></tr>'
                f'<tr><td align="center" style="padding:6px 32px 34px;text-align:center"><a href="{escape(url)}" style="{BUTTON_STYLE};padding:16px 34px;font-size:17px">{escape(label)}</a></td></tr>'
                + (f'<tr><td style="padding:0 28px 12px">{"".join(rest)}</td></tr>' if rest else ""))
    elif layout == "plain":
        link = f'<p style="margin:0 0 14px"><a href="{escape(url)}" style="color:#141517">{escape(label)}</a></p>' if (button or {}).get("url") or (button or {}).get("text") else ""
        rows = (f'<tr><td style="padding:32px 32px 8px;font-size:16px;line-height:1.7;color:#222">{email_paragraphs(message)}{link}'
                f'<p style="margin:18px 0 0">Eliaz<br><span style="color:#6b7480">Rotaidem</span></p></td></tr>'
                + (f'<tr><td style="padding:12px 32px 20px">{"".join(pictures)}</td></tr>' if pictures else ""))
    else:
        text = "<br>".join(escape(line) for line in (message or "").splitlines())
        rows = (f'{head}<tr><td style="padding:28px 28px 8px;font-size:16px;line-height:1.6">{text}</td></tr>'
                f'<tr><td style="padding:12px 28px 28px">{"".join(pictures)}{product_block}<a href="{escape(url)}" style="{BUTTON_STYLE}">{escape(label)}</a></td></tr>')
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#39424e">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff">
{rows}
{footer}
</table></td></tr></table></body></html>"""


def explain_resend_error(status, raw):
    """Turn a Resend error response into an instruction the admin can act on."""
    try:
        message = str(json.loads(raw).get("message", ""))
    except (ValueError, AttributeError):
        message = ""
    text = (message or raw).lower()
    if "own email address" in text or ("verify a domain" in text and "resend.dev" in MAIL_FROM):
        return ("Resend is still in test mode. With the onboarding@resend.dev sender it only delivers to the address "
                "you signed up to Resend with, so send the test to that address. To email anyone else, verify rotaidem.com "
                "at resend.com/domains and set MAIL_FROM, e.g. Rotaidem <no-reply@rotaidem.com>.")
    if "domain is not verified" in text or "not verified" in text:
        return (f"The sender domain in MAIL_FROM ({MAIL_FROM}) is not verified in Resend yet. Finish the DNS setup at "
                "resend.com/domains, or remove MAIL_FROM to use Resend's test sender.")
    if status in (401, 403) and ("api key" in text or "unauthorized" in text or "restricted" in text):
        return ("Resend rejected the API key. Create a key with sending access at resend.com/api-keys and put it in "
                "RESEND_API_KEY (on STRATO: rotaidem-data/config.env, picked up automatically; on Render: the "
                "service's environment). It starts with re_.")
    if "error code: 1010" in text or "<html" in text:
        return "Resend's firewall blocked the request. Try again in a minute; if it keeps happening, contact Resend support."
    if "from" in text and ("invalid" in text or "format" in text):
        return f"MAIL_FROM is not a valid sender ({MAIL_FROM}). Use the format: Rotaidem <no-reply@rotaidem.com>"
    return f"Resend said ({status}): {message or raw[:200]}"


def send_batch(messages):
    """Send up to 100 emails through Resend's batch API. Returns (sent, error)."""
    req = urllib.request.Request(f"{RESEND_API_URL}/emails/batch", data=json.dumps(messages).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json",
                                          "Accept": "application/json", "User-Agent": "Rotaidem-Website/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return len(messages), ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        print(f"Resend error {exc.code}: {raw[:500]}", file=sys.stderr, flush=True)
        return 0, explain_resend_error(exc.code, raw)
    except OSError as exc:
        print(f"Resend connection error: {exc}", file=sys.stderr, flush=True)
        return 0, f"Could not reach Resend: {exc}"


@app.post("/api/admin/audience")
def admin_audience():
    if not staff_user():
        return err("Admin only", 403)
    b = body()
    groups = [g for g in b.get("groups", []) if g in AUDIENCES]
    counts = {g: len(collect_recipients([g], "")) for g in AUDIENCES}
    return jsonify({"counts": counts, "total": len(collect_recipients(groups, b.get("extra", ""))),
                    "email_ready": bool(RESEND_API_KEY), "from": MAIL_FROM})


def email_options(b):
    """Pictures, layout and button of an email from the admin, checked; raises ValueError for the admin."""
    images = []
    for url in b.get("images") or []:
        clean = valid_image_url(url)
        if not clean:
            raise ValueError("Each picture must be uploaded or an https:// link")
        images.append(clean)
    if len(images) > EMAIL_IMAGES_MAX:
        raise ValueError(f"At most {EMAIL_IMAGES_MAX} pictures per email")
    layout = b.get("layout") if b.get("layout") in EMAIL_LAYOUTS else "classic"
    button = b.get("button") or {}
    url, text = str(button.get("url") or "").strip(), str(button.get("text") or "").strip()[:60]
    if url and not (url.startswith("https://") or (url.startswith("/") and not url.startswith("//"))):
        raise ValueError("The button link must start with https:// or be a page like /downloads")
    return images, layout, {"url": url[:500], "text": text}


@app.post("/api/admin/email-preview")
def admin_email_preview():
    """The email exactly as it will be sent, for the preview in the admin."""
    if not manager_user():
        return err("Only the Owner or an Admin can send emails", 403)
    b = body()
    try:
        images, layout, button = email_options(b)
    except ValueError as exc:
        return err(str(exc), 400)
    product = db().first("SELECT * FROM products WHERE id = ?", b["product_id"]) if b.get("product_id") else None
    message = str(b.get("message") or "").strip() or "Your message appears here."
    return jsonify({"html": product_email_html(product, message, "preview@example.com", images, layout,
                                               str(b.get("subject") or "").strip() or "Your subject", button)})


TEMPLATES_MAX = 30


@app.get("/api/admin/email-templates")
def admin_email_templates():
    if not manager_user():
        return err("Only the Owner or an Admin can send emails", 403)
    return jsonify({"templates": get_setting("email.templates", []) or []})


@app.post("/api/admin/email-templates")
def admin_save_email_template():
    """Saves the composed email under a name (the same name replaces it)."""
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can send emails", 403)
    b = body()
    name = str(b.get("name") or "").strip()[:60]
    if not name:
        return err("Give the template a name", 400)
    try:
        images, layout, button = email_options(b)
    except ValueError as exc:
        return err(str(exc), 400)
    item = {"name": name, "subject": str(b.get("subject") or "")[:200], "message": str(b.get("message") or "")[:20000],
            "product_id": str(b.get("product_id") or "")[:100], "images": images, "layout": layout, "button": button}
    templates = [t for t in (get_setting("email.templates", []) or []) if t.get("name") != name]
    if len(templates) >= TEMPLATES_MAX:
        return err(f"At most {TEMPLATES_MAX} templates. Delete one first.", 400)
    templates = sorted(templates + [item], key=lambda t: t["name"].lower())
    if len(json.dumps(templates)) > 60000:  # the settings column holds 64 KB on MySQL
        return err("There is no room for this template. Delete an old one or shorten the message.", 400)
    set_setting("email.templates", templates)
    audit(user, "save_email_template", name)
    return jsonify({"ok": True, "templates": get_setting("email.templates", [])})


@app.delete("/api/admin/email-templates")
def admin_delete_email_template():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can send emails", 403)
    name = str(request.args.get("name") or "")
    set_setting("email.templates", [t for t in (get_setting("email.templates", []) or []) if t.get("name") != name])
    audit(user, "delete_email_template", name)
    return jsonify({"ok": True, "templates": get_setting("email.templates", [])})


@app.post("/api/admin/broadcast")
def admin_broadcast():
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can send emails", 403)
    if not RESEND_API_KEY:
        return err("Email is not set up. Add RESEND_API_KEY to the settings (on STRATO: rotaidem-data/config.env).", 400)
    b = body()
    subject = str(b.get("subject", "")).strip()
    message = str(b.get("message", "")).strip()
    if not subject or not message:
        return err("Subject and message are required", 400)
    product = None
    if b.get("product_id"):
        product = db().first("SELECT * FROM products WHERE id = ?", b["product_id"])
        if not product:
            return err("That product no longer exists", 400)
    try:
        images, layout, button = email_options(b)
    except ValueError as exc:
        return err(str(exc), 400)
    test = str(b.get("test_email", "")).strip().lower()
    if test:
        if not EMAIL_RE.match(test):
            return err("Enter a valid test address", 400)
        recipients = [test]
    else:
        groups = [g for g in b.get("groups", []) if g in AUDIENCES]
        recipients = collect_recipients(groups, b.get("extra", ""))
    if not recipients:
        return err("No recipients match the chosen audience", 400)
    sent, failed, last_error = 0, 0, ""
    for i in range(0, len(recipients), 100):
        chunk = recipients[i:i + 100]
        messages = [{"from": MAIL_FROM, "to": [r], "subject": subject, "html": product_email_html(product, message, r, images, layout, subject, button),
                     "headers": unsubscribe_headers(r)} for r in chunk]
        ok, error = send_batch(messages)
        sent += ok
        if error:
            failed += len(chunk)
            last_error = error
    if not test:
        db().run("INSERT INTO campaigns (subject, product_id, audience, sent_count, failed_count, admin_id) VALUES (?, ?, ?, ?, ?, ?)",
                 subject, product["id"] if product else "", ",".join(b.get("groups", [])) + (" +extra" if b.get("extra") else ""), sent, failed, user["id"])
        audit(user, "send_email", f"{subject} ({sent} sent)")
    if failed and not sent:
        return err(last_error, 502)
    return jsonify({"ok": True, "sent": sent, "failed": failed, "test": bool(test), "error": last_error})


@app.get("/api/admin/campaigns")
def admin_campaigns():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"campaigns": db().all(
        "SELECT campaigns.*, users.name AS admin_name FROM campaigns LEFT JOIN users ON users.id = campaigns.admin_id ORDER BY campaigns.id DESC LIMIT 50")})


@app.get("/api/admin/subscribers")
def admin_subscribers():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"subscribers": db().all("SELECT * FROM subscribers ORDER BY id DESC")})


@app.post("/api/admin/subscribers")
def admin_add_subscribers():
    """Paste one or many addresses. Someone who unsubscribed is never signed up again from here."""
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    words = [w.strip().lower() for w in re.split(r"[\s,;]+", str(body().get("emails", ""))) if w.strip()]
    if not words:
        return err("Paste at least one email address", 400)
    if len(words) > 5000:
        return err("At most 5000 addresses at a time", 400)
    added, skipped, invalid = [], [], []
    for email in dict.fromkeys(words):
        if not EMAIL_RE.match(email) or len(email) > 200:
            invalid.append(email)
        elif db().first("SELECT id FROM subscribers WHERE email = ?", email):
            skipped.append(email)
        else:
            db().run("INSERT INTO subscribers (email, source) VALUES (?, 'added by admin')", email)
            added.append(email)
    if added:
        audit(user, "add_subscribers", f"{len(added)} added")
    return jsonify({"ok": True, "added": len(added), "skipped": len(skipped), "invalid": invalid[:50]})


@app.delete("/api/admin/subscribers/<int:sub_id>")
def admin_delete_subscriber(sub_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can remove subscribers", 403)
    db().run("DELETE FROM subscribers WHERE id = ?", sub_id)
    audit(user, "delete_subscriber", sub_id)
    return jsonify({"ok": True})


@app.route("/api/<path:_rest>", methods=["GET", "POST", "PUT", "DELETE"])
def api_not_found(_rest):
    return err("Not found", 404)


# ---------- static site ----------

@app.get("/health")
def health():
    """For monitoring: says whether the database answers, nothing else."""
    try:
        db().first("SELECT 1 AS ok")
        return jsonify({"ok": True, "database": "ok"})
    except Exception:  # noqa: BLE001 - the reason goes to the log, not to visitors
        app.logger.exception("Health check: database unavailable")
        return jsonify({"ok": False, "database": "unavailable"}), 503


# ---------- status page (/status and Admin -> Status) ----------
# STRATO has no cron, so the checks run when someone opens /status or the admin Status tab: at most
# once a minute. Every 5 minutes a snapshot is kept for the 24-hour uptime meters.
# shortcut: uptime only counts times someone was looking; point an outside pinger at /api/status if that matters.
STATUS_FRESH_SECONDS, STATUS_SNAPSHOT_SECONDS, STATUS_SNAPSHOTS = 60, 300, 288


class StatusOff(Exception):
    """The service is not set up (no key in config.env): shown as "Not in use", not as down."""


def status_database():
    db().first("SELECT 1 AS ok")
    stored = (db().first("SELECT value FROM meta WHERE key = 'schema_version'") or {}).get("value")
    if stored != SCHEMA_VERSION:
        raise RuntimeError(f"Tables are at version {stored}, the program expects {SCHEMA_VERSION}. Run: python3 rotaidem/strato/setup.py deploy")


def status_licences():
    signing_key()
    db().first("SELECT COUNT(*) AS n FROM license_activation_log")
    db().first("SELECT id, expires_at FROM licenses LIMIT 1")


def status_accounts():
    db().first("SELECT COUNT(*) AS n FROM users")
    db().first("SELECT COUNT(*) AS n FROM sessions")


def status_downloads():
    if not (db().first("SELECT COUNT(*) AS n FROM products") or {}).get("n"):
        raise RuntimeError("The catalogue has no products")


def status_checkout():
    if not STRIPE_SECRET_KEY:
        raise StatusOff()
    _, error = stripe_call("GET", "/v1/balance")
    if error:
        raise RuntimeError(error)


def status_email():
    if not RESEND_API_KEY:
        raise StatusOff()


def status_discord():
    if not DISCORD_BOT_TOKEN:
        raise StatusOff()
    _, error, _ = discord_call("GET", "/users/@me", timeout=8)
    if error:
        raise RuntimeError(error)


STATUS_CHECKS = (  # id, name on the page, check (None: the website answered, so it is up)
    ("website", "Website", None), ("database", "Database", status_database),
    ("licences", "HeavyPro licence activation", status_licences), ("accounts", "Accounts and My account", status_accounts),
    ("downloads", "Downloads and RotaHub updates", status_downloads), ("checkout", "Checkout (Stripe)", status_checkout),
    ("email", "Emails (licence keys, sign-up)", status_email), ("discord", "Discord bot", status_discord))


def status_check(fn):
    """(state, milliseconds, detail): "ok", "down" with the reason (staff only), or "off"."""
    start = time.perf_counter()
    state, detail = "ok", ""
    try:
        if fn:
            fn()
    except StatusOff:
        state, detail = "off", "Not set up"
    except Exception as exc:  # noqa: BLE001 - a failing check is exactly what this reports
        state, detail = "down", f"{type(exc).__name__}: {exc}"[:300]
    return state, int((time.perf_counter() - start) * 1000), detail


def status_alert(before, checks):
    """A post in the Discord status channel when something goes down or comes back. Never fails the caller."""
    was = {c["id"]: c["state"] for c in (before or {}).get("checks", [])}
    minute = int(time.time()) // 60
    for c in checks:
        if {was.get(c["id"]), c["state"]} != {"ok", "down"} or not discord_claim(f"status.{c['id']}.{minute}"):
            continue  # shortcut: one failed check posts at once; require two in a row if it gets noisy
        down = c["state"] == "down"
        discord_auto("status", {"embed": {
            "title": f"{'🔴' if down else '🟢'} {c['name']} {'is down' if down else 'is back up'}",
            "description": "We are looking into it." if down else "Working again.",
            "url": "/status", "color": "#e5484d" if down else "#4cc38a"},
            "buttons": [{"label": "Live status", "url": "/status"}]})


def status_run(force=False):
    """The latest checks (run again when older than a minute) and the uptime history."""
    now = time.time()
    try:
        last, history = get_setting("status.last", None), get_setting("status.history", []) or []
    except Exception:  # noqa: BLE001 - database down: check anyway, nothing to compare with
        last, history = None, []
    if last and not force and now - float(last.get("at") or 0) < STATUS_FRESH_SECONDS:
        return last, history
    checks = []
    for cid, name, fn in STATUS_CHECKS:
        state, ms, detail = status_check(fn)
        checks.append({"id": cid, "name": name, "state": state, "ms": ms, "detail": detail})
    current = {"at": now, "checks": checks}
    try:
        set_setting("status.last", current)
        if not history or now - float(history[-1]["t"]) >= STATUS_SNAPSHOT_SECONDS:
            history = (history + [{"t": int(now), "s": {c["id"]: c["state"] for c in checks}}])[-STATUS_SNAPSHOTS:]
            set_setting("status.history", history)
        status_alert(last, checks)
    except Exception:  # noqa: BLE001 - the answer still shows what is down
        app.logger.exception("Status page: could not save the checks")
    return current, history


def status_answer(current, history, staff=False):
    rows = []
    for c in current["checks"]:
        seen = [h["s"].get(c["id"]) for h in history if h["s"].get(c["id"]) in ("ok", "down")]
        row = {k: c[k] for k in ("id", "name", "state", "ms")}
        row["uptime"] = round(100 * seen.count("ok") / len(seen), 2) if seen else None
        row["recent"] = [h["s"].get(c["id"], "") for h in history[-48:]]  # the last 4 hours, oldest first
        if staff:
            row["detail"] = c["detail"]
        rows.append(row)
    states = {r["id"]: r["state"] for r in rows}
    overall = "down" if "down" in (states["website"], states["database"]) else "degraded" if "down" in states.values() else "ok"
    stamp = lambda t: datetime.fromtimestamp(float(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return {"overall": overall, "checked_at": stamp(current["at"]), "since": stamp(history[0]["t"]) if history else None, "checks": rows}


@app.get("/api/status")
def public_status():
    return jsonify(status_answer(*status_run()))


@app.get("/api/admin/status")
def admin_status():
    if not staff_user():
        return err("Admin only", 403)
    answer = status_answer(*status_run(force=request.args.get("refresh") == "1"), staff=True)
    answer["discord_channel"] = bool(discord_settings()["status_channel"]) and discord_ready()
    return jsonify(answer)


@app.get("/")
@app.get("/<path:path>")
def site(path=""):
    path = path.strip("/")
    for candidate in (path, f"{path}.html", f"{path}/index.html", "index.html" if not path else None):
        if candidate and (PUBLIC / candidate).is_file():
            if candidate in SHARE_FILES:  # with its link-preview tags
                resp = app.response_class(with_share_tags((PUBLIC / candidate).read_text(encoding="utf-8"), candidate), mimetype="text/html")
                resp.headers["Cache-Control"] = "no-cache"
                return resp
            return send_from_directory(PUBLIC, candidate)
    abort(404)


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return err("Not found", 404)
    return send_from_directory(PUBLIC, "404.html"), 404


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if request.path.startswith("/api/") or request.path.startswith("/admin"):
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


SCHEMA_READY = False


def init_schema():
    """Create tables once. A database outage must not stop the site from serving pages."""
    global SCHEMA_READY
    if SCHEMA_READY:
        return True
    if os.environ.get("ROTAIDEM_NO_SCHEMA_UPDATE"):
        SCHEMA_READY = True  # read-only diagnostics (strato/setup.py diagnose) must not change tables
        return True
    try:
        ensure_schema()
        SCHEMA_READY = True
    except Exception as exc:  # noqa: BLE001
        print(f"Database not ready: {exc}", file=sys.stderr, flush=True)
    return SCHEMA_READY


@app.before_request
def schema_guard():
    if request.path.startswith("/api/") and not init_schema():
        return err("Database is not reachable right now. Try again in a minute.", 503)


@app.before_request
def due_discord_posts():
    if request.path.startswith("/api/") and request.path != "/api/discord/interactions":
        try:
            discord_run_due()
        except Exception:  # noqa: BLE001 - visitors never see a Discord problem
            app.logger.exception("Due Discord posts failed")
        affiliate_payouts_due()
        privacy_cleanup()


init_schema()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 8000)), debug=True)
