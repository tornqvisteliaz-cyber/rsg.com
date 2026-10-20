"""Runs the Rotaidem Flask app (server.py) as a CGI script.

STRATO Webhosting has no long-running processes, so there is no Gunicorn: Apache starts
cgi-bin/rotaidem.py for each request to /api/* or /media/* (see the .htaccess written by
setup.py), and that script calls run() below. Nothing has to be kept running.
"""
import os
import sys
import traceback


def load_config(path):
    """Read KEY=value lines (the file setup.py writes). Lines starting with # are comments."""
    values = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def prepare(app_dir, config_path):
    """Load the configuration and make server.py importable. Shared by the site and setup.py."""
    os.environ.update(load_config(config_path))
    vendor = os.path.join(app_dir, "strato", "vendor")
    for path in (app_dir, vendor):
        if path not in sys.path:
            sys.path.insert(0, path)


def out(text):
    # Bytes, so non-ASCII text works even when the CGI environment has no UTF-8 locale.
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def fail(message):
    out("Status: 500 Internal Server Error\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + message + "\n")


def run(app_dir, config_path):
    try:
        prepare(app_dir, config_path)
        from server import app
    except PermissionError:
        traceback.print_exc()
        fail("Rotaidem: the web server may not read the settings file " + config_path + ".\n"
             "Fix: log in with SSH and run:  python3 rotaidem/strato/setup.py permissions")
        return
    except Exception:  # noqa: BLE001 - show the reason instead of a bare "500"
        traceback.print_exc()
        fail("Rotaidem could not start. Run the check page that setup.py printed, or run:\n"
             "python3 rotaidem/strato/setup.py check\n\n" + traceback.format_exc(limit=3))
        return

    env = os.environ
    # Apache does not hand the Authorization header to CGI scripts; the .htaccess copies it into
    # a variable, which mod_rewrite renames to REDIRECT_HTTP_AUTHORIZATION after the rewrite.
    if not env.get("HTTP_AUTHORIZATION"):
        for name in ("REDIRECT_HTTP_AUTHORIZATION", "REDIRECT_REDIRECT_HTTP_AUTHORIZATION"):
            if env.get(name):
                env["HTTP_AUTHORIZATION"] = env[name]
                break
    # Requests arrive as /cgi-bin/rotaidem.py/api/...; the app should see just /api/...
    env["SCRIPT_NAME"] = ""
    env.setdefault("PATH_INFO", "/")

    from wsgiref.handlers import CGIHandler
    CGIHandler().run(app)


# ---------- check page: cgi-bin/rotaidem-check.py?key=... ----------

def _html(rows, browser_tests):
    from html import escape
    items = "".join(f'<li class="{"ok" if ok else "bad"}"><b>{escape(name)}</b> {escape(detail)}</li>' for name, ok, detail in rows)
    return f"""<!DOCTYPE html><html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Rotaidem kontroll</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:760px;margin:32px auto;padding:0 16px;background:#141517;color:#e6e8eb}}
li{{list-style:none;margin:8px 0;padding:10px 14px;background:#1d1f22;border-left:4px solid #777}}li.ok{{border-color:#3fb950}}li.bad{{border-color:#f85149}}
li.ok b::before{{content:"OK  ";color:#3fb950}}li.bad b::before{{content:"FEL  ";color:#f85149}}ul{{padding:0}}code{{background:#000;padding:2px 5px}}</style></head>
<body><h1>Rotaidem på STRATO: kontroll</h1><p>Allt ska vara grönt. Är något rött, ta en skärmbild och skicka den.</p>
<h2>På servern</h2><ul>{items}</ul><h2>Från webbläsaren</h2><ul id="browser"><li>Testar ...</li></ul>
<script>
const tests = {json_dumps(browser_tests)};
const out = document.getElementById("browser"); out.innerHTML = "";
function show(name, ok, detail) {{ const li = document.createElement("li"); li.className = ok ? "ok" : "bad"; li.innerHTML = "<b></b> "; li.firstChild.textContent = name; li.append(detail); out.append(li); }}
(async () => {{
  for (const t of tests) {{
    try {{
      const r = await fetch(t.url, {{ headers: t.headers || {{}}, cache: "no-store", redirect: "manual" }});
      const text = await r.text();
      let ok = t.expect === "blocked" ? (r.status !== 200) : (r.status === 200 && (!t.contains || text.includes(t.contains)));
      show(t.name, ok, t.expect === "blocked" ? "(svar " + r.status + ", ska inte vara 200)" : "(svar " + r.status + ")" + (ok ? "" : " " + (t.help || "")));
    }} catch (e) {{ show(t.name, false, String(e)); }}
  }}
}})();
</script></body></html>"""


def json_dumps(value):
    import json
    return json.dumps(value).replace("</", "<\\/")


def run_check(app_dir, config_path):
    import hmac
    import json
    import platform
    import urllib.parse
    from urllib.request import urlopen

    query = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
    try:
        values = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        fail(f"Kan inte läsa inställningarna ({exc}).\nKör över SSH:  python3 rotaidem/strato/setup.py permissions")
        return
    if not values.get("CHECK_KEY") or not hmac.compare_digest(query.get("key", [""])[0], values["CHECK_KEY"]):
        out("Status: 404 Not Found\r\nContent-Type: text/plain\r\n\r\nNot found\n")
        return
    if query.get("echo"):  # the browser test below: did the login header reach the program?
        seen = {k: bool(os.environ.get(k)) for k in ("HTTP_AUTHORIZATION", "REDIRECT_HTTP_AUTHORIZATION", "HTTP_X_ROTAIDEM_TOKEN")}
        out("Content-Type: application/json\r\n\r\n" + json.dumps(seen))
        return

    rows = [("Python", sys.version_info >= (3, 8), f"{platform.python_version()} ({sys.executable}), användare {os.getuid()}")]
    rows.append(("Inställningar", True, config_path))
    try:
        prepare(app_dir, config_path)
        import flask
        rows.append(("Flask", True, getattr(flask, "__version__", "")))
        import server
        rows.append(("Programmet startar", True, "server.py"))
        server.ensure_schema()
        d = server.DB()
        d.run("DELETE FROM meta WHERE key = 'check_write'")
        d.run("INSERT INTO meta (key, value) VALUES ('check_write', ?)", "ok")
        d.run("DELETE FROM meta WHERE key = 'check_write'")
        users = d.first("SELECT COUNT(*) AS n FROM users")["n"]
        d.close()
        kind = "MySQL " + values["MYSQL_HOST"] if values.get("MYSQL_HOST") else "SQLite " + str(server.DATA_DIR)
        rows.append(("Databasen kan läsas och skrivas", True, f"{kind}, {users} konton"))
    except Exception as exc:  # noqa: BLE001
        rows.append(("Programmet eller databasen", False, f"{type(exc).__name__}: {exc}"))
    if values.get("RESEND_API_KEY"):
        try:
            urlopen("https://api.resend.com", timeout=15).close()
            rows.append(("E-post (Resend) kan nås", True, "api.resend.com"))
        except Exception as exc:  # noqa: BLE001
            ok = getattr(exc, "code", None) is not None  # any HTTP answer means the connection works
            rows.append(("E-post (Resend) kan nås", ok, "api.resend.com" if ok else str(exc)))
    else:
        rows.append(("E-post", True, "Ingen RESEND_API_KEY angiven: licensmejl och utskick är avstängda."))

    key = values["CHECK_KEY"]
    browser = [
        {"name": "Backend via /api", "url": "/api/site", "contains": "announcement",
         "help": "Adressen /api/ når inte programmet. Kontrollera .htaccess i webbplatsens mapp."},
        {"name": "Sidor utan .html (/login)", "url": "/login", "contains": "<html",
         "help": "mod_rewrite verkar inte vara på."},
        {"name": "Inloggningen når programmet", "url": f"/cgi-bin/rotaidem-check.py?key={key}&echo=1",
         "headers": {"Authorization": "Bearer test", "X-Rotaidem-Token": "test"}, "contains": "true"},
    ]
    public_dir = os.path.dirname(os.path.dirname(os.path.abspath(os.environ.get("SCRIPT_FILENAME", ""))))
    for label, path in (("Inställningsfilen är skyddad", config_path), ("Programkoden är skyddad", os.path.join(app_dir, "server.py"))):
        rel = os.path.relpath(path, public_dir) if public_dir else ".."
        if not rel.startswith(".."):
            browser.append({"name": label, "url": "/" + rel.replace(os.sep, "/"), "expect": "blocked"})
    out("Content-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\nX-Robots-Tag: noindex\r\n\r\n" + _html(rows, browser))
