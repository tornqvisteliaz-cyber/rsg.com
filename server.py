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
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_from_directory

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
SCHEMA_VERSION = "2026-10-21-profiles"


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
    ):
        d.run(sql + tail)
    for table, column in (("products", "images TEXT"), ("products", "tagline TEXT"), ("products", "features TEXT"),
                          ("products", "sort_order INTEGER DEFAULT 0"), ("orders", "product_id TEXT")):
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

def today():
    d = datetime.now()
    return f"{d:%B} {d.day}, {d.year}"


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def err(message, status):
    return jsonify({"error": message}), status


def bearer():
    # X-Rotaidem-Token is a copy for web servers that drop the Authorization header before CGI scripts.
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    return token or request.headers.get("X-Rotaidem-Token", "").strip()


def current_user():
    token = bearer()
    if not token:
        return None
    return db().first("SELECT * FROM users WHERE token = ?", token)


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
        token = uuid.uuid4().hex
        db().run("INSERT INTO users (name, email, password_hash, role, token) VALUES (?, ?, ?, 'Owner', ?)",
                 admin_name, admin_email, hash_password(password), token)
        return jsonify(session_payload({"name": admin_name, "email": admin_email, "role": "Owner"}, token))
    if not user or not verify_password(password, user["password_hash"]):
        return err("Invalid email or password", 401)
    token = uuid.uuid4().hex
    db().run("UPDATE users SET token = ? WHERE id = ?", token, user["id"])
    return jsonify(session_payload(user, token))


@app.post("/api/signup")
def signup():
    b = body()
    name = str(b.get("name", "")).strip()
    email = str(b.get("email", "")).strip().lower()
    password = str(b.get("password", ""))
    if not name or not email or len(password) < 6:
        return err("Name, email and a 6 character password are required", 400)
    if db().first("SELECT id FROM users WHERE LOWER(email) = ?", email):
        return err("Email already exists", 400)
    token = uuid.uuid4().hex
    db().run("INSERT INTO users (name, email, password_hash, role, token) VALUES (?, ?, ?, 'Customer', ?)",
             name, email, hash_password(password), token)
    return jsonify({"token": token, "name": name, "email": email, "role": "Customer", "is_admin": False})


@app.post("/api/logout")
def logout():
    token = bearer()
    if token:
        db().run("UPDATE users SET token = NULL WHERE token = ?", token)
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    count = db().first("SELECT COUNT(*) AS n FROM orders WHERE customer_id = ?", user["id"])
    return jsonify({"user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"],
                             "is_admin": user["role"] in STAFF, "orders_count": count["n"] if count else 0}})


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
    token = uuid.uuid4().hex  # signs out every other browser and signs this one in
    db().run("UPDATE users SET password_hash = ?, token = ? WHERE id = ?", hash_password(password), token, user["id"])
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

@app.get("/api/products")
def products():
    rows = db().all("SELECT * FROM products ORDER BY sort_order, name")
    return jsonify({"products": [with_images(r) for r in rows]})


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
    db().run(
        "INSERT INTO products (id, name, simulator, version, folder_name, download_url, image_url, images, price, description, buy_url, status, tagline, features, sort_order) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, simulator=excluded.simulator, "
        "version=excluded.version, folder_name=excluded.folder_name, download_url=excluded.download_url, image_url=excluded.image_url, "
        "images=excluded.images, price=excluded.price, description=excluded.description, buy_url=excluded.buy_url, status=excluded.status, "
        "tagline=excluded.tagline, features=excluded.features, sort_order=excluded.sort_order",
        pid, name, b.get("simulator") or "MSFS 2024", b.get("version") or "0.1.0", b.get("folder_name") or f"rotaidem-{pid}",
        b.get("download_url") or "", b.get("image_url") or "", json.dumps(list_images(b.get("images"))),
        b.get("price") or "", b.get("description") or "", b.get("buy_url") or "mailto:Eliaz@rotaidem.com",
        b.get("status") or "in_development", b.get("tagline") or "", json.dumps(list_images(b.get("features"))), sort_order)
    audit(user, "save_product", pid)
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
    return jsonify({"posts": db().all("SELECT * FROM posts ORDER BY id DESC")})


