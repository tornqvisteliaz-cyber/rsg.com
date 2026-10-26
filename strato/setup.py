"""Installs, updates and diagnoses Rotaidem on STRATO Webhosting (Apache + Python CGI, no server process).

Layout in the webspace (STRATO's new platform: /home/www):

    /home/www/rotaidem/          program source (this repository) - never served
    /home/www/rotaidem-data/     settings (config.env), logs, SQLite file if used - never served
    /home/www/rotaidem-public/   the website - select THIS folder as the domain target at STRATO
    /home/www/rotaidem-backups/  made by the backup command - never served

STRATO's new platform cannot use the webspace root (/home/www) as a domain target, and the program and
data must not sit inside the folder the domain shows, so the website gets its own folder.

Commands (run over SSH in /home/www):

    python3 rotaidem/strato/setup.py deploy       install or update the website; keeps all data
    python3 rotaidem/strato/setup.py diagnose     read-only checks: files, permissions, CGI, database, DNS, HTTP
    python3 rotaidem/strato/setup.py backup       copies the settings and the whole database to rotaidem-backups
    python3 rotaidem/strato/setup.py install      first installation: asks for the database and the admin account
    python3 rotaidem/strato/setup.py password     sets a new password for an account
    python3 rotaidem/strato/setup.py permissions [group]   makes the settings private again
    python3 rotaidem/strato/setup.py import FILE  loads a backup (replaces the database contents; asks first)
    python3 rotaidem/strato/setup.py reset-catalog  removes all products (asks first, keeps a copy)
    python3 rotaidem/strato/setup.py email-test ADDRESS  sends a test e-mail and shows what Resend answers
    python3 rotaidem/strato/setup.py admin-password  sets, changes or removes the extra password of the admin page
    python3 rotaidem/strato/setup.py signing-key     shows the public key HeavyPro uses to check licence answers
    python3 rotaidem/strato/setup.py stripe          sets up payments: the Stripe key and webhook secret (typed, never shown)
    python3 rotaidem/strato/setup.py fetch-installer [URL]  copies the newest HeavyPro installer from GitHub onto
                                                  rotaidem.com/files/ and points the Download buttons at it

Options:  deploy --public FOLDER   use another website folder (relative to the webspace or absolute)
          deploy --cgi-htaccess    also enable CGI with cgi-bin/.htaccess (only if diagnose says CGI does not run)
          diagnose --offline       skip the DNS and HTTP tests;  diagnose DOMAIN   test another domain
"""
import getpass
import hashlib
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

STRATO_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(STRATO_DIR)
WEBSPACE = os.path.dirname(APP_DIR)
PUBLIC_SRC = os.path.join(APP_DIR, "cloudflare", "public")
DEFAULT_DATA_DIR = os.path.join(WEBSPACE, "rotaidem-data")
DEFAULT_PUBLIC_DIR = os.path.join(WEBSPACE, "rotaidem-public")
BACKUP_DIR = os.path.join(WEBSPACE, "rotaidem-backups")
CONFIG_NAME = "config.env"
LOCATION_NAME = "installed.json"  # in the data folder, so it survives replacing the program folder
LEGACY_LOCATION = os.path.join(STRATO_DIR, LOCATION_NAME)
SKIP_PUBLIC = {"_routes.json", "_headers", "_redirects"}  # Cloudflare-only files
DEPLOY_ID_FILE = "rotaidem-deploy.txt"  # public; lets diagnose prove which folder the domain shows
CGI_PYTHON = "/usr/bin/python3"  # STRATO's documented interpreter path
MARK = "# Rotaidem (written by rotaidem/strato/setup.py)"

sys.path.insert(0, STRATO_DIR)
import cgi_app  # noqa: E402


# ---------- small helpers ----------

def say(text=""):
    print(text, flush=True)


def ask(question, default=""):
    hint = f" [{default}]" if default else ""
    answer = input(f"{question}{hint}: ").strip()
    return answer or default


def ask_yes(question, default=True):
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} ({hint}): ").strip().lower()
    return default if not answer else answer in ("y", "yes", "j", "ja")


def ask_secret(question, min_length=0, confirm=False):
    while True:
        value = getpass.getpass(f"{question}: ")
        if len(value) < min_length:
            say(f"  At least {min_length} characters. Try again.")
            continue
        if confirm and getpass.getpass("Type it again: ") != value:
            say("  The two did not match. Try again.")
            continue
        return value


def write_file(path, text, mode=0o644):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.chmod(path, mode)


def config_path(data_dir):
    return os.path.join(data_dir, CONFIG_NAME)


def write_config(path, values):
    lines = ["# Rotaidem settings. Keep this file secret: it holds passwords and keys.",
             "# Change a value, save, and the next page load uses it (no restart needed)."]
    lines += [f"{k}={v}" for k, v in values.items()]
    write_file(path, "\n".join(lines) + "\n", 0o600)


def read_location():
    """Where the last deploy put things. Older versions kept this file inside the program folder."""
    for path in (os.path.join(DEFAULT_DATA_DIR, LOCATION_NAME), LEGACY_LOCATION):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue
    return {}


def save_location(data_dir, info):
    write_file(os.path.join(data_dir, LOCATION_NAME), json.dumps(info, indent=2) + "\n", 0o600)


def current_data_dir():
    return read_location().get("data_dir") or DEFAULT_DATA_DIR


def require_config(data_dir):
    cfg = config_path(data_dir)
    if not os.path.isfile(cfg):
        say(f"No settings found at {cfg}.")
        say("For a first installation run:  python3 rotaidem/strato/setup.py install")
        sys.exit(1)
    return cgi_app.load_config(cfg)


def connect(data_dir):
    """Load the settings and return server.py, as the website itself would."""
    cgi_app.prepare(APP_DIR, config_path(data_dir))
    import server
    return server


def inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def public_dir_problem(public_dir, data_dir):
    """Why a folder must not become the website folder, or None. The website folder is public:
    it must not be, or contain, the program, the private data or the backups."""
    for label, folder in (("the program folder", APP_DIR), ("the private data folder", data_dir), ("the backups folder", BACKUP_DIR)):
        if inside(public_dir, folder):
            return f"{public_dir} is inside {label} ({folder})."
        if inside(folder, public_dir):
            return (f"{public_dir} contains {label} ({folder}), which would put private files under the website. "
                    "(STRATO's new platform also cannot show the webspace root.)")
    if os.path.isdir(public_dir):
        for marker in ("server.py", ".git", "strato", "website"):
            if os.path.exists(os.path.join(public_dir, marker)):
                return f"{public_dir} looks like a copy of the program source (it has '{marker}')."
    return None


def parent_htaccess_files(public_dir):
    """.htaccess files in the folders above the website folder (inside the webspace). Apache applies
    those to the website as well."""
    found, folder = [], os.path.dirname(os.path.realpath(public_dir))
    while inside(folder, WEBSPACE):
        path = os.path.join(folder, ".htaccess")
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                found.append((path, MARK in fh.read()))
        if os.path.realpath(folder) == os.path.realpath(WEBSPACE):
            break
        folder = os.path.dirname(folder)
    return found


def retire_parent_htaccess(public_dir):
    """An earlier version wrote its rules straight into the webspace root. Apache would still apply
    them to the new website folder (and some hosts answer 500 to the 'Options' line in it), so that
    file - and only a file this program wrote - is renamed, never deleted."""
    for path, ours in parent_htaccess_files(public_dir):
        if not ours:
            say(f"  Note: {path} was not written by setup.py; it is left alone, but Apache applies it to the website too.")
            continue
        target = path + ".disabled-by-rotaidem"
        if os.path.exists(target):
            target += datetime.now().strftime("-%Y%m%d-%H%M%S")
        os.rename(path, target)
        say(f"  The old rules in {path} also applied to the website folder; renamed to {target}")


def site_url(value):
    """'Rotaidem.com' or 'http://rotaidem.com/' -> 'https://rotaidem.com' (used for links in e-mails)."""
    value = (value or "").strip().rstrip("/")
    if not value:
        return "https://rotaidem.com"
    if "://" not in value:
        value = "https://" + value
    parts = urllib.parse.urlparse(value)
    return f"{parts.scheme}://{(parts.hostname or 'rotaidem.com')}{':' + str(parts.port) if parts.port else ''}"


