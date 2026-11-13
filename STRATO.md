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

**Posting with the bot:** Admin → Discord → *Post to Discord*. Pick a channel, write a message and/or a
card (title, text, link, picture), add link buttons, and choose who to ping (nobody, the buyer role,
@here or @everyone). *Start from…* fills it in from a product update or a news post. In an
announcement channel it is also published to servers that follow it. Posts can be edited or deleted
from the list below the form.

- **#channel links:** type `#channel-name` (or use *Insert a # channel*): it becomes a link in Discord.
- **Extras:** the bot can react with emojis, pin the post, and add a **poll** (2 to 10 answers, open
  from an hour to two weeks).
- **Post later:** pick a date and time. STRATO has no timer, so the post goes out at the first visit
  to the website after that time (with normal traffic, within a minute or two). *Post now* and
  *Cancel* are in the waiting list.
- **Automatic posts:** choose a channel for product updates (optionally pinging verified buyers), for
  news posts, and for sales (🎉 product and amount only; use a staff-only channel).

The bot needs **Send Messages**, **Embed Links**, **Add Reactions**, **Manage Messages** (pins),
**Read Message History**, **Send Polls** and, for @everyone pings, **Mention Everyone**: the invite
link from `setup.py discord` (and Admin → Discord → Invite the bot) asks for all of them. If you
invited the bot before, open the invite link again or give its role those permissions in Server
Settings → Roles.

**More tools in Admin → Discord:**

- **Support tickets:** every ticket from `/support` or the ticket button, open and closed. Read it,
  answer as the team (the bot posts it with your name), close or reopen it. A closed ticket's whole
  conversation is saved in **Messages** (marked handled), also when it is closed from Discord.
- **Members & moderation:** find a member by name or ID (you see their website account if linked),
  then **timeout** (10 minutes to a week), **kick** or **ban**; *Show bans* lists bans with **Unban**.
  The reason you type shows in Discord's audit log.
- **Role buttons:** pick roles (e.g. News pings, MSFS 2024) and post buttons; members click to get or
  remove a role. The buyer role and roles with moderator powers can never be handed out this way.
- **Your own slash commands:** e.g. `/rules` or `/livery` with your reply and a link button. Saving
  updates the commands in your server; `/help` lists them too.
- **Clean up a channel:** delete the last 5 to 100 messages, or only one member's (Discord allows this
  for messages from the last 14 days).
- **Channel controls:** slowmode, and **lock / unlock** a channel for @everyone (other permissions of
  the channel are kept), with an optional note posted in it.
- **Automation:** check the buyer role for every linked member once a day, and celebrate member
  milestones (25, 50, 100, 250, 500, 1,000 …) in a channel you choose.
- **Bot nickname:** what the bot is called in your server.

**Even more (Admin → Discord is split into Post, Support, Moderation, Community, Server, Insights and
Set-up):**

- **Saved answers** (`/tag`): as many as you like (install, refund, vatsim…). Members start typing and
  Discord suggests them; `/tag install for:@someone` mentions that person. Uses are counted.
- **Reminders** (`/remind in:2h about:Group flight`): the bot DMs them when it is time (or pings them in
  the channel if DMs are closed). Up to 30 days, 10 waiting per member.
- **Suggestions** (`/suggest`, when a suggestions channel is chosen): posted with 👍 👎; set Planned,
  Accepted, Declined or Done with a note in the admin, and the post in Discord changes with it.
- **Report to staff**: right-click any message → Apps → *Report to staff* (when a mod log channel is
  chosen). It goes to the mod log with a link; the admin lists reports to mark handled.
- **Forms** (e.g. a beta tester application, up to 5 questions): post its button; answers come to
  the admin; **Approve** can give a role, and the member gets a DM either way.
- **Button giveaways**: members press 🎉 Enter (optionally verified buyers only); winners are drawn
  by themselves when the time is up, with *End now* and *Re-draw*.
