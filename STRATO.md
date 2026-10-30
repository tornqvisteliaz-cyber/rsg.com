# Rotaidem on STRATO Webhosting

The website runs on STRATO Webhosting Basic without a server process of your own: Apache serves the
pages as plain files and hands `/api/...`, `/media/...` and `/health` to the Python backend
(`server.py`, Flask). Your domain, DNS and e-mail stay exactly as they are at STRATO.

**How STRATO runs Python (confirmed from a traceback on the live server):** files in `cgi-bin` are
not run as classic CGI programs. A FastCGI launcher managed by STRATO (`cm4all/fcgi/launcher.py`,
using `flup`) imports the file, keeps it loaded, and calls its `application` function (WSGI) for
each request. The entry scripts written by `deploy` provide `application` for this, and still work
as classic CGI on other hosts. Because the process stays loaded, a changed `config.env` or
`server.py` is picked up automatically on the next request.

## Why the site showed 403 Forbidden

- STRATO's new platform (home folder `/home/www`) **cannot use `/home/www` itself as a domain
  target**: you must pick a subfolder ([STRATO FAQ](https://www.strato.de/faq/domains/alles-zur-domainumleitung/)).
- The earlier setup copied the website straight into `/home/www`. So none of the subfolders STRATO
  offered (`rotaidem`, `cloudflare`, `website`, `strato`, `licensing`, `.github`, `__pycache__`,
  `rsg.com-claude-lucid-tesla-liazv2`) contains the website's `index.html`:
  - `rotaidem` is the private program folder. Its `.htaccess` denies everything, which gives exactly
    "403 Forbidden - You don't have permission to access this resource".
  - The others have no `index.html`, which also gives 403. `rsg.com-claude-lucid-tesla-liazv2` would
    even let anyone download the source code. **Never choose any of these.**
- The old `/home/www/.htaccess` also applied to every subfolder. `deploy` renames it (nothing is deleted).

**ERR_TIMED_OUT** is a different problem: the browser could not connect at all. It cannot come from
`.htaccess` or file permissions. Usual causes are DNS still pointing to an old address shortly after a
change, a wrong IPv6 (AAAA) entry, or the `www` name not being connected. `diagnose` tests this
(see below).

## Layout

```
/home/www/rotaidem/           program source (this repository)          - private
/home/www/rotaidem-data/      config.env (secrets), logs, installed.json - private
/home/www/rotaidem-public/    the website                                 <- domain target
/home/www/rotaidem-backups/   made by the backup command                  - private
```

`rotaidem-public` contains only the website files from `cloudflare/public`, a `.htaccess` and two
small CGI entry points in `cgi-bin/`. The program, the settings and the database are outside it, so
they cannot be reached through the domain at all. They also keep their deny-all `.htaccess` files.

## Deploy (do this once now)

Log in with SSH (your password is asked; it is not shown while typing):

```
ssh su929110@5021693289.ssh.w2.strato.hosting
```

Then run these lines one by one:

```
cd /home/www
git clone --depth 1 -b claude/lucid-tesla-liazv2 https://github.com/tornqvisteliaz-cyber/rsg.com.git rotaidem-new
python3 rotaidem-new/strato/setup.py backup
mv -T rotaidem rotaidem-old
mv -T rotaidem-new rotaidem
python3 rotaidem/strato/setup.py deploy
```

What each line does:

1. Goes to your webspace.
2. Downloads the fixed version into a new folder. Your current folder is not touched.
3. Copies your settings and your whole database into `rotaidem-backups/<date>/` (private).
4. Keeps the old program folder as `rotaidem-old`. If it says "Directory not empty", a
   `rotaidem-old` already exists: use `rotaidem-old2` instead.
5. Puts the new version in its place.
6. Creates `rotaidem-public` with the website, checks the database tables (adds missing ones, never
   deletes anything) and runs the checks. It must end with `Result: no problems found`.

`deploy` never changes `config.env`, never empties the database and never deletes or overwrites the
old files in `/home/www`.

## Point the domain at the website folder (STRATO login)

1. **Domains > Domainverwaltung > rotaidem.com > Einstellungen / Umleitung.**
2. Choose the internal target (Verzeichnis) **`rotaidem-public`** and save.
3. If `www.rotaidem.com` is listed separately, give it the same target `rotaidem-public`.
4. Under **SSL**, check that the certificate covers `rotaidem.com` (and `www.rotaidem.com`).

Do **not** change DNS records, nameservers or e-mail settings: they are not needed.

Wait a few minutes, then run:

```
python3 /home/www/rotaidem/strato/setup.py diagnose
```

`diagnose` only reads; it changes nothing. It checks the folders and permissions, runs the backend
once like Apache would, reads the database, and then tests the real domain: DNS, the connection,
that `https://rotaidem.com` shows **this** `rotaidem-public` (through `rotaidem-deploy.txt`), the
pages, `/health`, `/api/site`, and that private files cannot be downloaded.

Finally, open `https://rotaidem.com`, log in at `/login`, and open `/admin`.

## Updating later

With Git:

```
cd /home/www
git -C rotaidem pull
python3 rotaidem/strato/setup.py deploy
```

With FileZilla: download the ZIP of the branch from GitHub, upload its contents over
`/home/www/rotaidem` (let it overwrite), then run `python3 rotaidem/strato/setup.py deploy` over SSH.
Never upload into `rotaidem-data` or `rotaidem-public` by hand: `deploy` fills `rotaidem-public`.

## When diagnose reports a problem

| What you see | Cause | Fix |
|---|---|---|
| `https://rotaidem.com/rotaidem-deploy.txt -> 403` or `404` | The domain points to another folder (for example `rotaidem` or `cloudflare`) | Set the domain target to `rotaidem-public` (above) |
| `port 443 does not answer (timeout)` | DNS still has an old address, or IPv6 is wrong | Wait up to 24 h after changing the target. If it stays, send STRATO support the addresses diagnose prints |
| `the name does not resolve` | The domain or `www` has no address | Check that `www.rotaidem.com` exists and has the same target at STRATO |
| `certificate problem` | SSL not active for this name | Activate the SSL certificate for the domain at STRATO |
| `/health` or `/api/site` -> 404 or 500 while the pages load | Python CGI does not run in `cgi-bin` | `python3 rotaidem/strato/setup.py deploy --cgi-htaccess`. If that gives 500, undo it with `deploy --no-cgi-htaccess` and ask STRATO whether Python CGI is enabled for your package |
| `module 'rotaidem' has no attribute 'application'` (traceback in the browser) | The entry scripts come from an older version | Upload the new code and run `deploy` |
| `A 1.5 MB request ... -> 413` or `500` | STRATO limits request size | Image uploads in the admin are too big for the host; tell me the line |
| `/health` -> 503 `database unavailable` | MySQL not reachable or wrong login | Check the `MYSQL_*` lines in `rotaidem-data/config.env` against STRATO's database page |
| `old Rotaidem rules ... also apply to the website folder` | The old `/home/www/.htaccess` | Run `deploy` (it renames the file) |
| `The web server runs as user ...` (check page) | CGI does not run as your account | `python3 rotaidem/strato/setup.py permissions group` if you share a group; otherwise ask STRATO. Never make the files readable for everyone |

On your own computer you can test the connection too (Windows PowerShell):

```
Resolve-DnsName rotaidem.com
Test-NetConnection rotaidem.com -Port 443
```

`ipconfig /flushdns` clears your computer's DNS cache after a change.

Visitors never see error details. They are written to `rotaidem-data/logs/` (private), and
`diagnose` prints the last lines.

## Commands

```
python3 rotaidem/strato/setup.py deploy          install or update the website (keeps all data)
python3 rotaidem/strato/setup.py diagnose        read-only checks, including the live domain
python3 rotaidem/strato/setup.py diagnose --offline   only the checks on the server itself
python3 rotaidem/strato/setup.py backup          settings + full database into rotaidem-backups
python3 rotaidem/strato/setup.py password        new password for an account
python3 rotaidem/strato/setup.py permissions     make settings and logs private (700/600) again
python3 rotaidem/strato/setup.py admin-password  set, change or remove the extra admin page password (also when forgotten)
python3 rotaidem/strato/setup.py stripe          set up payments: Stripe key and webhook secret (typed, not shown)
python3 rotaidem/strato/setup.py discord         connect the Discord bot for the verified buyer role (secrets typed, not shown)
python3 rotaidem/strato/setup.py fetch-installer  copy the newest HeavyPro installer from GitHub to rotaidem.com/files/ (Download buttons follow)
python3 rotaidem/strato/setup.py import FILE     restore a backup (replaces the database; asks first)
python3 rotaidem/strato/setup.py reset-catalog   remove all products (asks first, keeps a copy)
python3 rotaidem/strato/setup.py install         first installation on a new webspace (asks questions)
```

There is also a check page in the browser:
`https://rotaidem.com/cgi-bin/rotaidem-check.py?key=<CHECK_KEY>`. The key is the `CHECK_KEY` line
in `rotaidem-data/config.env`; don't share screenshots that show it.

## Payments (Stripe)

1. In Stripe (dashboard.stripe.com): Developers → API keys → **Create restricted key** with
   *Checkout Sessions: Write* only. Use a `rk_test_`/`sk_test_` key first to try it with test cards.
2. Developers → Webhooks → **Add endpoint**: `https://rotaidem.com/api/stripe/webhook`, events
   `checkout.session.completed` and `checkout.session.async_payment_succeeded`. Reveal its signing secret.
3. Over SSH: `python3 rotaidem/strato/setup.py stripe`, paste both (nothing is shown), and it tests the key.
4. Admin → Products → a product → **Price at checkout** (amount and currency) → Save.

The product's Buy button then opens Stripe's payment page (the customer logs in first, so the key
is on their account). After paying they come back to `/checkout-done`, which shows the licence key;
the order (marked *Stripe* in Admin → Orders) and the key are made once, and the key is emailed.
The webhook does the same when someone closes the page right after paying. The keys are only in
`rotaidem-data/config.env`; never paste them anywhere else.

## Discord: verified buyers get the buyer channels

No bot program has to run: the website does everything through Discord's API when it is needed.
Customers press **Link Discord** under My account; the website adds them to your server and gives
licence holders a *Verified buyer* role. The role is taken away when a licence is revoked and given
back when it is restored. `/verify` in Discord lets members check themselves.

1. discord.com/developers/applications → **New Application** (e.g. Rotaidem).
2. **OAuth2 → Redirects → Add**: `https://rotaidem.com/api/discord/callback`.
3. Over SSH: `python3 rotaidem/strato/setup.py discord`. It asks for the Application ID and Public
   Key (General Information), the Client Secret (OAuth2 → Reset Secret) and the bot token (Bot → Reset
   Token). Secrets are not shown while typing. It tests the token and prints the invite link.
4. Open the invite link and add the bot to your server.
5. In Discord: Server Settings → Roles → make a role such as **Verified buyer**. Drag the bot's own
   role **above** it, or it cannot give it.
6. Lock the buyer channels: edit each channel → Permissions → `@everyone`: View Channel **off**,
   Verified buyer: View Channel **on**.
7. User Settings → Advanced → **Developer Mode** on. Right-click the server → Copy Server ID, and the
   role → Copy Role ID. Paste both in **Admin → Discord** on the website and save.
8. In the Developer Portal → General Information → **Interactions Endpoint URL**:
   `https://rotaidem.com/api/discord/interactions` → Save (Discord tests it then). Then press
   **Add the /verify command** in Admin → Discord.
9. Optional: in Admin → Discord, **Post a Verify button** in a channel such as #welcome.

## Security

- **Settings:** `config.env` holds the secrets (database password, `SECRET_KEY`, `LICENSE_API_KEY`,
  `RESEND_API_KEY`). It lives outside the website folder and is mode 600 (folder 700). It is never
  in Git and never printed by `diagnose` or `backup`.
- **Passwords:** stored only as PBKDF2 hashes. The admin password is never written to a file.
- **Website folder:** `.htaccess` refuses hidden files, `.py/.env/.sql/.db/.log/.bak/...` outside
  `cgi-bin`, and folder listings. It contains no `Options` line, so hosts that forbid it cannot answer
  500 for the whole site.
- **Backups:** written to `rotaidem-backups/` (mode 700, files 600, deny-all `.htaccess`).
- **Cleaning up later:** once everything works, the old copies (`/home/www/index.html` etc.,
  `cgi-bin`, `cloudflare`, `website`, `strato`, `licensing`, `.github`, `__pycache__`,
  `rsg.com-claude-lucid-tesla-liazv2`, `rotaidem-old`) can be deleted. Make a backup first. They are
  not used, but there is no hurry.

## Tests

`python3.11 -m unittest discover -s strato/tests -v` runs every request as a separate CGI process,
exactly as Apache does. It covers SQLite, and MySQL/MariaDB when `ROTAIDEM_TEST_MYSQL` is set (see
the file). It checks:

- installation and deploy safety: no install into unsafe folders, redeploy keeps data and settings,
  moving away from the old root layout, retiring old root rules;
- read-only `diagnose`, `backup` and import;
- login and roles, orders, licences, shared profiles, uploads, e-mail (fake Resend);
- `/health`, and that visitors see no error details.
