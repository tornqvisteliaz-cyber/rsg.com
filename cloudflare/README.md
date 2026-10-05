# Cloudflare Pages

This folder is the site. It is static HTML plus a Cloudflare Worker function. There is no Node server and no Flask process.

## Connect it

1. Cloudflare dashboard → Workers & Pages → Create → Pages → Connect to Git.
2. Repo: `tornqvisteliaz-cyber/rsg.com`
3. Root directory: `cloudflare`
4. Build command: leave empty
5. Build output directory: `public`

## Database

Pages → Settings → Functions → D1 database bindings.

- Variable name: `DB`
- Create a D1 database named `rsg`

Then run the schema once:

```sh
npx wrangler d1 execute rsg --remote --file=schema.sql
```

`npx` only downloads the Wrangler CLI. The site itself does not run on Node.

## Secrets

Pages → Settings → Environment variables:

- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`
- `ADMIN_USERNAME` = `eliaz`

The first login with those values creates the Owner account.

## Pages

Home, aircraft, Seabee, about, work, contact, newsletter, login, signup, account, admin, terms, privacy.

The installer API is the same shape: `/api/login`, `/api/products`, `/api/liveries`, `/api/save-product`, `/api/save-livery`.