def rel_name(path):
    rel = os.path.relpath(path, WEBSPACE)
    return path if rel.startswith("..") else rel


# ---------- what gets installed ----------

def htaccess_public():
    return f"""{MARK}
# The website is plain files. Only /api/..., /media/... and /health go to the Python program,
# which Apache starts per request (CGI): no server process has to keep running.
# No "Options" or "DirectoryIndex" lines on purpose: hosts that do not allow them answer 500.
ErrorDocument 404 /404.html

<IfModule mod_rewrite.c>
RewriteEngine On
RewriteBase /

# Apache does not pass the login header to CGI programs unless it is copied like this.
RewriteCond %{{HTTP:Authorization}} .
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{{HTTP:Authorization}}]

# Hidden files, and file types that never belong to the public website.
RewriteRule (^|/)\\.(?!well-known/) - [F,L]
RewriteRule ^(?!cgi-bin/).*\\.(py|pyc|env|ini|cfg|sql|sqlite|sqlite3|db|log|bak)$ - [F,L]

# The backend.
RewriteRule ^(api/.*|health)$ cgi-bin/rotaidem.py/$1 [L,QSA]

# Uploaded pictures and the site settings script: plain files once the program has written them
# into this folder (fast); until then the program answers and writes them.
RewriteCond %{{REQUEST_FILENAME}} !-f
RewriteRule ^(media/.+|site-settings\\.js)$ cgi-bin/rotaidem.py/$1 [L,QSA]

# No folder listings: a folder without index.html is refused.
RewriteCond %{{REQUEST_FILENAME}} -d
RewriteCond %{{REQUEST_FILENAME}}/index.html !-f
RewriteRule ^ - [F,L]

# Pretty addresses: /login serves login.html, /heavypro serves heavypro.html, and so on.
RewriteCond %{{REQUEST_FILENAME}} !-f
RewriteCond %{{REQUEST_FILENAME}} !-d
RewriteCond %{{REQUEST_FILENAME}}.html -f
RewriteRule ^(.+)$ $1.html [L]
</IfModule>

<IfModule mod_headers.c>
Header always set X-Content-Type-Options "nosniff"
Header always set Referrer-Policy "strict-origin-when-cross-origin"
Header always set X-Frame-Options "SAMEORIGIN"
# Pictures and fonts may be kept by the browser for a day. Pages, scripts and the stylesheet are
# checked every time (an unchanged file is just "304 not modified"), so a deploy shows at once.
<FilesMatch "\\.(jpg|jpeg|png|webp|gif|svg|woff2?)$">
Header set Cache-Control "public, max-age=86400"
</FilesMatch>
<FilesMatch "\\.(html|js|css)$">
Header set Cache-Control "no-cache"
</FilesMatch>
# The admin and account pages are never stored (after the rule above, so this one wins for them).
<FilesMatch "^(admin|account)\\.html$">
Header set Cache-Control "private, no-store"
</FilesMatch>
</IfModule>
"""


HTACCESS_MEDIA = f"""{MARK}
# Uploaded pictures never change (a new upload gets a new number), so browsers keep them for a year.
<IfModule mod_headers.c>
<FilesMatch "\\.(jpg|png|webp|gif)$">
Header set Cache-Control "public, max-age=31536000, immutable"
</FilesMatch>
</IfModule>
"""


HTACCESS_CGI = f"""{MARK}
# Written by: setup.py deploy --cgi-htaccess
# Only needed where the host does not already run scripts in cgi-bin.
Options +ExecCGI
AddHandler cgi-script .py
"""

DENY_ALL = f"""{MARK}
# Nothing in this folder may be downloaded from the web.
<IfModule mod_authz_core.c>
Require all denied
</IfModule>
<IfModule !mod_authz_core.c>
Order deny,allow
Deny from all
</IfModule>
"""


ENTRY_POINTS = (("rotaidem.py", "wsgi_app", "run"), ("rotaidem-check.py", "wsgi_check", "run_check"))


def cgi_script(wsgi_target, cgi_target, data_dir, public_dir):
    return f"""#!{CGI_PYTHON}
# Written by rotaidem/strato/setup.py: the entry point of the Rotaidem backend.
# STRATO's new platform imports this file into a FastCGI process and calls `application` (WSGI).
# Classic CGI hosts run it as a program, once per request (the __main__ part).
import importlib
import os
import sys
os.environ["ROTAIDEM_PUBLIC_DIR"] = {public_dir!r}  # pictures and site settings are copied here as plain files
sys.path.insert(0, {STRATO_DIR!r})
import cgi_app
cgi_app = importlib.reload(cgi_app)  # a process that stays loaded may still hold an older copy

application = cgi_app.{wsgi_target}({APP_DIR!r}, {config_path(data_dir)!r})

if __name__ == "__main__":
    cgi_app.{cgi_target}({APP_DIR!r}, {config_path(data_dir)!r})
"""


