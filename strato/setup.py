"""Installs and maintains Rotaidem on STRATO Webhosting (or any Apache host with Python CGI).

Run over SSH from the webspace folder that contains the uploaded "rotaidem" folder:

    python3 rotaidem/strato/setup.py              first installation (asks questions)
    python3 rotaidem/strato/setup.py update       after uploading a new version
    python3 rotaidem/strato/setup.py check        tests the settings and the database
    python3 rotaidem/strato/setup.py permissions  if the web server may not read the settings
    python3 rotaidem/strato/setup.py password     sets a new password for an account
    python3 rotaidem/strato/setup.py import FILE  loads a backup downloaded from the old site
"""
import getpass
import json
import os
import re
import secrets
import shutil
import sys
import urllib.request

STRATO_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(STRATO_DIR)
PUBLIC_SRC = os.path.join(APP_DIR, "cloudflare", "public")
DEFAULT_DATA_DIR = os.path.join(os.path.dirname(APP_DIR), "rotaidem-data")
CONFIG_NAME = "config.env"
LOCATION_FILE = os.path.join(STRATO_DIR, "installed.json")  # remembers the folders for update/check
SKIP_PUBLIC = {"_routes.json", "_headers", "_redirects"}  # Cloudflare-only files
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
    hint = "J/n" if default else "j/N"
    answer = input(f"{question} ({hint}): ").strip().lower()
    return default if not answer else answer in ("j", "ja", "y", "yes")


def ask_secret(question, min_length=0, confirm=False):
    while True:
        value = getpass.getpass(f"{question}: ")
        if len(value) < min_length:
            say(f"  Minst {min_length} tecken. Försök igen.")
            continue
        if confirm and getpass.getpass("Skriv samma igen: ") != value:
            say("  De två var inte lika. Försök igen.")
            continue
        return value


def write_file(path, text, mode=0o644):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.chmod(path, mode)


