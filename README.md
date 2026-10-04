# RSG Software website

Website made for the developers of RSG Software. All rights to RSG Software, Eliaz T and Vanya.

## Production deployment

The application exposes the Flask WSGI object as `main:app`. Use a Python hosting service that supports Gunicorn, or build the included Dockerfile.

Required environment settings:

- `APP_ENV=production`
- `SECRET_KEY`: a unique, randomly generated value kept in the hosting provider's secret store
- `DATABASE_URL`: a persistent PostgreSQL database URL (the SQLite default is for local development only)
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `ADMIN_EMAIL`: credentials for the first Owner account

The app listens on the provider's `PORT` (default `8000`). `/health` returns a small health response for platform checks. Do not place the database file or customer uploads on an ephemeral container filesystem; attach persistent storage or use a persistent external service for uploads.

The initial Owner is created from the environment settings when the app starts. Keep `ADMIN_RESET_PASSWORD=false`; set it to `true` for one restart only when deliberately resetting that account's password, then switch it off.

### Cloudflare

Point the domain to the hosting origin and enable the Cloudflare proxy after the origin is reachable. Configure Cloudflare's SSL/TLS mode to **Full (strict)** when the hosting origin has a valid TLS certificate.

The application marks session cookies as Secure in production. Forwarded client IP and HTTPS headers are ignored by default. Set `PROXY_FIX_X_FOR` and `PROXY_FIX_X_PROTO` only after confirming the exact number of trusted proxy hops in your Starto and Cloudflare route, and that those proxies overwrite the forwarded headers. Keep the origin restricted to trusted ingress where the provider supports it. This avoids accepting spoofed client IP or scheme headers.

### Start command

For a Python service, use:

```sh
gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60 main:app
```

The included `Procfile` provides the same start command. The included `Dockerfile` defaults to port 8000 and honors `PORT` when the container platform sets it.
