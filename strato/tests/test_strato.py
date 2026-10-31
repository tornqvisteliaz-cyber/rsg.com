"""Automated tests for the STRATO (CGI) setup.

Each test installs Rotaidem into a temporary "webspace" with strato/setup.py and then sends every
request through cgi-bin/rotaidem.py as a separate process, the way Apache on STRATO runs it
(minimal environment, no UTF-8 locale). Run with the Python version the host uses:

    python3.11 -m unittest discover -s strato/tests -v

SQLite is always tested. To also test MySQL/MariaDB, point at empty databases (all tables in them
are dropped):

    ROTAIDEM_TEST_MYSQL="mysql8=127.0.0.1:3308:user:password:db;mariadb=127.0.0.1:3306:user:password:db"
"""
import base64
import hashlib
import hmac
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
OWNER = ("eliaz", "Eliaz@rotaidem.com", "owner-pass-123")
IMAGE = os.path.join(REPO, "cloudflare", "public", "img", "night.jpg")


class FakeResend:
    """Stands in for api.resend.com: records each batch and answers like Resend."""

    def __init__(self):
        self.requests, self.fail_with = [], None
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(raw)})
                status, reply = outer.fail_with or (200, {"data": [{"id": "test"}]})
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()


class FakeStripe:
    """Stands in for api.stripe.com: Checkout sessions made, read back, and marked paid by the test."""

    def __init__(self):
        self.created, self.sessions = [], {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self, status, data):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()).items()}
                if self.path != "/v1/checkout/sessions" or not self.headers.get("Authorization", "").startswith("Bearer sk_test_"):
                    return self.reply(400, {"error": {"message": "bad request"}})
                outer.created.append(form)
                sid = f"cs_test_{len(outer.created)}{os.urandom(6).hex()}"
                outer.sessions[sid] = {"id": sid, "object": "checkout.session", "mode": form["mode"], "status": "open", "payment_status": "unpaid",
                                       "amount_total": int(form["line_items[0][price_data][unit_amount]"]), "currency": form["line_items[0][price_data][currency]"],
                                       "customer_email": form.get("customer_email"), "url": f"https://checkout.stripe.test/c/{sid}",
                                       "metadata": {k[9:-1]: v for k, v in form.items() if k.startswith("metadata[")}}
                self.reply(200, outer.sessions[sid])

            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                if path == "/v1/checkout/sessions":
                    return self.reply(200, {"object": "list", "data": []})
                session = outer.sessions.get(path.rsplit("/", 1)[-1])
                self.reply(200, session) if session else self.reply(404, {"error": {"message": "No such checkout.session"}})

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def pay(self, sid):
        self.sessions[sid].update(status="complete", payment_status="paid")
        return self.sessions[sid]

    def stop(self):
        self.server.shutdown()


class FakeDiscord:
    """Stands in for discord.com/api: OAuth2 code exchange, the user, server members and their roles, commands, messages.
    An OAuth2 code "code-<discord id>" belongs to that Discord user."""
    BOT = "Bot bot-token-test"

    def __init__(self):
        self.members, self.commands, self.messages = {}, [], []  # members: {discord id: set of role ids}
        self.posted, self.crossposted = {}, []  # message id -> (channel, message) for Post to Discord
        self.reactions, self.pins = [], []
        self.bot_position = 5  # the bot's role; the buyer role is at 3. Below it, Discord refuses to give the role.
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self, status, data=None):
                raw = json.dumps(data).encode() if data is not None else b""
                self.send_response(status)
                if raw:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def handle_any(self):
                parts, auth, raw = self.path.split("?")[0].strip("/").split("/"), self.headers.get("Authorization", ""), self.body()
                if self.command == "POST" and self.path == "/oauth2/token":
                    form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                    ok = form.get("client_secret") == "client-secret-test" and form.get("code", "").startswith("code-")
                    return self.reply(200, {"access_token": "access-" + form["code"][5:], "token_type": "Bearer"}) if ok else self.reply(400, {"error": "invalid_grant"})
                if self.path == "/users/@me" and auth.startswith("Bearer access-"):
                    uid = auth[len("Bearer access-"):]
                    return self.reply(200, {"id": uid, "username": "pilot" + uid[-3:], "global_name": "Pilot " + uid[-3:]})
                if auth != FakeDiscord.BOT:
                    return self.reply(401, {"message": "401: Unauthorized"})
                if parts[:1] == ["guilds"] and len(parts) == 4 and parts[2] == "members" and self.command == "PUT":
                    data = json.loads(raw)
                    if data.get("access_token") != "access-" + parts[3]:
                        return self.reply(403, {"message": "Invalid OAuth2 access token"})
                    if parts[3] in outer.members:
                        return self.reply(204)
                    outer.members[parts[3]] = set(data.get("roles") or [])
                    return self.reply(201, {"user": {"id": parts[3]}})
                if parts[:1] == ["guilds"] and parts[2:] == ["roles"]:
                    return self.reply(200, [{"id": parts[1], "name": "@everyone", "position": 0, "permissions": "1024"},
                                            {"id": "1200000000000000002", "name": "Verified buyer", "position": 3, "permissions": "0"},
                                            {"id": "1200000000000000009", "name": "Rotaidem bot", "position": outer.bot_position, "permissions": str((1 << 28) | 1)}])
                if parts[:1] == ["guilds"] and len(parts) == 4 and parts[2] == "members" and self.command == "GET":
                    return self.reply(200, {"roles": ["1200000000000000009"]}) if parts[3] == "999" else self.reply(404, {"message": "Unknown Member"})
                if parts[:1] == ["guilds"] and len(parts) == 6 and parts[4] == "roles":
                    if outer.bot_position <= 3:
                        return self.reply(403, {"message": "Missing Permissions"})
                    if parts[3] not in outer.members:
                        return self.reply(404, {"message": "Unknown Member"})
                    (outer.members[parts[3]].add if self.command == "PUT" else outer.members[parts[3]].discard)(parts[5])
                    return self.reply(204)
                if parts[:1] == ["applications"] and self.command == "PUT":
                    outer.commands = json.loads(raw)
                    return self.reply(200, outer.commands)
                if parts[:1] == ["guilds"] and parts[2:] == ["channels"]:
                    return self.reply(200, [{"id": "1400000000000000001", "type": 4, "name": "Info", "position": 0},
                                            {"id": "1400000000000000002", "type": 0, "name": "general", "parent_id": "1400000000000000001", "position": 1},
                                            {"id": "1400000000000000003", "type": 5, "name": "announcements", "parent_id": "1400000000000000001", "position": 0},
                                            {"id": "1400000000000000004", "type": 2, "name": "Voice", "position": 2}])
                if parts[:1] == ["guilds"] and len(parts) == 2 and self.command == "GET":
                    return self.reply(200, {"id": parts[1], "name": "Rotaidem", "approximate_member_count": 128, "approximate_presence_count": 17})
                if parts[:1] == ["channels"] and len(parts) == 7 and parts[4] == "reactions":
                    outer.reactions.append((parts[3], urllib.parse.unquote(parts[5])))
                    return self.reply(204)
                if parts[:1] == ["channels"] and len(parts) == 4 and parts[2] == "pins":
                    outer.pins.append(parts[3])
                    return self.reply(204)
                if parts[:1] == ["channels"] and len(parts) == 5 and parts[4] == "crosspost":
                    outer.crossposted.append(parts[3])
                    return self.reply(200, {"id": parts[3]})
                if parts[:1] == ["channels"] and len(parts) == 4 and self.command in ("PATCH", "DELETE"):
                    if parts[3] not in outer.posted:
                        return self.reply(404, {"message": "Unknown Message"})
                    if self.command == "DELETE":
                        del outer.posted[parts[3]]
                        return self.reply(204)
                    outer.posted[parts[3]] = (parts[1], json.loads(raw))
                    return self.reply(200, {"id": parts[3]})
                if parts[:1] == ["channels"] and self.command == "POST":
                    outer.messages.append((parts[1], json.loads(raw)))
                    mid = str(1500000000000000000 + len(outer.messages))
                    outer.posted[mid] = outer.messages[-1]
                    return self.reply(200, {"id": mid})
                if self.path == "/users/@me":
                    return self.reply(200, {"id": "999", "username": "Rotaidem bot"})
                self.reply(404, {"message": "Unknown"})

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()


# Ed25519 signing for the test's own "Discord" (the website only checks signatures). RFC 8032.
_EP = 2 ** 255 - 19
_EL = 2 ** 252 + 27742317777372353535851937790883648493
_ED = -121665 * pow(121666, _EP - 2, _EP) % _EP


def _ed_add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % _EP, (a[1] + a[0]) * (b[1] + b[0]) % _EP
    C, D = 2 * a[3] * b[3] * _ED % _EP, 2 * a[2] * b[2] % _EP
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _EP, G * H % _EP, F * G % _EP, E * H % _EP)


def _ed_mul(s, point):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _ed_add(q, point)
        point, s = _ed_add(point, point), s >> 1
    return q