def load_location():
    if not os.path.exists(LOCATION_FILE):
        say("Rotaidem är inte installerat än. Kör först:  python3 rotaidem/strato/setup.py")
        sys.exit(1)
    with open(LOCATION_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def save_location(public_dir, data_dir):
    write_file(LOCATION_FILE, json.dumps({"public_dir": public_dir, "data_dir": data_dir}, indent=2) + "\n")


def config_path(data_dir):
    return os.path.join(data_dir, CONFIG_NAME)


def write_config(path, values):
    lines = ["# Rotaidem settings. Keep this file secret: it holds passwords and keys.",
             "# Change a value, save, and the next page load uses it (no restart needed)."]
    lines += [f"{k}={v}" for k, v in values.items()]
    write_file(path, "\n".join(lines) + "\n", 0o600)


def connect(data_dir):
    """Load the settings and return server.py's database class, as the website itself would."""
    cgi_app.prepare(APP_DIR, config_path(data_dir))
    import server
    return server


# ---------- installing the files ----------

def htaccess_public(public_dir, data_dir):
    inside = []
    for folder in (APP_DIR, data_dir):
        rel = os.path.relpath(folder, public_dir)
        if not rel.startswith(".."):
            inside.append(re.escape(rel.replace(os.sep, "/")))
    block_internal = f"RewriteRule ^({'|'.join(inside)})(/|$) - [F,L]\n" if inside else ""
    return f"""{MARK}
# The website is plain files. Only /api/..., /media/... and /health go to the Python program.
Options -Indexes
DirectoryIndex index.html
ErrorDocument 404 /404.html

<IfModule mod_rewrite.c>
RewriteEngine On
RewriteBase /

# Apache does not pass the login header to CGI programs unless it is copied like this.
RewriteCond %{{HTTP:Authorization}} .
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{{HTTP:Authorization}}]

# Never serve the program folder or the settings and database folder.
{block_internal}RewriteRule (^|/)\\.(?!well-known) - [F,L]

# The backend: Apache runs cgi-bin/rotaidem.py for each request; no server process has to keep running.
RewriteRule ^((api|media)/.*|health)$ cgi-bin/rotaidem.py/$1 [L,QSA]

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
<FilesMatch "^(admin|account)\\.html$">
Header set Cache-Control "private, no-store"
</FilesMatch>
</IfModule>
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


def cgi_script(target, data_dir):
    return f"""#!/usr/bin/python3
# Written by rotaidem/strato/setup.py. Runs the Rotaidem backend once per request (CGI).
import sys
sys.path.insert(0, {STRATO_DIR!r})
import cgi_app
cgi_app.{target}({APP_DIR!r}, {config_path(data_dir)!r})
"""


def install_files(public_dir, data_dir):
    say("Kopierar webbplatsens filer ...")
    for name in os.listdir(PUBLIC_SRC):
        if name in SKIP_PUBLIC:
            continue
        src, dst = os.path.join(PUBLIC_SRC, name), os.path.join(public_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)

    htaccess = os.path.join(public_dir, ".htaccess")
    if os.path.exists(htaccess):
        with open(htaccess, encoding="utf-8", errors="replace") as fh:
            old = fh.read()
        backup = htaccess + ".before-rotaidem"
        if MARK not in old and not os.path.exists(backup):
            shutil.copy2(htaccess, backup)
            say(f"  Den gamla .htaccess sparades som {backup}")
    write_file(htaccess, htaccess_public(public_dir, data_dir))
    write_file(os.path.join(APP_DIR, ".htaccess"), DENY_ALL)
    write_file(os.path.join(data_dir, ".htaccess"), DENY_ALL)

    cgi_bin = os.path.join(public_dir, "cgi-bin")
    os.makedirs(cgi_bin, exist_ok=True)
    os.chmod(cgi_bin, 0o755)
    for name, target in (("rotaidem.py", "run"), ("rotaidem-check.py", "run_check")):
        write_file(os.path.join(cgi_bin, name), cgi_script(target, data_dir), 0o755)


def prepare_database(data_dir):
    say("Skapar tabellerna i databasen ...")
    server = connect(data_dir)
    server.ensure_schema()
    d = server.DB()
    users = d.first("SELECT COUNT(*) AS n FROM users")["n"]
    d.close()
    say(f"  Databasen fungerar. Konton just nu: {users}")


# ---------- commands ----------

def ask_database():
    while True:
        say()
        say("Var ska databasen ligga?")
        say("  1 = MySQL-databas som ingår i ditt STRATO-paket (rekommenderas)")
        say("  2 = En SQLite-fil i mappen rotaidem-data")
        choice = ask("Välj 1 eller 2", "1")
        if choice == "2":
            return {}
        say("Uppgifterna finns hos STRATO under Datenbanken / Databaser (Datenbankverwaltung).")
        values = {
            "MYSQL_HOST": ask("Server (Host), t.ex. database-5012345678.webspace-host.com"),
            "MYSQL_PORT": ask("Port", "3306"),
            "MYSQL_DATABASE": ask("Databasnamn, t.ex. dbs12345678"),
            "MYSQL_USER": ask("Användarnamn, t.ex. dbu12345678"),
            "MYSQL_PASSWORD": ask_secret("Databasens lösenord (syns inte när du skriver)"),
        }
        try:
            sys.path.insert(0, os.path.join(STRATO_DIR, "vendor"))
            import pymysql
            options = dict(host=values["MYSQL_HOST"], port=int(values["MYSQL_PORT"]), user=values["MYSQL_USER"],
                           password=values["MYSQL_PASSWORD"], database=values["MYSQL_DATABASE"], connect_timeout=10)
            try:
                pymysql.connect(ssl={"check_hostname": False}, **options).close()  # same as server.py
            except Exception:  # noqa: BLE001
                pymysql.connect(**options).close()
            say("  Anslutningen till MySQL fungerar.")
            return values
        except Exception as exc:  # noqa: BLE001
            say(f"  Kunde inte ansluta: {exc}")
            if not ask_yes("Försöka igen?"):
                sys.exit(1)


def cmd_install():
    say("=== Installera Rotaidem på STRATO ===")
    say(f"Programmet ligger i: {APP_DIR}")
    say()
    say("Vilken mapp visar STRATO för rotaidem.com? Det står hos STRATO under")
    say("Domains > Domainverwaltung > rotaidem.com > Einstellungen (Ziel / Verzeichnis).")
    say("Står det bara / är det mappen nedan. Ändra inget hos STRATO, skriv bara av mappen.")
    webspace = os.path.dirname(APP_DIR)
    while True:
        folder = ask("Mappen för rotaidem.com", "/")
        public_dir = os.path.normpath(os.path.join(webspace, folder.strip("/"))) if not folder.startswith(webspace) else folder
        if os.path.isdir(public_dir):
            break
        say(f"  Hittar inte {public_dir}. Kontrollera stavningen.")
    data_dir = DEFAULT_DATA_DIR
    os.makedirs(data_dir, exist_ok=True)
    os.chmod(data_dir, 0o700)
    cfg = config_path(data_dir)

    if os.path.exists(cfg) and ask_yes("Det finns redan inställningar. Behålla dem?"):
        values = cgi_app.load_config(cfg)
    else:
        values = {"SECRET_KEY": secrets.token_hex(32), "CHECK_KEY": secrets.token_hex(8)}
        values.update(ask_database())
        if "MYSQL_HOST" not in values:
            values["DATA_DIR"] = data_dir
        say()
        say("Ditt administratörskonto (används första gången du loggar in på /login):")
        values["ADMIN_USERNAME"] = ask("Användarnamn", "eliaz")
        values["ADMIN_EMAIL"] = ask("E-post", "Eliaz@rotaidem.com")
        values["ADMIN_PASSWORD"] = ask_secret("Lösenord, minst 8 tecken (syns inte när du skriver)", 8, confirm=True)
        say()
        values["SITE_URL"] = ask("Webbplatsens adress", "https://rotaidem.com")
        say()
        say("E-post via Resend (för licensmejl och utskick). Tryck bara Enter för att hoppa över.")
        key = ask("RESEND_API_KEY (börjar med re_)")
        if key:
            values["RESEND_API_KEY"] = key
            values["MAIL_FROM"] = ask("Avsändare", "Rotaidem <no-reply@rotaidem.com>")
        say()
        say("Nyckeln som licensgeneratorn använder. Har du en från Render, klistra in samma.")
        values["LICENSE_API_KEY"] = ask("LICENSE_API_KEY (Enter = skapa en ny)") or secrets.token_hex(24)
        write_config(cfg, values)
        say(f"Inställningarna sparades i {cfg}")

    save_location(public_dir, data_dir)
    install_files(public_dir, data_dir)
    prepare_database(data_dir)
    finish(values)


def finish(values):
    site = values.get("SITE_URL", "https://rotaidem.com").rstrip("/")
    say()
    say("=== Klart ===")
    say("1. Öppna kontrollsidan i webbläsaren. Allt ska vara grönt:")
    say(f"   {site}/cgi-bin/rotaidem-check.py?key={values.get('CHECK_KEY', '')}")
    say(f"2. Logga in på {site}/login med ditt administratörskonto.")
    if values.get("LICENSE_API_KEY"):
        say(f"3. Licensgeneratorns nyckel (LICENSE_API_KEY): {values['LICENSE_API_KEY']}")


def cmd_update():
    loc = load_location()
    install_files(loc["public_dir"], loc["data_dir"])
    prepare_database(loc["data_dir"])
    say("Uppdateringen är klar.")


def cmd_check():
    loc = load_location()
    cfg = config_path(loc["data_dir"])
    say(f"Webbplatsens mapp: {loc['public_dir']}")
    say(f"Inställningar:     {cfg}")
    values = cgi_app.load_config(cfg)
    say("Databas:           " + ("MySQL " + values["MYSQL_HOST"] if values.get("MYSQL_HOST") else "SQLite i " + values.get("DATA_DIR", "")))
    cgi_app.prepare(APP_DIR, cfg)  # also puts strato/vendor on the path, as the website does
    try:
        import flask
        say(f"Flask:             {getattr(flask, '__version__', 'ok')}")
    except ImportError:
        say("Flask:             SAKNAS. Se felsökning i STRATO.md.")
    prepare_database(loc["data_dir"])
    site = values.get("SITE_URL", "https://rotaidem.com").rstrip("/")
    try:
        with urllib.request.urlopen(site + "/api/site", timeout=20) as resp:
            json.load(resp)
        say(f"Webbplatsen svarar på {site}/api/site")
    except Exception as exc:  # noqa: BLE001 - SSH may not be allowed to reach the web; the check page covers it
        say(f"Kunde inte testa {site}/api/site härifrån ({exc}). Öppna kontrollsidan i webbläsaren i stället.")
    say(f"Kontrollsida: {site}/cgi-bin/rotaidem-check.py?key={values.get('CHECK_KEY', '')}")


def cmd_permissions():
    loc = load_location()
    cfg = config_path(loc["data_dir"])
    os.chmod(loc["data_dir"], 0o755)
    os.chmod(cfg, 0o644)
    say("Webbservern får nu läsa inställningarna (mappen skyddas fortfarande av .htaccess).")
    values = cgi_app.load_config(cfg)
    if not values.get("MYSQL_HOST"):
        os.chmod(loc["data_dir"], 0o777)
        db_file = os.path.join(loc["data_dir"], "rotaidem.db")
        if os.path.exists(db_file):
            os.chmod(db_file, 0o666)
        say("SQLite-filen får nu skrivas av webbservern.")


def cmd_password():
    loc = load_location()
    server = connect(loc["data_dir"])
    server.ensure_schema()
    d = server.DB()
    who = ask("E-post eller användarnamn för kontot").lower()
    user = d.first("SELECT id, name FROM users WHERE LOWER(email) = ? OR LOWER(name) = ?", who, who)
    if not user:
        say("Hittar inget sådant konto.")
        return
    password = ask_secret("Nytt lösenord, minst 8 tecken", 8, confirm=True)
    d.run("UPDATE users SET password_hash = ?, token = NULL WHERE id = ?", server.hash_password(password), user["id"])
    d.close()
    say(f"Lösenordet för {user['name']} är ändrat.")


TIMESTAMP_COLUMNS = {"created_at", "first_seen", "last_seen"}


def cmd_import(path):
    loc = load_location()
    if not os.path.isfile(path):
        say(f"Hittar inte filen {path}. Ladda upp den med SFTP till samma mapp som rotaidem-mappen.")
        return
    with open(path, encoding="utf-8") as fh:
        backup = json.load(fh)
    tables = backup.get("tables") or {}
    say("Säkerhetskopian innehåller: " + ", ".join(f"{t} ({len(r)})" for t, r in tables.items()))
    if input("Allt som redan finns i den nya databasen ersätts. Skriv JA för att fortsätta: ").strip() != "JA":
        say("Avbrutet. Inget ändrades.")
        return
    server = connect(loc["data_dir"])
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
        say(f"  {table}: {len(rows)} rader")
    d.close()
    say("Importen är klar. Logga in med samma konton och lösenord som på den gamla webbplatsen.")


def main():
    if sys.version_info < (3, 8):
        say("Python 3.8 eller nyare behövs. Skriv python3, inte python.")
        sys.exit(1)
    args = sys.argv[1:]
    command = args[0] if args else "install"
    if command == "install":
        cmd_install()
    elif command == "update":
        cmd_update()
    elif command == "check":
        cmd_check()
    elif command == "permissions":
        cmd_permissions()
    elif command == "password":
        cmd_password()
    elif command == "import" and len(args) == 2:
        cmd_import(args[1])
    else:
        say(__doc__)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nAvbrutet.")