@app.post("/api/newsletter")
def save_newsletter():
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    b = body()
    if not b.get("title") or not b.get("content"):
        return err("Title and content are required", 400)
    if b.get("id"):
        db().run("UPDATE posts SET title = ?, content = ?, image_url = ? WHERE id = ?",
                 b["title"], b["content"], b.get("image_url") or "", int(b["id"]))
    else:
        db().run("INSERT INTO posts (title, content, image_url, date) VALUES (?, ?, ?, ?)",
                 b["title"], b["content"], b.get("image_url") or "", today())
    audit(user, "save_post", b["title"])
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
    db().run("INSERT INTO messages (name, email, message) VALUES (?, ?, ?)",
             str(b.get("name", ""))[:200], str(b.get("email", ""))[:200], str(b.get("message", ""))[:5000])
    return jsonify({"ok": True})


@app.post("/api/subscribe")
def subscribe():
    b = body()
    email = str(b.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        return err("Please enter a valid email address", 400)
    existing = db().first("SELECT id FROM subscribers WHERE email = ?", email)
    if existing:
        db().run("UPDATE subscribers SET unsubscribed = 0 WHERE id = ?", existing["id"])
    else:
        db().run("INSERT INTO subscribers (email, source) VALUES (?, ?)", email, str(b.get("source", ""))[:100])
    return jsonify({"ok": True})


def unsubscribe_token(email):
    return hmac.new(SECRET_KEY.encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:32]


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
                    "orders": n("SELECT COUNT(*) AS n FROM orders"),
                    "posts": n("SELECT COUNT(*) AS n FROM posts"),
                    "messages": n("SELECT COUNT(*) AS n FROM messages"),
                    "subscribers": n("SELECT COUNT(*) AS n FROM subscribers WHERE unsubscribed = 0"),
                    "licenses": n("SELECT COUNT(*) AS n FROM licenses WHERE status = 'active'"),
                    "email_ready": bool(RESEND_API_KEY),
                    "revenue": float(revenue["n"] if revenue else 0)})


@app.get("/api/admin/customers")
def admin_customers():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"customers": db().all("SELECT id, name, email, role, created_at FROM users ORDER BY id DESC")})


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
    db().run("UPDATE users SET role = ?, token = NULL WHERE id = ?", role, user_id)
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


@app.get("/api/admin/messages")
def admin_messages():
    if not staff_user():
        return err("Admin only", 403)
    return jsonify({"messages": db().all("SELECT * FROM messages ORDER BY id DESC")})


@app.delete("/api/admin/messages/<int:msg_id>")
def admin_delete_message(msg_id):
    user = staff_user()
    if not user:
        return err("Admin only", 403)
    db().run("DELETE FROM messages WHERE id = ?", msg_id)
    return jsonify({"ok": True})


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


