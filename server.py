"""Rotaidem site for Python hosts such as Starto.

Serves the static site in cloudflare/public and the same /api routes as
cloudflare/functions/api/[[path]].js, so one codebase runs on Cloudflare
Pages or on any host that runs `gunicorn server:app`.

Database: PostgreSQL when DATABASE_URL is set, otherwise SQLite at
DATA_DIR/rotaidem.db (put DATA_DIR on persistent storage).
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
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent / "instance"))
STAFF = ("Owner", "Admin", "Support")
SECRET_KEY = os.environ.get("SECRET_KEY") or os.environ.get("ADMIN_PASSWORD") or "rotaidem-dev-secret"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
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
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


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
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(DATA_DIR / "rotaidem.db")
            self.conn.row_factory = sqlite3.Row
            self.conn.isolation_level = None

    def run(self, sql, *args):
        if USE_PG:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, args)

    def first(self, sql, *args):
        row = self.run(sql, *args).fetchone()
        return dict(row) if row else None

    def all(self, sql, *args):
        return [dict(r) for r in self.run(sql, *args).fetchall()]

    def close(self):
        self.conn.close()


def db():
    if "db" not in g:
        g.db = DB()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def ensure_schema():
    d = DB()
    pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    now = "CURRENT_TIMESTAMP"
    for sql in (
        f"CREATE TABLE IF NOT EXISTS users (id {pk}, name TEXT, email TEXT UNIQUE, password_hash TEXT, role TEXT DEFAULT 'Customer', token TEXT, created_at TEXT DEFAULT {now})",
        "CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, name TEXT, simulator TEXT, version TEXT, folder_name TEXT, download_url TEXT, image_url TEXT, price TEXT, description TEXT, buy_url TEXT, status TEXT, images TEXT, tagline TEXT, features TEXT, sort_order INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS liveries (id TEXT PRIMARY KEY, name TEXT, aircraft TEXT, folder_name TEXT, download_url TEXT, image_url TEXT)",
        f"CREATE TABLE IF NOT EXISTS posts (id {pk}, title TEXT, content TEXT, image_url TEXT, date TEXT)",
        f"CREATE TABLE IF NOT EXISTS messages (id {pk}, name TEXT, email TEXT, message TEXT, created_at TEXT DEFAULT {now})",
        f"CREATE TABLE IF NOT EXISTS orders (id {pk}, customer_id INTEGER, product_id TEXT, status TEXT DEFAULT 'Completed', payment_status TEXT DEFAULT 'Paid', amount NUMERIC DEFAULT 0, invoice_file TEXT, created_at TEXT DEFAULT {now})",
        f"CREATE TABLE IF NOT EXISTS audit_logs (id {pk}, admin_id INTEGER, action TEXT, target TEXT, ip_address TEXT, created_at TEXT DEFAULT {now})",
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
        f"CREATE TABLE IF NOT EXISTS subscribers (id {pk}, email TEXT UNIQUE, source TEXT, unsubscribed INTEGER DEFAULT 0, created_at TEXT DEFAULT {now})",
        f"CREATE TABLE IF NOT EXISTS licenses (id {pk}, license_key TEXT UNIQUE, product_id TEXT, email TEXT, customer_id INTEGER, order_id INTEGER, status TEXT DEFAULT 'active', max_activations INTEGER DEFAULT 2, note TEXT, created_by TEXT, created_at TEXT DEFAULT {now})",
        f"CREATE TABLE IF NOT EXISTS license_activations (id {pk}, license_id INTEGER, machine_id TEXT, machine_name TEXT, first_seen TEXT DEFAULT {now}, last_seen TEXT DEFAULT {now}, UNIQUE (license_id, machine_id))",
        f"CREATE TABLE IF NOT EXISTS campaigns (id {pk}, subject TEXT, product_id TEXT, audience TEXT, sent_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0, admin_id INTEGER, created_at TEXT DEFAULT {now})",
    ):
        d.run(sql)
    for table, column in (("products", "images TEXT"), ("products", "tagline TEXT"), ("products", "features TEXT"),
                          ("products", "sort_order INTEGER DEFAULT 0"), ("orders", "product_id TEXT")):
        try:
            d.run(f"ALTER TABLE {table} ADD COLUMN {column}")
        except Exception:
            pass
    # One-time wipe of the old catalog for the HeavyPro relaunch.
    if not d.first("SELECT value FROM meta WHERE key = 'catalog_reset_heavypro'"):
        d.run("DELETE FROM products")
        d.run("INSERT INTO meta (key, value) VALUES ('catalog_reset_heavypro', ?)", today())
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
    return request.headers.get("Authorization", "").replace("Bearer ", "").strip()


def current_user():
    token = bearer()
    if not token:
        return None
    return db().first("SELECT * FROM users WHERE token = ?", token)


def staff_user():
    user = current_user()
    return user if user and user["role"] in STAFF else None


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
    return jsonify({"name": user["name"], "role": user["role"],
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
    db().run("UPDATE licenses SET status = ?, email = ?, customer_id = ?, max_activations = ?, note = ? WHERE id = ?",
             status, email, customer["id"] if customer else None, clamp_int(b.get("max_activations"), lic["max_activations"], 1, 100),
             str(b.get("note", lic["note"]) or "")[:200], license_id)
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
    if product and product != lic["product_id"]:
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


@app.get("/api/account/licenses")
def account_licenses():
    user = current_user()
    if not user:
        return err("Unauthorized", 401)
    rows = db().all("SELECT licenses.id, license_key, product_id, status, max_activations, licenses.created_at, "
                    "(SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS activations "
                    "FROM licenses WHERE customer_id = ? OR LOWER(email) = ? ORDER BY licenses.id DESC", user["id"], (user["email"] or "").lower())
    return jsonify({"licenses": rows})


# ---------- email broadcasts ----------

def manager_user():
    """Owner or Admin: allowed to email people."""
    user = current_user()
    return user if user and user["role"] in ("Owner", "Admin") else None


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
    return (os.environ.get("SITE_URL") or request.host_url).rstrip("/")


def product_email_html(product, message, email):
    base = site_url()
    link = f"{base}/addons/view?id={product['id']}" if product else base
    unsub = f"{base}/unsubscribe?email={urllib.parse.quote(email)}&token={unsubscribe_token(email)}"
    text = "<br>".join(escape(line) for line in (message or "").splitlines())
    block = ""
    if product:
        image = product.get("image_url") or ""
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
        return ("Resend rejected the API key. Check RESEND_API_KEY in Render (it starts with re_ and must have "
                "sending access), save, and let the service redeploy.")
    if "error code: 1010" in text or "<html" in text:
        return "Resend's firewall blocked the request. Try again in a minute; if it keeps happening, contact Resend support."
    if "from" in text and ("invalid" in text or "format" in text):
        return f"MAIL_FROM is not a valid sender ({MAIL_FROM}). Use the format: Rotaidem <no-reply@rotaidem.com>"
    return f"Resend said ({status}): {message or raw[:200]}"


def send_batch(messages):
    """Send up to 100 emails through Resend's batch API. Returns (sent, error)."""
    req = urllib.request.Request("https://api.resend.com/emails/batch", data=json.dumps(messages).encode(), method="POST",
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
        return err("Email is not set up. Add RESEND_API_KEY to the server environment and redeploy.", 400)
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
    return jsonify({"ok": True})


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