- **Ticket topics, Claim and auto-close**: the ticket panel becomes a topic menu (Technical help,
  Licence, Billing…), the team can **Claim** a ticket, and tickets with no reply for N days close.
- **Staff commands in Discord**: `/warn`, `/warnings`, `/timeout`, `/clear`, `/slowmode`, `/lock`,
  `/unlock` and right-click a member → Apps → *User info* (linked account, licences, warnings).
  Only members with the matching Discord permission see them.
- **Warnings**: a DM to the member; at the limit (e.g. 3) they are timed out automatically.
- **Mod log**: every timeout, kick, ban, warning, clean-up, lock, report, application and giveaway.
- **Server management**: create roles (always without permissions), rename/recolour/delete them;
  create channels (optionally only for verified buyers) and delete them; upload custom emojis; list
  and delete invites; read Discord's audit log.
- **Live stat channels**: e.g. `👥 Members: {members}` or `✈️ Pilots: {buyers}`, renamed about every
  30 minutes.
- **Product roles**: an extra role per product (e.g. *HeavyPro owner*) that follows the licence.
- **Repeating posts**: every day or every week at a time you choose (e.g. *Screenshot Saturday*).
- **Give a role to many people**: everyone linked, only licence holders, or only those without.
- **Insights**: what is waiting for you, the most used commands and buttons, and use per day.

Timed things (reminders, giveaways, repeating posts, stat channels, auto-close) run at the first
visit to the website after they are due, like scheduled posts.

**📖 Guide (Admin → Discord → Guide):** every feature with *what it does*, *how to set it up* step by
step and *what members see*, searchable, with a *Take me there* button. Part 4 added:

- **AutoMod** (Discord's own filters, run by Discord all the time): blocked words (with an optional
  timeout), invite links to other servers, mention spam, Discord's spam filter, and ready-made word
  lists. Blocked messages are reported in the mod log.
- **Lockdown**: lock every public channel at once, and open exactly those again.
- **Server safety settings** (who can talk, picture scanning, default notifications) and a **server
  snapshot** file with all roles, channels and permissions.
- **Rules gate**: an *I accept the rules* button that gives a role.
- **Welcome post and welcome DM** with your own text, a **role for everyone who links**, and a
  **thank-you DM** to buyers after a purchase.
- **Invite licence holders** who have not linked Discord, by email (once each, 50 per press).
- **`/orders`**, **`/faq`** answers from the website FAQ, and a *Send me the full keys by DM*
  button on `/licence`.
- Tickets: **one open ticket each**, a **⭐ rating** DM and the **conversation by email** after
  closing, and **saved answers** inside ticket replies.
- **Post as another name and picture** (through a channel webhook), **message templates**, and a
  **discussion thread** under each news post.

The bot also needs **Manage Webhooks** and **Create Public Threads** now: press **Invite the bot**
again and accept.

These need **Timeout Members**, **Kick Members**, **Ban Members**, **Manage Channels**, **Manage
Server** (to list invites), **View Audit Log**, **Manage Expressions** (emojis) and **Change
Nickname** as well. The invite link asks for them; a bot invited earlier: press **Invite the bot**
again and accept (or give its role those permissions). The bot's role must be above the roles it
gives and the members it moderates.

## Affiliate program

Reward people who send you buyers. In **Admin -> Affiliates**, add an affiliate with a short **code**
and a **percent** (e.g. `erik`, 25%). They share their link, `https://rotaidem.com/?ref=erik`. When a
visitor arrives with that link and buys (within 30 days), the affiliate earns that percent of the sale.

- The code travels to Stripe Checkout as metadata, and a paid order credits the commission. Nobody can
  refer themselves (same account or email earns nothing).
- The **To be paid** column is what you owe. Pay it however you like (bank transfer, PayPal, a free
  licence), then press **Mark paid** to clear it. Export CSV for your records.