def license_email_html(keys, product_name, max_activations):
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
<tr><td style="padding:20px 28px;border-top:1px solid #e4e4e7;font-size:12px;color:#6d6f75">Keep this email somewhere safe. Questions? Email <a href="mailto:Eliaz@rotaidem.com" style="color:#6d6f75">Eliaz@rotaidem.com</a>.</td></tr>
</table></td></tr></table></body></html>"""


def email_license_keys(email, keys, product_id):
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
                               "html": license_email_html(keys, name, lic["max_activations"] if lic else 2)}])
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
    db().run("UPDATE licenses SET status = ?, email = ?, customer_id = ?, max_activations = ?, note = ?, product_id = ? WHERE id = ?",
             status, email, customer["id"] if customer else None, clamp_int(b.get("max_activations"), lic["max_activations"], 1, 100),
             str(b.get("note", lic["note"]) or "")[:200], product_id, license_id)
    audit(user, "update_license", f"{lic['license_key']} ({status})")
    return jsonify({"ok": True})


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


def license_for_request():
    """Shared checks for the public activation endpoints. Returns (licence, machine_id, error_response)."""
    b = body()
    key = normalize_key(b.get("key"))
    if not key:
        return None, "", (jsonify({"valid": False, "reason": "invalid_format", "error": "That is not a valid licence key."}), 400)
    lic = db().first("SELECT * FROM licenses WHERE license_key = ?", key)
    if not lic:
        return None, "", (jsonify({"valid": False, "reason": "not_found", "error": "This licence key does not exist."}), 404)
    if lic["status"] != "active":
        return None, "", (jsonify({"valid": False, "reason": "revoked", "error": "This licence key has been revoked."}), 403)
    product = str(b.get("product") or "").strip()
    if product and not same_product(product, lic["product_id"]):
        return None, "", (jsonify({"valid": False, "reason": "wrong_product", "error": "This licence key is for another product."}), 403)
    machine = str(b.get("machine_id") or "").strip()[:128]
    if not machine:
        return None, "", (jsonify({"valid": False, "reason": "missing_machine", "error": "machine_id is required."}), 400)
    return lic, machine, None


def license_reply(lic, used):
    return jsonify({"valid": True, "product_id": lic["product_id"], "activations_used": used,
                    "max_activations": lic["max_activations"], "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})


@app.post("/api/licenses/activate")
def license_activate():
    lic, machine, error = license_for_request()
    if error:
        return error
    name = str(body().get("machine_name") or "")[:100]
    existing = db().first("SELECT id FROM license_activations WHERE license_id = ? AND machine_id = ?", lic["id"], machine)
    used = (db().first("SELECT COUNT(*) AS n FROM license_activations WHERE license_id = ?", lic["id"]) or {}).get("n", 0)
    if existing:
        db().run("UPDATE license_activations SET last_seen = CURRENT_TIMESTAMP, machine_name = ? WHERE id = ?", name, existing["id"])
    elif used >= lic["max_activations"]:
        return jsonify({"valid": False, "reason": "activation_limit", "activations_used": used, "max_activations": lic["max_activations"],
                        "error": f"This key is already active on {used} computer(s). Deactivate one first or contact support."}), 403
    else:
        db().run("INSERT INTO license_activations (license_id, machine_id, machine_name) VALUES (?, ?, ?)", lic["id"], machine, name)
        used += 1
    return license_reply(lic, used)


@app.post("/api/licenses/validate")
def license_validate():
    lic, machine, error = license_for_request()
    if error:
        return error
    existing = db().first("SELECT id FROM license_activations WHERE license_id = ? AND machine_id = ?", lic["id"], machine)
    if not existing:
        return jsonify({"valid": False, "reason": "not_activated", "error": "This computer has not been activated with this key."}), 403
    db().run("UPDATE license_activations SET last_seen = CURRENT_TIMESTAMP WHERE id = ?", existing["id"])
    used = (db().first("SELECT COUNT(*) AS n FROM license_activations WHERE license_id = ?", lic["id"]) or {}).get("n", 0)
    return license_reply(lic, used)


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
    rows = db().all("SELECT licenses.id, license_key, product_id, status, max_activations, licenses.created_at, "
                    "(SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS activations "
                    "FROM licenses WHERE customer_id = ? OR LOWER(email) = ? ORDER BY licenses.id DESC", user["id"], (user["email"] or "").lower())
    return jsonify({"licenses": rows})


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


def site_settings():
    images = dict(IMAGE_SLOTS)
    images.update({k: v for k, v in (get_setting("site.images", {}) or {}).items() if k in IMAGE_SLOTS and v})
    banner = get_setting("site.announcement", {}) or {}
    texts = get_setting("site.texts", {}) or {}
    return {"images": images, "defaults": IMAGE_SLOTS, "texts": texts if isinstance(texts, dict) else {},
            "announcement": {"enabled": bool(banner.get("enabled")), "text": str(banner.get("text") or ""), "link": str(banner.get("link") or "")}}


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
        set_setting("site.announcement", {"enabled": bool(a.get("enabled")), "text": str(a.get("text") or "")[:200], "link": link[:300]})
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
    audit(user, "save_settings", ", ".join(k for k in ("images", "announcement", "texts") if k in b)
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
    return jsonify({"ok": True, "id": row["id"], "url": f"/media/{row['id']}.{MEDIA_TYPES[mime]}"})


@app.delete("/api/admin/media/<int:media_id>")
def admin_delete_media(media_id):
    user = manager_user()
    if not user:
        return err("Only the Owner or an Admin can delete images", 403)
    row = db().first("SELECT filename FROM media WHERE id = ?", media_id)
    db().run("DELETE FROM media WHERE id = ?", media_id)
    audit(user, "delete_media", row["filename"] if row else media_id)
    return jsonify({"ok": True})


@app.get("/media/<int:media_id>.<ext>")
def media_file(media_id, ext):
    row = db().first("SELECT mime, data FROM media WHERE id = ?", media_id)
    if not row or MEDIA_TYPES.get(row["mime"]) != ext:
        abort(404)
    resp = app.response_class(base64.b64decode(row["data"]), mimetype=row["mime"])
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------- backup ----------

BACKUP_TABLES = ("users", "products", "liveries", "posts", "messages", "orders", "audit_logs", "meta", "subscribers",
                 "licenses", "license_activations", "media", "campaigns", "shared_profiles")


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
    u = db().first("SELECT id, name, email, role, created_at FROM users WHERE id = ?", user_id)
    if not u:
        return err("User not found", 404)
    u["orders"] = db().all("SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC", user_id)
    u["licenses"] = db().all("SELECT licenses.*, (SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS activations "
                             "FROM licenses WHERE customer_id = ? OR LOWER(email) = ? ORDER BY id DESC", user_id, (u["email"] or "").lower())
    return jsonify({"user": u})


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


def product_email_html(product, message, email):
    base = site_url()
    link = f"{base}/addons/view?id={product['id']}" if product else base
    unsub = f"{base}/unsubscribe?email={urllib.parse.quote(email)}&token={unsubscribe_token(email)}"
    text = "<br>".join(escape(line) for line in (message or "").splitlines())
    block = ""
    if product:
        image = product.get("image_url") or ""
        if image.startswith("/media/") or image.startswith("/img/"):
            image = base + image  # uploaded images need a full address inside an email
        img = f'<img src="{escape(image)}" alt="" width="560" style="display:block;width:100%;max-width:560px;height:auto;margin:0 0 20px">' if image.startswith("https://") else ""
        block = (f'{img}<h2 style="margin:0 0 6px;font-size:22px;color:#141517">{escape(product["name"])}</h2>'
                 f'<p style="margin:0 0 6px;color:#6b7480">{escape(product.get("tagline") or "")}</p>'
                 f'<p style="margin:0 0 22px;font-weight:600;color:#141517">{escape(product.get("price") or "")}</p>')
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#39424e">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff">
<tr><td style="background:#141517;padding:20px 28px;color:#ffffff;font-weight:bold;letter-spacing:3px;font-size:15px">ROTAIDEM</td></tr>
<tr><td style="padding:28px 28px 8px;font-size:16px;line-height:1.6">{text}</td></tr>
<tr><td style="padding:12px 28px 28px">{block}<a href="{escape(link)}" style="display:inline-block;background:#141517;color:#ffffff;text-decoration:none;padding:13px 24px;font-weight:bold">{"View " + escape(product["name"]) if product else "Visit rotaidem.com"}</a></td></tr>
<tr><td style="padding:20px 28px;border-top:1px solid #e2e5e9;font-size:12px;color:#6b7480">You are receiving this because you have an account with Rotaidem, contacted us or subscribed to our news.
<a href="{escape(unsub)}" style="color:#6b7480">Unsubscribe</a></td></tr>
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
        messages = [{"from": MAIL_FROM, "to": [r], "subject": subject, "html": product_email_html(product, message, r)} for r in chunk]
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


@app.get("/")
@app.get("/<path:path>")
def site(path=""):
    path = path.strip("/")
    for candidate in (path, f"{path}.html", f"{path}/index.html", "index.html" if not path else None):
        if candidate and (PUBLIC / candidate).is_file():
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


init_schema()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 8000)), debug=True)
