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
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_from_directory

PUBLIC = Path(__file__).resolve().parent / "cloudflare" / "public"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_PG = DATABASE_URL.startswith("postgresql://")
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent / "instance"))
STAFF = ("Owner", "Admin", "Support")
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
    return jsonify({"ok": True})


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
