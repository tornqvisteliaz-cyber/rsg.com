"""Runs the Rotaidem Flask app (server.py) as a CGI script.

Requests to /api/*, /media/* and /health go to cgi-bin/rotaidem.py (see the .htaccess written by
setup.py). How that file is run depends on the host, and both ways work:
- STRATO's new platform imports it into a FastCGI process it manages and calls its `application`
  (WSGI): see wsgi_app(). Nothing has to be started or kept running by you.
- Classic CGI hosts start it once per request as a program: see run().

Visitors never see a stack trace. When something breaks, they get a short JSON error and the
details go to rotaidem-data/logs/ (private), which `setup.py diagnose` prints.
"""
import os
import sys
import traceback
import urllib.parse
from datetime import datetime, timezone


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


def log_dir(config_path):
    return os.path.join(os.path.dirname(config_path), "logs")


def log_error(config_path, text, env=None):
    """Append to the private error log; also to stderr, which Apache keeps in its error log."""
    env = os.environ if env is None else env
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"[{stamp}] {env.get('REQUEST_METHOD', '')} {env.get('PATH_INFO', '')}\n{text.rstrip()}\n\n"
    sys.stderr.write(entry)
    try:
        os.makedirs(log_dir(config_path), mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(log_dir(config_path), "cgi-error.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(entry)
    except OSError:
        pass  # e.g. the folder is not writable for the web server; stderr still has it


def out(text):
    # Bytes, so non-ASCII text works even when the CGI environment has no UTF-8 locale.
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def fail(message, status="500 Internal Server Error"):
    out(f"Status: {status}\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\n\r\n{message}\n")


def unavailable():
    """What a visitor sees when the program cannot start: no paths, versions or error text."""
    out("Status: 503 Service Unavailable\r\nContent-Type: application/json\r\nCache-Control: no-store\r\n\r\n"
        '{"error": "The service is temporarily unavailable. Please try again later."}\n')


def web_user_report(config_path):
    """Who the web server runs CGI programs as, compared with who owns the settings. No secrets."""
    try:
        st = os.stat(config_path)
    except OSError:
        try:
            st = os.stat(os.path.dirname(config_path))  # a private folder hides the file; its owner is the same
        except OSError as exc:
            return False, f"Cannot read {config_path}: {exc}"
    uid, groups = os.getuid(), set(os.getgroups()) | {os.getgid()}
    if uid == st.st_uid:
        return True, f"The web server runs the program as your own account (user {uid}), so the settings can stay private (600)."
    if st.st_gid in groups:
        return False, (f"The web server runs as user {uid}, the files belong to {st.st_uid}, but you share group {st.st_gid}. "
                       "Run over SSH:  python3 rotaidem/strato/setup.py permissions group")
    return False, (f"The web server runs as user {uid}, the files belong to {st.st_uid}, and you share no group. "
                   "The secrets cannot then be protected from other customers on the server. Do not make the files "
                   "readable for everyone; ask STRATO whether CGI can run as your own account.")


def attach_error_log(app, config_path):
    """Send errors inside requests (Flask logs them) to the same private log."""
    import logging
    try:
        os.makedirs(log_dir(config_path), mode=0o700, exist_ok=True)
        handler = logging.FileHandler(os.path.join(log_dir(config_path), "app-error.log"), encoding="utf-8")
    except OSError:
        return
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    app.logger.addHandler(handler)


def fix_environ(env):
    """Make the request look the same to Flask however Apache handed it over."""
    # Apache does not pass the Authorization header to CGI/FastCGI scripts; the .htaccess copies it
    # into a variable, which mod_rewrite renames to REDIRECT_HTTP_AUTHORIZATION after the rewrite.
    if not env.get("HTTP_AUTHORIZATION"):
        for name in ("REDIRECT_HTTP_AUTHORIZATION", "REDIRECT_REDIRECT_HTTP_AUTHORIZATION"):
            if env.get(name):
                env["HTTP_AUTHORIZATION"] = env[name]
                break
    # Requests arrive as /cgi-bin/rotaidem.py/api/...; the app should see just /api/...
    script = env.get("SCRIPT_NAME") or ""
    path = env.get("PATH_INFO") or ""
    if script and path.startswith(script + "/"):
        path = path[len(script):]
    if not path:  # some FastCGI setups leave PATH_INFO empty: fall back to the address that was asked for
        uri = urllib.parse.unquote((env.get("REQUEST_URI") or "").split("?", 1)[0])
        path = uri[len(script):] if script and uri.startswith(script) else uri
    env["PATH_INFO"] = path or "/"
    env["SCRIPT_NAME"] = ""
    return env


def load_app(app_dir, config_path, env):
    """Import server.py's Flask app, or log why not and return None."""
    os.umask(0o077)  # files the program creates (logs, SQLite journal) stay private
    try:
        prepare(app_dir, config_path)
        if "server" in sys.modules:  # loaded earlier in this process, maybe an older version
            import importlib
            app = importlib.reload(sys.modules["server"]).app
        else:
            from server import app
    except PermissionError:
        log_error(config_path, "The settings file cannot be read.\n" + web_user_report(config_path)[1] + "\n" + traceback.format_exc(), env)
        return None
    except Exception:  # noqa: BLE001 - details to the private log, a plain message to the visitor
        log_error(config_path, "The program could not start.\n" + traceback.format_exc(), env)
        return None
    if not getattr(app, "_rotaidem_log", False):
        attach_error_log(app, config_path)
        app._rotaidem_log = True
    return app


def reload_app(app_dir, config_path, env):
    """Re-read config.env and server.py inside a process that stays loaded (STRATO's FastCGI)."""
    import importlib
    try:
        os.environ.update(load_config(config_path))  # a line deleted from the file stays until the process restarts
        server = importlib.reload(sys.modules["server"])
    except Exception:  # noqa: BLE001
        log_error(config_path, "Reloading after a change failed.\n" + traceback.format_exc(), env)
        return None
    attach_error_log(server.app, config_path)
    server.app._rotaidem_log = True
    return server.app


UNAVAILABLE = b'{"error": "The service is temporarily unavailable. Please try again later."}\n'


def wsgi_app(app_dir, config_path):
    """The `application` STRATO's new platform calls. It does not run cgi-bin/*.py as plain CGI: a
    FastCGI launcher (cm4all, flup) imports the file once, keeps it loaded, and calls its
    `application(environ, start_response)` for every request.
    - The Flask app is loaded on the first request (and retried after a failure), so a broken
      setting never breaks importing the file.
    - Because the process stays loaded, a changed config.env or server.py (after `setup.py deploy`)
      is picked up on the next request instead of whenever STRATO restarts the process."""
    import threading
    state, lock = {}, threading.Lock()
    watched = (config_path, os.path.join(app_dir, "server.py"))

    def stamp():
        out = []
        for path in watched:
            try:
                out.append(os.stat(path).st_mtime_ns)
            except OSError:
                out.append(None)
        return tuple(out)

    def application(environ, start_response):
        env = fix_environ(dict(environ))
        with lock:
            current, app = stamp(), state.get("app")
            if app is not None and state.get("stamp") != current:
                app = reload_app(app_dir, config_path, env)
            if app is None:
                app = load_app(app_dir, config_path, env)
            state["app"], state["stamp"] = app, current
        if app is None:
            start_response("503 Service Unavailable", [("Content-Type", "application/json"), ("Cache-Control", "no-store"),
                                                       ("Content-Length", str(len(UNAVAILABLE)))])
            return [UNAVAILABLE]
        return app(env, start_response)

    return application


def run(app_dir, config_path):
    """Plain CGI (hosts that start the script once per request)."""
    env = fix_environ(os.environ)
    app = load_app(app_dir, config_path, env)
    if app is None:
        unavailable()
        return
    from wsgiref.handlers import CGIHandler
    CGIHandler().run(app)


# ---------- check page: cgi-bin/rotaidem-check.py?key=... ----------

def _html(rows, browser_tests):
    from html import escape
    items = "".join(f'<li class="{"ok" if ok else "bad"}"><b>{escape(name)}</b> {escape(detail)}</li>' for name, ok, detail in rows)
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Rotaidem check</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:760px;margin:32px auto;padding:0 16px;background:#141517;color:#e6e8eb}}
li{{list-style:none;margin:8px 0;padding:10px 14px;background:#1d1f22;border-left:4px solid #777}}li.ok{{border-color:#3fb950}}li.bad{{border-color:#f85149}}
li.ok b::before{{content:"OK  ";color:#3fb950}}li.bad b::before{{content:"FAIL  ";color:#f85149}}ul{{padding:0}}code{{background:#000;padding:2px 5px}}</style></head>
<body><h1>Rotaidem on STRATO: check</h1><p>Everything should be green. If something is red, take a screenshot (crop the address bar: it contains the key).</p>
<h2>On the server</h2><ul>{items}</ul><h2>From your browser</h2><ul id="browser"><li>Testing ...</li></ul>
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
      show(t.name, ok, t.expect === "blocked" ? "(status " + r.status + ", must not be 200)" : "(status " + r.status + ")" + (ok ? "" : " " + (t.help || "")));
    }} catch (e) {{ show(t.name, false, String(e)); }}
  }}
}})();
</script></body></html>"""


def json_dumps(value):
    import json
    return json.dumps(value).replace("</", "<\\/")


def check_page(app_dir, config_path, env):
    """The key-protected check page. Returns (status, content type, body)."""
    import hmac
    import json
    import platform
    from urllib.request import urlopen

    os.umask(0o077)
    query = urllib.parse.parse_qs(env.get("QUERY_STRING", ""))
    try:
        values = load_config(config_path)
    except Exception:  # noqa: BLE001
        # No key can be checked without the settings, so say only what the owner needs: who runs what.
        log_error(config_path, "Check page: settings unreadable.\n" + traceback.format_exc(), env)
        return "503 Service Unavailable", "text/plain; charset=utf-8", "Rotaidem cannot read its settings.\n\n" + web_user_report(config_path)[1] + "\n"
    if not values.get("CHECK_KEY") or not hmac.compare_digest(query.get("key", [""])[0], values["CHECK_KEY"]):
        return "404 Not Found", "text/plain", "Not found\n"
    if query.get("echo"):  # the browser test below: did the login header reach the program?
        seen = {k: bool(env.get(k)) for k in ("HTTP_AUTHORIZATION", "REDIRECT_HTTP_AUTHORIZATION", "HTTP_X_ROTAIDEM_TOKEN")}
        return "200 OK", "application/json", json.dumps(seen)

    mode_name = "FastCGI (application)" if env.get("wsgi.version") else "CGI"
    rows = [("Python", sys.version_info >= (3, 9), f"{platform.python_version()} ({sys.executable}), user {os.getuid()}, {mode_name}")]
    rows.append(("Settings", True, config_path))
    rows.append(("Web server user", *web_user_report(config_path)))
    mode = os.stat(config_path).st_mode & 0o777
    rows.append(("Settings are private", mode & 0o007 == 0, f"mode {mode:o} (should be 600 or 640)"))
    try:
        prepare(app_dir, config_path)
        import flask
        from importlib.metadata import version
        rows.append(("Flask", True, f"{version('flask')} from {os.path.dirname(os.path.dirname(flask.__file__))}"))
        import server
        rows.append(("Program starts", True, "server.py"))
        server.ensure_schema()
        d = server.DB()
        d.run("DELETE FROM meta WHERE key = 'check_write'")
        d.run("INSERT INTO meta (key, value) VALUES ('check_write', ?)", "ok")
        d.run("DELETE FROM meta WHERE key = 'check_write'")
        users = d.first("SELECT COUNT(*) AS n FROM users")["n"]
        d.close()
        kind = "MySQL " + values["MYSQL_HOST"] if values.get("MYSQL_HOST") else "SQLite " + str(server.DATA_DIR)
        rows.append(("Database can be read and written", True, f"{kind}, {users} accounts"))
    except Exception as exc:  # noqa: BLE001
        rows.append(("Program or database", False, f"{type(exc).__name__}: {exc}"))
    if values.get("RESEND_API_KEY"):
        resend = values.get("RESEND_API_URL", "https://api.resend.com")
        try:
            urlopen(resend, timeout=15).close()
            rows.append(("E-mail (Resend) reachable", True, resend))
        except Exception as exc:  # noqa: BLE001
            ok = getattr(exc, "code", None) is not None  # any HTTP answer means the connection works
            rows.append(("E-mail (Resend) reachable", ok, resend if ok else str(exc)))
    else:
        rows.append(("E-mail", True, "No RESEND_API_KEY set: licence e-mails and newsletters are switched off."))

    key = values["CHECK_KEY"]
    browser = [
        {"name": "Backend via /api", "url": "/api/site", "contains": "announcement",
         "help": "/api/ does not reach the program. Check the .htaccess in the website folder."},
        {"name": "Health check (/health)", "url": "/health", "contains": '"database":"ok"',
         "help": "The program runs but the database does not answer."},
        {"name": "Pages without .html (/login)", "url": "/login", "contains": "<html",
         "help": "mod_rewrite seems to be off."},
        {"name": "Login header reaches the program", "url": f"/cgi-bin/rotaidem-check.py?key={key}&echo=1",
         "headers": {"Authorization": "Bearer test", "X-Rotaidem-Token": "test"}, "contains": "true"},
    ]
    public_dir = os.path.dirname(os.path.dirname(os.path.abspath(env.get("SCRIPT_FILENAME", ""))))
    for label, path in (("Settings file is blocked", config_path), ("Program code is blocked", os.path.join(app_dir, "server.py"))):
        rel = os.path.relpath(path, public_dir) if public_dir else ".."
        if not rel.startswith(".."):
            browser.append({"name": label, "url": "/" + rel.replace(os.sep, "/"), "expect": "blocked"})
    return "200 OK", "text/html; charset=utf-8", _html(rows, browser)


def wsgi_check(app_dir, config_path):
    """The check page as `application`, for STRATO's FastCGI launcher."""
    def application(environ, start_response):
        status, ctype, body = check_page(app_dir, config_path, environ)
        data = body.encode("utf-8")
        start_response(status, [("Content-Type", ctype), ("Cache-Control", "no-store"), ("X-Robots-Tag", "noindex"),
                                ("Content-Length", str(len(data)))])
        return [data]
    return application


def run_check(app_dir, config_path):
    """The check page as plain CGI."""
    status, ctype, body = check_page(app_dir, config_path, os.environ)
    out(f"Status: {status}\r\nContent-Type: {ctype}\r\nCache-Control: no-store\r\nX-Robots-Tag: noindex\r\n\r\n{body}")