def install_files(public_dir, data_dir, cgi_htaccess, deploy_id):
    """Copy the website into public_dir. Only writes inside public_dir, plus the deny-all
    .htaccess files that keep the program and data folders private."""
    say(f"Copying the website into {public_dir} ...")
    os.makedirs(public_dir, exist_ok=True)
    os.chmod(public_dir, 0o755)
    for name in os.listdir(PUBLIC_SRC):
        if name in SKIP_PUBLIC:
            continue
        src, dst = os.path.join(PUBLIC_SRC, name), os.path.join(public_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        # The web server must be able to read the pages, whatever modes the upload left behind.
        if os.path.isfile(dst):
            os.chmod(dst, 0o644)
        for root, _dirs, files in os.walk(dst):
            os.chmod(root, 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)

    htaccess = os.path.join(public_dir, ".htaccess")
    if os.path.exists(htaccess):
        with open(htaccess, encoding="utf-8", errors="replace") as fh:
            old = fh.read()
        backup = htaccess + ".before-rotaidem"
        if MARK not in old and not os.path.exists(backup):
            shutil.copy2(htaccess, backup)
            say(f"  The previous .htaccess was kept as {backup}")
    write_file(htaccess, htaccess_public())
    write_file(os.path.join(APP_DIR, ".htaccess"), DENY_ALL)
    write_file(os.path.join(data_dir, ".htaccess"), DENY_ALL)

    cgi_bin = os.path.join(public_dir, "cgi-bin")
    os.makedirs(cgi_bin, exist_ok=True)
    os.chmod(cgi_bin, 0o755)
    for name, wsgi_target, cgi_target in ENTRY_POINTS:
        write_file(os.path.join(cgi_bin, name), cgi_script(wsgi_target, cgi_target, data_dir, public_dir), 0o755)
    cgi_ht = os.path.join(cgi_bin, ".htaccess")
    if cgi_htaccess:
        write_file(cgi_ht, HTACCESS_CGI)
    elif os.path.exists(cgi_ht):
        with open(cgi_ht, encoding="utf-8", errors="replace") as fh:
            if MARK in fh.read():
                os.remove(cgi_ht)  # ours from an earlier --cgi-htaccess deploy; anything else is left alone
    write_file(os.path.join(public_dir, DEPLOY_ID_FILE), f"rotaidem {deploy_id}\n")
    media = os.path.join(public_dir, "media")
    os.makedirs(media, exist_ok=True)
    os.chmod(media, 0o755)
    write_file(os.path.join(media, ".htaccess"), HTACCESS_MEDIA)


def publish_static(public_dir, data_dir, refresh=False):
    """Write uploaded pictures and site-settings.js into the website folder, so the web server sends
    them as plain files. refresh=True rewrites pictures that are already there (after an import)."""
    server = connect(data_dir)
    os.environ["ROTAIDEM_PUBLIC_DIR"] = public_dir
    d = server.DB()
    rows = d.all("SELECT id, mime FROM media ORDER BY id")
    written = 0
    if refresh:  # drop copies of pictures that no longer exist
        keep = {f"{r['id']}.{server.MEDIA_TYPES.get(r['mime'], 'jpg')}" for r in rows}
        folder = os.path.join(public_dir, "media")
        for name in os.listdir(folder) if os.path.isdir(folder) else []:
            if re.match(r"^\d+\.(jpg|png|webp|gif)$", name) and name not in keep:
                os.remove(os.path.join(folder, name))
    for row in rows:
        name = f"media/{row['id']}.{server.MEDIA_TYPES.get(row['mime'], 'jpg')}"
        if refresh or not os.path.exists(os.path.join(public_dir, name)):
            data = d.first("SELECT data FROM media WHERE id = ?", row["id"])["data"]
            server.write_public(name, __import__("base64").b64decode(data))
            written += 1
    d.close()
    with server.app.app_context():
        server.publish_site_settings()
    say(f"  Pictures as plain files: {len(rows)} ({written} written now), site-settings.js updated")


def prepare_database(data_dir):
    say("Checking the database tables (this never deletes anything) ...")
    server = connect(data_dir)
    server.ensure_schema()
    d = server.DB()
    counts = {t: d.first(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in ("users", "products", "orders", "licenses")}
    d.close()
    os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
    secure_data(data_dir)
    say("  Database OK: " + ", ".join(f"{n} {t}" for t, n in counts.items()))
    # Made now, so no customer waits for it; HeavyPro's build fetches the public half.
    say(f"  Licence signing key {server.signing_key_id(server.signing_key())} is ready")


def secure_data(data_dir, group=False):
    """Only your own account may open the settings, logs and the SQLite database (with group=True
    also your group). Never other users: on shared hosting those are other customers."""
    sqlite = not cgi_app.load_config(config_path(data_dir)).get("MYSQL_HOST")
    # SQLite writes a journal next to the database and the program writes logs, so with group
    # access those folders must be group-writable.
    dir_mode = (0o770 if sqlite else 0o750) if group else 0o700
    os.chmod(data_dir, dir_mode)
    for root, dirs, files in os.walk(data_dir):
        for name in dirs:
            os.chmod(os.path.join(root, name), 0o770 if group else 0o700)
        for name in files:
            path = os.path.join(root, name)
            if name == ".htaccess":
                os.chmod(path, 0o644)  # holds no secrets; Apache may need to read it
            elif name.startswith("rotaidem.db") or root != data_dir:
                os.chmod(path, 0o660 if group else 0o600)
            else:
                os.chmod(path, 0o640 if group else 0o600)


def ensure_owner(data_dir, name, email, password):
    """Create (or reset) the Owner account directly, so no password has to be stored in config.env."""
    server = connect(data_dir)
    d = server.DB()
    row = d.first("SELECT id FROM users WHERE LOWER(email) = ? OR LOWER(name) = ?", email.lower(), name.lower())
    if row:
        d.run("UPDATE users SET name = ?, email = ?, password_hash = ?, role = 'Owner', token = NULL WHERE id = ?",
              name, email, server.hash_password(password), row["id"])
    else:
        d.run("INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'Owner')",
              name, email, server.hash_password(password))
    d.close()
    say(f"  Admin account {name} is ready.")


def deploy_to(public_dir, data_dir, cgi_htaccess=False):
    problem = public_dir_problem(public_dir, data_dir)
    if problem:
        say("Stopped: " + problem)
        say(f"Use the default website folder ({rel_name(DEFAULT_PUBLIC_DIR)}) or another empty folder.")
        sys.exit(2)
    old = read_location()
    deploy_id = secrets.token_hex(8)
    retire_parent_htaccess(public_dir)
    install_files(public_dir, data_dir, cgi_htaccess, deploy_id)
    prepare_database(data_dir)
    publish_static(public_dir, data_dir)
    save_location(data_dir, {"public_dir": public_dir, "data_dir": data_dir, "app_dir": APP_DIR,
                             "cgi_htaccess": bool(cgi_htaccess), "deploy_id": deploy_id,
                             "deployed_at": datetime.now().isoformat(timespec="seconds")})
    if os.path.exists(LEGACY_LOCATION):
        say(f"  (Older location file {LEGACY_LOCATION} is no longer used.)")
    if old.get("public_dir") and os.path.realpath(old["public_dir"]) != os.path.realpath(public_dir):
        say(f"  The previous website folder {old['public_dir']} was left untouched; it is no longer used.")


def install(public_dir, data_dir, values, owner=None, cgi_htaccess=False):
    """Everything after the questions: used by cmd_install and by the automated tests."""
    os.makedirs(data_dir, exist_ok=True)
    os.chmod(data_dir, 0o700)
    cfg = config_path(data_dir)
    if not os.path.exists(cfg) or values != cgi_app.load_config(cfg):
        write_config(cfg, values)
        say(f"Settings saved in {cfg}")
    deploy_to(public_dir, data_dir, cgi_htaccess)
    if owner:
        ensure_owner(data_dir, *owner)


# ---------- deploy ----------

def resolve_public_dir(arg, data_dir):
    if arg:
        return os.path.normpath(arg if os.path.isabs(arg) else os.path.join(WEBSPACE, arg.strip("/")))
    previous = read_location().get("public_dir")
    if previous:
        if not public_dir_problem(previous, data_dir):
            return previous
        say(f"The previous installation put the website straight into {previous}.")
        say("That folder cannot be a domain target on STRATO's new platform, and it also holds the program")
        say(f"and its data. The website now gets its own folder: {DEFAULT_PUBLIC_DIR}")
        say(f"Nothing in {previous} is deleted or changed.")
        say()
    return DEFAULT_PUBLIC_DIR


def cmd_deploy(args):
    data_dir = current_data_dir()
    require_config(data_dir)
    public_arg = None
    if "--public" in args:
        i = args.index("--public")
        public_arg = args[i + 1] if i + 1 < len(args) else ""
    cgi_htaccess = read_location().get("cgi_htaccess", False)
    if "--cgi-htaccess" in args:
        cgi_htaccess = True
    if "--no-cgi-htaccess" in args:
        cgi_htaccess = False
    public_dir = resolve_public_dir(public_arg, data_dir)
    say("=== Rotaidem deploy ===")
    say(f"Program:        {APP_DIR}")
    say(f"Private data:   {data_dir}  (kept as it is)")
    say(f"Website folder: {public_dir}")
    say()
    deploy_to(public_dir, data_dir, cgi_htaccess)
    say()
    diagnose(offline=True, brief=True)
    say()
    say("=== Next step at STRATO ===")
    say(f"Domains > rotaidem.com > target / Ziel: choose the folder  {rel_name(public_dir)}")
    say("Do the same for www.rotaidem.com if it is listed separately. Do not change DNS or e-mail settings.")
    say("Then run:  python3 rotaidem/strato/setup.py diagnose")


# ---------- first installation ----------

def ask_database():
    while True:
        say()
        say("Where should the database live?")
        say("  1 = the MySQL database included in your STRATO package (recommended)")
        say("  2 = a SQLite file in the rotaidem-data folder")
        if ask("Choose 1 or 2", "1") == "2":
            return {}
        say("The details are in the STRATO login under Databases (Datenbankverwaltung).")
        values = {
            "MYSQL_HOST": ask("Server (host), e.g. database-5012345678.webspace-host.com"),
            "MYSQL_PORT": ask("Port", "3306"),
            "MYSQL_DATABASE": ask("Database name, e.g. dbs12345678"),
            "MYSQL_USER": ask("User name, e.g. dbu12345678"),
            "MYSQL_PASSWORD": ask_secret("Database password (not shown while typing)"),
        }
        try:
            raw_connect(values).close()
            say("  The MySQL connection works.")
            return values
        except Exception as exc:  # noqa: BLE001
            say(f"  Could not connect: {exc}")
            if not ask_yes("Try again?"):
                sys.exit(1)


def cmd_install():
    say("=== Install Rotaidem on STRATO ===")
    say(f"Program folder: {APP_DIR}")
    data_dir = current_data_dir()
    cfg = config_path(data_dir)
    owner = None
    if os.path.exists(cfg) and ask_yes(f"Settings already exist in {cfg}. Keep them (recommended)?"):
        values = cgi_app.load_config(cfg)
    else:
        values = {"SECRET_KEY": secrets.token_hex(32), "CHECK_KEY": secrets.token_hex(8)}
        values.update(ask_database())
        if "MYSQL_HOST" not in values:
            values["DATA_DIR"] = data_dir
        say()
        say("Your admin account (the password is only stored hashed, in the database):")
        owner = (ask("User name", "eliaz"), ask("E-mail", "Eliaz@rotaidem.com"),
                 ask_secret("Password, at least 8 characters (not shown while typing)", 8, confirm=True))
        say()
        values["SITE_URL"] = site_url(ask("Website address", "https://rotaidem.com"))
        say()
        say("E-mail through Resend (licence e-mails and newsletters). Press Enter to skip.")
        key = ask("RESEND_API_KEY (starts with re_)")
        if key:
            values["RESEND_API_KEY"] = key
            values["MAIL_FROM"] = ask("Sender", "Rotaidem <no-reply@rotaidem.com>")
        say()
        say("The key the licence generator uses. If you already have one, paste the same one.")
        values["LICENSE_API_KEY"] = ask("LICENSE_API_KEY (Enter = create a new one)") or secrets.token_hex(24)
    say()
    folder = ask("Website folder inside the webspace", rel_name(resolve_public_dir(None, data_dir)))
    public_dir = os.path.normpath(folder if os.path.isabs(folder) else os.path.join(WEBSPACE, folder.strip("/")))
    install(public_dir, data_dir, values, owner)
    site = values.get("SITE_URL", "https://rotaidem.com").rstrip("/")
    say()
    say("=== Done ===")
    say(f"1. At STRATO, choose the folder {rel_name(public_dir)} as the target of rotaidem.com (and www).")
    say("2. Run:  python3 rotaidem/strato/setup.py diagnose")
    say(f"3. Check page in the browser:  {site}/cgi-bin/rotaidem-check.py?key=<CHECK_KEY from config.env>")
    say(f"4. Log in at {site}/login")


# ---------- diagnose (read-only) ----------

class Report:
    def __init__(self):
        self.fails, self.warns = [], []

    def ok(self, text):
        say(f"  [ OK ] {text}")

    def warn(self, text, hint=""):
        self.warns.append(text)
        say(f"  [WARN] {text}" + (f"\n         -> {hint}" if hint else ""))

    def fail(self, text, hint=""):
        self.fails.append(text)
        say(f"  [FAIL] {text}" + (f"\n         -> {hint}" if hint else ""))


def mode_of(path):
    return os.stat(path).st_mode & 0o777


def tail(path, lines=15):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return ""


def run_cgi(script, path, extra_env=None):
    """Run a CGI entry point the way Apache would, but from SSH. Returns (status, headers, body)."""
    env = {"PATH": "/usr/bin:/bin", "GATEWAY_INTERFACE": "CGI/1.1", "REQUEST_METHOD": "GET", "PATH_INFO": path,
           "QUERY_STRING": "", "SCRIPT_NAME": "/cgi-bin/" + os.path.basename(script), "SCRIPT_FILENAME": script,
           "SERVER_NAME": "localhost", "SERVER_PORT": "443", "HTTPS": "on", "SERVER_PROTOCOL": "HTTP/1.1",
           "REMOTE_ADDR": "127.0.0.1", "ROTAIDEM_NO_SCHEMA_UPDATE": "1", **(extra_env or {})}
    python = CGI_PYTHON if os.path.exists(CGI_PYTHON) else sys.executable
    p = subprocess.run([python, script], env=env, capture_output=True, timeout=90)
    head, _, body = p.stdout.partition(b"\r\n\r\n")
    headers = {}
    for line in head.decode("latin-1").split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    status = int(headers.get("status", "200").split()[0]) if p.stdout else 0
    return status, headers, body.decode("utf-8", "replace")


def run_wsgi(script, requests):
    """Call the entry script like STRATO's FastCGI launcher does (see wsgi_probe.py)."""
    python = CGI_PYTHON if os.path.exists(CGI_PYTHON) else sys.executable
    env = {**os.environ, "ROTAIDEM_NO_SCHEMA_UPDATE": "1"}
    p = subprocess.run([python, os.path.join(STRATO_DIR, "wsgi_probe.py"), script], input=json.dumps(requests),
                       env=env, capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip() else "no output")
    return json.loads(p.stdout)


def check_layout(r, loc, data_dir, values):
    say("Program and settings")
    if sys.version_info >= (3, 9):
        r.ok(f"Python {sys.version.split()[0]} for SSH")
    else:
        r.fail(f"Python {sys.version.split()[0]} is too old", "Run with python3 (3.9 or newer).")
    if os.path.exists(CGI_PYTHON):
        version = subprocess.run([CGI_PYTHON, "--version"], capture_output=True, text=True).stdout.strip()
        r.ok(f"{CGI_PYTHON} exists ({version}): the CGI scripts use it")
    else:
        r.fail(f"{CGI_PYTHON} does not exist", "The CGI scripts cannot start. Tell me what `which python3` prints.")
    if os.path.isdir(os.path.join(STRATO_DIR, "vendor", "flask")):
        r.ok("Bundled packages (strato/vendor) are present")
    else:
        r.fail("strato/vendor/flask is missing", "The program folder is incomplete: fetch the code again.")
    cfg = config_path(data_dir)
    if values is None:
        r.fail(f"No settings at {cfg}", "Run:  python3 rotaidem/strato/setup.py install")
        return
    r.ok(f"Settings: {cfg} (mode {mode_of(cfg):o})")
    if mode_of(cfg) & 0o007:
        r.fail("The settings file is readable by other users", "Run:  python3 rotaidem/strato/setup.py permissions")
    if mode_of(data_dir) & 0o007:
        r.fail(f"{data_dir} is open to other users", "Run:  python3 rotaidem/strato/setup.py permissions")
    missing = [k for k in ("SECRET_KEY",) if not values.get(k)]
    if not values.get("MYSQL_HOST") and not values.get("DATA_DIR"):
        missing.append("MYSQL_HOST or DATA_DIR")
    if missing:
        r.fail("Settings are missing: " + ", ".join(missing))

    say()
    say("Website folder")
    public_dir = loc.get("public_dir")
    if not public_dir:
        r.fail("No deploy recorded yet", "Run:  python3 rotaidem/strato/setup.py deploy")
        return
    problem = public_dir_problem(public_dir, data_dir)
    if problem:
        r.fail(problem, "Run:  python3 rotaidem/strato/setup.py deploy   (moves the website to its own folder)")
        return
    if not os.path.isdir(public_dir):
        r.fail(f"{public_dir} does not exist", "Run:  python3 rotaidem/strato/setup.py deploy")
        return
    r.ok(f"{public_dir}  <- choose '{rel_name(public_dir)}' as the domain target at STRATO")
    for name in ("index.html", "login.html", "admin.html", "site.css", "auth.js", ".htaccess", DEPLOY_ID_FILE):
        path = os.path.join(public_dir, name)
        if not os.path.isfile(path):
            r.fail(f"{name} is missing in the website folder", "Run:  python3 rotaidem/strato/setup.py deploy")
        elif not mode_of(path) & 0o004:
            r.warn(f"{name} is not readable by other users (mode {mode_of(path):o})", "Run deploy again; it sets 644.")
    htaccess = os.path.join(public_dir, ".htaccess")
    if os.path.isfile(htaccess):
        with open(htaccess, encoding="utf-8", errors="replace") as fh:
            if MARK not in fh.read():
                r.warn(".htaccess in the website folder was not written by setup.py", "Run deploy again.")
    for name in ("rotaidem.py", "rotaidem-check.py"):
        path = os.path.join(public_dir, "cgi-bin", name)
        if not os.path.isfile(path):
            r.fail(f"cgi-bin/{name} is missing", "Run:  python3 rotaidem/strato/setup.py deploy")
            continue
        with open(path, "rb") as fh:
            raw = fh.read()
        if not raw.startswith(f"#!{CGI_PYTHON}\n".encode()) or b"\r" in raw:
            r.fail(f"cgi-bin/{name} has a wrong first line or Windows line endings", "Run deploy again.")
        elif mode_of(path) != 0o755:
            r.fail(f"cgi-bin/{name} has mode {mode_of(path):o}; it must be 755", "Run deploy again.")
        elif APP_DIR.encode() not in raw:
            r.warn(f"cgi-bin/{name} points to another program folder", "Run deploy again from this folder.")
        else:
            r.ok(f"cgi-bin/{name}: correct first line, mode 755")
    for path, ours in parent_htaccess_files(public_dir):
        if ours:
            r.fail(f"{path} holds old Rotaidem rules that also apply to the website folder",
                   "Run:  python3 rotaidem/strato/setup.py deploy   (renames it; deletes nothing)")
        else:
            r.warn(f"{path} also applies to the website folder",
                   "If the site answers 500 Internal Server Error, this file is the first suspect.")
    # Apache (or whoever serves the static files) must be able to walk into the folder.
    for path in (WEBSPACE, public_dir, os.path.join(public_dir, "cgi-bin")):
        m = mode_of(path)
        if not m & 0o001:
            r.warn(f"{path} has mode {m:o}: other users cannot enter it",
                   "If the web server is not your own account, this gives 403 Forbidden. "
                   "STRATO sets the webspace mode; the website folders should be 755.")
    # Copies of the source next to the website: never choose these as the domain target.
    risky = []
    for name in sorted(os.listdir(WEBSPACE)):
        path = os.path.join(WEBSPACE, name)
        if not os.path.isdir(path) or inside(path, public_dir) or name in ("rotaidem-data", "rotaidem-backups"):
            continue
        if any(os.path.exists(os.path.join(path, m)) for m in ("server.py", "strato", "functions", "templates", "README.md")) \
                or name in (".github", "__pycache__", "cloudflare", "licensing", "website", "strato"):
            risky.append(name)
    if risky:
        r.warn("These folders hold program files and must NEVER be chosen as the domain target: " + ", ".join(risky),
               f"Only '{rel_name(public_dir)}' is the website. The others can be deleted later, after a backup.")
    if os.path.exists(os.path.join(WEBSPACE, "server.py")) or os.path.exists(os.path.join(WEBSPACE, "index.html")):
        r.warn(f"Old copies of files lie directly in {WEBSPACE}", "They are not used (STRATO cannot show the webspace root). Leave them for now.")


def check_program(r, loc, data_dir, values):
    say()
    say("Program test (runs the CGI script from SSH, like Apache would)")
    public_dir = loc.get("public_dir")
    script = os.path.join(public_dir or "", "cgi-bin", "rotaidem.py")
    if not public_dir or not os.path.isfile(script) or values is None:
        r.fail("Skipped: no deployed CGI script")
        return
    logs = os.path.join(data_dir, "logs")
    for path, expect in (("/health", '"database":"ok"'), ("/api/site", '"announcement"'), ("/api/products", '"products"')):
        try:
            status, headers, body = run_cgi(script, path)
        except subprocess.TimeoutExpired:
            r.fail(f"{path}: no answer within 90 seconds")
            continue
        if status == 200 and expect in body:
            r.ok(f"{path} -> 200 {headers.get('content-type', '')}")
        else:
            r.fail(f"{path} -> {status or 'no output'}: {body[:160].strip()}",
                   "Details from the private log follow below." if status else "The script printed nothing.")
    say()
    say("Program test, STRATO way (imports the script and calls its `application`, like STRATO's FastCGI launcher)")
    try:
        replies = run_wsgi(script, [{"path": "/health"}, {"path": "/api/site"}, {"path": "/api/products"}])
        for path, reply, expect in zip(("/health", "/api/site", "/api/products"), replies,
                                       ('"database":"ok"', '"announcement"', '"products"')):
            if reply["status"] == 200 and expect in reply["body"]:
                r.ok(f"{path} -> 200 {reply['headers'].get('content-type', '')}")
            else:
                r.fail(f"{path} -> {reply['status']}: {reply['body'][:160].strip()}", "Details from the private log follow below.")
    except Exception as exc:  # noqa: BLE001
        r.fail(f"The script cannot be loaded the STRATO way: {exc}",
               "Run deploy again: older versions of the entry script had no `application`.")
    for name in ("cgi-error.log", "app-error.log"):
        text = tail(os.path.join(logs, name))
        if text:
            say(f"  Last lines of {os.path.join(logs, name)} (private, do not share publicly):")
            say("    " + text.strip().replace("\n", "\n    "))

    say()
    say("Database (read-only)")
    try:
        os.environ["ROTAIDEM_NO_SCHEMA_UPDATE"] = "1"
        server = connect(data_dir)
        d = server.DB()
        counts = {}
        for t in ("users", "products", "orders", "licenses", "shared_profiles"):
            try:
                counts[t] = d.first(f"SELECT COUNT(*) AS n FROM {t}")["n"]
            except Exception:  # noqa: BLE001 - e.g. a table a newer version adds
                counts[t] = "missing"
        try:
            stored = (d.first("SELECT value FROM meta WHERE key = 'schema_version'") or {}).get("value")
        except Exception:  # noqa: BLE001
            stored = None
        d.close()
        kind = f"MySQL on {values['MYSQL_HOST']}" if values.get("MYSQL_HOST") else f"SQLite in {server.DATA_DIR}"
        r.ok(f"{kind}: " + ", ".join(f"{n} {t}" for t, n in counts.items()))
        if stored != server.SCHEMA_VERSION:
            r.warn(f"Tables are at version {stored}, the program expects {server.SCHEMA_VERSION}",
                   "Run:  python3 rotaidem/strato/setup.py deploy   (adds missing tables, deletes nothing)")
    except Exception as exc:  # noqa: BLE001
        r.fail(f"Cannot use the database: {type(exc).__name__}: {exc}",
               "Check MYSQL_HOST, MYSQL_DATABASE, MYSQL_USER and MYSQL_PASSWORD in config.env.")


def fetch(url, timeout=15, data=None, extra_headers=None):
    headers = {"User-Agent": "rotaidem-diagnose/1", "Cache-Control": "no-cache", **(extra_headers or {})}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(65536).decode("utf-8", "replace"), resp.geturl(), None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096).decode("utf-8", "replace"), url, None
    except Exception as exc:  # noqa: BLE001
        return None, "", url, getattr(exc, "reason", exc)


def check_domain(r, loc, values, domain):
    say()
    say(f"Domain {domain} (from this server; your own computer can give different answers)")
    scheme = os.environ.get("ROTAIDEM_DIAGNOSE_SCHEME", "https")  # http only for local tests
    name, _, port = domain.partition(":")
    plain = name.replace(".", "").isdigit() or port  # an IP address or host:port: no www variant
    hosts = [domain] if plain or name.startswith("www.") else [domain, "www." + domain]
    ports = (int(port),) if port else (443, 80)
    deploy_id = loc.get("deploy_id", "")
    for host in hosts:
        hostname = host.partition(":")[0]
        try:
            addrs = sorted({a[4][0] for a in socket.getaddrinfo(hostname, ports[0], proto=socket.IPPROTO_TCP)})
        except socket.gaierror as exc:
            r.fail(f"{host}: the name does not resolve ({exc})",
                   "DNS problem: the browser shows ERR_NAME_NOT_RESOLVED or a timeout. Check the domain at STRATO.")
            continue
        r.ok(f"{host} resolves to " + ", ".join(addrs))
        for ip in addrs[:4]:
            for p in ports:
                try:
                    socket.create_connection((ip, p), timeout=8).close()
                    r.ok(f"{ip} port {p} answers")
                except socket.timeout:
                    r.fail(f"{ip} port {p} does not answer (timeout)",
                           "This is what ERR_TIMED_OUT means. Usually the domain still points to an old address "
                           "(wait up to 24 h after changing the target) or an IPv6 (AAAA) entry is wrong.")
                except OSError as exc:
                    if ":" in ip and getattr(exc, "errno", None) in (101, 99):
                        r.warn(f"{ip} (IPv6) cannot be tested from this server ({exc})")
                    else:
                        r.fail(f"{ip} port {p}: {exc}")
        base = f"{scheme}://{host}"
        status, body, final, err = fetch(f"{base}/{DEPLOY_ID_FILE}")
        if err is not None:
            if isinstance(err, ssl.SSLError) or "CERTIFICATE" in str(err).upper():
                r.fail(f"{base}: certificate problem ({err})",
                       "Activate the SSL certificate for this name in the STRATO login (SSL settings).")
            else:
                r.fail(f"{base}: {err}", "No answer: see the port tests above.")
            continue
        if status == 200 and deploy_id and deploy_id in body:
            r.ok(f"{base} shows the folder {rel_name(loc.get('public_dir', ''))} (deploy id matches)")
        elif status == 200:
            r.fail(f"{base} shows another copy of the website (deploy id differs)",
                   f"At STRATO choose the folder {rel_name(loc.get('public_dir', ''))} for {host}.")
        else:
            r.fail(f"{base}/{DEPLOY_ID_FILE} -> {status}: {host} does not show the folder {rel_name(loc.get('public_dir', ''))}",
                   f"At STRATO set the target of {host} to the folder {rel_name(loc.get('public_dir', ''))}. "
                   "403 here usually means the domain points to 'rotaidem' (private) or a folder without index.html.")
            continue
        for path, expect, label in (("/", "Rotaidem", "home page"), ("/login", "<form", "login page"),
                                    ("/site.css", "", "stylesheet"), ("/health", '"database":"ok"', "health check"),
                                    ("/api/site", '"announcement"', "API")):
            status, body, final, err = fetch(f"{base}{path}")
            if status == 200 and expect in body:
                r.ok(f"{base}{path} -> 200 ({label})")
            else:
                hint = ""
                if path in ("/health", "/api/site"):
                    hint = ("The pages load but the program does not: CGI is not running in cgi-bin. "
                            "Run  python3 rotaidem/strato/setup.py deploy --cgi-htaccess  (if that gives 500, "
                            "undo it with  deploy --no-cgi-htaccess  and ask STRATO whether Python CGI is enabled)")
                r.fail(f"{base}{path} -> {status or err} ({label})", hint)
        # The admin and account pages send the login token in a header. Both kinds must reach the
        # program, which answers a made-up token with 401 JSON.
        for label, extra in (("X-Rotaidem-Token header (used by the site)", {"X-Rotaidem-Token": "diagnose-test"}),
                             ("Authorization header", {"Authorization": "Bearer diagnose-test"})):
            status, body, _final, err = fetch(f"{base}/api/me", extra_headers=extra)
            if status == 401 and '"error"' in body:
                r.ok(f"{base}/api/me with the {label} reaches the program")
            elif "X-Rotaidem" in label:
                r.fail(f"{base}/api/me with the {label} -> {status or err}: {body[:120].strip()}",
                       "Logged-in pages (account, admin) will stay on 'Loading'. Tell me this line.")
            else:
                r.warn(f"{base}/api/me with the {label} -> {status or err}",
                       "The host interferes with Authorization headers; the site does not need them.")
        # Image uploads in the admin are sent as one request of up to a few MB. Without a login the
        # program answers 403; anything else means the host stopped the request before the program.
        big = json.dumps({"mime": "image/jpeg", "data": "A" * 1_500_000}).encode()
        status, _body, _final, err = fetch(f"{base}/api/admin/media", timeout=60, data=big)
        if status == 403:
            r.ok(f"{base} accepts 1.5 MB requests (image uploads in the admin fit)")
        else:
            r.fail(f"A 1.5 MB request to {base}/api/admin/media -> {status or err}",
                   "The host limits request size, so uploading larger images in the admin fails. Tell me this line.")
        for path in ("/.htaccess", "/cgi-bin/", "/rotaidem-data/config.env", "/rotaidem/server.py"):
            status, _body, _final, _err = fetch(f"{base}{path}")
            if status == 200:
                r.fail(f"{base}{path} can be downloaded", "This must never happen: tell me immediately.")
            else:
                r.ok(f"{base}{path} is not downloadable ({status or 'no answer'})")


def diagnose(offline=False, brief=False, domain=None):
    r = Report()
    loc = read_location()
    data_dir = loc.get("data_dir") or DEFAULT_DATA_DIR
    try:
        values = cgi_app.load_config(config_path(data_dir))
    except OSError:
        values = None
    say("=== Rotaidem diagnose (read-only: changes nothing) ===")
    check_layout(r, loc, data_dir, values)
    check_program(r, loc, data_dir, values)
    if not offline and values is not None:
        site = domain or urllib.parse.urlparse(site_url(values.get("SITE_URL"))).hostname
        check_domain(r, loc, values, site)
    say()
    if r.fails:
        say(f"Result: {len(r.fails)} problem(s), {len(r.warns)} warning(s). Fix the first [FAIL] first.")
    else:
        say(f"Result: no problems found ({len(r.warns)} warning(s))." + ("" if offline or brief else " The live site answered correctly."))
    return r


# ---------- backup ----------

def raw_connect(values):
    """A plain database connection that never changes anything (no table updates)."""
    sys.path.insert(0, os.path.join(STRATO_DIR, "vendor"))
    import pymysql
    options = dict(host=values["MYSQL_HOST"], port=int(values.get("MYSQL_PORT") or 3306), user=values.get("MYSQL_USER", ""),
                   password=values.get("MYSQL_PASSWORD", ""), database=values.get("MYSQL_DATABASE", ""),
                   charset="utf8mb4", connect_timeout=10, cursorclass=pymysql.cursors.DictCursor)
    try:
        return pymysql.connect(ssl={"check_hostname": False}, **options)  # same as server.py
    except Exception:  # noqa: BLE001
        return pymysql.connect(**options)


def dump_database(values, data_dir):
    tables = {}
    if values.get("MYSQL_HOST"):
        conn = raw_connect(values)
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            for name in [list(row.values())[0] for row in cur.fetchall()]:
                cur.execute(f"SELECT * FROM `{name}`")
                tables[name] = list(cur.fetchall())
        conn.close()
    else:
        import sqlite3
        path = os.path.join(values.get("DATA_DIR") or data_dir, "rotaidem.db")
        if not os.path.exists(path):
            return tables
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
            tables[name] = [dict(row) for row in conn.execute(f'SELECT * FROM "{name}"').fetchall()]
        conn.close()
    return tables


def cmd_backup():
    data_dir = current_data_dir()
    values = require_config(data_dir)
    target = os.path.join(BACKUP_DIR, datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(target, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    os.chmod(target, 0o700)
    write_file(os.path.join(BACKUP_DIR, ".htaccess"), DENY_ALL)
    say(f"Backing up into {target} (private, never served) ...")
    archive = os.path.join(target, "rotaidem-data.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(data_dir, arcname="rotaidem-data")
    os.chmod(archive, 0o600)
    say("  Settings and data folder: " + archive)
    tables = dump_database(values, data_dir)
    dump = os.path.join(target, "database.json")
    write_file(dump, json.dumps({"format": "rotaidem-backup-1", "created_at": datetime.now().isoformat(timespec="seconds"),
                                 "tables": tables}, default=str) + "\n", 0o600)
    say("  Database: " + dump)
    say("  " + ", ".join(f"{t}: {len(rows)}" for t, rows in sorted(tables.items())))
    say("Restore later with:  python3 rotaidem/strato/setup.py import " + dump)


# ---------- other commands ----------

def cmd_permissions(mode):
    data_dir = current_data_dir()
    require_config(data_dir)
    if mode == "group":
        secure_data(data_dir, group=True)
        say("Your account and your group may read the settings. Other users may not.")
        say("Use this only if the check page says the web server is in your group.")
    else:
        secure_data(data_dir)
        say("Only your own account may read the settings, logs and database file (700/600).")
    say("The files are never made readable for everyone: on shared hosting that would be other customers.")


def cmd_reset_catalog():
    data_dir = current_data_dir()
    require_config(data_dir)
    server = connect(data_dir)
    server.ensure_schema()
    d = server.DB()
    products = d.all("SELECT * FROM products")
    if not products:
        say("The product catalogue is already empty. Nothing changed.")
        return
    say(f"This removes all {len(products)} products from the website: " + ", ".join(p["name"] or p["id"] for p in products))
    say("Accounts, orders, licences and images are not affected.")
    if input("Type YES to remove the products: ").strip() not in ("YES", "JA"):
        say("Cancelled. Nothing changed.")
        return
    copy = os.path.join(data_dir, f"products-before-reset-{datetime.now():%Y%m%d-%H%M%S}.json")
    write_file(copy, json.dumps(products, indent=2, default=str) + "\n", 0o600)
    d.run("DELETE FROM products")
    d.close()
    say(f"The products are removed. A copy was saved in {copy}")


def cmd_password():
    data_dir = current_data_dir()
    require_config(data_dir)
    server = connect(data_dir)
    server.ensure_schema()
    d = server.DB()
    who = ask("E-mail or user name of the account").lower()
    user = d.first("SELECT id, name FROM users WHERE LOWER(email) = ? OR LOWER(name) = ?", who, who)
    if not user:
        say("No such account.")
        return
    password = ask_secret("New password, at least 8 characters", 8, confirm=True)
    d.run("UPDATE users SET password_hash = ?, token = NULL WHERE id = ?", server.hash_password(password), user["id"])
    d.close()
    say(f"The password for {user['name']} has been changed.")


def cmd_email_test(address):
    """Send one e-mail through Resend exactly as the website does, and say why it fails. Never prints the key."""
    data_dir = current_data_dir()
    values = require_config(data_dir)
    key = values.get("RESEND_API_KEY", "")
    say(f"Settings: {config_path(data_dir)}")
    if not key:
        say("  RESEND_API_KEY is missing. Add the line  RESEND_API_KEY=re_...  to config.env.")
        return
    problems = []
    if not key.startswith("re_"):
        problems.append("it does not start with re_")
    if any(c.isspace() for c in key) or any(c in key for c in "\"'<>"):
        problems.append("it contains spaces, quotes or < > (copy only the key itself)")
    if len(key) < 30:
        problems.append(f"it is only {len(key)} characters long (Resend keys are about 36)")
    say(f"  RESEND_API_KEY: set, {len(key)} characters, starts with {key[:3]!r}" + ("" if not problems else " - looks wrong: " + "; ".join(problems)))
    say(f"  MAIL_FROM: {values.get('MAIL_FROM') or 'Rotaidem <no-reply@rotaidem.com> (default)'}")
    os.environ["ROTAIDEM_NO_SCHEMA_UPDATE"] = "1"
    server = connect(data_dir)
    sent, error = server.send_batch([{"from": server.MAIL_FROM, "to": [address], "subject": "Rotaidem test e-mail",
                                      "html": "<p>This is a test from rotaidem.com. If you can read this, e-mail works.</p>"}])
    if sent:
        say(f"Resend accepted the e-mail to {address}. It should arrive within a minute (check spam too).")
    else:
        say("Resend did not send it:")
        say("  " + error)


def cmd_stripe():
    """Payments: the Stripe secret key (and the webhook's signing secret) into config.env, then a test call."""
    data_dir = current_data_dir()
    values = require_config(data_dir)
    say("Stripe payments. Find the keys at dashboard.stripe.com -> Developers -> API keys.")
    say("Best: a restricted key (Create restricted key) with  Checkout Sessions: Write  and nothing else.")
    say("Nothing you type is shown. Press Enter to keep what is there now.")
    say()
    key = ask_secret("Secret or restricted key (sk_live_..., rk_live_..., or _test_ for trying it out)")
    if key:
        if not re.match(r"^(sk|rk)_(live|test)_[A-Za-z0-9]{10,}$", key):
            say("That does not look like a Stripe secret key (sk_live_... or rk_live_...). Nothing was changed.")
            sys.exit(1)
        values["STRIPE_SECRET_KEY"] = key
    say()
    say("Webhook (recommended: orders are made even when a customer closes the page right after paying).")
    say("In Stripe: Developers -> Webhooks -> Add endpoint:")
    say(f"  URL     {site_url(values.get('SITE_URL'))}/api/stripe/webhook")
    say("  Events  checkout.session.completed  and  checkout.session.async_payment_succeeded")
    say("then reveal its Signing secret (whsec_...).")
    hook = ask_secret("Signing secret (whsec_..., Enter = skip)")
    if hook:
        if not hook.startswith("whsec_"):
            say("That does not look like a signing secret (whsec_...). Nothing was changed.")
            sys.exit(1)
        values["STRIPE_WEBHOOK_SECRET"] = hook
    if key or hook:
        write_config(config_path(data_dir), values)
        say("Saved in " + config_path(data_dir) + " (only you can read it).")
    secret = values.get("STRIPE_SECRET_KEY", "")
    if not secret:
        say("No Stripe key is set: the Buy buttons keep using each product's Buy link.")
        return
    say()
    api = values.get("STRIPE_API_URL", "https://api.stripe.com").rstrip("/")
    req = urllib.request.Request(api + "/v1/checkout/sessions?limit=1", headers={"Authorization": f"Bearer {secret}"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
        mode = "TEST mode (no real money)" if "_test_" in secret else "LIVE mode (real payments)"
        say(f"Stripe answered: the key works, {mode}.")
        say("Now give products a price in Admin -> Products -> Price at checkout. Their Buy buttons then open Stripe.")
    except urllib.error.HTTPError as exc:
        say(f"Stripe refused the key ({exc.code}). Check that it is copied completely and that a restricted key has "
            "Checkout Sessions: Write.")
    except OSError as exc:
        say(f"Could not reach Stripe from this server: {exc}")


def cmd_signing_key():
    """Print the public half of the licence signing key (safe to share; the private half stays in the database)."""
    data_dir = current_data_dir()
    values = require_config(data_dir)
    server = connect(data_dir)
    server.ensure_schema()
    key = server.signing_key()
    say(f"Key ID: {server.signing_key_id(key)}")
    say("Public key as JSON (HeavyPro's build reads it from " + site_url(values.get("SITE_URL")) + "/api/licenses/jwks.json):")
    say(json.dumps(server.signing_jwk(key)))
    say()
    say(server.signing_pem(key))
    say("The private key is in the database (meta 'license_signing_key') and in every backup. Keep backups private;")
    say("if it is lost, HeavyPro needs a new build with the new public key.")


def cmd_admin_password():
    """Set or remove the admin page password (also possible in Admin > Security). Works when it is forgotten."""
    data_dir = current_data_dir()
    require_config(data_dir)
    server = connect(data_dir)
    server.ensure_schema()
    d = server.DB()
    enabled = bool(d.first("SELECT value FROM meta WHERE key = ?", server.GATE_KEY))
    say("The admin page password is " + ("ON." if enabled else "OFF.") + " Staff enter it after logging in, before the admin page opens.")
    password = getpass.getpass("New admin password, at least 8 characters (Enter = " + ("remove it" if enabled else "keep it off") + "): ")
    if not password:
        if enabled:
            d.run("DELETE FROM meta WHERE key = ?", server.GATE_KEY)
            say("The admin password is removed. Staff only need their own login now.")
        d.close()
        return
    if len(password) < 8:
        say("Too short: nothing changed.")
    elif getpass.getpass("Type it again: ") != password:
        say("The two did not match: nothing changed.")
    else:
        d.run("DELETE FROM meta WHERE key = ?", server.GATE_KEY)
        d.run("INSERT INTO meta (key, value) VALUES (?, ?)", server.GATE_KEY, json.dumps(server.hash_password(password)))
        say("The admin password is set. Everyone who has the admin page open must enter it again.")
    d.close()


# Where fetch-installer copies from: the "beta" release, which every build of HeavyPro's main updates.
INSTALLER_SOURCE = "https://github.com/tornqvisteliaz-cyber/heavypro/releases/download/beta/HeavyProSetup.exe"
INSTALLER_MAX = 1024 * 1024 * 1024


def download_file(url, target):
    """Download url to target in chunks (no size limit in memory). Returns the size."""
    req = urllib.request.Request(url, headers={"User-Agent": "rotaidem-setup/1"})
    size = 0
    with urllib.request.urlopen(req, timeout=60) as resp, open(target, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > INSTALLER_MAX:
                raise ValueError("the file is larger than 1 GB")
            out.write(chunk)
            if size % (10 * 1024 * 1024) < len(chunk):
                say(f"  {size // (1024 * 1024)} MB ...")
    return size


def cmd_fetch_installer(args):
    """Host the installer on rotaidem.com itself: rotaidem-public/files/NAME, plus a .zip and a .sha256.
    Testers then download from your own domain instead of GitHub, and the Download buttons point there."""
    import zipfile
    data_dir = current_data_dir()
    values = require_config(data_dir)
    public_dir = read_location().get("public_dir")
    if not public_dir or not os.path.isdir(public_dir):
        say(f"The website folder is not set up yet. Run first:  python3 rotaidem/strato/setup.py deploy")
        sys.exit(1)
    rest = [a for a in args if not a.startswith("--")]
    source = rest[0] if rest else INSTALLER_SOURCE
    product_id = rest[1] if len(rest) > 1 else "heavypro"
    name = os.path.basename(urllib.parse.urlparse(source).path)
    local_test = source.startswith("http://127.0.0.1:")  # only for the automated tests
    if not (source.startswith("https://") or local_test) or not re.match(r"^[A-Za-z0-9._-]+\.(exe|msi|zip)$", name):
        say("Give a https:// link that ends in a file name like HeavyProSetup.exe, .msi or .zip.")
        sys.exit(1)
    files = os.path.join(public_dir, "files")
    os.makedirs(files, exist_ok=True)
    os.chmod(files, 0o755)
    temp = os.path.join(data_dir, name + ".download")  # private until it is checked
    say(f"Downloading {source} ...")
    try:
        size = download_file(source, temp)
    except urllib.error.HTTPError as exc:
        say(f"GitHub answered {exc.code}. " + ("The release has no file with that name yet: check that the HeavyPro build "
            "ran on main (Actions tab) and that the beta release lists HeavyProSetup.exe." if exc.code == 404 else ""))
        sys.exit(1)
    except (OSError, ValueError) as exc:
        say(f"The download failed: {exc}")
        sys.exit(1)
    with open(temp, "rb") as fh:
        head = fh.read(2)
    expected = b"PK" if name.endswith(".zip") else (b"MZ" if name.endswith(".exe") else b"")
    if size < 100 * 1024 or (expected and head != expected):
        os.remove(temp)
        say(f"That is not an installer ({size} bytes). Nothing on the website was changed.")
        sys.exit(1)
    digest = hashlib.sha256()
    with open(temp, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    sha = digest.hexdigest()
    target = os.path.join(files, name)
    os.chmod(temp, 0o644)
    os.replace(temp, target)  # one step, so a download that is running never gets half a file
    write_file(target + ".sha256", f"{sha}  {name}\n")
    made = [name, name + ".sha256"]
    if name.endswith(".exe"):
        # Some browsers, mail filters and company networks block bare .exe files; a zip gets through.
        zip_name = name[:-4] + ".zip"
        temp_zip = os.path.join(data_dir, zip_name + ".download")
        with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(target, name)
        os.chmod(temp_zip, 0o644)
        os.replace(temp_zip, os.path.join(files, zip_name))
        made.append(zip_name)
    say(f"  {size / (1024 * 1024):.1f} MB, SHA-256 {sha}")
    say("  Saved in " + files + ": " + ", ".join(made))
    link = f"{site_url(values.get('SITE_URL'))}/files/{name}?v={sha[:10]}"  # ?v= makes browsers fetch a new build
    os.environ["ROTAIDEM_NO_SCHEMA_UPDATE"] = "1"
    server = connect(data_dir)
    d = server.DB()
    row = d.first("SELECT id, download_url FROM products WHERE id = ?", product_id)
    if row:
        try:  # updated_at tells RotaHub there is a new release
            d.run("UPDATE products SET download_url = ?, updated_at = ? WHERE id = ?", link, server.utc_stamp(), product_id)
        except Exception:  # noqa: BLE001 - a database from before that column: the link alone
            d.run("UPDATE products SET download_url = ? WHERE id = ?", link, product_id)
        say(f"The Download buttons for '{product_id}' now point to:  {link}")
        if row.get("download_url") and not str(row["download_url"]).startswith(link.split("?")[0]):
            say(f"  (Before: {row['download_url']})")
    else:
        say(f"There is no product '{product_id}' in the catalogue. Put this link in Admin > Products > Installer download link:")
        say("  " + link)
    d.close()


TIMESTAMP_COLUMNS = {"created_at", "updated_at", "first_seen", "last_seen"}


def cmd_import(path):
    data_dir = current_data_dir()
    require_config(data_dir)
    if not os.path.isfile(path):
        say(f"File not found: {path}")
        return
    with open(path, encoding="utf-8") as fh:
        backup = json.load(fh)
    tables = backup.get("tables") or {}
    say("The backup contains: " + ", ".join(f"{t} ({len(r)})" for t, r in tables.items()))
    if input("Everything now in the database is replaced. Type YES to continue: ").strip() not in ("YES", "JA"):
        say("Cancelled. Nothing changed.")
        return
    server = connect(data_dir)
    server.ensure_schema()
    d = server.DB()
    for table, rows in tables.items():
        if table not in server.BACKUP_TABLES:
            continue
        columns = set(server.table_columns(d, table))
        d.run(f"DELETE FROM {table}" + (" WHERE key <> 'schema_version'" if table == "meta" else ""))
        for row in rows:
            if table == "meta" and row.get("key") == "schema_version":
                continue
            row = {k: v for k, v in row.items() if k in columns}
            for k in TIMESTAMP_COLUMNS & row.keys():
                if isinstance(row[k], str):
                    row[k] = row[k][:19].replace("T", " ")  # PostgreSQL adds fractions and a time zone
            names = ", ".join(row)
            d.run(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' for _ in row)})", *row.values())
        say(f"  {table}: {len(rows)} rows")
    d.close()
    public_dir = read_location().get("public_dir")
    if public_dir and os.path.isdir(public_dir):
        publish_static(public_dir, data_dir, refresh=True)  # picture numbers now belong to the imported pictures
    say("Import finished. Log in with the accounts and passwords from the backup.")


def main():
    if sys.version_info < (3, 9):
        say("Python 3.9 or newer is needed. Type python3, not python.")
        sys.exit(1)
    if not os.access(WEBSPACE, os.W_OK):
        say(f"This copy of the program is in {APP_DIR}, but its parent folder {WEBSPACE} is not writable.")
        say("Run the setup from the program folder inside your webspace, for example:")
        say("    cd /home/www && python3 rotaidem/strato/setup.py deploy")
        sys.exit(1)
    os.umask(0o077)  # anything this program creates is private unless chmod'ed on purpose
    args = sys.argv[1:]
    command = args[0] if args else "help"
    if command in ("deploy", "update"):
        cmd_deploy(args[1:])
    elif command in ("diagnose", "check"):
        rest = [a for a in args[1:] if not a.startswith("--")]
        report = diagnose(offline="--offline" in args, domain=rest[0] if rest else None)
        sys.exit(1 if report.fails else 0)
    elif command == "backup":
        cmd_backup()
    elif command == "install":
        cmd_install()
    elif command == "permissions":
        cmd_permissions(args[1] if len(args) > 1 else "strict")
    elif command == "reset-catalog":
        cmd_reset_catalog()
    elif command == "password":
        cmd_password()
    elif command == "import" and len(args) == 2:
        cmd_import(args[1])
    elif command == "email-test" and len(args) == 2:
        cmd_email_test(args[1])
    elif command == "admin-password":
        cmd_admin_password()
    elif command == "signing-key":
        cmd_signing_key()
    elif command == "stripe":
        cmd_stripe()
    elif command == "fetch-installer":
        cmd_fetch_installer(args[1:])
    elif command in ("help", "-h", "--help"):
        say(__doc__)
    else:
        import difflib
        known = ["deploy", "diagnose", "backup", "install", "password", "permissions", "import", "reset-catalog", "update",
                 "check", "email-test", "admin-password", "fetch-installer", "signing-key", "stripe"]
        close = difflib.get_close_matches(command, known, n=1)
        say(f"Unknown command '{command}'." + (f" Did you mean:  python3 rotaidem/strato/setup.py {close[0]}" if close else ""))
        say("All commands:  python3 rotaidem/strato/setup.py help")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nCancelled.")