- **Which orders?** Press **Orders** on a row to see every sale that affiliate brought — the date, the
  buyer, the product, the sale and the commission — plus where they want to be paid and how many times
  their link was opened (**Clicks**).
- **Only you add affiliates.** There is no public sign-up. You create each one in Admin; set their
  **status** to **Pending** while you get them set up, then **Active** when ready (or **Paused** to stop
  it without deleting the history).
- If you set an affiliate's **account email**, that person sees their link, their referred orders, their
  running total and their payout details on their own **My account** page. Nobody else sees anything
  about affiliates there.
- This is separate from the Endorsely script; use either or both.

**Automatic payouts (PayPal).** Instead of paying each affiliate by hand, let the site do it:

- One-time set-up: get a **PayPal Payouts** API app (PayPal Developer dashboard → My Apps & Credentials,
  a Live app with the Payouts feature), then over SSH run `python3 rotaidem/strato/setup.py paypal` and
  paste its **Client ID** and **Secret**. They are stored in `config.env`, never in Git.
- The affiliate just enters their **PayPal email** on their My account page — no onboarding, no bank
  details to you.
- In **Admin -> Affiliates -> Automatic payouts**, switch **Pay affiliates automatically** on. From then
  on, each commission is sent **straight to the affiliate's PayPal the moment their referred sale is
  paid** — automatic and direct, no button to press.
- The **catch-up minimum** only covers affiliates who add their PayPal *after* they have already earned:
  the site sweeps up what they are owed above that minimum (weekly, or with **Run payouts now**). Anyone
  who already had a PayPal email is paid instantly, minimum or not.
- You always receive the full sale first, so your balance covers the commission. Each payout carries a
  `sender_batch_id`, so a retry can never pay twice. A buyer's success page never fails over a payout —
  if PayPal cannot be reached the commission stays pending and the next run settles it.
- Affiliates who do not give a PayPal email are still paid by hand with **Mark paid** — nothing changes.
- STRATO has no always-on cron, so the weekly catch-up happens on the first visit after it is due (the
  same way scheduled Discord posts work).

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

## Legal pages, consent and your customers' data

The site has a **privacy policy** (`/privacy`), **terms** (`/terms`), a **refund policy** (`/refunds`)
and a **cookie policy** (`/cookies`), linked in every footer. Their text can be changed under
Admin → Site → Page texts. Read them once: they describe what the site really does, but they are
not legal advice, and choices such as Swedish law and the refund rules are yours to confirm.

**Fill in Admin → Site → Business details** (company name, organisation number, VAT number if any,
address). EU law asks a web shop to show who sells; they appear in the footer and on the legal pages.

What the site does for you:

- **Cookie banner**: asked once, *Accept* and *Only necessary* are equally easy. Endorsely (affiliate
  tracking) and the 30-day partner-link memory only load after *Accept*. *Cookie settings* in the
  footer changes the choice. Fonts are served from the site itself (no Google).
- **Sign-up** asks to accept the terms and to confirm the person is 13 or older (with a parent's
  permission under 18); the acceptance is saved.
- **Checkout** shows the total price and asks the buyer to tick that they want the key right away and
  that the 14-day withdrawal right ends on activation (EU digital-content rule). The consent is saved
  with the order, sent to Stripe, and confirmed in the licence email.
- **My account → Your data**: *Download my data* (everything we keep, as a file) and *Delete my
  account* (orders stay 7 years for bookkeeping, without name or email; licence keys keep working).
- **Old data is removed by itself**: IP addresses of downloads after 30 days and in the admin log
  after 180 days, handled messages after 2 years, expired logins after 60 days.
- **Unsubscribe**: every newsletter has a link, and mail apps show a one-click *Unsubscribe* button.
- **Reviews**: under *What pilots say* the site says quotes are real, shared with permission and not
  paid for. Only add real quotes.
- Accessibility: a *Skip to content* link, keyboard-friendly menus and dialogs, and text alternatives
  for pictures (checked with axe-core on 18 pages: no problems found).
