# Rotaidem website

Home of HeavyPro, a control-feel app for Microsoft Flight Simulator 2024. All rights to Rotaidem, Eliaz T and Vanya.

The site lives in `cloudflare/public` (static pages) and has one API with two interchangeable backends:

- `server.py`: Flask, for Python hosts such as **Starto** (also Docker / Procfile).
- `cloudflare/functions/api/[[path]].js`: Cloudflare Pages Functions with D1 (see `cloudflare/README.md`).

## Deploy on Render (quickest way to try the backend)

1. Render dashboard → **New → Blueprint** → connect this GitHub repo.
2. Render reads `render.yaml` and creates the web service and a free Postgres database.
3. When asked, enter `ADMIN_EMAIL` and `ADMIN_PASSWORD`, then click **Apply**.
4. Open the `onrender.com` URL, go to `/login`, and log in with that email and password. The first login creates the Owner account and takes you to `/admin`.

The free web service sleeps when idle, so the first visit after a break takes up to a minute.

## Emailing a product

Admin → **Email a product** sends a product announcement to any mix of: everyone with an account, newsletter subscribers (the sign-up form in the site footer and on the home page), people who used the contact form, buyers, and any extra addresses you paste in. Duplicates are removed, every email has an unsubscribe link, and unsubscribed addresses are always skipped.

Sending uses [Resend](https://resend.com). Set these environment variables:

- `RESEND_API_KEY`: your Resend API key.
- `MAIL_FROM` (optional): the sender. Defaults to `Rotaidem <no-reply@rotaidem.com>`; the rotaidem.com domain must be verified in Resend.
- `SECRET_KEY`: any long random value; it signs unsubscribe links (the Render blueprint generates one).
- `SITE_URL` (optional): public address used for links in emails, e.g. `https://rotaidem.com`.

## Licence keys

HeavyPro keys (`HPRO-XXXX-XXXX-XXXX`) are created with the licence generator program in `licensing/`, or in Admin → **Licences**, and activated by the app and installer through `/api/licenses/*`. Set `LICENSE_API_KEY` on the server so the generator can register keys. See [licensing/README.md](licensing/README.md).

## Deploy on Starto

1. Create a Python app from this repository.
2. Start command (also in `Procfile`):

   ```sh
   gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60 main:app
   ```

3. Environment variables:
   - `ADMIN_EMAIL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`: the first login with these creates the Owner account.
   - `DATABASE_URL`: PostgreSQL URL (recommended). Without it the app uses SQLite at `DATA_DIR/rotaidem.db`.
   - `DATA_DIR`: only for SQLite; point it at a persistent volume, never the ephemeral container disk.
4. Health check path: `/health`.

The schema is created automatically on start. On first start the old product catalog is cleared once so the HeavyPro catalog starts empty; add products from **/admin → Products**.

## Admin panel

`/admin` (staff only): dashboard with revenue and counts, products (cover, gallery, features, status, sort order), news posts with editing, manual orders, users with role changes (Owner only), contact-form inbox and an activity log.

## Local development

```sh
pip install -r requirements.txt
ADMIN_EMAIL=you@example.com ADMIN_PASSWORD=test1234 python server.py
```

The older Jinja app in `website/` is no longer the entry point; `main.py` now serves `server.py`.