def _ed_base():
    y = 4 * pow(5, _EP - 2, _EP) % _EP
    x2 = (y * y - 1) * pow(_ED * y * y + 1, _EP - 2, _EP) % _EP
    x = pow(x2, (_EP + 3) // 8, _EP)
    if (x * x - x2) % _EP:
        x = x * pow(2, (_EP - 1) // 4, _EP) % _EP
    x = _EP - x if x & 1 else x
    return (x, y, 1, x * y % _EP)


def _ed_bytes(point):
    zi = pow(point[2], _EP - 2, _EP)
    x, y = point[0] * zi % _EP, point[1] * zi % _EP
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def ed25519_keys(seed):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    return a, h[32:], _ed_bytes(_ed_mul(a, _ed_base()))


def ed25519_sign(seed, message):
    a, prefix, public = ed25519_keys(seed)
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _EL
    big_r = _ed_bytes(_ed_mul(r, _ed_base()))
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % _EL
    return big_r + ((r + k * a) % _EL).to_bytes(32, "little")


DISCORD_SEED = b"rotaidem-test-discord-application"


class Response:
    def __init__(self, raw, stderr):
        head, _, self.body = raw.partition(b"\r\n\r\n")
        self.headers, self.status = {}, 200
        for line in head.decode("latin-1").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                self.headers[k.strip().lower()] = v.strip()
        if "status" in self.headers:
            self.status = int(self.headers["status"].split()[0])
        self.stderr = stderr.decode(errors="replace")

    def json(self):
        return json.loads(self.body)


def mysql_targets():
    out = []
    for item in filter(None, os.environ.get("ROTAIDEM_TEST_MYSQL", "").split(";")):
        label, spec = item.split("=", 1)
        host, port, user, password, database = spec.split(":", 4)
        out.append((label, {"MYSQL_HOST": host, "MYSQL_PORT": port, "MYSQL_USER": user,
                            "MYSQL_PASSWORD": password, "MYSQL_DATABASE": database}))
    return out


def empty_mysql(cfg):
    sys.path.insert(0, os.path.join(REPO, "strato", "vendor"))
    import pymysql
    conn = pymysql.connect(host=cfg["MYSQL_HOST"], port=int(cfg["MYSQL_PORT"]), user=cfg["MYSQL_USER"],
                           password=cfg["MYSQL_PASSWORD"], database=cfg["MYSQL_DATABASE"], ssl={"check_hostname": False})
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        for (table,) in cur.fetchall():
            cur.execute(f"DROP TABLE `{table}`")
    conn.close()


class Webspace:
    """A temporary STRATO-like webspace: rotaidem/ (the program), rotaidem-data/ (private) and
    rotaidem-public/ (the folder the domain shows), as on STRATO's new platform."""

    def __init__(self, db_values, resend_url, stripe_url="", discord_url=""):
        self.root = tempfile.mkdtemp(prefix="strato-")
        self.app = os.path.join(self.root, "rotaidem")
        os.mkdir(self.app)
        shutil.copy2(os.path.join(REPO, "server.py"), self.app)
        shutil.copytree(os.path.join(REPO, "cloudflare", "public"), os.path.join(self.app, "cloudflare", "public"))
        shutil.copytree(os.path.join(REPO, "strato"), os.path.join(self.app, "strato"),
                        ignore=shutil.ignore_patterns("__pycache__", "installed.json", "tests"))
        self.data = os.path.join(self.root, "rotaidem-data")
        self.public = os.path.join(self.root, "rotaidem-public")
        self.values = {"SECRET_KEY": "s" * 64, "CHECK_KEY": "checkkey123", "SITE_URL": "https://rotaidem.test",
                       "LICENSE_API_KEY": "generator-key-0123456789", "RESEND_API_KEY": "re_test", "RESEND_API_URL": resend_url,
                       "MAIL_FROM": "Rotaidem <no-reply@rotaidem.com>", **db_values}
        if stripe_url:
            self.values.update(STRIPE_SECRET_KEY="sk_test_fake0123456789", STRIPE_API_URL=stripe_url, STRIPE_WEBHOOK_SECRET="whsec_testsecret")
        if discord_url:
            self.values.update(DISCORD_CLIENT_ID="112233445566778899", DISCORD_CLIENT_SECRET="client-secret-test", DISCORD_BOT_TOKEN="bot-token-test",
                               DISCORD_PUBLIC_KEY=ed25519_keys(DISCORD_SEED)[2].hex(), DISCORD_API_URL=discord_url)
        if "MYSQL_HOST" not in self.values:
            self.values["DATA_DIR"] = self.data
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import setup; "
                "setup.install(sys.argv[2], sys.argv[3], __import__('json').loads(sys.argv[4]), tuple(sys.argv[5:8]))")
        self.setup_out = self.run_python(["-c", code, os.path.join(self.app, "strato"), self.public, self.data,
                                          json.dumps(self.values), *OWNER])

    def run_python(self, args, stdin=b""):
        p = subprocess.run([PY, *args], input=stdin, capture_output=True, cwd=self.root, timeout=120)
        if p.returncode:
            raise AssertionError(p.stdout.decode() + p.stderr.decode())
        return p.stdout.decode()

    def setup_cmd(self, *args, stdin=""):
        return self.run_python([os.path.join(self.app, "strato", "setup.py"), *args], stdin.encode())

    def setup_run(self, *args, stdin="", env=None):
        """Like setup_cmd, but returns (exit code, output) instead of failing on a non-zero exit."""
        p = subprocess.run([PY, os.path.join(self.app, "strato", "setup.py"), *args], input=stdin.encode(), capture_output=True,
                           cwd=self.root, timeout=180, env={**os.environ, **(env or {})})
        return p.returncode, p.stdout.decode() + p.stderr.decode()

    def cgi(self, method, path, body=None, token=None, headers=None, script="rotaidem.py", query="", flags=(), remote_addr="203.0.113.5"):
        script_path = os.path.join(self.public, "cgi-bin", script)
        env = {"PATH": "/usr/bin:/bin", "GATEWAY_INTERFACE": "CGI/1.1", "REQUEST_METHOD": method,
               "SCRIPT_NAME": "/cgi-bin/" + script, "SCRIPT_FILENAME": script_path, "PATH_INFO": path,
               "QUERY_STRING": query, "SERVER_NAME": "rotaidem.test", "SERVER_PORT": "443", "HTTPS": "on",
               "SERVER_PROTOCOL": "HTTP/1.1", "REMOTE_ADDR": remote_addr, "HTTP_HOST": "rotaidem.test"}
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
        if data:
            env.update(CONTENT_TYPE="application/json", CONTENT_LENGTH=str(len(data)))
        if token:
            env["HTTP_AUTHORIZATION"] = "Bearer " + token
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        p = subprocess.run([PY, *flags, script_path], input=data, env=env, capture_output=True, timeout=60)
        return Response(p.stdout, p.stderr)

    def wsgi(self, requests, script="rotaidem.py", flags=()):
        """STRATO's way: import the entry script once and call its `application` for each request."""
        probe = os.path.join(self.app, "strato", "wsgi_probe.py")
        p = subprocess.run([PY, *flags, probe, os.path.join(self.public, "cgi-bin", script)], input=json.dumps(requests),
                           capture_output=True, text=True, timeout=120)
        if p.returncode:
            raise AssertionError(p.stdout + p.stderr)
        return json.loads(p.stdout)

    def remove(self):
        shutil.rmtree(self.root, ignore_errors=True)


class StratoCGITests:
    db_values = {}

    @classmethod
    def setUpClass(cls):
        if cls.db_values:
            empty_mysql(cls.db_values)
        cls.resend = FakeResend()
        cls.stripe = FakeStripe()
        cls.discord = FakeDiscord()
        cls.w = Webspace(cls.db_values, cls.resend.url, cls.stripe.url, cls.discord.url)

    @classmethod
    def tearDownClass(cls):
        cls.w.remove()
        cls.resend.stop()
        cls.stripe.stop()
        cls.discord.stop()

    @classmethod
    def login(cls, who, password):
        r = cls.w.cgi("POST", "/api/login", {"email": who, "password": password})
        assert r.status == 200, (r.status, r.body, r.stderr)
        return r.json()["token"]

    @property
    def owner_token(self):
        # Each login replaces the account's previous token (one session per account), so log in per use.
        return self.login(OWNER[0], OWNER[2])

    def ok(self, r, status=200):
        self.assertEqual(r.status, status, f"{r.body[:300]!r} {r.stderr[-1500:]}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.body

    def unsubscribe_token(self, email):
        import hashlib, hmac
        return hmac.new(self.w.values["SECRET_KEY"].encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:32]

    def confirm_link_token(self, email):
        """The token from the last confirmation email sent to this address (as the person would click it)."""
        import re
        sent = [m for req in self.resend.requests for m in req["body"] if m["to"] == [email] and "verify-email?token=" in m["html"]]
        return urllib.parse.unquote(re.search(r"verify-email\?token=([^\"&<]+)", sent[-1]["html"]).group(1))

    def customer(self, name):
        """Signs up and confirms the email, like a real customer. Returns (session token, email)."""
        email = f"{name}@example.com"
        r = self.ok(self.w.cgi("POST", "/api/signup", {"name": name, "email": email, "password": "customer-pass-1"}))
        self.assertTrue(r["verify"])
        return self.ok(self.w.cgi("POST", "/api/verify-email", {"token": self.confirm_link_token(email)}))["token"], email

    # ---------- installation ----------

    def test_install_files_and_permissions(self):
        for name in ("rotaidem.py", "rotaidem-check.py"):
            path = os.path.join(self.w.public, "cgi-bin", name)
            with open(path, "rb") as fh:
                raw = fh.read()
            self.assertTrue(raw.startswith(b"#!/usr/bin/python3\n"), "STRATO's interpreter path, LF line ends")
            self.assertNotIn(b"\r", raw)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o755)
        cfg = os.path.join(self.w.data, "config.env")
        self.assertEqual(os.stat(cfg).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.w.data).st_mode & 0o777, 0o700)
        with open(cfg) as fh:
            self.assertNotIn("ADMIN_PASSWORD", fh.read(), "the owner password must only exist hashed in the database")
        db_file = os.path.join(self.w.data, "rotaidem.db")
        if os.path.exists(db_file):
            self.assertEqual(os.stat(db_file).st_mode & 0o777, 0o600)
        with open(os.path.join(self.w.public, ".htaccess")) as fh:
            ht = fh.read()
        self.assertIn("E=HTTP_AUTHORIZATION", ht)
        self.assertIn("cgi-bin/rotaidem.py/$1", ht)
        for risky in ("\nOptions", "\nDirectoryIndex"):  # hosts that forbid these answer 500 for the whole site
            self.assertNotIn(risky, ht)
        for folder in (self.w.app, self.w.data):
            with open(os.path.join(folder, ".htaccess")) as fh:
                self.assertIn("Require all denied", fh.read())
            self.assertFalse(folder.startswith(self.w.public + os.sep), "program and data stay outside the website folder")
        for name in ("index.html", "login.html", "admin.html", "account.html", "site.css", "auth.js", "addons/index.html", "img/hero.jpg"):
            self.assertEqual(os.stat(os.path.join(self.w.public, name)).st_mode & 0o777, 0o644, name)
        for name in ("_routes.json", "server.py", "config.env", "rotaidem.db"):
            self.assertFalse(os.path.exists(os.path.join(self.w.public, name)), f"{name} must not be public")
        with open(os.path.join(self.w.public, "rotaidem-deploy.txt")) as fh:
            self.assertRegex(fh.read(), r"^rotaidem [0-9a-f]{16}\n$")

    def test_permissions_command_never_opens_to_everyone(self):
        cfg = os.path.join(self.w.data, "config.env")
        self.w.setup_cmd("permissions", "group")
        self.assertEqual(os.stat(cfg).st_mode & 0o777, 0o640)
        self.assertEqual(os.stat(self.w.data).st_mode & 0o007, 0)
        self.w.setup_cmd("permissions")
        self.assertEqual(os.stat(cfg).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.w.data).st_mode & 0o777, 0o700)
        for name in os.listdir(self.w.data):
            if name != ".htaccess":
                self.assertEqual(os.stat(os.path.join(self.w.data, name)).st_mode & 0o007, 0, name)

    def test_runs_with_only_the_vendored_packages(self):
        r = self.w.cgi("GET", "/api/site", flags=("-S",))  # -S: no globally installed packages at all
        self.assertIn("announcement", self.ok(r))

    def test_check_page(self):
        r = self.w.cgi("GET", "", script="rotaidem-check.py", query="key=checkkey123")
        html = self.ok(r).decode()
        self.assertIn("Database can be read and written", html)
        server_rows = html.split("From your browser")[0]
        self.assertNotIn('class="bad"', server_rows, server_rows)
        self.assertIn("your own account", server_rows)
        self.assertEqual(self.w.cgi("GET", "", script="rotaidem-check.py", query="key=wrong").status, 404)

    # ---------- routing and login ----------

    def test_routing(self):
        self.assertEqual(self.ok(self.w.cgi("GET", "/health")), {"ok": True, "database": "ok"})
        self.assertEqual(self.w.cgi("GET", "/api/does-not-exist").status, 404)
        self.assertEqual(self.w.cgi("GET", "/api/does-not-exist").json(), {"error": "Not found"})
        self.assertEqual(self.w.cgi("GET", "/media/9999.jpg").status, 404)
        self.assertEqual(self.w.cgi("GET", "/api/login").status, 404)  # only POST is routed; JSON 404
        self.assertEqual(self.w.cgi("GET", "/api/site").headers.get("cache-control"), "private, no-store")

    def test_login_and_tokens(self):
        self.assertEqual(self.w.cgi("POST", "/api/login", {"email": OWNER[0], "password": "wrong"}).status, 401)
        token = self.login(OWNER[1], OWNER[2])  # e-mail works as well as the user name
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/me", token=token))["user"]["role"], "Owner")
        # Fallback header for servers that drop Authorization before CGI scripts
        me = self.w.cgi("GET", "/api/me", headers={"X-Rotaidem-Token": token})
        self.assertEqual(self.ok(me)["user"]["name"], OWNER[0])
        self.ok(self.w.cgi("POST", "/api/logout", token=token))
        self.assertEqual(self.w.cgi("GET", "/api/me", token=token).status, 401)

    def test_customer_cannot_use_admin(self):
        token, email = self.customer("pilot1")
        self.assertEqual(self.w.cgi("POST", "/api/signup", {"name": "x", "email": email, "password": "customer-pass-1"}).status, 400)
        for path in ("/api/admin/overview", "/api/admin/customers", "/api/admin/backup", "/api/admin/media"):
            self.assertEqual(self.w.cgi("GET", path, token=token).status, 403, path)
        self.assertEqual(self.w.cgi("POST", "/api/save-product", {"name": "Hack"}, token=token).status, 403)

    # ---------- catalogue, orders, licences ----------

    def test_products_survive_updates_and_migrations(self):
        t = self.owner_token
        self.ok(self.w.cgi("POST", "/api/save-product", {"name": "HeavyPro", "price": "€29.99", "status": "published"}, token=t))
        self.ok(self.w.cgi("POST", "/api/save-product", {"id": "heavypro", "name": "HeavyPro", "tagline": "Ändrad åäö"}, token=t))
        self.w.setup_cmd("update")  # what you run after uploading a new version
        # A future schema version: the full CREATE/ALTER pass runs again and must not touch data.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import setup; s = setup.connect(sys.argv[2]); d = s.DB(); "
                "d.run(\"UPDATE meta SET value = 'old' WHERE key = 'schema_version'\"); d.close()")
        self.w.run_python(["-c", code, os.path.join(self.w.app, "strato"), self.w.data])
        products = self.ok(self.w.cgi("GET", "/api/products"))["products"]
        heavypro = [p for p in products if p["id"] == "heavypro"]
        self.assertEqual(len(heavypro), 1)
        self.assertEqual(heavypro[0]["tagline"], "Ändrad åäö")

    def test_orders(self):
        t = self.owner_token
        ctoken, email = self.customer("buyer1")
        uid = self.ok(self.w.cgi("GET", "/api/me", token=ctoken))["user"]["id"]
        r = self.ok(self.w.cgi("POST", "/api/admin/orders", {"customer_id": uid, "product_id": "heavypro", "amount": "29.99",
                                                            "create_license": True}, token=t))
        self.assertRegex(r["license_key"], r"^HPRO-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        orders = self.ok(self.w.cgi("GET", "/api/account/orders", token=ctoken))["orders"]
        self.assertEqual(float(orders[0]["amount"]), 29.99)
        self.assertRegex(orders[0]["created_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
        keys = [x["license_key"] for x in self.ok(self.w.cgi("GET", "/api/account/licenses", token=ctoken))["licenses"]]
        self.assertIn(r["license_key"], keys)
        self.assertGreaterEqual(self.ok(self.w.cgi("GET", "/api/admin/overview", token=t))["revenue"], 29.99)
        self.assertEqual(self.w.cgi("POST", "/api/admin/orders", {"customer_id": uid, "amount": "abc"}, token=t).status, 400)

    def test_licences_as_the_app_uses_them(self):
        t = self.owner_token
        key = self.ok(self.w.cgi("POST", "/api/admin/licenses/generate", {"count": 1, "max_activations": 1}, token=t))["keys"][0]
        req = lambda action, machine: self.w.cgi("POST", f"/api/licenses/{action}", {"key": key, "machine_id": machine,
                                                                                      "machine_name": "PC", "product": "heavypro"})
        self.assertTrue(self.ok(req("activate", "PC-1"))["valid"])
        self.assertEqual(req("activate", "PC-2").json()["reason"], "activation_limit")
        self.assertTrue(self.ok(req("validate", "PC-1"))["valid"])
        self.assertEqual(req("validate", "PC-2").json()["reason"], "not_activated")
        self.ok(req("deactivate", "PC-1"))
        self.assertTrue(self.ok(req("activate", "PC-2"))["valid"])
        lic = [x for x in self.ok(self.w.cgi("GET", "/api/admin/licenses", token=t))["licenses"] if x["license_key"] == key][0]
        self.ok(self.w.cgi("POST", f"/api/admin/licenses/{lic['id']}", {"status": "revoked"}, token=t))
        self.assertEqual(req("validate", "PC-2").json()["reason"], "revoked")
        bad = self.w.cgi("POST", "/api/licenses/validate", {"key": "HPRO-AAAA-BBBB-CCCC", "machine_id": "x"})
        self.assertEqual(bad.json()["reason"], "invalid_format")
        # The licence generator program registers keys with its API key
        imported = self.ok(self.w.cgi("POST", "/api/admin/licenses/generate", {"count": 1}, token=t))["keys"][0]
        # A key that already exists is skipped, not duplicated
        r = self.w.cgi("POST", "/api/licenses/import", {"licenses": [{"key": imported}]}, headers={"X-License-Api-Key": "generator-key-0123456789"})
        self.assertEqual(self.ok(r)["skipped"], [imported])
        r = self.w.cgi("POST", "/api/licenses/import", {"licenses": [{"key": imported}]}, headers={"X-License-Api-Key": "wrong"})
        self.assertEqual(r.status, 403)

    def test_signed_licence_answers(self):
        """HeavyPro trusts only answers signed by this server: a fake server cannot say "valid"."""
        import hashlib
        import hmac
        t = self.owner_token
        key = self.ok(self.w.cgi("POST", "/api/admin/licenses/generate", {"count": 1, "max_activations": 2}, token=t))["keys"][0]
        jwk = self.ok(self.w.cgi("GET", "/api/licenses/jwks.json"))["keys"][0]
        self.assertEqual((jwk["kty"], jwk["alg"], jwk["e"]), ("RSA", "RS256", "AQAB"))
        self.assertIn(b"BEGIN PUBLIC KEY", self.w.cgi("GET", "/api/licenses/public-key").body)
        b64 = lambda text: base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))  # noqa: E731
        n, e = int.from_bytes(b64(jwk["n"]), "big"), int.from_bytes(b64(jwk["e"]), "big")

        def verified(token):
            head, body, sig = token.split(".")
            size = (n.bit_length() + 7) // 8
            digest = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(f"{head}.{body}".encode()).digest()
            expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
            ok = hmac.compare_digest(pow(int.from_bytes(b64(sig), "big"), e, n).to_bytes(size, "big"), expected)
            return json.loads(b64(body)) if ok else None

        r = self.ok(self.w.cgi("POST", "/api/licenses/activate", {"key": key, "machine_id": "pc-hash", "machine_name": "PC",
                                                                  "product": "heavypro", "nonce": "abc123"}))
        claims = verified(r["token"])
        self.assertIsNotNone(claims, "the token verifies with the published key")
        self.assertEqual((claims["key"], claims["mid"], claims["nonce"], claims["max_activations"]), (key, "pc-hash", "abc123", 2))
        self.assertEqual(claims["exp"] - claims["iat"], 14 * 86400)
        self.assertTrue(r["lease_until"].endswith("Z"))
        head, body, sig = r["token"].split(".")
        edited = json.loads(b64(body))
        edited["exp"] += 86400 * 365
        forged = head + "." + base64.urlsafe_b64encode(json.dumps(edited).encode()).rstrip(b"=").decode() + "." + sig
        self.assertIsNone(verified(forged), "an edited token does not verify")
        again = self.ok(self.w.cgi("POST", "/api/licenses/validate", {"key": key, "machine_id": "pc-hash"}))
        self.assertNotIn("nonce", verified(again["token"]))
        self.assertEqual(verified(again["token"])["iat"] >= claims["iat"], True)
        # Refusals carry no token; guessing keys from one address is cut off after 30 tries an hour.
        self.assertNotIn("token", self.w.cgi("POST", "/api/licenses/validate", {"key": key, "machine_id": "other"}).json())
        guesses = [self.w.cgi("POST", "/api/licenses/activate", {"key": "HPRO-AAAA-BBBB-CCCC", "machine_id": "x"},
                              remote_addr="198.51.100.77").status for _ in range(31)]
        self.assertEqual((guesses[0], guesses[-1]), (400, 429))
        self.assertTrue(self.ok(self.w.cgi("POST", "/api/licenses/validate", {"key": key, "machine_id": "pc-hash"}))["valid"],
                        "other addresses are not affected")
        out = self.w.setup_cmd("signing-key")
        self.assertIn(jwk["kid"], out)
        self.assertNotIn('"d"', out)

    def test_shared_profiles_by_name(self):
        t = self.owner_token
        alice, bob = self.ok(self.w.cgi("POST", "/api/admin/licenses/generate", {"count": 2}, token=t))["keys"]
        for key, pc in ((alice, "PC-A"), (bob, "PC-B")):
            self.ok(self.w.cgi("POST", "/api/licenses/activate", {"key": key, "machine_id": pc, "product": "heavypro"}))
        call = lambda action, key, pc, **extra: self.w.cgi("POST", f"/api/profiles/{action}",
                                                            {"key": key, "machine_id": pc, "product": "heavypro", **extra})
        profile = {"Inertia": 81, "PitchDamping": 64, "InputCurve": "Expo"}
        self.assertFalse(self.ok(call("publish", alice, "PC-A", name="777  Heavy", profile=profile))["updated"])
        # Anyone with the program gets it by name, case and spacing ignored
        got = self.ok(call("get", bob, "PC-B", name="777 heavy"))
        self.assertEqual(got["name"], "777 Heavy")
        self.assertEqual(got["profile"], profile)
        # The name belongs to whoever shared it first
        self.assertEqual(call("publish", bob, "PC-B", name="777 heavy", profile={"Inertia": 1}).status, 409)
        self.assertTrue(self.ok(call("publish", alice, "PC-A", name="777 Heavy", profile={"Inertia": 90}))["updated"])
        self.assertEqual(self.ok(call("get", bob, "PC-B", name="777 Heavy"))["profile"], {"Inertia": 90})
        self.assertEqual(call("delete", bob, "PC-B", name="777 Heavy").status, 403)
        # A computer that is not activated cannot use it, nor can a made-up name
        self.assertEqual(call("get", bob, "PC-X", name="777 Heavy").json()["reason"], "not_activated")
        self.assertEqual(call("get", bob, "PC-B", name="nope").status, 404)
        self.assertEqual(call("publish", alice, "PC-A", name="x", profile=profile).json()["reason"], "invalid_name")
        self.assertEqual(call("publish", alice, "PC-A", name="big", profile={"x": "y" * 5000}).json()["reason"], "too_large")
        call("publish", bob, "PC-B", name="Cessna light", profile=profile)
        listed = self.ok(call("list", bob, "PC-B"))["profiles"]
        self.assertEqual([p["name"] for p in listed], ["777 Heavy", "Cessna light"])
        self.assertEqual([p["name"] for p in self.ok(call("list", alice, "PC-A", search="cess"))["profiles"]], ["Cessna light"])
        self.ok(call("delete", alice, "PC-A", name="777 Heavy"))
        self.assertEqual(call("get", bob, "PC-B", name="777 Heavy").status, 404)

    # ---------- uploads ----------

    def test_image_upload_serve_and_delete(self):
        t = self.owner_token
        with open(IMAGE, "rb") as fh:
            raw = fh.read()
        r = self.ok(self.w.cgi("POST", "/api/admin/media", {"filename": "natt.jpg", "mime": "image/jpeg", "width": 2400, "height": 1350,
                                                           "data": "data:image/jpeg;base64," + base64.b64encode(raw).decode()}, token=t))
        served = self.w.cgi("GET", r["url"])
        self.assertEqual(served.status, 200)
        self.assertEqual(served.headers["content-type"], "image/jpeg")
        self.assertEqual(served.body, raw)
        self.assertEqual(self.w.cgi("GET", r["url"].replace(".jpg", ".png")).status, 404)
        site = self.ok(self.w.cgi("POST", "/api/admin/settings", {"images": {"hero": r["url"]}}, token=t))
        self.assertEqual(site["images"]["hero"], r["url"])
        self.assertEqual(self.w.cgi("POST", "/api/admin/media", {"mime": "text/html", "data": "PGI+"}, token=t).status, 400)
        big = base64.b64encode(os.urandom(5 * 1024 * 1024 + 10)).decode()
        self.assertEqual(self.w.cgi("POST", "/api/admin/media", {"mime": "image/png", "data": big}, token=t).status, 400)
        self.ok(self.w.cgi("POST", "/api/admin/settings", {"images": {}}, token=t))
        self.ok(self.w.cgi("DELETE", "/api/admin/media/" + r["url"].split("/")[2].split(".")[0], token=t))
        self.assertEqual(self.w.cgi("GET", r["url"]).status, 404)

    def test_pictures_and_settings_as_plain_files(self):
        """Apache sends uploaded pictures and site-settings.js as plain files, without starting the program."""
        t = self.owner_token
        media = os.path.join(self.w.public, "media")
        with open(os.path.join(media, ".htaccess")) as fh:
            self.assertIn("immutable", fh.read())
        with open(os.path.join(self.w.public, ".htaccess")) as fh:
            rules = fh.read()
        self.assertIn("RewriteCond %{REQUEST_FILENAME} !-f\nRewriteRule ^(media/.+|site-settings\\.js)$", rules)
        with open(os.path.join(self.w.public, "cgi-bin", "rotaidem.py")) as fh:
            self.assertIn(f'os.environ["ROTAIDEM_PUBLIC_DIR"] = {self.w.public!r}', fh.read())
        self.assertTrue(os.path.isfile(os.path.join(self.w.public, "site-settings.js")), "deploy writes it")
        with open(IMAGE, "rb") as fh:
            raw = fh.read()
        r = self.ok(self.w.cgi("POST", "/api/admin/media", {"filename": "plain.jpg", "mime": "image/jpeg",
                                                           "data": base64.b64encode(raw).decode()}, token=t))
        copy = os.path.join(self.w.public, r["url"].lstrip("/"))
        with open(copy, "rb") as fh:
            self.assertEqual(fh.read(), raw)
        self.assertEqual(os.stat(copy).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(media).st_mode & 0o777, 0o755)
        os.remove(copy)
        self.assertEqual(self.w.cgi("GET", r["url"]).body, raw)  # the program answers once ...
        self.assertTrue(os.path.isfile(copy))                     # ... and writes the copy again
        self.ok(self.w.cgi("POST", "/api/admin/settings", {"images": {"hero": r["url"]}, "texts": {"index.products": "Our add-ons"}}, token=t))
        with open(os.path.join(self.w.public, "site-settings.js")) as fh:
            js = fh.read()
        css = f'[data-image="hero"]:not(img){{background-image:url("{r["url"]}")!important}}'
        self.assertIn(json.dumps(css), js)  # inside the script as a JSON string
        self.assertIn('"index.products": "Our add-ons"', js)
        self.assertNotIn("</", js)
        served = self.w.cgi("GET", "/site-settings.js")
        self.assertEqual((served.status, served.headers["content-type"].split(";")[0]), (200, "application/javascript"))
        self.assertTrue(served.body.decode().startswith("window.RSG_SITE="))
        self.ok(self.w.cgi("POST", "/api/admin/settings", {"images": {}, "texts": {"index.products": None}}, token=t))
        with open(os.path.join(self.w.public, "site-settings.js")) as fh:
            self.assertNotIn("data-image", fh.read())
        self.ok(self.w.cgi("DELETE", "/api/admin/media/" + r["url"].split("/")[2].split(".")[0], token=t))
        self.assertFalse(os.path.exists(copy))
        for page in ("index.html", "about.html", "addons/view.html", "admin.html"):
            with open(os.path.join(self.w.public, page)) as fh:
                self.assertIn('<script src="/site-settings.js"></script>', fh.read(), page)

    def test_link_previews_written_into_the_pages(self):
        """Discord, Messenger and others never run scripts: the preview tags are in the page files."""
        t = self.owner_token
        page = lambda name: open(os.path.join(self.w.public, name), encoding="utf-8").read()  # noqa: E731
        home = page("index.html")
        self.assertEqual(home.count("<!-- share -->"), 1, "deploy writes them")
        self.assertIn('<meta property="og:image" content="https://rotaidem.test/img/hero.jpg">', home)
        self.assertIn('<meta property="og:url" content="https://rotaidem.test/">', home)
        self.assertIn('<meta property="og:title" content="Rotaidem | HeavyPro for Microsoft Flight Simulator 2024">', home, "the page's own title")
        self.assertIn('<meta property="og:image" content="https://rotaidem.test/img/clouds.jpg">', page("heavypro.html"), "each page its own picture")
        self.assertNotIn("<!-- share -->", page("admin.html"))
        bad = {"share": {"pages": {"/heavypro": {"image": "javascript:alert(1)"}}}}
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", bad, token=t).status, 400)
        share = {"default": {"image": "/media/7.png"},
                 "pages": {"/heavypro": {"image": "https://cdn.example.com/heavy.jpg", "title": 'HeavyPro "launch" & more', "description": "Feel the weight"},
                           "/not-a-page": {"title": "dropped"}, "/about": {"title": "  "}}}
        saved = self.ok(self.w.cgi("POST", "/api/admin/settings", {"share": share}, token=t))["share"]
        self.assertEqual(saved, {"default": {"image": "/media/7.png"}, "pages": {"/heavypro": share["pages"]["/heavypro"]}})
        heavy = page("heavypro.html")
        self.assertEqual(heavy.count("<!-- share -->"), 1, "replaced, not added again")
        self.assertIn('<meta property="og:image" content="https://cdn.example.com/heavy.jpg">', heavy)
        self.assertIn('<meta property="og:title" content="HeavyPro &quot;launch&quot; &amp; more">', heavy)
        self.assertIn('<meta property="og:description" content="Feel the weight">', heavy)
        self.assertIn('<meta name="twitter:image" content="https://cdn.example.com/heavy.jpg">', heavy)
        self.assertIn('<meta property="og:image" content="https://rotaidem.test/media/7.png">', page("index.html"), "the default picture for the rest")
        self.assertIn('<meta property="og:image" content="https://rotaidem.test/media/7.png">', page("addons/view.html"))
        # pages the program itself sends carry the same tags
        served = self.w.cgi("GET", "/heavypro").body.decode()
        self.assertEqual(served.count("<!-- share -->"), 1)
        self.assertIn("https://cdn.example.com/heavy.jpg", served)
        self.ok(self.w.cgi("POST", "/api/admin/settings", {"share": {}}, token=t))
        self.assertIn('<meta property="og:image" content="https://rotaidem.test/img/clouds.jpg">', page("heavypro.html"), "back to the page's own picture")

    # ---------- e-mail ----------

    def test_email_through_resend(self):
        t = self.owner_token
        self.resend.requests.clear()
        r = self.ok(self.w.cgi("POST", "/api/admin/licenses/generate", {"count": 1, "email": "kund@example.com"}, token=t))
        self.assertEqual(r["emailed"], ["kund@example.com"])
        sent = self.resend.requests[-1]
        self.assertEqual(sent["path"], "/emails/batch")
        self.assertEqual(sent["auth"], "Bearer re_test")
        self.assertEqual(sent["body"][0]["to"], ["kund@example.com"])
        self.assertIn(r["keys"][0], sent["body"][0]["html"])
        r = self.ok(self.w.cgi("POST", "/api/admin/broadcast", {"subject": "Nyhet åäö", "message": "Hej!", "test_email": "test@example.com"}, token=t))
        self.assertEqual(r["sent"], 1)
        self.resend.fail_with = (403, {"message": "The rotaidem.com domain is not verified"})
        try:
            r = self.w.cgi("POST", "/api/admin/broadcast", {"subject": "x", "message": "y", "test_email": "test@example.com"}, token=t)
        finally:
            self.resend.fail_with = None
        self.assertEqual(r.status, 502)
        self.assertIn("not verified", r.json()["error"])

    def test_product_email_with_several_pictures(self):
        t = self.owner_token
        self.resend.requests.clear()
        pics = ["/img/hero.jpg", "https://cdn.example.com/cockpit.jpg", "/img/clouds.jpg"]
        self.ok(self.w.cgi("POST", "/api/admin/broadcast", {"subject": "Update", "message": "Look", "images": pics,
                                                            "test_email": "test@example.com"}, token=t))
        html = self.resend.requests[-1]["body"][0]["html"]
        found = [html.find(u) for u in ("https://rotaidem.test/img/hero.jpg", "https://cdn.example.com/cockpit.jpg", "https://rotaidem.test/img/clouds.jpg")]
        self.assertTrue(all(i > 0 for i in found) and found == sorted(found), "all pictures, full addresses, in the chosen order")
        r = self.w.cgi("POST", "/api/admin/broadcast", {"subject": "x", "message": "y", "images": ["javascript:alert(1)"], "test_email": "test@example.com"}, token=t)
        self.assertEqual(r.status, 400)
        r = self.w.cgi("POST", "/api/admin/broadcast", {"subject": "x", "message": "y", "images": ["/img/hero.jpg"] * 13, "test_email": "test@example.com"}, token=t)
        self.assertEqual(r.status, 400)

    def test_signup_needs_email_confirmation(self):
        self.resend.requests.clear()
        r = self.ok(self.w.cgi("POST", "/api/signup", {"name": "Welcome Wendy", "email": "Wendy@Example.com", "password": "wendy-pass-1"}))
        self.assertEqual((r["verify"], r["email"]), (True, "wendy@example.com"))
        self.assertNotIn("token", r, "no session before the email is confirmed")
        sent = self.resend.requests[-1]["body"][0]
        self.assertEqual((sent["to"], sent["subject"]), (["wendy@example.com"], "Confirm your email for Rotaidem"))
        self.assertIn("https://rotaidem.test/verify-email?token=", sent["html"])
        self.assertNotIn("wendy-pass-1", sent["html"])  # never the password
        # someone else's address: no way in without the email
        r = self.w.cgi("POST", "/api/login", {"email": "wendy@example.com", "password": "wendy-pass-1"})
        self.assertEqual((r.status, r.json().get("unverified")), (403, True))
        self.assertEqual(self.w.cgi("POST", "/api/login", {"email": "wendy@example.com", "password": "wrong-pass"}).status, 401, "a wrong password reveals nothing")
        self.assertEqual(self.w.cgi("POST", "/api/verify-email", {"token": "1.9999999999.forged"}).status, 400)
        token = self.confirm_link_token("wendy@example.com")
        r = self.ok(self.w.cgi("POST", "/api/verify-email", {"token": token}))
        self.assertTrue(r["verified"])
        self.ok(self.w.cgi("GET", "/api/me", token=r["token"]))
        self.ok(self.w.cgi("POST", "/api/login", {"email": "wendy@example.com", "password": "wendy-pass-1"}))
        # a new link can be asked for; the answer is the same whether the account exists or not
        self.ok(self.w.cgi("POST", "/api/signup", {"name": "Slow Sam", "email": "sam@example.com", "password": "sam-pass-1"}))
        self.resend.requests.clear()
        a = self.ok(self.w.cgi("POST", "/api/verify-email/resend", {"email": "sam@example.com"}))
        b = self.ok(self.w.cgi("POST", "/api/verify-email/resend", {"email": "nobody@example.com"}))
        self.assertEqual(a, b)
        self.assertEqual(len(self.resend.requests), 1)
        self.assertEqual(self.w.cgi("POST", "/api/signup", {"name": "X", "email": "not-an-email", "password": "secret-1"}).status, 400)
        # when the email cannot be sent, no half-made account blocks the address
        self.resend.fail_with = (500, {"message": "down"})
        try:
            r = self.w.cgi("POST", "/api/signup", {"name": "Late Larry", "email": "larry@example.com", "password": "larry-pass-1"})
        finally:
            self.resend.fail_with = None
        self.assertEqual(r.status, 502)
        self.ok(self.w.cgi("POST", "/api/signup", {"name": "Late Larry", "email": "larry@example.com", "password": "larry-pass-1"}))

    def test_staff_can_confirm_and_reset_confirms(self):
        t = self.owner_token
        self.ok(self.w.cgi("POST", "/api/signup", {"name": "Lost Mail", "email": "lost@example.com", "password": "lost-pass-1"}))
        user = next(u for u in self.ok(self.w.cgi("GET", "/api/admin/customers", token=t))["customers"] if u["email"] == "lost@example.com")
        self.assertEqual(int(user["email_verified"]), 0)
        self.ok(self.w.cgi("POST", f"/api/admin/users/{user['id']}/verify", {}, token=t))
        self.ok(self.w.cgi("POST", "/api/login", {"email": "lost@example.com", "password": "lost-pass-1"}))
        # a password reset by email proves the address too
        self.ok(self.w.cgi("POST", "/api/signup", {"name": "Reset Rita", "email": "rita@example.com", "password": "rita-pass-1"}))
        self.resend.requests.clear()
        self.ok(self.w.cgi("POST", "/api/password/forgot", {"email": "rita@example.com"}, remote_addr="198.51.100.77"))
        import re
        link = re.search(r"reset-password\?token=([^\"&<]+)", self.resend.requests[-1]["body"][0]["html"]).group(1)
        self.ok(self.w.cgi("POST", "/api/password/reset", {"token": urllib.parse.unquote(link), "password": "rita-new-1"}))
        self.ok(self.w.cgi("POST", "/api/login", {"email": "rita@example.com", "password": "rita-new-1"}))
        customer, _ = self.customer("notstaff")
        self.assertEqual(self.w.cgi("POST", f"/api/admin/users/{user['id']}/verify", {}, token=customer).status, 403)

    def test_forgot_password(self):
        import re
        import urllib.parse
        token, email = self.customer("forgetful")
        self.resend.requests.clear()
        # Unknown addresses get the same answer and no email.
        same = self.ok(self.w.cgi("POST", "/api/password/forgot", {"email": "nobody@example.com"}))
        self.assertEqual(self.resend.requests, [])
        self.assertEqual(self.ok(self.w.cgi("POST", "/api/password/forgot", {"email": email.upper()})), same)
        sent = self.resend.requests[-1]["body"][0]
        self.assertEqual((sent["to"], sent["subject"]), ([email], "Reset your Rotaidem password"))
        link = re.search(r'href="([^"]*/reset-password\?token=[^"]+)"', sent["html"]).group(1)
        reset = urllib.parse.unquote(link.split("token=", 1)[1])
        self.assertEqual(self.ok(self.w.cgi("POST", "/api/password/check", {"token": reset}))["email"], "f••••••@example.com")
        for bad in ("", "nonsense", reset[:-1] + ("0" if reset[-1] != "0" else "1"), "999." + reset.split(".", 1)[1]):
            self.assertEqual(self.w.cgi("POST", "/api/password/check", {"token": bad}).status, 400, bad)
        self.assertEqual(self.w.cgi("POST", "/api/password/reset", {"token": reset, "password": "short"}).status, 400)
        session = self.ok(self.w.cgi("POST", "/api/password/reset", {"token": reset, "password": "brand-new-pass"}))
        self.assertEqual((session["email"], session["is_admin"]), (email, False))
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/me", token=session["token"]))["user"]["email"], email)
        self.assertEqual(self.w.cgi("GET", "/api/me", token=token).status, 401)  # other sessions are signed out
        self.assertEqual(self.w.cgi("POST", "/api/password/reset", {"token": reset, "password": "another-pass"}).status, 400)  # used once
        self.assertEqual(self.w.cgi("POST", "/api/login", {"email": email, "password": "customer-pass-1"}).status, 401)
        self.login(email, "brand-new-pass")
        # At most 3 emails per account per hour; the answer stays the same.
        self.resend.requests.clear()
        for _ in range(4):
            self.assertEqual(self.ok(self.w.cgi("POST", "/api/password/forgot", {"email": "forgetful"})), same)  # username works too
        self.assertEqual(len(self.resend.requests), 2)  # 1 sent earlier this hour + 2 now = 3
        # At most 10 requests per address per hour.
        statuses = [self.w.cgi("POST", "/api/password/forgot", {"email": "x@example.com"}).status for _ in range(5)]
        self.assertEqual(statuses[-1], 429)
        logs = self.ok(self.w.cgi("GET", "/api/admin/audit", token=self.owner_token))["logs"]
        self.assertIn("password_reset", [entry["action"] for entry in logs])

    def test_news_drafts_and_dates(self):
        t = self.owner_token
        self.ok(self.w.cgi("POST", "/api/newsletter", {"title": "Secret draft", "content": "Soon", "status": "draft", "date": "2026-12-24"}, token=t))
        self.ok(self.w.cgi("POST", "/api/newsletter", {"title": "Out now", "content": "Live"}, token=t))
        public = [p["title"] for p in self.ok(self.w.cgi("GET", "/api/newsletter"))["posts"]]
        self.assertIn("Out now", public)
        self.assertNotIn("Secret draft", public)
        admin = {p["title"]: p for p in self.ok(self.w.cgi("GET", "/api/admin/posts", token=t))["posts"]}
        self.assertEqual((admin["Secret draft"]["status"], admin["Secret draft"]["date"]), ("draft", "December 24, 2026"))
        self.ok(self.w.cgi("POST", "/api/newsletter", {"id": admin["Secret draft"]["id"], "title": "Secret draft", "content": "Now", "status": "published"}, token=t))
        post = next(p for p in self.ok(self.w.cgi("GET", "/api/newsletter"))["posts"] if p["title"] == "Secret draft")
        self.assertEqual(post["date"], "December 24, 2026", "publishing keeps the chosen date")
        customer, _ = self.customer("drafty")
        self.assertEqual(self.w.cgi("GET", "/api/admin/posts", token=customer).status, 403)

    def test_messages_can_be_marked_handled(self):
        t = self.owner_token
        self.ok(self.w.cgi("POST", "/api/contact", {"name": "Pilot", "email": "pilot@example.com", "message": "Help with my stick"}))
        msg = next(m for m in self.ok(self.w.cgi("GET", "/api/admin/messages", token=t))["messages"] if m["email"] == "pilot@example.com")
        before = self.ok(self.w.cgi("GET", "/api/admin/overview", token=t))["open_messages"]
        self.ok(self.w.cgi("POST", f"/api/admin/messages/{msg['id']}", {"handled": True}, token=t))
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/admin/overview", token=t))["open_messages"], before - 1)
        msg = next(m for m in self.ok(self.w.cgi("GET", "/api/admin/messages", token=t))["messages"] if m["id"] == msg["id"])
        self.assertEqual(int(msg["handled"]), 1)
        self.ok(self.w.cgi("POST", f"/api/admin/messages/{msg['id']}", {"handled": False}, token=t))
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/admin/overview", token=t))["open_messages"], before)

    def test_subscribers_added_in_bulk(self):
        t = self.owner_token
        self.ok(self.w.cgi("POST", "/api/subscribe", {"email": "gone@example.com"}))
        sub = next(s for s in self.ok(self.w.cgi("GET", "/api/admin/subscribers", token=t))["subscribers"] if s["email"] == "gone@example.com")
        self.ok(self.w.cgi("POST", "/api/unsubscribe", {"email": "gone@example.com", "token": self.unsubscribe_token("gone@example.com")}))
        r = self.ok(self.w.cgi("POST", "/api/admin/subscribers", {"emails": "one@example.com, Two@Example.com\nnot-an-email; one@example.com gone@example.com"}, token=t))
        self.assertEqual((r["added"], r["skipped"], r["invalid"]), (2, 1, ["not-an-email"]))
        subs = {s["email"]: s for s in self.ok(self.w.cgi("GET", "/api/admin/subscribers", token=t))["subscribers"]}
        self.assertIn("two@example.com", subs)
        self.assertEqual(int(subs["gone@example.com"]["unsubscribed"]), 1, "someone who unsubscribed stays unsubscribed")
        self.assertEqual(sub["email"], "gone@example.com")
        self.assertEqual(self.w.cgi("POST", "/api/admin/subscribers", {"emails": "  "}, token=t).status, 400)

    def test_staff_can_send_a_reset_link(self):
        t = self.owner_token
        _, email = self.customer("needsreset")
        user = next(u for u in self.ok(self.w.cgi("GET", "/api/admin/customers", token=t))["customers"] if u["email"] == email)
        self.resend.requests.clear()
        r = self.ok(self.w.cgi("POST", f"/api/admin/users/{user['id']}/reset-link", {}, token=t))
        self.assertEqual(r["sent_to"], email)
        sent = self.resend.requests[-1]["body"][0]
        self.assertEqual((sent["to"], sent["subject"]), ([email], "Reset your Rotaidem password"))
        self.assertIn("/reset-password?token=", sent["html"])
        customer, _ = self.customer("sneaky")
        self.assertEqual(self.w.cgi("POST", f"/api/admin/users/{user['id']}/reset-link", {}, token=customer).status, 403)

    def test_general_site_settings(self):
        t = self.owner_token
        config = {"contact_email": "hello@rotaidem.com", "footer_text": "Made in Sweden",
                  "socials": {"discord": "https://discord.gg/rotaidem", "youtube": "", "myspace": "https://myspace.com/x"},
                  "maintenance": {"enabled": True, "message": "Back at 18:00"}}
        r = self.ok(self.w.cgi("POST", "/api/admin/settings", {"config": config}, token=t))
        self.assertEqual(r["config"], {"contact_email": "hello@rotaidem.com", "footer_text": "Made in Sweden",
                                       "socials": {"discord": "https://discord.gg/rotaidem"},
                                       "maintenance": {"enabled": True, "message": "Back at 18:00"}})
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/site"))["config"]["maintenance"]["enabled"], True)
        js = self.ok(self.w.cgi("GET", "/site-settings.js"))
        self.assertIn("discord.gg/rotaidem", js if isinstance(js, str) else js.decode())
        for bad in ({"contact_email": "nope"}, {"socials": {"discord": "http://discord.gg/x"}}, {"socials": {"x": "javascript:alert(1)"}}):
            self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"config": bad}, token=t).status, 400, bad)
        r = self.ok(self.w.cgi("POST", "/api/admin/settings", {"config": {"maintenance": {"enabled": False}}}, token=t))
        self.assertEqual((r["config"]["maintenance"]["enabled"], r["config"]["contact_email"]), (False, "Eliaz@rotaidem.com"))
        customer, _ = self.customer("configy")
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"config": config}, token=customer).status, 403)

    def test_roadmap_faq_and_short_links(self):
        t = self.owner_token
        roadmap = [{"stage": "Done", "title": "Signed licences", "text": "Safer keys"}, {"stage": "Bogus", "title": "Public beta"}, {"stage": "Next", "title": "  "}]
        r = self.ok(self.w.cgi("POST", "/api/admin/settings", {"roadmap": roadmap, "faq": [{"q": "Is it free?", "a": "No"}, {"q": "Empty answer", "a": ""}]}, token=t))
        self.assertEqual(r["roadmap"], [{"stage": "Done", "title": "Signed licences", "text": "Safer keys"}, {"stage": "Next", "title": "Public beta", "text": ""}])
        self.assertEqual(r["faq"], [{"q": "Is it free?", "a": "No"}])
        site = self.ok(self.w.cgi("GET", "/api/site"))
        self.assertEqual((len(site["roadmap"]), len(site["faq"])), (2, 1))
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"roadmap": [{"title": "x"}] * 41}, token=t).status, 400)
        r = self.ok(self.w.cgi("POST", "/api/admin/settings", {"links": {"discord": "https://discord.gg/rotaidem", "/Beta": "/downloads", "": ""}}, token=t))
        self.assertEqual(r["links"], {"discord": "https://discord.gg/rotaidem", "beta": "/downloads"})
        for bad in ({"downloads": "https://example.com"}, {"Bad Slug": "https://example.com"}, {"x": "javascript:alert(1)"}, {"y": "//evil.example"}):
            self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"links": bad}, token=t).status, 400, bad)
        with open(os.path.join(self.w.public, "404.html")) as fh:
            self.assertIn("location.replace(links[slug])", fh.read(), "the not-found page forwards short links")
        customer, _ = self.customer("linky")
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"links": {"free": "https://example.com"}}, token=customer).status, 403)

    def test_banner_dates_sign_out_and_new_accounts(self):
        t = self.owner_token
        r = self.ok(self.w.cgi("POST", "/api/admin/settings", {"announcement": {"enabled": True, "text": "Beta weekend", "from": "2026-11-01", "until": "2026-11-03"}}, token=t))
        self.assertEqual((r["announcement"]["from"], r["announcement"]["until"]), ("2026-11-01", "2026-11-03"))
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"announcement": {"text": "x", "from": "next week"}}, token=t).status, 400)
        before = self.ok(self.w.cgi("GET", "/api/admin/overview", token=t))["new_users_7d"]
        token, email = self.customer("signedin")
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/admin/overview", token=t))["new_users_7d"], before + 1)
        self.ok(self.w.cgi("GET", "/api/me", token=token))
        user = next(u for u in self.ok(self.w.cgi("GET", "/api/admin/customers", token=t))["customers"] if u["email"] == email)
        self.ok(self.w.cgi("POST", f"/api/admin/users/{user['id']}/sign-out", {}, token=t))
        self.assertEqual(self.w.cgi("GET", "/api/me", token=token).status, 401, "the old session no longer works")
        owner = next(u for u in self.ok(self.w.cgi("GET", "/api/admin/customers", token=t))["customers"] if u["role"] == "Owner")
        self.assertEqual(self.w.cgi("POST", f"/api/admin/users/{owner['id']}/sign-out", {}, token=self.owner_token).status, 400)

    def test_look_and_media(self):
        t = self.owner_token
        extras = {"accent": "amber", "logo": "/img/hero.jpg", "gallery": ["/img/clouds.jpg", "https://cdn.example.com/a.png"],
                  "video": "https://youtu.be/dQw4w9WgXcQ?t=5", "launch": {"enabled": True, "at": "2026-12-01T17:00:00.000Z"}, "hide": ["news", "nonsense"]}
        r = self.ok(self.w.cgi("POST", "/api/admin/settings", {"extras": extras}, token=t))
        self.assertEqual(r["extras"], {"accent": "amber", "logo": "/img/hero.jpg", "gallery": ["/img/clouds.jpg", "https://cdn.example.com/a.png"],
                                       "video": "dQw4w9WgXcQ", "launch": {"enabled": True, "at": "2026-12-01T17:00:00.000Z"}, "hide": ["news"]})
        js = self.ok(self.w.cgi("GET", "/site-settings.js"))
        self.assertIn("--accent:#f2a900", js if isinstance(js, str) else js.decode(), "the colour applies before the page is drawn")
        for video in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"):
            r = self.ok(self.w.cgi("POST", "/api/admin/settings", {"extras": {**extras, "video": video}}, token=t))
            self.assertEqual(r["extras"]["video"], "dQw4w9WgXcQ", video)
        for bad in ({"video": "https://evil.example/watch?v=dQw4w9WgXcQ"}, {"logo": "javascript:alert(1)"}, {"gallery": ["ftp://x"]},
                    {"gallery": ["/img/hero.jpg"] * 25}, {"launch": {"enabled": True, "at": ""}}, {"launch": {"enabled": True, "at": "tomorrow"}}):
            self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"extras": {**extras, **bad}}, token=t).status, 400, bad)
        r = self.ok(self.w.cgi("POST", "/api/admin/settings", {"extras": {"accent": "steel"}}, token=t))
        js = self.ok(self.w.cgi("GET", "/site-settings.js"))
        self.assertNotIn("--accent:", js if isinstance(js, str) else js.decode())
        customer, _ = self.customer("looky")
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"extras": extras}, token=customer).status, 403)

    def test_cut_off_upload_says_so(self):
        r = self.w.cgi("POST", "/api/admin/media", b'{"filename": "big.jpg", "mime": "image/jpeg", "data": "/9j/4AAQ', token=self.owner_token)
        self.assertEqual(r.status, 400)
        self.assertIn("cut off", r.json()["error"])

    def test_logged_in_devices(self):
        laptop, email = self.customer("traveller")
        phone = self.ok(self.w.cgi("POST", "/api/login", {"email": email, "password": "customer-pass-1"},
                                   headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"}))["token"]
        self.ok(self.w.cgi("GET", "/api/me", token=laptop))  # logging in on the phone did not log the laptop out
        rows = self.ok(self.w.cgi("GET", "/api/account/sessions", token=phone))["sessions"]
        self.assertEqual(len(rows), 2)
        this = next(r for r in rows if r["current"])
        self.assertEqual((this["device"], this["ip_address"]), ("Safari on iPhone", "203.0.113.5"))
        other = next(r for r in rows if not r["current"])
        self.ok(self.w.cgi("DELETE", f"/api/account/sessions/{other['id']}", token=phone))
        self.assertEqual(self.w.cgi("GET", "/api/me", token=laptop).status, 401, "the removed device is logged out")
        self.ok(self.w.cgi("GET", "/api/me", token=phone))
        # someone else's device cannot be removed
        intruder, _ = self.customer("intruder")
        self.ok(self.w.cgi("DELETE", f"/api/account/sessions/{this['id']}", token=intruder))
        self.ok(self.w.cgi("GET", "/api/me", token=phone))
        # log out everywhere else
        second = self.ok(self.w.cgi("POST", "/api/login", {"email": email, "password": "customer-pass-1"}))["token"]
        self.ok(self.w.cgi("POST", "/api/account/sessions/others", {}, token=second))
        self.assertEqual(self.w.cgi("GET", "/api/me", token=phone).status, 401)
        self.ok(self.w.cgi("GET", "/api/me", token=second))
        # a new password logs out the other devices too
        third = self.ok(self.w.cgi("POST", "/api/login", {"email": email, "password": "customer-pass-1"}))["token"]
        self.ok(self.w.cgi("POST", "/api/account/settings", {"name": "traveller", "current_password": "customer-pass-1", "new_password": "customer-pass-2"}, token=second))
        self.assertEqual(self.w.cgi("GET", "/api/me", token=third).status, 401)
        self.ok(self.w.cgi("GET", "/api/me", token=second))
        # the admin sees them and can end one
        t = self.owner_token
        user = next(u for u in self.ok(self.w.cgi("GET", "/api/admin/customers", token=t))["customers"] if u["email"] == email)
        sessions = self.ok(self.w.cgi("GET", f"/api/admin/users/{user['id']}", token=t))["user"]["sessions"]
        self.assertEqual(len(sessions), 1)
        self.ok(self.w.cgi("DELETE", f"/api/admin/users/{user['id']}/sessions/{sessions[0]['id']}", token=t))
        self.assertEqual(self.w.cgi("GET", "/api/me", token=second).status, 401)

    def test_logins_from_before_sessions_keep_working(self):
        db_file = os.path.join(self.w.data, "rotaidem.db")
        if not os.path.exists(db_file):
            self.skipTest("writes the old token straight into the SQLite file")
        import sqlite3
        _, email = self.customer("oldtimer")
        conn = sqlite3.connect(db_file)
        conn.execute("UPDATE users SET token = 'legacy-token-123' WHERE email = ?", (email,))
        conn.commit()
        conn.close()
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/me", token="legacy-token-123"))["user"]["email"], email)
        conn = sqlite3.connect(db_file)
        left = conn.execute("SELECT token FROM users WHERE email = ?", (email,)).fetchone()[0]
        conn.close()
        self.assertIsNone(left, "the old token moved into a session; only its hash is stored")
        self.ok(self.w.cgi("GET", "/api/me", token="legacy-token-123"))
        self.ok(self.w.cgi("POST", "/api/logout", token="legacy-token-123"))
        self.assertEqual(self.w.cgi("GET", "/api/me", token="legacy-token-123").status, 401)

    def test_email_layouts_preview_and_templates(self):
        t = self.owner_token
        draft = {"subject": "HeavyPro 0.5", "message": "What changed:\n- Faster start\n- Realistic rudder\n\nThanks for testing!", "images": ["/img/hero.jpg", "/img/clouds.jpg"]}
        html = lambda **x: self.ok(self.w.cgi("POST", "/api/admin/email-preview", {**draft, **x}, token=t))["html"]  # noqa: E731
        update = html(layout="update")
        self.assertIn(">UPDATE<", update)
        self.assertIn("<li style=\"margin:0 0 6px\">Faster start</li>", update)
        self.assertIn("https://rotaidem.test/downloads", update)
        self.assertIn("Download the update", update)
        launch = html(layout="launch", button={"text": "Get HeavyPro", "url": "/heavypro"})
        self.assertIn("<h1", launch)
        self.assertIn("https://rotaidem.test/heavypro", launch)
        self.assertLess(launch.find("https://rotaidem.test/img/hero.jpg"), launch.find("HeavyPro 0.5"), "the first picture is on top")
        plain = html(layout="plain")
        self.assertNotIn(">ROTAIDEM<", plain)
        self.assertIn("Eliaz", plain)
        self.assertIn("Unsubscribe", plain)
        self.assertEqual(self.w.cgi("POST", "/api/admin/email-preview", {**draft, "button": {"url": "javascript:alert(1)"}}, token=t).status, 400)
        # the real email uses the same layout
        self.resend.requests.clear()
        self.ok(self.w.cgi("POST", "/api/admin/broadcast", {**draft, "layout": "update", "test_email": "test@example.com"}, token=t))
        self.assertIn(">UPDATE<", self.resend.requests[-1]["body"][0]["html"])
        # saved templates
        r = self.ok(self.w.cgi("POST", "/api/admin/email-templates", {**draft, "name": "Patch notes", "layout": "update"}, token=t))
        self.assertEqual([x["name"] for x in r["templates"]], ["Patch notes"])
        self.ok(self.w.cgi("POST", "/api/admin/email-templates", {**draft, "name": "Patch notes", "layout": "plain"}, token=t))
        templates = self.ok(self.w.cgi("GET", "/api/admin/email-templates", token=t))["templates"]
        self.assertEqual((len(templates), templates[0]["layout"], templates[0]["images"]), (1, "plain", draft["images"]), "the same name replaces it")
        self.assertEqual(self.w.cgi("POST", "/api/admin/email-templates", {**draft, "name": " "}, token=t).status, 400)
        r = self.ok(self.w.cgi("DELETE", "/api/admin/email-templates", token=t, query="name=Patch%20notes"))
        self.assertEqual(r["templates"], [])
        customer, _ = self.customer("templater")
        self.assertEqual(self.w.cgi("GET", "/api/admin/email-templates", token=customer).status, 403)

    def test_downloads_counted_and_licence_only(self):
        t = self.owner_token
        product = {"name": "Gated App", "id": "gated-app", "download_url": "https://rotaidem.test/files/GatedSetup.exe", "status": "published",
                   "changelog": [{"version": "1.1", "date": "2026-10-20", "notes": "Faster"}, {"version": "", "notes": "ignored"}, {"version": "1.0", "date": "bad"}]}
        self.ok(self.w.cgi("POST", "/api/save-product", product, token=t))
        listed = next(p for p in self.ok(self.w.cgi("GET", "/api/products"))["products"] if p["id"] == "gated-app")
        self.assertEqual(listed["changelog"], [{"version": "1.1", "date": "2026-10-20", "notes": "Faster"}, {"version": "1.0", "date": "", "notes": ""}])
        self.assertTrue(self.ok(self.w.cgi("POST", "/api/download/gated-app"))["hub"], "the website only hands out RotaHub")
        self.ok(self.w.cgi("POST", "/api/download/gated-app"))  # same address again soon: counted once
        self.ok(self.w.cgi("POST", "/api/download/gated-app", remote_addr="198.51.100.20"))
        stats = self.ok(self.w.cgi("GET", "/api/admin/downloads", token=t))
        row = next(p for p in stats["products"] if p["product_id"] == "gated-app")
        self.assertEqual((row["total"], row["last_7"], row["last_30"]), (2, 2, 2))
        listed = next(p for p in self.ok(self.w.cgi("GET", "/api/products"))["products"] if p["id"] == "gated-app")
        self.assertEqual((listed["download_url"], listed["has_download"]), ("", True), "the installer link stays off the website")
        staff_view = next(p for p in self.ok(self.w.cgi("GET", "/api/products", token=t))["products"] if p["id"] == "gated-app")
        self.assertEqual(staff_view["download_url"], product["download_url"], "the admin still sees the link")
        # RotaHub gets the installer; licence holders only: RotaHub needs an account with a licence
        hub = {"User-Agent": "RotaHub/0.2.0 (Windows NT 10.0)"}
        self.assertEqual(self.ok(self.w.cgi("POST", "/api/download/gated-app", headers=hub))["url"], product["download_url"])
        self.ok(self.w.cgi("POST", "/api/save-product", {**product, "download_gate": "licence"}, token=t))
        r = self.w.cgi("POST", "/api/download/gated-app", headers=hub)
        self.assertEqual((r.status, r.json().get("login")), (401, True))
        customer, email = self.customer("downloader")
        self.assertEqual(self.w.cgi("POST", "/api/download/gated-app", token=customer, headers=hub).status, 403)
        self.assertTrue(self.ok(self.w.cgi("POST", "/api/download/gated-app", token=customer))["hub"], "the website still offers RotaHub")
        self.ok(self.w.cgi("POST", "/api/admin/licenses/generate", {"count": 1, "email": email, "product_id": "gated-app", "send_email": False}, token=t))
        self.assertEqual(self.ok(self.w.cgi("POST", "/api/download/gated-app", token=customer, headers=hub))["url"], product["download_url"])
        self.assertEqual(self.w.cgi("POST", "/api/download/no-such-product").status, 404)

    def test_dashboard_recap_and_team_board(self):
        t = self.owner_token
        before = self.ok(self.w.cgi("GET", "/api/admin/insights", token=t))
        self.customer("recapped")
        self.ok(self.w.cgi("POST", "/api/save-product", {"name": "Recap App", "id": "recap-app", "download_url": "https://rotaidem.test/files/R.exe", "status": "published"}, token=t))
        self.ok(self.w.cgi("POST", "/api/download/recap-app", remote_addr="198.51.100.31"))
        after = self.ok(self.w.cgi("GET", "/api/admin/insights", token=t))
        self.assertEqual(after["recap"]["accounts"]["this_week"], before["recap"]["accounts"]["this_week"] + 1)
        self.assertEqual(after["recap"]["downloads"]["this_week"], before["recap"]["downloads"]["this_week"] + 1)
        self.assertEqual(sum(after["heatmap"].values()), sum(before["heatmap"].values()) + 2)
        overview = self.ok(self.w.cgi("GET", "/api/admin/overview", token=t))
        self.assertGreaterEqual(overview["downloads"], 1)
        self.assertIn("emails_sent", overview)
        self.assertIn("media", overview)
        # sticky notes on the team board
        self.assertEqual(self.w.cgi("POST", "/api/admin/notes", {"text": "   "}, token=t).status, 400)
        first = self.ok(self.w.cgi("POST", "/api/admin/notes", {"text": "Ship 1.3 on Friday", "colour": "blue"}, token=t))["notes"][0]
        self.ok(self.w.cgi("POST", "/api/admin/notes", {"text": "Order coffee", "colour": "purple"}, token=t))
        notes = self.ok(self.w.cgi("GET", "/api/admin/notes", token=t))["notes"]
        self.assertEqual([(n["text"], n["colour"]) for n in notes[:2]], [("Order coffee", "yellow"), ("Ship 1.3 on Friday", "blue")])
        self.assertTrue(first["author"])
        customer, _ = self.customer("peeker")
        for method, path in (("GET", "/api/admin/notes"), ("POST", "/api/admin/notes"), ("DELETE", f"/api/admin/notes/{first['id']}"), ("GET", "/api/admin/insights")):
            self.assertEqual(self.w.cgi(method, path, {"text": "hi"}, token=customer).status, 403, path)
        left = self.ok(self.w.cgi("DELETE", f"/api/admin/notes/{first['id']}", token=t))["notes"]
        self.assertNotIn(first["id"], [n["id"] for n in left])

    def test_reply_by_email_saved_answers_and_auto_reply(self):
        t = self.owner_token
        self.ok(self.w.cgi("POST", "/api/contact", {"name": "Pilot Pete", "email": "pete@example.com", "message": "Does it work with a yoke?"}))
        msg = next(m for m in self.ok(self.w.cgi("GET", "/api/admin/messages", token=t))["messages"] if m["email"] == "pete@example.com")
        self.resend.requests.clear()
        self.assertEqual(self.w.cgi("POST", f"/api/admin/messages/{msg['id']}/reply", {"text": "  "}, token=t).status, 400)
        self.ok(self.w.cgi("POST", f"/api/admin/messages/{msg['id']}/reply", {"text": "Yes!\n\nAny yoke that MSFS sees works."}, token=t))
        sent = self.resend.requests[-1]["body"][0]
        self.assertEqual((sent["to"], sent["subject"], sent["reply_to"]), (["pete@example.com"], "Re: your message to Rotaidem", "Eliaz@rotaidem.com"))
        self.assertIn("Any yoke that MSFS sees works.", sent["html"])
        self.assertIn("Does it work with a yoke?", sent["html"], "the original message is quoted")
        msg = next(m for m in self.ok(self.w.cgi("GET", "/api/admin/messages", token=t))["messages"] if m["id"] == msg["id"])
        self.assertEqual((int(msg["handled"]), msg["reply"]), (1, "Yes!\n\nAny yoke that MSFS sees works."))
        self.assertTrue(msg["replied_by"] and msg["replied_at"])
        self.assertEqual(self.w.cgi("POST", "/api/admin/messages/999999/reply", {"text": "x"}, token=t).status, 404)
        # saved answers and the automatic reply
        self.assertEqual(self.w.cgi("POST", "/api/admin/inbox", {"replies": [{"title": "No text"}]}, token=t).status, 400)
        r = self.ok(self.w.cgi("POST", "/api/admin/inbox", {"autoreply": {"enabled": True, "subject": "Thanks!", "text": "We will answer soon."},
                                                            "replies": [{"title": "Yoke", "text": "Any yoke works."}, {"title": "", "text": ""}]}, token=t))
        self.assertEqual(r["replies"], [{"title": "Yoke", "text": "Any yoke works."}])
        self.resend.requests.clear()
        self.ok(self.w.cgi("POST", "/api/contact", {"name": "Auto Al", "email": "al@example.com", "message": "Hello"}))
        self.ok(self.w.cgi("POST", "/api/contact", {"name": "Auto Al", "email": "al@example.com", "message": "Hello again"}))
        auto = [x["body"][0] for x in self.resend.requests]
        self.assertEqual([(a["to"], a["subject"]) for a in auto], [(["al@example.com"], "Thanks!")], "one automatic reply a day per address")
        self.assertIn("We will answer soon.", auto[0]["html"])
        self.ok(self.w.cgi("POST", "/api/admin/inbox", {"autoreply": {"enabled": False}, "replies": []}, token=t))
        customer, _ = self.customer("inboxpeek")
        self.assertEqual(self.w.cgi("GET", "/api/admin/inbox", token=customer).status, 403)
        self.assertEqual(self.w.cgi("POST", f"/api/admin/messages/{msg['id']}/reply", {"text": "x"}, token=customer).status, 403)

    def test_blocklist_and_system_info(self):
        t = self.owner_token
        self.assertEqual(self.w.cgi("POST", "/api/admin/blocklist", {"entries": ["not valid!"]}, token=t).status, 400)
        r = self.ok(self.w.cgi("POST", "/api/admin/blocklist", {"entries": ["@spam.test", "Bad@Example.com", "192.0.2.", "", "@spam.test"]}, token=t))
        self.assertEqual(r["entries"], ["@spam.test", "bad@example.com", "192.0.2."])
        try:
            r = self.w.cgi("POST", "/api/signup", {"name": "Spammer", "email": "x@spam.test", "password": "spam-pass-1"})
            self.assertEqual(r.status, 403)
            self.ok(self.w.cgi("POST", "/api/contact", {"name": "Bad", "email": "bad@example.com", "message": "Buy now"}))
            self.ok(self.w.cgi("POST", "/api/contact", {"name": "Ip", "email": "fine@example.com", "message": "From a blocked network"}, remote_addr="192.0.2.9"))
            self.ok(self.w.cgi("POST", "/api/subscribe", {"email": "list@spam.test"}))
            messages = [m["message"] for m in self.ok(self.w.cgi("GET", "/api/admin/messages", token=t))["messages"]]
            self.assertNotIn("Buy now", messages)
            self.assertNotIn("From a blocked network", messages)
            self.assertNotIn("list@spam.test", [s["email"] for s in self.ok(self.w.cgi("GET", "/api/admin/subscribers", token=t))["subscribers"]])
            self.ok(self.w.cgi("POST", "/api/contact", {"name": "Ok", "email": "fine@example.com", "message": "Allowed"}, remote_addr="198.51.100.9"))
            system = self.ok(self.w.cgi("GET", "/api/admin/system", token=t))
            self.assertEqual(system["blocked"], 3)
            self.assertGreaterEqual(system["tables"]["users"], 1)
            self.assertIn(system["database"], ("SQLite", "MySQL / MariaDB"))
            self.assertNotIn("re_test", json.dumps(system), "no secrets in the system info")
            customer, _ = self.customer("blockpeek")
            for path in ("/api/admin/blocklist", "/api/admin/system"):
                self.assertEqual(self.w.cgi("GET", path, token=customer).status, 403)
        finally:
            self.ok(self.w.cgi("POST", "/api/admin/blocklist", {"entries": []}, token=t))
        overview = self.ok(self.w.cgi("GET", "/api/admin/overview", token=t))
        self.assertIn(overview["name"], [s["name"] for s in overview["staff_online"]])

    def test_orders_bulk_licences_and_timeline(self):
        t = self.owner_token
        customer, email = self.customer("timeline")
        user = next(u for u in self.ok(self.w.cgi("GET", "/api/admin/customers", token=t))["customers"] if u["email"] == email)
        self.ok(self.w.cgi("POST", "/api/admin/orders", {"customer_id": user["id"], "product_id": "heavypro", "amount": "29.99"}, token=t))
        order = next(o for o in self.ok(self.w.cgi("GET", "/api/admin/orders", token=t))["orders"] if o["customer_id"] == user["id"])
        self.assertEqual(self.w.cgi("POST", f"/api/admin/orders/{order['id']}", {"payment_status": "Lost"}, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", f"/api/admin/orders/{order['id']}", {"amount": -1}, token=t).status, 400)
        self.ok(self.w.cgi("POST", f"/api/admin/orders/{order['id']}", {"payment_status": "Refunded"}, token=t))
        order = next(o for o in self.ok(self.w.cgi("GET", "/api/admin/orders", token=t))["orders"] if o["id"] == order["id"])
        self.assertEqual((order["payment_status"], float(order["amount"])), ("Refunded", 29.99))
        # the timeline: messages from their address and their downloads come with the user
        self.ok(self.w.cgi("POST", "/api/contact", {"name": "Timeline", "email": email, "message": "Where is my key?"}))
        detail = self.ok(self.w.cgi("GET", f"/api/admin/users/{user['id']}", token=t))["user"]
        self.assertEqual([m["message"] for m in detail["messages"]], ["Where is my key?"])
        self.assertEqual(detail["downloads"], [])
        # bulk licence changes
        keys = self.ok(self.w.cgi("POST", "/api/admin/licenses/generate", {"count": 3, "send_email": False}, token=t))["keys"]
        ids = [l["id"] for l in self.ok(self.w.cgi("GET", "/api/admin/licenses", token=t))["licenses"] if l["license_key"] in keys]
        self.assertEqual(self.w.cgi("POST", "/api/admin/licenses/bulk", {"ids": ids, "action": "explode"}, token=t).status, 400)
        self.assertEqual(self.ok(self.w.cgi("POST", "/api/admin/licenses/bulk", {"ids": ids[:2], "action": "revoke"}, token=t))["changed"], 2)
        self.assertEqual(self.ok(self.w.cgi("POST", "/api/admin/licenses/bulk", {"ids": ids, "action": "revoke"}, token=t))["changed"], 1)
        statuses = {l["id"]: l["status"] for l in self.ok(self.w.cgi("GET", "/api/admin/licenses", token=t))["licenses"]}
        self.assertEqual({statuses[i] for i in ids}, {"revoked"})
        self.ok(self.w.cgi("POST", "/api/admin/licenses/bulk", {"ids": ids, "action": "activate"}, token=t))
        for method, path in (("POST", f"/api/admin/orders/{order['id']}"), ("DELETE", f"/api/admin/orders/{order['id']}"), ("POST", "/api/admin/licenses/bulk")):
            self.assertEqual(self.w.cgi(method, path, {"ids": ids, "action": "revoke"}, token=customer).status, 403, path)
        self.ok(self.w.cgi("DELETE", f"/api/admin/orders/{order['id']}", token=t))
        self.assertNotIn(order["id"], [o["id"] for o in self.ok(self.w.cgi("GET", "/api/admin/orders", token=t))["orders"]])
        self.assertEqual(self.w.cgi("DELETE", f"/api/admin/orders/{order['id']}", token=t).status, 404)

    def test_link_clicks_goals_and_leaderboard(self):
        t = self.owner_token
        self.ok(self.w.cgi("POST", "/api/admin/settings", {"links": {"discord": "https://discord.gg/rotaidem"}}, token=t))
        try:
            before = (self.ok(self.w.cgi("GET", "/api/admin/link-hits", token=t))["hits"].get("discord") or {}).get("total", 0)
            self.ok(self.w.cgi("POST", "/api/link-hit/discord"))
            self.ok(self.w.cgi("POST", "/api/link-hit/discord"))
            self.ok(self.w.cgi("POST", "/api/link-hit/not-a-link"))
            hits = self.ok(self.w.cgi("GET", "/api/admin/link-hits", token=t))["hits"]
            self.assertEqual(hits["discord"]["total"], before + 2)
            self.assertNotIn("not-a-link", hits)
        finally:
            self.ok(self.w.cgi("POST", "/api/admin/settings", {"links": {}}, token=t))
        self.assertEqual(self.w.cgi("POST", "/api/admin/goals", {"revenue": "lots"}, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", "/api/admin/goals", {"customers": -5}, token=t).status, 400)
        goals = self.ok(self.w.cgi("POST", "/api/admin/goals", {"revenue": "1000.5", "customers": "50", "downloads": 0}, token=t))["goals"]
        self.assertEqual(goals, {"revenue": 1000.5, "customers": 50, "downloads": 0, "subscribers": 0})
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/admin/goals", token=t))["goals"], goals)
        board = self.ok(self.w.cgi("GET", "/api/admin/leaderboard", token=t))
        self.assertTrue(any(r["action"] == "save_goals" and r["n"] >= 1 for r in board["rows"]))
        customer, _ = self.customer("goalpeek")
        self.assertEqual(self.w.cgi("POST", "/api/admin/goals", {"revenue": 1}, token=customer).status, 403)
        for path in ("/api/admin/goals", "/api/admin/leaderboard", "/api/admin/link-hits"):
            self.assertEqual(self.w.cgi("GET", path, token=customer).status, 403, path)

    def test_rotahub_add_a_key(self):
        t = self.owner_token
        alice, _ = self.customer("hubalice")
        bob, _ = self.customer("hubbob")
        keys = self.ok(self.w.cgi("POST", "/api/admin/licenses/generate", {"count": 3, "send_email": False}, token=t))["keys"]
        self.assertEqual(self.w.cgi("POST", "/api/account/licenses/claim", {"key": keys[0]}).status, 401)
        self.assertEqual(self.w.cgi("POST", "/api/account/licenses/claim", {"key": "HPRO-NOPE"}, token=alice).status, 400)
        r = self.ok(self.w.cgi("POST", "/api/account/licenses/claim", {"key": keys[0].lower()}, token=alice))
        self.assertEqual((r["license"]["license_key"], r["license"]["status"], int(r["license"]["activations"])), (keys[0], "active", 0))
        self.assertIn(keys[0], [l["license_key"] for l in self.ok(self.w.cgi("GET", "/api/account/licenses", token=alice))["licenses"]])
        self.ok(self.w.cgi("POST", "/api/account/licenses/claim", {"key": keys[0]}, token=alice))  # again: fine
        r = self.w.cgi("POST", "/api/account/licenses/claim", {"key": keys[0]}, token=bob)
        self.assertEqual(r.status, 409, "a key on another account stays there")
        # a key sold to another address (a gift) can be added by whoever has it
        lic = next(l for l in self.ok(self.w.cgi("GET", "/api/admin/licenses", token=t))["licenses"] if l["license_key"] == keys[1])
        self.ok(self.w.cgi("POST", f"/api/admin/licenses/{lic['id']}", {"email": "gift-giver@example.com"}, token=t))
        self.assertEqual(self.ok(self.w.cgi("POST", "/api/account/licenses/claim", {"key": keys[1]}, token=bob))["license"]["license_key"], keys[1])
        lic = next(l for l in self.ok(self.w.cgi("GET", "/api/admin/licenses", token=t))["licenses"] if l["license_key"] == keys[2])
        self.ok(self.w.cgi("POST", f"/api/admin/licenses/{lic['id']}", {"status": "revoked"}, token=t))
        self.assertEqual(self.w.cgi("POST", "/api/account/licenses/claim", {"key": keys[2]}, token=bob).status, 403)

    def upload(self, name, data, token, chunk=300):
        upload_id = os.urandom(10).hex()
        pieces = [data[i:i + chunk] for i in range(0, len(data), chunk)] or [b""]
        r = None
        for i, piece in enumerate(pieces):
            r = self.w.cgi("POST", "/api/admin/files", {"name": name, "upload_id": upload_id, "index": i, "total": len(pieces),
                                                        "data": base64.b64encode(piece).decode()}, token=token)
            if r.status != 200:
                return r
        return r

    def test_files_uploaded_from_the_admin(self):
        t = self.owner_token
        setup = b"MZ" + os.urandom(1000)
        r = self.upload("RotaHub.exe", setup, t)
        self.assertEqual(r.status, 200, r.body)
        f = r.json()["file"]
        self.assertTrue(f["url"].startswith("/files/RotaHub.exe?v="))
        with open(os.path.join(self.w.public, "files", "RotaHub.exe"), "rb") as fh:
            self.assertEqual(fh.read(), setup, "all pieces, in order, in the website's files folder")
        self.assertEqual(os.stat(os.path.join(self.w.public, "files", "RotaHub.exe")).st_mode & 0o777, 0o644)
        self.assertIn("RotaHub.exe", [x["name"] for x in self.ok(self.w.cgi("GET", "/api/admin/files", token=t))["files"]])
        self.assertEqual(self.upload("Fake.exe", b"not a program" * 10, t).status, 400, "an .exe must be a Windows program")
        self.assertFalse(os.path.exists(os.path.join(self.w.public, "files", "Fake.exe")))
        self.assertEqual(self.upload("my setup.exe", setup, t).status, 400)
        self.assertEqual(self.upload("../evil.exe", setup, t).status, 400)
        self.assertEqual(self.upload("script.php", b"<?php", t).status, 400)
        customer, _ = self.customer("filepeek")
        self.assertEqual(self.upload("Other.exe", setup, customer).status, 403)
        self.assertEqual(self.w.cgi("GET", "/api/admin/files", token=customer).status, 403)
        self.assertEqual(self.w.cgi("DELETE", "/api/admin/files/RotaHub.exe", token=customer).status, 403)
        self.ok(self.w.cgi("DELETE", "/api/admin/files/RotaHub.exe", token=t))
        self.assertFalse(os.path.exists(os.path.join(self.w.public, "files", "RotaHub.exe")))
        self.assertEqual(self.w.cgi("DELETE", "/api/admin/files/RotaHub.exe", token=t).status, 404)

    def webhook(self, event, secret="whsec_testsecret", stamp=None):
        payload = json.dumps(event).encode()
        stamp = str(int(time.time()) if stamp is None else stamp)
        sig = hmac.new(secret.encode(), stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
        return self.w.cgi("POST", "/api/stripe/webhook", payload, headers={"Stripe-Signature": f"t={stamp},v1={sig}", "Content-Type": "application/json"})

    def test_stripe_checkout_order_and_key(self):
        t = self.owner_token
        product = {"name": "Pay App", "id": "pay-app", "status": "published", "tagline": "Paid add-on", "pay_amount": "149", "pay_currency": "sek"}
        self.assertEqual(self.w.cgi("POST", "/api/save-product", {**product, "pay_amount": "lots"}, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", "/api/save-product", {**product, "pay_currency": "xyz"}, token=t).status, 400)
        self.ok(self.w.cgi("POST", "/api/save-product", product, token=t))
        listed = next(p for p in self.ok(self.w.cgi("GET", "/api/products"))["products"] if p["id"] == "pay-app")
        self.assertEqual((listed["can_buy"], listed["pay_amount"], listed["pay_currency"]), (True, 14900, "sek"))
        r = self.w.cgi("POST", "/api/checkout/pay-app")
        self.assertEqual((r.status, r.json().get("login")), (401, True))
        buyer, email = self.customer("stripebuyer")
        url = self.ok(self.w.cgi("POST", "/api/checkout/pay-app", token=buyer))["url"]
        form = self.stripe.created[-1]
        sid = url.rsplit("/", 1)[-1]
        self.assertEqual((form["line_items[0][price_data][unit_amount]"], form["line_items[0][price_data][currency]"], form["customer_email"], form["metadata[product_id]"]),
                         ("14900", "sek", email, "pay-app"))
        self.assertEqual(form["success_url"], "https://rotaidem.test/checkout-done?session_id={CHECKOUT_SESSION_ID}")
        self.assertFalse(self.ok(self.w.cgi("GET", "/api/checkout/status", token=buyer, query="session_id=" + sid))["paid"])
        # paid: the page after paying gets the key, the order is made once and the key emailed
        self.stripe.pay(sid)
        self.resend.requests.clear()
        done = self.ok(self.w.cgi("GET", "/api/checkout/status", token=buyer, query="session_id=" + sid))
        self.assertTrue(done["paid"])
        self.assertTrue(done["license_key"].startswith("PAYA-"), done)
        self.assertIn(done["license_key"], self.resend.requests[-1]["body"][0]["html"])
        again = self.ok(self.w.cgi("GET", "/api/checkout/status", token=buyer, query="session_id=" + sid))
        self.assertEqual(again["license_key"], done["license_key"])
        self.assertEqual(self.ok(self.webhook({"type": "checkout.session.completed", "data": {"object": self.stripe.sessions[sid]}})), {"received": True})
        orders = [o for o in self.ok(self.w.cgi("GET", "/api/admin/orders", token=t))["orders"] if o.get("payment_ref") == sid]
        self.assertEqual(len(orders), 1, "one order however often it is confirmed")
        self.assertEqual((float(orders[0]["amount"]), orders[0]["currency"], orders[0]["payment_status"]), (149.0, "sek", "Paid"))
        self.assertIn(done["license_key"], [l["license_key"] for l in self.ok(self.w.cgi("GET", "/api/account/licenses", token=buyer))["licenses"]])
        r = self.w.cgi("POST", "/api/checkout/pay-app", token=buyer)
        self.assertEqual((r.status, r.json().get("owned")), (409, True), "already owns it")
        # someone else cannot look at this order
        other, _ = self.customer("stripepeek")
        self.assertEqual(self.w.cgi("GET", "/api/checkout/status", token=other, query="session_id=" + sid).status, 404)
        # the webhook alone makes the order when the buyer closed the page after paying
        url = self.ok(self.w.cgi("POST", "/api/checkout/pay-app", token=other))["url"]
        sid2 = url.rsplit("/", 1)[-1]
        self.assertEqual(self.webhook({"type": "checkout.session.completed", "data": {"object": self.stripe.pay(sid2)}}, secret="whsec_wrong").status, 400)
        self.assertEqual(self.webhook({"type": "checkout.session.completed", "data": {"object": self.stripe.sessions[sid2]}}, stamp=int(time.time()) - 3600).status, 400)
        self.ok(self.webhook({"type": "checkout.session.completed", "data": {"object": self.stripe.sessions[sid2]}}))
        self.assertTrue(any(l["product_id"] == "pay-app" for l in self.ok(self.w.cgi("GET", "/api/account/licenses", token=other))["licenses"]))
        # not for sale: no price, or not available
        self.ok(self.w.cgi("POST", "/api/save-product", {**product, "status": "coming_soon"}, token=t))
        self.assertEqual(self.w.cgi("POST", "/api/checkout/pay-app", token=other).status, 404)
        self.assertFalse(next(p for p in self.ok(self.w.cgi("GET", "/api/products"))["products"] if p["id"] == "pay-app")["can_buy"])
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/admin/system", token=t))["stripe"], "test")

    def test_sale_price_and_the_shop(self):
        t = self.owner_token
        product = {"name": "Sale App", "id": "sale-app", "status": "published", "pay_amount": "7.99", "pay_currency": "usd"}
        self.assertEqual(self.w.cgi("POST", "/api/save-product", {**product, "sale_amount": "9.99"}, token=t).status, 400, "a sale is cheaper")
        self.assertEqual(self.w.cgi("POST", "/api/save-product", {**product, "sale_amount": "cheap"}, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", "/api/save-product", {**product, "sale_amount": "5.99", "sale_until": "soon"}, token=t).status, 400)
        self.ok(self.w.cgi("POST", "/api/save-product", {**product, "sale_amount": "5.99", "sale_until": "2999-12-31"}, token=t))
        listed = next(p for p in self.ok(self.w.cgi("GET", "/api/products"))["products"] if p["id"] == "sale-app")
        self.assertEqual((listed["sale_active"], listed["price_now"], listed["pay_amount"], listed["sale_until"]), (True, 599, 799, "2999-12-31"))
        # the shop: discount codes are off until switched on; customers see none of it
        buyer, _ = self.customer("salebuyer")
        self.assertEqual(self.w.cgi("GET", "/api/admin/shop", token=buyer).status, 403)
        self.assertEqual(self.w.cgi("POST", "/api/admin/shop", {"promo_codes": True}, token=buyer).status, 403)
        shop = self.ok(self.w.cgi("GET", "/api/admin/shop", token=t))
        self.assertEqual((shop["stripe"]["connected"], shop["stripe"]["mode"], shop["settings"]["promo_codes"]), (True, "test", False))
        self.assertTrue(shop["stripe"]["webhook_url"].endswith("/api/stripe/webhook"))
        self.ok(self.w.cgi("POST", "/api/admin/shop", {"promo_codes": True}, token=t))
        # buyers pay the sale price, and may enter a code
        sid = self.ok(self.w.cgi("POST", "/api/checkout/sale-app", token=buyer))["url"].rsplit("/", 1)[-1]
        form = self.stripe.created[-1]
        self.assertEqual((form["line_items[0][price_data][unit_amount]"], form["line_items[0][price_data][currency]"], form.get("allow_promotion_codes")), ("599", "usd", "true"))
        self.assertNotIn("metadata[endorsely_referral]", form, "no referral, none sent")
        self.stripe.pay(sid)
        self.assertTrue(self.ok(self.w.cgi("GET", "/api/checkout/status", token=buyer, query="session_id=" + sid))["paid"])
        item = next(p for p in self.ok(self.w.cgi("GET", "/api/admin/shop", token=t))["products"] if p["id"] == "sale-app")
        self.assertEqual((item["sale_active"], item["price_now"], item["can_buy"]), (True, 599, True))
        self.assertEqual(item["sales"], [{"currency": "usd", "orders": 1, "total": 5.99, "last_30_days": 1}])
        # a sale past its last day is over: the normal price again, and codes switched off
        self.ok(self.w.cgi("POST", "/api/save-product", {**product, "sale_amount": "5.99", "sale_until": "2020-01-01"}, token=t))
        self.ok(self.w.cgi("POST", "/api/admin/shop", {"promo_codes": False}, token=t))
        other, _ = self.customer("salelate")
        self.assertEqual(self.w.cgi("POST", "/api/checkout/sale-app", {"referral": "bad referral <x>"}, token=other).status, 200)
        self.assertNotIn("metadata[endorsely_referral]", self.stripe.created[-1], "an odd referral is left out")
        self.ok(self.w.cgi("POST", "/api/checkout/sale-app", {"referral": "ref_8Kd2-x"}, token=other))
        form = self.stripe.created[-1]
        self.assertEqual(form["metadata[endorsely_referral]"], "ref_8Kd2-x", "Endorsely credits the affiliate")
        self.assertEqual((form["line_items[0][price_data][unit_amount]"], "allow_promotion_codes" in form), ("799", False))
        listed = next(p for p in self.ok(self.w.cgi("GET", "/api/products"))["products"] if p["id"] == "sale-app")
        self.assertEqual((listed["sale_active"], listed["price_now"]), (False, 799))
        self.assertIn("save_shop", [a["action"] for a in self.ok(self.w.cgi("GET", "/api/admin/audit", token=t))["logs"]])

    def test_aircraft_list_from_the_admin(self):
        t = self.owner_token
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/site"))["aircraft"], [], "nothing saved: the pages keep their own list")
        aircraft = [{"name": "PMDG 737", "profile": "Boeing 737", "note": "Full support", "home": True, "home_text": "Boeing narrow-body profile"},
                    {"name": "  ", "profile": "dropped: no name"}, {"name": "<b>GA</b>", "profile": "Generic GA", "home": "yes", "extra": "ignored"}]
        saved = self.ok(self.w.cgi("POST", "/api/admin/settings", {"aircraft": aircraft}, token=t))["aircraft"]
        self.assertEqual(saved, [aircraft[0], {"name": "<b>GA</b>", "profile": "Generic GA", "note": "", "home": True, "home_text": ""}])
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/site"))["aircraft"][0]["name"], "PMDG 737")
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"aircraft": [{"name": "x"}] * 31}, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"aircraft": "PMDG"}, token=t).status, 400)
        buyer, _ = self.customer("aircraftfan")
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"aircraft": []}, token=buyer).status, 403)
        self.assertEqual(self.ok(self.w.cgi("POST", "/api/admin/settings", {"aircraft": []}, token=t))["aircraft"], [])

    def discord_call(self, event, seed=DISCORD_SEED, stamp=None):
        raw = json.dumps(event).encode()
        stamp = stamp or str(int(time.time()))
        sig = ed25519_sign(seed, stamp.encode() + raw).hex()
        return self.w.cgi("POST", "/api/discord/interactions", raw, headers={"X-Signature-Ed25519": sig, "X-Signature-Timestamp": stamp})

    def discord_link(self, token, discord_id):
        """My account -> Link Discord -> Discord asks -> back to the website. Returns where the customer lands."""
        url = self.ok(self.w.cgi("POST", "/api/discord/start", token=token))["url"]
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual((q["client_id"], q["scope"], q["redirect_uri"]), ("112233445566778899", "identify guilds.join", "https://rotaidem.test/api/discord/callback"))
        r = self.w.cgi("GET", "/api/discord/callback", query=urllib.parse.urlencode({"code": "code-" + discord_id, "state": q["state"]}))
        self.assertEqual(r.status, 302, r.stderr[-500:])
        return r.headers["location"]

    def test_discord_verified_buyer_role(self):
        t, guild, role = self.owner_token, "1100000000000000001", "1200000000000000002"
        buyer, buyer_email = self.customer("discordbuyer")
        self.assertFalse(self.ok(self.w.cgi("GET", "/api/me", token=buyer))["user"]["discord"]["available"], "not until server and role are chosen")
        self.assertEqual(self.w.cgi("POST", "/api/discord/start", token=buyer).status, 503)
        self.assertEqual(self.w.cgi("POST", "/api/admin/discord", {"guild_id": "my server", "role_id": role}, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", "/api/admin/discord", {"guild_id": guild, "role_id": role}, token=buyer).status, 403)
        self.assertTrue(self.ok(self.w.cgi("POST", "/api/admin/discord", {"guild_id": guild, "role_id": role}, token=t))["ready"])
        try:
            lic = self.ok(self.w.cgi("POST", "/api/admin/licenses/generate", {"count": 1, "email": buyer_email, "product_id": "heavypro", "send_email": False}, token=t))
            # a licence holder links Discord: joins the server with the buyer role
            self.assertEqual(self.discord_link(buyer, "300000000000000001"), "https://rotaidem.test/account?discord=linked#discord")
            self.assertEqual(self.discord.members["300000000000000001"], {role})
            me = self.ok(self.w.cgi("GET", "/api/me", token=buyer))["user"]["discord"]
            self.assertEqual((me["linked"], me["name"], me["buyer"]), (True, "Pilot 001", True))
            # the role follows the licence
            lic_id = next(l["id"] for l in self.ok(self.w.cgi("GET", "/api/admin/licenses", token=t))["licenses"] if l["license_key"] == lic["keys"][0])
            self.ok(self.w.cgi("POST", f"/api/admin/licenses/{lic_id}", {"status": "revoked"}, token=t))
            self.assertEqual(self.discord.members["300000000000000001"], set(), "revoked: role taken")
            self.ok(self.w.cgi("POST", "/api/admin/licenses/bulk", {"action": "activate", "ids": [lic_id]}, token=t))
            self.assertEqual(self.discord.members["300000000000000001"], {role}, "active again: role back")
            # someone without a licence joins the server but gets no role; a bad or old link does nothing
            other, _ = self.customer("discordfan")
            self.assertEqual(self.discord_link(other, "300000000000000002"), "https://rotaidem.test/account?discord=linked-no-licence#discord")
            self.assertEqual(self.discord.members["300000000000000002"], set())
            r = self.w.cgi("GET", "/api/discord/callback", query="code=code-300000000000000003&state=1.9999999999.abc.forged")
            self.assertTrue(r.headers["location"].endswith("?discord=expired#discord"))
            self.assertNotIn("300000000000000003", self.discord.members)
            self.assertTrue(self.w.cgi("GET", "/api/discord/callback", query="error=access_denied").headers["location"].endswith("?discord=cancelled#discord"))
            # /verify in Discord: only signed calls; linked members get their role, others a Verify button
            self.assertEqual(self.w.cgi("POST", "/api/discord/interactions", {"type": 1}).status, 401)
            self.assertEqual(self.discord_call({"type": 1}, seed=b"someone else").status, 401)
            self.assertEqual(self.ok(self.discord_call({"type": 1})), {"type": 1})
            self.discord.members["300000000000000001"].clear()
            answer = self.ok(self.discord_call({"type": 2, "data": {"name": "verify"}, "member": {"user": {"id": "300000000000000001"}}}))
            self.assertEqual((answer["type"], answer["data"]["flags"]), (4, 64))
            self.assertIn("You are verified", answer["data"]["content"])
            self.assertEqual(self.discord.members["300000000000000001"], {role})
            answer = self.ok(self.discord_call({"type": 2, "data": {"name": "verify"}, "member": {"user": {"id": "300000000000000009"}}}))
            self.assertEqual(answer["data"]["components"][0]["components"][0]["url"], "https://rotaidem.test/account#discord")
            # the admin: members, check everyone, /verify command, a Verify button in a channel
            info = self.ok(self.w.cgi("GET", "/api/admin/discord", token=t))
            mine = {"300000000000000001", "300000000000000002"}
            self.assertEqual({m["discord_id"]: m["buyer"] for m in info["members"] if m["discord_id"] in mine}, {"300000000000000001": True, "300000000000000002": False})
            self.assertEqual(info["interactions_url"], "https://rotaidem.test/api/discord/interactions")
            self.assertTrue(all(info["setup"].values()))
            self.discord.members["300000000000000002"].add(role)  # given by hand in Discord, no licence
            counts = self.ok(self.w.cgi("POST", "/api/admin/discord/sync", token=t))["counts"]
            self.assertEqual((counts.get("removed"), counts.get("failed")), (1, None))
            self.assertGreaterEqual(counts.get("given", 0), 1)
            self.assertEqual(self.discord.members["300000000000000002"], set())
            self.ok(self.w.cgi("POST", "/api/admin/discord/command", token=t))
            self.assertEqual([c["name"] for c in self.discord.commands], ["verify"])
            self.assertEqual(self.w.cgi("POST", "/api/admin/discord/post", {"channel_id": "general", "text": "Hi"}, token=t).status, 400)
            self.ok(self.w.cgi("POST", "/api/admin/discord/post", {"channel_id": "1300000000000000003", "text": "Verify here"}, token=t))
            channel, message = self.discord.messages[-1]
            self.assertEqual((channel, message["content"], message["components"][0]["components"][0]["label"]), ("1300000000000000003", "Verify here", "Verify on rotaidem.com"))
            # unlink: role off, account no longer linked; settings stay staff-only
            self.ok(self.w.cgi("POST", "/api/discord/unlink", token=buyer))
            self.assertEqual(self.discord.members["300000000000000001"], set())
            self.assertFalse(self.ok(self.w.cgi("GET", "/api/me", token=buyer))["user"]["discord"]["linked"])
            self.assertEqual(self.w.cgi("GET", "/api/admin/discord", token=buyer).status, 403)
        finally:
            self.ok(self.w.cgi("POST", "/api/admin/discord", {"guild_id": "", "role_id": ""}, token=t))

    def test_pilot_quotes(self):
        t = self.owner_token
        quotes = [{"name": "Captain Erik L.", "about": "Airline pilot, Boeing 737", "text": "Closest a desk stick has felt.", "image": "/media/4.jpg"},
                  {"name": "  ", "text": "no name: dropped"}, {"name": "Anna", "text": "", "image": ""},
                  {"name": "Sim pilot", "text": "Smooth.", "image": "https://cdn.example.com/a.png", "extra": 1}]
        saved = self.ok(self.w.cgi("POST", "/api/admin/settings", {"testimonials": quotes}, token=t))["testimonials"]
        self.assertEqual(saved, [quotes[0], {"name": "Sim pilot", "about": "", "text": "Smooth.", "image": "https://cdn.example.com/a.png"}])
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/site"))["testimonials"][0]["name"], "Captain Erik L.")
        bad = {"testimonials": [{"name": "X", "text": "Hi", "image": "javascript:alert(1)"}]}
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", bad, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"testimonials": [{"name": "x", "text": "y"}] * 21}, token=t).status, 400)
        with open(os.path.join(self.w.public, "site-settings.js")) as fh:
            self.assertIn("Closest a desk stick has felt.", fh.read(), "published with the site settings")
        fan, _ = self.customer("quotefan")
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"testimonials": []}, token=fan).status, 403)
        self.assertEqual(self.ok(self.w.cgi("POST", "/api/admin/settings", {"testimonials": []}, token=t))["testimonials"], [])

    def test_post_to_discord_from_the_admin(self):
        t, guild, role = self.owner_token, "1100000000000000001", "1200000000000000002"
        self.assertEqual(self.w.cgi("GET", "/api/admin/discord/channels", token=t).status, 503, "not set up")
        self.ok(self.w.cgi("POST", "/api/admin/discord", {"guild_id": guild, "role_id": role}, token=t))
        try:
            chans = self.ok(self.w.cgi("GET", "/api/admin/discord/channels", token=t))["channels"]
            self.assertEqual([(c["name"], c["announcement"], c["category"]) for c in chans], [("announcements", True, "Info"), ("general", False, "Info")], "text channels only")
            fan, _ = self.customer("discordposter")
            post = {"channel_id": "1400000000000000003", "channel_name": "announcements", "ping": "buyers", "text": "New version!",
                    "embed": {"title": "HeavyPro 0.7.0 is out", "description": "Smoother pitch", "url": "/heavypro", "image": "/media/3.png", "color": "#4cc38a"},
                    "buttons": [{"label": "Update in RotaHub", "url": "/get-rotahub?product=HeavyPro"}, {"label": "", "url": ""}], "publish": True}
            self.assertEqual(self.w.cgi("POST", "/api/admin/discord/message", post, token=fan).status, 403)
            for bad in ({"channel_id": "general"}, {"text": "", "embed": {}}, {"buttons": [{"label": "Go", "url": "javascript:alert(1)"}]},
                        {"embed": {"title": "x", "image": "file:///c:/x.png"}}, {"text": "x" * 2001}):
                self.assertEqual(self.w.cgi("POST", "/api/admin/discord/message", {**post, **bad}, token=t).status, 400, bad)
            sent = self.ok(self.w.cgi("POST", "/api/admin/discord/message", post, token=t))
            self.assertTrue(sent["published"])
            channel, msg = self.discord.posted[sent["id"]]
            self.assertEqual(channel, "1400000000000000003")
            self.assertEqual(msg["content"], f"<@&{role}>\nNew version!")
            self.assertEqual(msg["allowed_mentions"], {"parse": [], "roles": [role]}, "only the buyer role is pinged")
            self.assertEqual(msg["embeds"][0], {"title": "HeavyPro 0.7.0 is out", "description": "Smoother pitch", "url": "https://rotaidem.test/heavypro",
                                                 "color": 0x4cc38a, "image": {"url": "https://rotaidem.test/media/3.png"}})
            self.assertEqual(msg["components"][0]["components"], [{"type": 2, "style": 5, "label": "Update in RotaHub", "url": "https://rotaidem.test/get-rotahub?product=HeavyPro"}])
            self.assertEqual(self.discord.crossposted, [sent["id"]])
            # nobody is pinged unless asked
            plain = self.ok(self.w.cgi("POST", "/api/admin/discord/message", {"channel_id": "1400000000000000002", "text": "@everyone hi"}, token=t))
            self.assertEqual(self.discord.posted[plain["id"]][1]["allowed_mentions"], {"parse": [], "roles": []})
            # the list of posts; edit and delete
            mine = lambda: [m for m in self.ok(self.w.cgi("GET", "/api/admin/discord/messages", token=t))["messages"] if m["id"] in (sent["id"], plain["id"])]  # noqa: E731
            history = mine()
            self.assertEqual([m["summary"] for m in history], ["@everyone hi", "HeavyPro 0.7.0 is out"])
            self.assertEqual(history[1]["draft"]["embed"]["title"], "HeavyPro 0.7.0 is out")
            self.ok(self.w.cgi("POST", f"/api/admin/discord/message/1400000000000000003/{sent['id']}", {**post, "embed": {**post["embed"], "title": "HeavyPro 0.7.1 is out"}}, token=t))
            self.assertEqual(self.discord.posted[sent["id"]][1]["embeds"][0]["title"], "HeavyPro 0.7.1 is out")
            self.assertEqual(mine()[1]["summary"], "HeavyPro 0.7.1 is out")
            self.ok(self.w.cgi("DELETE", f"/api/admin/discord/message/1400000000000000003/{sent['id']}", token=t))
            self.assertNotIn(sent["id"], self.discord.posted)
            del self.discord.posted[plain["id"]]  # deleted in Discord by hand: forgotten here too
            self.ok(self.w.cgi("DELETE", f"/api/admin/discord/message/1400000000000000002/{plain['id']}", token=t))
            self.assertEqual(mine(), [])
            self.assertIn("562950222081089", self.ok(self.w.cgi("GET", "/api/admin/discord", token=t))["invite_url"], "the bot may post and embed")
        finally:
            self.ok(self.w.cgi("POST", "/api/admin/discord", {"guild_id": "", "role_id": ""}, token=t))

    def test_discord_extras_and_automatic_posts(self):
        t, guild, role = self.owner_token, "1100000000000000001", "1200000000000000002"
        general, news, sales = "1400000000000000002", "1400000000000000003", "1400000000000000002"
        self.ok(self.w.cgi("POST", "/api/newsletter", {"title": "Old news", "content": "Posted before the news channel was chosen."}, token=t))
        self.ok(self.w.cgi("POST", "/api/admin/discord", {"guild_id": guild, "role_id": role}, token=t))
        try:
            info = self.ok(self.w.cgi("GET", "/api/admin/discord", token=t))
            self.assertEqual(info["server"], {"name": "Rotaidem", "members": 128, "online": 17})
            # #channel links, a poll, reactions and a pin
            post = {"channel_id": general, "channel_name": "general", "text": "Talk about it in #general (not #nowhere)",
                    "embed": {"title": "Vote", "description": "Results in #announcements"}, "reactions": "✈️ 🔥 <bad>",
                    "pin": True, "poll": {"question": "Next profile?", "answers": ["iFly 737 MAX", "PMDG 777F"], "hours": 48, "multiple": True}}
            self.assertEqual(self.w.cgi("POST", "/api/admin/discord/message", {**post, "poll": {"question": "One answer?", "answers": ["only"]}}, token=t).status, 400)
            mid = self.ok(self.w.cgi("POST", "/api/admin/discord/message", post, token=t))["id"]
            msg = self.discord.posted[mid][1]
            self.assertEqual(msg["content"], "Talk about it in <#1400000000000000002> (not #nowhere)")
            self.assertEqual(msg["embeds"][0]["description"], "Results in <#1400000000000000003>")
            self.assertEqual((msg["poll"]["question"]["text"], [a["poll_media"]["text"] for a in msg["poll"]["answers"]], msg["poll"]["duration"], msg["poll"]["allow_multiselect"]),
                             ("Next profile?", ["iFly 737 MAX", "PMDG 777F"], 48, True))
            self.assertEqual(self.discord.reactions[-2:], [(mid, "✈️"), (mid, "🔥")])
            self.assertEqual(self.discord.pins[-1], mid)
            # later: waits in the list; Post now or Cancel
            later = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(time.time() + 7200))
            self.assertEqual(self.w.cgi("POST", "/api/admin/discord/message", {**post, "at": "2020-01-01T10:00"}, token=t).status, 400, "not in the past")
            a = self.ok(self.w.cgi("POST", "/api/admin/discord/message", {"channel_id": general, "text": "Later 1", "at": later}, token=t))
            b = self.ok(self.w.cgi("POST", "/api/admin/discord/message", {"channel_id": general, "text": "Later 2", "at": later}, token=t))
            waiting = self.ok(self.w.cgi("GET", "/api/admin/discord/messages", token=t))["scheduled"]
            self.assertEqual([i["post"]["text"] for i in waiting], ["Later 1", "Later 2"])
            before = len(self.discord.messages)
            self.ok(self.w.cgi("POST", f"/api/admin/discord/scheduled/{a['scheduled']}/now", token=t))
            self.assertEqual(self.discord.messages[-1][1]["content"], "Later 1")
            self.assertEqual(self.w.cgi("POST", f"/api/admin/discord/scheduled/{a['scheduled']}/now", token=t).status, 404, "posted once")
            self.ok(self.w.cgi("DELETE", f"/api/admin/discord/scheduled/{b['scheduled']}", token=t))
            self.assertEqual(self.ok(self.w.cgi("GET", "/api/admin/discord/messages", token=t))["scheduled"], [])
            self.assertEqual(len(self.discord.messages), before + 1)
            # automatic posts: product update (pings buyers), news (only new ones), sales (no customer details)
            self.assertEqual(self.w.cgi("POST", "/api/admin/discord", {"guild_id": guild, "role_id": role, "news_channel": "#news"}, token=t).status, 400)
            self.ok(self.w.cgi("POST", "/api/admin/discord", {"guild_id": guild, "role_id": role, "updates_channel": news, "updates_ping": True,
                                                               "news_channel": news, "sales_channel": sales}, token=t))
            product = {"name": "Auto App", "id": "auto-app", "status": "published", "version": "1.0.0", "pay_amount": "5", "pay_currency": "usd",
                       "changelog": [{"version": "1.1.0", "date": "2026-11-01", "notes": "Smoother pitch"}]}
            self.ok(self.w.cgi("POST", "/api/save-product", product, token=t))
            count = len(self.discord.messages)
            self.ok(self.w.cgi("POST", "/api/save-product", {**product, "tagline": "same version"}, token=t))
            self.assertEqual(len(self.discord.messages), count, "no new version, no post")
            self.ok(self.w.cgi("POST", "/api/save-product", {**product, "version": "1.1.0"}, token=t))
            channel, msg = self.discord.messages[-1]
            self.assertEqual((channel, msg["embeds"][0]["title"], msg["embeds"][0]["description"], msg["content"]), (news, "Auto App 1.1.0 is out", "Smoother pitch", f"<@&{role}>"))
            self.ok(self.w.cgi("POST", "/api/newsletter", {"title": "Fresh news", "content": "Something new."}, token=t))
            channel, msg = self.discord.messages[-1]
            self.assertEqual((channel, msg["embeds"][0]["title"]), (news, "Fresh news"))
            self.assertNotIn("Old news", [m[1].get("embeds", [{}])[0].get("title") if m[1].get("embeds") else "" for m in self.discord.messages])
            buyer, email = self.customer("autosale")
            sid = self.ok(self.w.cgi("POST", "/api/checkout/auto-app", token=buyer))["url"].rsplit("/", 1)[-1]
            self.stripe.pay(sid)
            self.ok(self.w.cgi("GET", "/api/checkout/status", token=buyer, query="session_id=" + sid))
            channel, msg = self.discord.messages[-1]
            self.assertEqual(channel, sales)
            self.assertTrue(msg["content"].startswith("🎉 New sale: **Auto App** for 5.00 USD"), msg["content"])
            self.assertNotIn(email, msg["content"])
        finally:
            self.ok(self.w.cgi("POST", "/api/admin/discord", {"guild_id": "", "role_id": "", "updates_channel": "", "news_channel": "", "sales_channel": ""}, token=t))

    def test_discord_set_up_check(self):
        t, guild, role = self.owner_token, "1100000000000000001", "1200000000000000002"
        self.assertFalse(self.ok(self.w.cgi("GET", "/api/admin/discord/check", token=t))["ok"], "nothing chosen yet")
        self.ok(self.w.cgi("POST", "/api/admin/discord", {"guild_id": guild, "role_id": role}, token=t))
        try:
            check = self.ok(self.w.cgi("GET", "/api/admin/discord/check", token=t))
            self.assertTrue(check["ok"], check)
            # the bot's role below the buyer role: the check says so, and so does the member's last result
            self.discord.bot_position = 2
            check = self.ok(self.w.cgi("GET", "/api/admin/discord/check", token=t))
            self.assertFalse(check["ok"])
            self.assertIn("BELOW", " ".join(c["text"] for c in check["checks"] if not c["ok"]))
            buyer, email = self.customer("rolecheck")
            self.ok(self.w.cgi("POST", "/api/admin/licenses/generate", {"count": 1, "email": email, "product_id": "heavypro", "send_email": False}, token=t))
            self.discord_link(buyer, "300000000000000055")
            member = next(m for m in self.ok(self.w.cgi("GET", "/api/admin/discord", token=t))["members"] if m["discord_id"] == "300000000000000055")
            self.assertIn("bot's role must be ABOVE", member["discord_result"])
            self.assertEqual(self.discord.members["300000000000000055"], {role}, "joining with the role still worked")
            # fixed in Discord: Check everyone again gives the role
            self.discord.bot_position = 5
            self.discord.members["300000000000000055"].clear()
            self.assertGreaterEqual(self.ok(self.w.cgi("POST", "/api/admin/discord/sync", token=t))["counts"].get("given", 0), 1)
            self.assertEqual(self.discord.members["300000000000000055"], {role})
            member = next(m for m in self.ok(self.w.cgi("GET", "/api/admin/discord", token=t))["members"] if m["discord_id"] == "300000000000000055")
            self.assertIn("Role given", member["discord_result"])
            self.assertEqual(self.w.cgi("GET", "/api/admin/discord/check", token=buyer).status, 403)
        finally:
            self.discord.bot_position = 5
            self.ok(self.w.cgi("POST", "/api/admin/discord", {"guild_id": "", "role_id": ""}, token=t))

    def test_update_feed_for_rotahub(self):
        t = self.owner_token
        setup = b"MZ" + os.urandom(2000)
        f = self.upload("FeedAppSetup.exe", setup, t).json()["file"]
        product = {"name": "Feed App", "id": "feed-app", "status": "published", "version": "v1.2", "download_url": f["url"],
                   "changelog": [{"version": "v1.2", "date": "2026-10-26", "notes": "Smoother"}]}
        self.assertEqual(self.w.cgi("POST", "/api/save-product", {**product, "download_url": "javascript:alert(1)"}, token=t).status, 400)
        self.ok(self.w.cgi("POST", "/api/save-product", product, token=t))
        feed = self.ok(self.w.cgi("GET", "/api/hub/products"))
        item = next(p for p in feed["products"] if p["id"] == "feed-app")
        self.assertEqual((item["version"], item["version_number"], item["notes"], item["required"], item["licence_only"]), ("v1.2", "1.2.0", "Smoother", False, False))
        self.assertTrue(item["updated_at"])
        self.assertEqual((item["download"]["available"], item["download"]["kind"], item["download"]["size"]), (True, "exe", len(setup)))
        self.assertEqual(item["download"]["sha256"], hashlib.sha256(setup).hexdigest(), "RotaHub can check the file it downloads")
        self.assertNotIn("FeedAppSetup", json.dumps(item), "the installer's address stays out of the feed")
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/hub/products", query="since=" + feed["revision"])), {"revision": feed["revision"], "unchanged": True})
        self.ok(self.w.cgi("POST", "/api/save-product", product, token=t))  # saved again, nothing new
        self.assertTrue(self.ok(self.w.cgi("GET", "/api/hub/products", query="since=" + feed["revision"])).get("unchanged"))
        # a new version: RotaHub sees a new revision with the new number, notes and "required"
        self.ok(self.w.cgi("POST", "/api/save-product", {**product, "version": "1.3.0", "update_required": True,
                                                          "changelog": [{"version": "1.3.0", "notes": "Required fix"}] + product["changelog"]}, token=t))
        feed2 = self.ok(self.w.cgi("GET", "/api/hub/products", query="since=" + feed["revision"]))
        item = next(p for p in feed2["products"] if p["id"] == "feed-app")
        self.assertEqual((item["version_number"], item["notes"], item["required"]), ("1.3.0", "Required fix", True))
        self.assertEqual(self.ok(self.w.cgi("POST", "/api/download/feed-app", headers={"User-Agent": "RotaHub/0.2.0"}))["url"], f["url"])
        self.ok(self.w.cgi("DELETE", "/api/admin/files/FeedAppSetup.exe", token=t))
        self.assertFalse(os.path.exists(os.path.join(self.w.public, "files", "FeedAppSetup.exe.sha256")))
        self.assertEqual(next(p for p in self.ok(self.w.cgi("GET", "/api/hub/products"))["products"] if p["id"] == "feed-app")["download"]["sha256"], "")
        self.ok(self.w.cgi("DELETE", "/api/products/feed-app", token=t))

    def test_downloads_through_rotahub_and_the_apps_look(self):
        t = self.owner_token
        self.ok(self.w.cgi("POST", "/api/save-product", {"name": "Hub Product", "id": "hub-product", "status": "published",
                                                         "download_url": "https://rotaidem.test/files/HubProductSetup.exe"}, token=t))
        self.assertEqual(self.w.cgi("POST", "/api/admin/hub", {"downloads": {"rotahub_url": "ftp://x/RotaHub.exe"}}, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", "/api/admin/hub", {"look": {"login_image": "javascript:alert(1)"}}, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", "/api/admin/hub", {"look": {"login_title": "x" * 61}}, token=t).status, 400)
        saved = self.ok(self.w.cgi("POST", "/api/admin/hub", {
            "downloads": {"rotahub_url": "/files/RotaHub.exe?v=abc123"},
            "look": {"accent": "mint", "login_image": "/media/3.jpg", "library_image": "https://cdn.example.com/banner.jpg", "login_title": "Hello pilot"},
            "products": [{"id": "hub-product", "image": "/img/hero.jpg"}],
            "changelog": [{"version": "0.2.0", "date": "2026-10-26", "notes": "Messages from the website"}, {"version": "", "notes": "dropped"}]}, token=t))
        try:
            self.assertEqual(self.ok(self.w.cgi("GET", "/api/hub"))["changelog"], [{"version": "0.2.0", "date": "2026-10-26", "notes": "Messages from the website"}])
            self.assertEqual((saved["look"]["accent"], saved["look"]["accent_color"], saved["look"]["login_title"]), ("mint", "#4cc38a", "Hello pilot"))
            self.assertEqual(self.ok(self.w.cgi("GET", "/api/hub"))["products"][0]["image"], "/img/hero.jpg")
            # visitors get RotaHub, RotaHub gets the installer
            r = self.ok(self.w.cgi("POST", "/api/download/hub-product"))
            self.assertEqual((r["hub"], r["url"], r["product"]), (True, "/files/RotaHub.exe?v=abc123", "Hub Product"))
            r = self.ok(self.w.cgi("POST", "/api/download/hub-product", headers={"User-Agent": "RotaHub/0.2.0 (Windows NT 10.0)"}))
            self.assertEqual(r, {"url": "https://rotaidem.test/files/HubProductSetup.exe"})
            listed = next(p for p in self.ok(self.w.cgi("GET", "/api/products"))["products"] if p["id"] == "hub-product")
            self.assertEqual((listed["download_url"], listed["has_download"]), ("", True), "the installer link stays out of the website")
            staff_view = next(p for p in self.ok(self.w.cgi("GET", "/api/products", token=t))["products"] if p["id"] == "hub-product")
            self.assertEqual(staff_view["download_url"], "https://rotaidem.test/files/HubProductSetup.exe")
            with open(os.path.join(self.w.public, "site-settings.js")) as fh:
                self.assertIn('"rotahub_url": "/files/RotaHub.exe?v=abc123"', fh.read(), "the RotaHub page learns the link at once")
        finally:
            self.ok(self.w.cgi("POST", "/api/admin/hub", {}, token=t))
        r = self.ok(self.w.cgi("POST", "/api/download/hub-product"))
        self.assertEqual((r["hub"], r["url"]), (True, ""), "no RotaHub link yet: the RotaHub page says it is not ready")

    def test_rotahub_settings_and_stats(self):
        t = self.owner_token
        public = self.ok(self.w.cgi("GET", "/api/hub"))
        self.assertEqual((public["message"]["enabled"], public["min_version"], public["products"]), (False, "", []))
        bad = [{"message": {"enabled": True, "text": ""}}, {"min_version": "two"}, {"help_url": "javascript:alert(1)"},
               {"products": [{"id": "heavypro", "licence_store": "..\\\\evil"}]}, {"products": [{"id": "heavypro", "manifest_url": "http://plain.example/x.jws"}]},
               {"products": [{"id": "heavypro", "key_prefix": "toolongprefix"}]}]
        for b in bad:
            self.assertEqual(self.w.cgi("POST", "/api/admin/hub", b, token=t).status, 400, b)
        saved = self.ok(self.w.cgi("POST", "/api/admin/hub", {
            "message": {"enabled": True, "text": "HeavyPro 0.6 is out", "level": "warning", "link": "/downloads"},
            "min_version": "0.2.0", "help_url": "https://discord.gg/rotaidem",
            "products": [{"id": "heavypro", "featured": True, "licence_ids": "heavypro, rotaidem-heavypro", "key_prefix": "hpro"},
                         {"id": "secret-tool", "hidden": True}, {"id": "plain"}]}, token=t))
        try:
            public = self.ok(self.w.cgi("GET", "/api/hub"))
            self.assertEqual(public["message"], {"enabled": True, "text": "HeavyPro 0.6 is out", "level": "warning", "link": "/downloads"})
            self.assertEqual(public["min_version"], "0.2.0")
            self.assertEqual([p["id"] for p in public["products"]], ["heavypro", "secret-tool"], "entries with nothing set are left out")
            self.assertEqual((public["products"][0]["licence_ids"], public["products"][0]["key_prefix"]), (["heavypro", "rotaidem-heavypro"], "HPRO"))
            self.assertNotIn("stats", public)
            # app logins: counted per version, and shown as RotaHub on the account page
            customer, email = self.customer("hubuser")
            app = self.ok(self.w.cgi("POST", "/api/login", {"email": email, "password": "customer-pass-1"}, headers={"User-Agent": "RotaHub/0.1.0 (Windows NT 10.0)"}))["token"]
            devices = [s["device"] for s in self.ok(self.w.cgi("GET", "/api/account/sessions", token=customer))["sessions"]]
            self.assertIn("RotaHub on Windows", devices)
            stats = self.ok(self.w.cgi("GET", "/api/admin/hub", token=t))["stats"]
            self.assertGreaterEqual(stats["people"], 1)
            self.assertIn({"version": "0.1.0", "people": 1}, stats["versions"])
            self.assertGreaterEqual(stats["outdated"], 1, "0.1.0 is below the lowest version 0.2.0")
            self.assertEqual(self.w.cgi("POST", "/api/admin/hub", {}, token=app).status, 403)
            self.assertEqual(self.w.cgi("GET", "/api/admin/hub", token=app).status, 403)
        finally:
            self.ok(self.w.cgi("POST", "/api/admin/hub", {}, token=t))

    def test_user_notes_and_scheduled_posts(self):
        t = self.owner_token
        _, email = self.customer("noted")
        user = next(u for u in self.ok(self.w.cgi("GET", "/api/admin/customers", token=t))["customers"] if u["email"] == email)
        self.ok(self.w.cgi("POST", f"/api/admin/users/{user['id']}/note", {"note": "Beta tester, Thrustmaster stick"}, token=t))
        self.assertEqual(self.ok(self.w.cgi("GET", f"/api/admin/users/{user['id']}", token=t))["user"]["admin_note"], "Beta tester, Thrustmaster stick")
        customer, _ = self.customer("nosy")
        self.assertNotIn("admin_note", self.ok(self.w.cgi("GET", "/api/me", token=customer))["user"])
        self.assertEqual(self.w.cgi("POST", f"/api/admin/users/{user['id']}/note", {"note": "x"}, token=customer).status, 403)
        # a post scheduled for later is not on the site yet; one whose time has come is
        self.ok(self.w.cgi("POST", "/api/newsletter", {"title": "Future post", "content": "Soon", "status": "scheduled", "publish_at": "2099-01-01T09:00:00.000Z"}, token=t))
        self.assertNotIn("Future post", [p["title"] for p in self.ok(self.w.cgi("GET", "/api/newsletter"))["posts"]])
        post = next(p for p in self.ok(self.w.cgi("GET", "/api/admin/posts", token=t))["posts"] if p["title"] == "Future post")
        self.assertEqual((post["status"], post["publish_at"], post["date"]), ("scheduled", "2099-01-01 09:00:00", "January 1, 2099"))
        self.assertEqual(self.w.cgi("POST", "/api/newsletter", {"title": "Late", "content": "x", "status": "scheduled", "publish_at": "2001-01-01T09:00:00.000Z"}, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", "/api/newsletter", {"title": "No time", "content": "x", "status": "scheduled"}, token=t).status, 400)
        db_file = os.path.join(self.w.data, "rotaidem.db")
        if os.path.exists(db_file):  # move the time into the past, as if it had passed
            import sqlite3
            conn = sqlite3.connect(db_file)
            conn.execute("UPDATE posts SET publish_at = '2001-01-01 09:00:00' WHERE title = 'Future post'")
            conn.commit()
            conn.close()
            self.assertIn("Future post", [p["title"] for p in self.ok(self.w.cgi("GET", "/api/newsletter"))["posts"]])

    def test_page_texts(self):
        t = self.owner_token
        key = "index.welcome-to-rotaidem"
        r = self.ok(self.w.cgi("POST", "/api/admin/settings", {"texts": {key: "Hello pilots\r\nof **MSFS**", "about.who-we-are": "Us"}}, token=t))
        self.assertEqual(r["texts"][key], "Hello pilots\nof **MSFS**")
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/site"))["texts"], {key: "Hello pilots\nof **MSFS**", "about.who-we-are": "Us"})
        r = self.ok(self.w.cgi("POST", "/api/admin/settings", {"texts": {"about.who-we-are": None, key: ""}}, token=t))
        self.assertEqual(r["texts"], {})
        for bad in ({"<script>": "x"}, {"index": "x"}, {key: "x" * 5001}):
            self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"texts": bad}, token=t).status, 400, bad)
        customer, _ = self.customer("texty")
        self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"texts": {key: "hacked"}}, token=customer).status, 403)

    def test_page_text_markers_are_unique_and_valid(self):
        import glob
        import re
        seen = {}
        for path in glob.glob(os.path.join(REPO, "cloudflare", "public", "**", "*.html"), recursive=True):
            with open(path, encoding="utf-8") as fh:
                for key in re.findall(r'data-text="([^"]+)"', fh.read()):
                    self.assertRegex(key, r"^[a-z0-9-]{1,40}\.[a-z0-9-]{1,60}$")
                    self.assertNotIn(key, seen, f"{key} is used in {seen.get(key)} and {path}")
                    seen[key] = path
        self.assertGreater(len(seen), 100)

    def test_admin_password(self):
        t = self.owner_token
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/admin/gate", token=t)), {"enabled": False, "unlocked": True, "can_change": True})
        self.assertEqual(self.w.cgi("POST", "/api/admin/gate", {"new_password": "short"}, token=t).status, 400)
        self.assertEqual(self.w.cgi("POST", "/api/admin/gate", {"new_password": OWNER[2]}, token=t).status, 400)  # not the login password
        unlock = self.ok(self.w.cgi("POST", "/api/admin/gate", {"new_password": "gate-pass-123"}, token=t))["unlock"]
        try:
            # Without the unlock code every admin request asks for the admin password.
            r = self.w.cgi("GET", "/api/admin/overview", token=t)
            self.assertEqual((r.status, r.json().get("locked")), (403, True))
            self.assertEqual(self.w.cgi("POST", "/api/admin/settings", {"texts": {}}, token=t).json().get("locked"), True)
            self.assertEqual(self.w.cgi("GET", "/api/admin/overview", token=t, headers={"X-Rotaidem-Admin": "1.abc"}).status, 403)
            self.ok(self.w.cgi("GET", "/api/admin/overview", token=t, headers={"X-Rotaidem-Admin": unlock}))
            # Customers still get the plain answer, the shop's licence tool still works, public pages still work.
            customer, _ = self.customer("gatecustomer")
            self.assertNotIn("locked", self.w.cgi("GET", "/api/admin/overview", token=customer).json())
            self.ok(self.w.cgi("POST", "/api/licenses/import", {"product_id": "heavypro", "licenses": []}, headers={"X-License-Api-Key": "generator-key-0123456789"}))
            self.ok(self.w.cgi("GET", "/api/site"))
            self.assertEqual(self.ok(self.w.cgi("GET", "/api/me", token=t))["user"]["is_admin"], True)
            # Unlocking.
            self.assertEqual(self.w.cgi("POST", "/api/admin/unlock", {"password": "wrong-one"}, token=t).status, 401)
            fresh = self.ok(self.w.cgi("POST", "/api/admin/unlock", {"password": "gate-pass-123"}, token=t))["unlock"]
            self.ok(self.w.cgi("GET", "/api/admin/customers", token=t, headers={"X-Rotaidem-Admin": fresh}))
            self.assertEqual(self.w.cgi("POST", "/api/admin/unlock", {"password": "gate-pass-123"}, token=customer).status, 403)
            # A new login (new session) needs a new unlock.
            t2 = self.owner_token
            self.assertEqual(self.w.cgi("GET", "/api/admin/overview", token=t2, headers={"X-Rotaidem-Admin": fresh}).status, 403)
            fresh = self.ok(self.w.cgi("POST", "/api/admin/unlock", {"password": "gate-pass-123"}, token=t2))["unlock"]
            h = {"X-Rotaidem-Admin": fresh}
            self.assertEqual(self.ok(self.w.cgi("GET", "/api/admin/gate", token=t2, headers=h))["unlocked"], True)
            # Changing it needs the current one and makes old unlock codes useless.
            self.assertEqual(self.w.cgi("POST", "/api/admin/gate", {"current_password": "nope", "new_password": "gate-pass-456"}, token=t2, headers=h).status, 400)
            newer = self.ok(self.w.cgi("POST", "/api/admin/gate", {"current_password": "gate-pass-123", "new_password": "gate-pass-456"}, token=t2, headers=h))["unlock"]
            self.assertEqual(self.w.cgi("GET", "/api/admin/overview", token=t2, headers=h).status, 403)
            self.ok(self.w.cgi("GET", "/api/admin/overview", token=t2, headers={"X-Rotaidem-Admin": newer}))
            # Too many wrong attempts lock the unlock form for a while.
            statuses = [self.w.cgi("POST", "/api/admin/unlock", {"password": "guess"}, token=t2).status for _ in range(6)]
            self.assertEqual(statuses[-1], 429)
            logs = [l["action"] for l in self.ok(self.w.cgi("GET", "/api/admin/audit", token=t2, headers={"X-Rotaidem-Admin": newer}))["logs"]]
            self.assertIn("admin_unlock_failed", logs)
            self.assertIn("admin_password", logs)
        finally:
            out = self.w.setup_cmd("admin-password", stdin="\n")  # Enter = remove (the SSH way back in)
        self.assertIn("removed", out)
        self.ok(self.w.cgi("GET", "/api/admin/overview", token=self.owner_token))

    def test_fetch_installer(self):
        files = {"/HeavyProSetup.exe": b"MZ" + os.urandom(300 * 1024), "/broken.exe": b"<html>Not Found</html>" * 10000}

        class Files(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = files.get(self.path)
                self.send_response(200 if data else 404)
                self.send_header("Content-Length", str(len(data or b"")))
                self.end_headers()
                self.wfile.write(data or b"")

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Files)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            t = self.owner_token
            self.ok(self.w.cgi("POST", "/api/save-product", {"id": "heavypro", "name": "HeavyPro", "status": "published"}, token=t))
            code, out = self.w.setup_run("fetch-installer", base + "/missing.exe")
            self.assertEqual(code, 1, out)
            self.assertIn("404", out)
            code, out = self.w.setup_run("fetch-installer", base + "/broken.exe")
            self.assertIn("not an installer", out)
            self.assertFalse(os.path.exists(os.path.join(self.w.public, "files", "broken.exe")))
            self.assertEqual(self.w.setup_run("fetch-installer", "ftp://example.com/x.exe")[0], 1)
            out = self.w.setup_cmd("fetch-installer", base + "/HeavyProSetup.exe")
        finally:
            server.shutdown()
        import hashlib
        import zipfile
        folder = os.path.join(self.w.public, "files")
        with open(os.path.join(folder, "HeavyProSetup.exe"), "rb") as fh:
            self.assertEqual(fh.read(), files["/HeavyProSetup.exe"])
        sha = hashlib.sha256(files["/HeavyProSetup.exe"]).hexdigest()
        with open(os.path.join(folder, "HeavyProSetup.exe.sha256")) as fh:
            self.assertEqual(fh.read(), f"{sha}  HeavyProSetup.exe\n")
        with zipfile.ZipFile(os.path.join(folder, "HeavyProSetup.zip")) as z:
            self.assertEqual(z.read("HeavyProSetup.exe"), files["/HeavyProSetup.exe"])
        for name in os.listdir(folder):
            self.assertEqual(os.stat(os.path.join(folder, name)).st_mode & 0o777, 0o644, name)
        self.assertEqual(os.listdir(self.w.data).count("HeavyProSetup.exe.download"), 0)
        products = {p["id"]: p for p in self.ok(self.w.cgi("GET", "/api/products", token=self.owner_token))["products"]}
        self.assertEqual(products["heavypro"]["download_url"], f"https://rotaidem.test/files/HeavyProSetup.exe?v={sha[:10]}")
        self.assertIn("now point to", out)

    def test_email_test_command(self):
        self.resend.requests.clear()
        out = self.w.setup_cmd("email-test", "me@example.com")
        self.assertIn("Resend accepted the e-mail to me@example.com", out)
        self.assertNotIn("re_test", out.replace("starts with 're_'", ""), "the key itself is never printed")
        self.assertEqual(self.resend.requests[-1]["body"][0]["to"], ["me@example.com"])
        self.resend.fail_with = (401, {"message": "API key is invalid"})
        try:
            out = self.w.setup_cmd("email-test", "me@example.com")
        finally:
            self.resend.fail_with = None
        self.assertIn("Resend rejected the API key", out)

    # ---------- backup, import, explicit catalogue reset ----------

    def test_backup_import_and_reset_catalog(self):
        t = self.owner_token
        self.ok(self.w.cgi("POST", "/api/save-product", {"name": "Backup Test", "status": "published"}, token=t))
        backup = self.w.cgi("GET", "/api/admin/backup", token=t)
        self.assertEqual(backup.status, 200)
        data = json.loads(backup.body)
        for row in data["tables"]["users"]:  # make it look like Render's PostgreSQL
            row["created_at"] = str(row["created_at"]).replace(" ", "T") + ".123456+00"
        path = os.path.join(self.w.root, "backup.json")
        with open(path, "w") as fh:
            json.dump(data, fh)

        out = self.w.setup_cmd("reset-catalog", stdin="no\n")
        self.assertIn("Cancelled", out)
        self.assertTrue(self.ok(self.w.cgi("GET", "/api/products"))["products"])
        out = self.w.setup_cmd("reset-catalog", stdin="YES\n")
        self.assertIn("A copy was saved", out)
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/products"))["products"], [])
        copies = [f for f in os.listdir(self.w.data) if f.startswith("products-before-reset-")]
        self.assertEqual(os.stat(os.path.join(self.w.data, copies[0])).st_mode & 0o777, 0o600)

        self.assertIn("Cancelled", self.w.setup_cmd("import", path, stdin="no\n"))
        self.assertIn("Import finished", self.w.setup_cmd("import", path, stdin="YES\n"))
        names = [p["name"] for p in self.ok(self.w.cgi("GET", "/api/products"))["products"]]
        self.assertIn("Backup Test", names)
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/me", token=self.owner_token))["user"]["role"], "Owner")


    # ---------- STRATO's FastCGI launcher (imports the script, calls `application`) ----------

    def test_strato_fastcgi_mode(self):
        login = self.w.wsgi([{"method": "POST", "path": "/api/login", "body": {"email": OWNER[0], "password": OWNER[2]}}])[0]
        self.assertEqual(login["status"], 200, login["body"])
        token = json.loads(login["body"])["token"]
        with open(IMAGE, "rb") as fh:
            image = "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()
        replies = self.w.wsgi([
            {"path": "/health"},
            {"path": "/api/site", "no_path_info": True},  # PATH_INFO left empty: taken from REQUEST_URI
            {"path": "/api/me", "headers": {"Authorization": "Bearer " + token}, "redirect_auth": True},
            {"path": "/api/me", "headers": {"X-Rotaidem-Token": token}},
            {"path": "/api/admin/overview", "headers": {"Authorization": "Bearer " + token}},
            {"method": "POST", "path": "/api/admin/media", "headers": {"Authorization": "Bearer " + token},
             "body": {"filename": "x.jpg", "mime": "image/jpeg", "data": image}},
            {"path": "/api/admin/overview"},  # no token: refused
            {"path": "/api/nope"},
        ])
        self.assertEqual([r["status"] for r in replies], [200, 200, 200, 200, 200, 200, 403, 404], [r["body"][:120] for r in replies])
        self.assertEqual(json.loads(replies[0]["body"]), {"ok": True, "database": "ok"})
        self.assertIn("announcement", replies[1]["body"])
        self.assertEqual(json.loads(replies[2]["body"])["user"]["role"], "Owner")
        media_url = json.loads(replies[5]["body"])["url"]
        served = self.w.wsgi([{"path": media_url}])[0]
        self.assertEqual((served["status"], served["headers"]["content-type"]), (200, "image/jpeg"))
        check = self.w.wsgi([{"path": "/cgi-bin/rotaidem-check.py", "query": "key=checkkey123"},
                             {"path": "/cgi-bin/rotaidem-check.py", "query": "key=wrong"},
                             {"path": "/cgi-bin/rotaidem-check.py", "query": "key=checkkey123&echo=1",
                              "headers": {"Authorization": "Bearer x"}, "redirect_auth": True}], script="rotaidem-check.py")
        self.assertEqual([c["status"] for c in check], [200, 404, 200])
        self.assertIn("Database can be read and written", check[0]["body"])
        self.assertIn("FastCGI (application)", check[0]["body"])
        self.assertNotIn('class="bad"', check[0]["body"].split("From your browser")[0])
        self.assertTrue(json.loads(check[2]["body"])["REDIRECT_HTTP_AUTHORIZATION"])

    def test_strato_fastcgi_process_with_an_older_copy_loaded(self):
        """A FastCGI process that ran an older version still holds the old cgi_app and server modules.
        Loading the new entry script in that process must use the new code (no restart needed)."""
        code = f"""
import json, sys
sys.path.insert(0, {os.path.join(self.w.app, "strato")!r})
import cgi_app, wsgi_probe
del cgi_app.wsgi_app          # what an older cgi_app looked like: no wsgi_app
cgi_app.prepare({self.w.app!r}, {os.path.join(self.w.data, "config.env")!r})
import server
server.SCHEMA_VERSION = "old"  # mark the server module loaded before the update (reload redefines it)
script = {os.path.join(self.w.public, "cgi-bin", "rotaidem.py")!r}
app = wsgi_probe.load(script)
print(json.dumps([wsgi_probe.call(app, script, {{"path": "/health"}})["status"], sys.modules["server"].SCHEMA_VERSION == "old"]))
"""
        p = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), [200, False])

    def test_site_sends_the_token_in_its_own_header(self):
        for name in ("admin.html", "account.html", "auth.js"):
            with open(os.path.join(self.w.public, name)) as fh:
                text = fh.read()
            self.assertIn("X-Rotaidem-Token", text, name)
            self.assertNotIn("Authorization:", text, name)

    def test_strato_fastcgi_picks_up_changed_settings(self):
        """The FastCGI process stays loaded: an edited config.env must apply without a restart."""
        cfg = os.path.join(self.w.data, "config.env")
        with open(cfg, "rb") as fh:
            original = fh.read()
        code = f"""
import importlib.machinery, importlib.util, io, json, os, sys, time
sys.path.insert(0, {os.path.join(self.w.app, "strato")!r})
import wsgi_probe
script = {os.path.join(self.w.public, "cgi-bin", "rotaidem.py")!r}
app = wsgi_probe.load(script)
req = lambda key: wsgi_probe.call(app, script, {{"method": "POST", "path": "/api/licenses/import",
    "headers": {{"X-License-Api-Key": key}}, "body": {{"licenses": []}}}})["status"]
before = (req("generator-key-0123456789"), req("a-brand-new-generator-key"))
time.sleep(0.05)
text = open({cfg!r}).read().replace("LICENSE_API_KEY=generator-key-0123456789", "LICENSE_API_KEY=a-brand-new-generator-key")
open({cfg!r}, "w").write(text)
after = (req("generator-key-0123456789"), req("a-brand-new-generator-key"))
print(json.dumps([before, after]))
"""
        try:
            p = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout), [[200, 403], [403, 200]])
        finally:
            with open(cfg, "wb") as fh:
                fh.write(original)
            os.chmod(cfg, 0o600)

    def test_strato_fastcgi_startup_failure_is_generic(self):
        vendor = os.path.join(self.w.app, "strato", "vendor")
        os.rename(vendor, vendor + ".away")
        try:
            # Importing the script must still work (the launcher needs `application`); requests get a plain 503.
            replies = self.w.wsgi([{"path": "/api/site"}, {"path": "/health"}], flags=("-S",))
        finally:
            os.rename(vendor + ".away", vendor)
        for r in replies:
            self.assertEqual(r["status"], 503)
            self.assertNotIn("Traceback", r["body"])
            self.assertNotIn(self.w.app, r["body"])
        # The next request after the problem is fixed works without restarting anything.
        self.assertEqual(self.w.wsgi([{"path": "/health"}])[0]["status"], 200)

    def test_entry_scripts_offer_both_ways(self):
        for name, wsgi_target, cgi_target in (("rotaidem.py", "wsgi_app", "run"), ("rotaidem-check.py", "wsgi_check", "run_check")):
            with open(os.path.join(self.w.public, "cgi-bin", name)) as fh:
                text = fh.read()
            self.assertIn(f"application = cgi_app.{wsgi_target}(", text)
            self.assertIn(f'if __name__ == "__main__":\n    cgi_app.{cgi_target}(', text)

    # ---------- deploy safety ----------

    def test_refuses_unsafe_website_folders(self):
        before = sorted(os.listdir(self.w.root))
        for target in ("/", self.w.root, "rotaidem", "rotaidem/cloudflare/public", "rotaidem-data", "rotaidem-data/site"):
            code, out = self.w.setup_run("deploy", "--public", target)
            self.assertEqual(code, 2, f"{target}: {out}")
            self.assertIn("Stopped:", out)
        self.assertEqual(sorted(os.listdir(self.w.root)), before)
        for folder in (self.w.root, self.w.app, self.w.data):
            self.assertFalse(os.path.exists(os.path.join(folder, "index.html")), folder)

    def test_redeploy_keeps_data_and_settings(self):
        t = self.owner_token
        self.ok(self.w.cgi("POST", "/api/save-product", {"name": "Redeploy Test", "status": "published"}, token=t))
        ctoken, _email = self.customer("redeploy1")
        with open(os.path.join(self.w.data, "config.env"), "rb") as fh:
            config_before = fh.read()
        with open(os.path.join(self.w.public, "rotaidem-deploy.txt")) as fh:
            id_before = fh.read()
        for _ in range(2):
            out = self.w.setup_cmd("deploy")
            self.assertIn("rotaidem-public", out)
        with open(os.path.join(self.w.data, "config.env"), "rb") as fh:
            self.assertEqual(fh.read(), config_before, "deploy must never rewrite the settings")
        with open(os.path.join(self.w.public, "rotaidem-deploy.txt")) as fh:
            self.assertNotEqual(fh.read(), id_before)
        self.assertIn("Redeploy Test", [p["name"] for p in self.ok(self.w.cgi("GET", "/api/products"))["products"]])
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/me", token=ctoken))["user"]["name"], "redeploy1")

    def test_moves_the_old_root_layout_without_touching_it(self):
        # What the earlier version produced: website files straight in the webspace root.
        with open(os.path.join(self.w.root, "index.html"), "w") as fh:
            fh.write("OLD COPY")
        legacy = os.path.join(self.w.app, "strato", "installed.json")
        with open(legacy, "w") as fh:
            json.dump({"public_dir": self.w.root, "data_dir": self.w.data}, fh)
        moved = os.path.join(self.w.data, "installed.json")
        os.rename(moved, moved + ".hidden")
        try:
            users_before = len(self.ok(self.w.cgi("GET", "/api/admin/customers", token=self.owner_token))["customers"])
            out = self.w.setup_cmd("deploy")
            self.assertIn("Nothing in", out)
            with open(os.path.join(self.w.root, "index.html")) as fh:
                self.assertEqual(fh.read(), "OLD COPY", "the old copy is left alone")
            with open(moved) as fh:
                self.assertEqual(json.load(fh)["public_dir"], self.w.public)
            self.assertEqual(len(self.ok(self.w.cgi("GET", "/api/admin/customers", token=self.owner_token))["customers"]), users_before)
        finally:
            os.remove(os.path.join(self.w.root, "index.html"))
            os.remove(legacy)
            if os.path.exists(moved + ".hidden"):
                os.remove(moved + ".hidden")

    def test_retires_old_rules_in_the_webspace_root(self):
        root_ht = os.path.join(self.w.root, ".htaccess")
        with open(root_ht, "w") as fh:
            fh.write("# Rotaidem (written by rotaidem/strato/setup.py)\nOptions -Indexes\n")
        code, out = self.w.setup_run("diagnose", "--offline")
        self.assertEqual(code, 1)
        self.assertIn("old Rotaidem rules", out)
        out = self.w.setup_cmd("deploy")
        self.assertFalse(os.path.exists(root_ht))
        self.assertTrue(os.path.exists(root_ht + ".disabled-by-rotaidem"), out)
        os.remove(root_ht + ".disabled-by-rotaidem")
        with open(root_ht, "w") as fh:
            fh.write("AddDefaultCharset UTF-8\n")  # someone else's file: never touched
        try:
            out = self.w.setup_cmd("deploy")
            with open(root_ht) as fh:
                self.assertEqual(fh.read(), "AddDefaultCharset UTF-8\n")
            self.assertIn("was not written by setup.py", out)
        finally:
            os.remove(root_ht)

    def test_diagnose_offline_is_read_only(self):
        def snapshot():
            modes = {}
            for root, dirs, files in os.walk(self.w.data):
                for name in dirs + files:
                    path = os.path.join(root, name)
                    modes[path] = (os.stat(path).st_mode & 0o777, os.path.getsize(path) if name == "config.env" else 0)
            return modes
        before = snapshot()
        code, out = self.w.setup_run("diagnose", "--offline")
        self.assertEqual(code, 0, out)
        self.assertNotIn("[FAIL]", out)
        self.assertIn("choose 'rotaidem-public' as the domain target", out)
        self.assertIn("/health -> 200", out)
        self.assertIn("Program test, STRATO way", out)
        self.assertIn("Database (read-only)", out)
        after = snapshot()
        self.assertEqual({k: v for k, v in after.items() if "logs" not in k}, {k: v for k, v in before.items() if "logs" not in k})

    def test_diagnose_finds_a_broken_cgi_script(self):
        script = os.path.join(self.w.public, "cgi-bin", "rotaidem.py")
        os.chmod(script, 0o644)
        try:
            code, out = self.w.setup_run("diagnose", "--offline")
        finally:
            os.chmod(script, 0o755)
        self.assertEqual(code, 1)
        self.assertIn("cgi-bin/rotaidem.py has mode 644; it must be 755", out)

    def test_backup_command(self):
        out = self.w.setup_cmd("backup")
        backups = os.path.join(self.w.root, "rotaidem-backups")
        newest = os.path.join(backups, sorted(d for d in os.listdir(backups) if not d.startswith("."))[-1])
        self.assertEqual(os.stat(backups).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(newest).st_mode & 0o777, 0o700)
        for name in ("rotaidem-data.tar.gz", "database.json"):
            self.assertEqual(os.stat(os.path.join(newest, name)).st_mode & 0o777, 0o600, name)
        with open(os.path.join(backups, ".htaccess")) as fh:
            self.assertIn("Require all denied", fh.read())
        with open(os.path.join(newest, "database.json")) as fh:
            tables = json.load(fh)["tables"]
        self.assertIn(OWNER[0], [u["name"] for u in tables["users"]])
        self.assertIn("shared_profiles", tables)
        self.assertIn("users:", out)
        self.assertIn("Import finished", self.w.setup_cmd("import", os.path.join(newest, "database.json"), stdin="YES\n"))
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/me", token=self.owner_token))["user"]["role"], "Owner")

    # ---------- errors are safe ----------

    def test_health_reports_the_database(self):
        self.assertEqual(self.ok(self.w.cgi("GET", "/health")), {"ok": True, "database": "ok"})
        cfg = os.path.join(self.w.data, "config.env")
        with open(cfg, "rb") as fh:
            original = fh.read()
        broken = original.replace(b"MYSQL_PORT=", b"MYSQL_PORT=1#") if b"MYSQL_PORT=" in original else \
            original.replace(b"DATA_DIR=", b"DATA_DIR=/proc/rotaidem-nope#")
        with open(cfg, "wb") as fh:
            fh.write(broken.replace(b"#", b"\nIGNORED="))
        try:
            r = self.w.cgi("GET", "/health")
            self.assertEqual(r.status, 503)
            self.assertEqual(r.json(), {"ok": False, "database": "unavailable"})
            r = self.w.cgi("GET", "/api/site")
            self.assertEqual(r.status, 503)
            for leak in (b"Traceback", b"/proc", b"127.0.0.1", b"password"):
                self.assertNotIn(leak, r.body)
        finally:
            with open(cfg, "wb") as fh:
                fh.write(original)

    def test_startup_failure_shows_no_details(self):
        vendor = os.path.join(self.w.app, "strato", "vendor")
        os.rename(vendor, vendor + ".away")
        try:
            r = self.w.cgi("GET", "/api/site", flags=("-S",))  # Flask cannot be imported
        finally:
            os.rename(vendor + ".away", vendor)
        self.assertEqual(r.status, 503)
        self.assertEqual(r.json(), {"error": "The service is temporarily unavailable. Please try again later."})
        self.assertNotIn(self.w.app.encode(), r.body)
        log = os.path.join(self.w.data, "logs", "cgi-error.log")
        self.assertEqual(os.stat(log).st_mode & 0o777, 0o600)
        with open(log) as fh:
            self.assertIn("ModuleNotFoundError", fh.read())


class SQLiteTests(StratoCGITests, unittest.TestCase):
    db_values = {}


for _label, _cfg in mysql_targets():
    globals()[f"MySQLTests_{_label}"] = type(f"MySQLTests_{_label}", (StratoCGITests, unittest.TestCase), {"db_values": _cfg})


if __name__ == "__main__":
    unittest.main()
