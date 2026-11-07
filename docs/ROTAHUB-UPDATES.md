# RotaHub product updates: what rotaidem.com offers

How RotaHub learns about new product versions set in **Admin → Products** on rotaidem.com, and
how it downloads them. Both backends (STRATO `server.py`, Cloudflare function) answer the same.

## 1. The update feed: `GET /api/hub/products`

Public, no login needed. Poll it at start, on Refresh, and every 15–30 minutes while RotaHub runs.

```json
{
  "revision": "3f9c0a1b2c4d5e6f",
  "products": [
    {
      "id": "heavypro",
      "name": "HeavyPro",
      "status": "published",
      "version": "0.6.1",
      "version_number": "0.6.1",
      "updated_at": "2026-10-26 09:14:02",
      "required": false,
      "licence_only": true,
      "notes": "Smoother stick forces\nNew A320 profile",
      "changelog": [{"version": "0.6.1", "date": "2026-10-26", "notes": "..."}, {"version": "0.6.0", "date": "", "notes": "..."}],
      "download": {"available": true, "kind": "exe", "id": "a1b2c3d4e5f6", "sha256": "9f86d0...", "size": 861184}
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `revision` | Changes whenever anything in the feed changes. Ask `GET /api/hub/products?since=<revision>`; when nothing changed the answer is just `{"revision": "...", "unchanged": true}`. |
| `status` | `published` (Available), `coming_soon`, `in_development`. Offer installs and updates for `published`. |
| `version` | What the admin typed, shown as is. |
| `version_number` | The same as `major.minor.patch` (`"v1.2"` → `"1.2.0"`), `""` when the version is not a number. **Compare this** with the installed version: newer = update available. |
| `updated_at` | UTC time the version or the download last changed (`YYYY-MM-DD HH:MM:SS`). |
| `required` | Admin ticked "Everyone must update": ask before starting an older installed version, and suggest updating first. |
| `licence_only` | Only accounts with an active licence for the product get the download (see 2). |
| `notes` | What's new in this version (the newest changelog entry when its version matches `version`), `""` otherwise. One point per line. |
| `changelog` | The whole version history, newest first. |
| `download.available` | The product has an installer. |
| `download.kind` | `exe`, `msi`, `zip`, `7z` or `other`. |
| `download.id` | Changes when the admin points the product at another file (even with the same version). |
| `download.sha256` / `download.size` | For installers uploaded to rotaidem.com (`/files/...`): the file's SHA-256 (hex) and size in bytes. **Check the downloaded file against `sha256` before running it**; refuse it when it differs. Empty / 0 for installers hosted elsewhere (e.g. GitHub) or on the Cloudflare copy of the site. |

The feed never contains the installer's address.

## 2. Getting the installer: `POST /api/download/<id>`

Send RotaHub's User-Agent (it must start with `RotaHub`, e.g. `RotaHub/0.2.0 (Windows NT 10.0)`)
and the login token (`Authorization: Bearer <token>` and the copy `X-Rotaidem-Token: <token>`).

- `200 {"url": "https://..."}` or `{"url": "/files/HeavyProSetup.exe?v=..."}`: download it (make a
  relative link absolute with the site's address). The download is counted for the admin's stats.
- `401 {"login": true, "error": "..."}`: `licence_only` and not logged in.
- `403 {"error": "..."}`: `licence_only` and the account has no active licence for this product.
- `404`: no download yet.

Anything else that asks (browsers) gets `{"hub": true, "url": "<RotaHub download>"}`: the website
only hands out RotaHub.

## 3. Suggested flow in RotaHub

1. Read the feed (with `since`), keep the last answer.
2. For each installed product: if `version_number` > installed version, show **Update** with `notes`
   (and the changelog). If `required`, ask before launching the old version.
3. On Update: `POST /api/download/<id>` → download → compare SHA-256 with `download.sha256` when
   it is set → run the installer (or unpack, for RotaHub packages) → record the installed
   `version_number`.
4. Products with their own signed RotaHub release (`rotahub.jws`, Admin → RotaHub → Install
   settings) keep using that; the feed still tells when there is something new.

## Where it is set

Admin → Products → a product → **Publish an update**: new version, installer (upload, pick an
uploaded file, or a link), what's new, and "Everyone must update". Saving updates the version,
the download, the version history and `updated_at` in one go.
