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
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

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

    def __init__(self, db_values, resend_url):
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
        cls.w = Webspace(cls.db_values, cls.resend.url)

    @classmethod
    def tearDownClass(cls):
        cls.w.remove()
        cls.resend.stop()

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

    def customer(self, name):
        email = f"{name}@example.com"
        r = self.w.cgi("POST", "/api/signup", {"name": name, "email": email, "password": "customer-pass-1"})
        return self.ok(r)["token"], email

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

    def test_signup_sends_a_welcome_email(self):
        self.resend.requests.clear()
        r = self.ok(self.w.cgi("POST", "/api/signup", {"name": "Welcome Wendy", "email": "Wendy@Example.com", "password": "wendy-pass-1"}))
        self.assertEqual(r["email"], "wendy@example.com")
        sent = self.resend.requests[-1]["body"][0]
        self.assertEqual((sent["to"], sent["subject"]), (["wendy@example.com"], "Welcome to Rotaidem"))
        self.assertIn("Welcome Wendy", sent["html"])
        self.assertIn("https://rotaidem.test/account", sent["html"])
        self.assertNotIn("wendy-pass-1", sent["html"])  # never the password
        self.assertEqual(self.w.cgi("POST", "/api/signup", {"name": "X", "email": "not-an-email", "password": "secret-1"}).status, 400)
        self.resend.fail_with = (500, {"message": "down"})
        try:
            r = self.w.cgi("POST", "/api/signup", {"name": "Late Larry", "email": "larry@example.com", "password": "larry-pass-1"})
        finally:
            self.resend.fail_with = None
        self.assertEqual(r.status, 200, "a failing email service never blocks signing up")

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
        products = {p["id"]: p for p in self.ok(self.w.cgi("GET", "/api/products"))["products"]}
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
