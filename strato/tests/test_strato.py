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
    """A temporary STRATO-like webspace: /rotaidem (the upload), /rotaidem-data, site files in /."""

    def __init__(self, db_values, resend_url):
        self.root = tempfile.mkdtemp(prefix="strato-")
        self.app = os.path.join(self.root, "rotaidem")
        os.mkdir(self.app)
        shutil.copy2(os.path.join(REPO, "server.py"), self.app)
        shutil.copytree(os.path.join(REPO, "cloudflare", "public"), os.path.join(self.app, "cloudflare", "public"))
        shutil.copytree(os.path.join(REPO, "strato"), os.path.join(self.app, "strato"),
                        ignore=shutil.ignore_patterns("__pycache__", "installed.json", "tests"))
        self.data = os.path.join(self.root, "rotaidem-data")
        self.values = {"SECRET_KEY": "s" * 64, "CHECK_KEY": "checkkey123", "SITE_URL": "https://rotaidem.test",
                       "LICENSE_API_KEY": "generator-key-0123456789", "RESEND_API_KEY": "re_test", "RESEND_API_URL": resend_url,
                       "MAIL_FROM": "Rotaidem <no-reply@rotaidem.com>", **db_values}
        if "MYSQL_HOST" not in self.values:
            self.values["DATA_DIR"] = self.data
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import setup; "
                "setup.install(sys.argv[2], sys.argv[3], __import__('json').loads(sys.argv[4]), tuple(sys.argv[5:8]))")
        self.setup_out = self.run_python(["-c", code, os.path.join(self.app, "strato"), self.root, self.data,
                                          json.dumps(self.values), *OWNER])

    def run_python(self, args, stdin=b""):
        p = subprocess.run([PY, *args], input=stdin, capture_output=True, cwd=self.root, timeout=120)
        if p.returncode:
            raise AssertionError(p.stdout.decode() + p.stderr.decode())
        return p.stdout.decode()

    def setup_cmd(self, *args, stdin=""):
        return self.run_python([os.path.join(self.app, "strato", "setup.py"), *args], stdin.encode())

    def cgi(self, method, path, body=None, token=None, headers=None, script="rotaidem.py", query="", flags=()):
        script_path = os.path.join(self.root, "cgi-bin", script)
        env = {"PATH": "/usr/bin:/bin", "GATEWAY_INTERFACE": "CGI/1.1", "REQUEST_METHOD": method,
               "SCRIPT_NAME": "/cgi-bin/" + script, "SCRIPT_FILENAME": script_path, "PATH_INFO": path,
               "QUERY_STRING": query, "SERVER_NAME": "rotaidem.test", "SERVER_PORT": "443", "HTTPS": "on",
               "SERVER_PROTOCOL": "HTTP/1.1", "REMOTE_ADDR": "203.0.113.5", "HTTP_HOST": "rotaidem.test"}
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
        if data:
            env.update(CONTENT_TYPE="application/json", CONTENT_LENGTH=str(len(data)))
        if token:
            env["HTTP_AUTHORIZATION"] = "Bearer " + token
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        p = subprocess.run([PY, *flags, script_path], input=data, env=env, capture_output=True, timeout=60)
        return Response(p.stdout, p.stderr)

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

    def customer(self, name):
        email = f"{name}@example.com"
        r = self.w.cgi("POST", "/api/signup", {"name": name, "email": email, "password": "customer-pass-1"})
        return self.ok(r)["token"], email

    # ---------- installation ----------

    def test_install_files_and_permissions(self):
        for name in ("rotaidem.py", "rotaidem-check.py"):
            path = os.path.join(self.w.root, "cgi-bin", name)
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
        with open(os.path.join(self.w.root, ".htaccess")) as fh:
            ht = fh.read()
        self.assertIn("E=HTTP_AUTHORIZATION", ht)
        self.assertIn("cgi-bin/rotaidem.py/$1", ht)
        self.assertIn("RewriteRule ^(rotaidem|rotaidem\\-data)(/|$) - [F,L]", ht)
        for folder in (self.w.app, self.w.data):
            with open(os.path.join(folder, ".htaccess")) as fh:
                self.assertIn("Require all denied", fh.read())
        self.assertEqual(os.stat(os.path.join(self.w.root, "index.html")).st_mode & 0o777, 0o644)

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
        self.assertIn("Databasen kan läsas och skrivas", html)
        server_rows = html.split("Från webbläsaren")[0]
        self.assertNotIn('class="bad"', server_rows, server_rows)
        self.assertIn("ditt eget konto", server_rows)
        self.assertEqual(self.w.cgi("GET", "", script="rotaidem-check.py", query="key=wrong").status, 404)

    # ---------- routing and login ----------

    def test_routing(self):
        self.assertEqual(self.ok(self.w.cgi("GET", "/health")), {"ok": True})
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

        out = self.w.setup_cmd("reset-catalog", stdin="nej\n")
        self.assertIn("Avbrutet", out)
        self.assertTrue(self.ok(self.w.cgi("GET", "/api/products"))["products"])
        out = self.w.setup_cmd("reset-catalog", stdin="JA\n")
        self.assertIn("En kopia sparades", out)
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/products"))["products"], [])
        copies = [f for f in os.listdir(self.w.data) if f.startswith("products-before-reset-")]
        self.assertEqual(os.stat(os.path.join(self.w.data, copies[0])).st_mode & 0o777, 0o600)

        self.assertIn("Avbrutet", self.w.setup_cmd("import", path, stdin="no\n"))
        self.assertIn("Importen är klar", self.w.setup_cmd("import", path, stdin="JA\n"))
        names = [p["name"] for p in self.ok(self.w.cgi("GET", "/api/products"))["products"]]
        self.assertIn("Backup Test", names)
        self.assertEqual(self.ok(self.w.cgi("GET", "/api/me", token=self.owner_token))["user"]["role"], "Owner")


class SQLiteTests(StratoCGITests, unittest.TestCase):
    db_values = {}


for _label, _cfg in mysql_targets():
    globals()[f"MySQLTests_{_label}"] = type(f"MySQLTests_{_label}", (StratoCGITests, unittest.TestCase), {"db_values": _cfg})


if __name__ == "__main__":
    unittest.main()
