# Rotaidem website

Home of HeavyPro, a control-feel app for Microsoft Flight Simulator 2024. All rights to Rotaidem, Eliaz T and Vanya.

The site lives in `cloudflare/public` (static pages) and has one API with two interchangeable backends:

- `server.py`: Flask, for Python hosts such as **Starto** (also Docker / Procfile).
- `cloudflare/functions/api/[[path]].js`: Cloudflare Pages Functions with D1 (see `cloudflare/README.md`).

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
