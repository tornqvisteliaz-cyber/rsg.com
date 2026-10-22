# Rotaidem licensing

Licence keys look like `HPRO-7KQM-2XRT-9HCW`. This folder has two .NET 8 projects:

| Project | What it is |
| --- | --- |
| `src/Rotaidem.Licensing` | Class library for HeavyPro, its installer and the generator: key format, machine id, online activation, local key storage. Builds for `net48` (HeavyPro's WPF app) and `net8.0` (installer, generator). |
| `src/Rotaidem.LicenseGenerator` | The licence generator program (`RotaidemLicenseGenerator.exe`). |
| `tests/Rotaidem.Licensing.Tests` | Unit tests, plus live tests against a running website. |

The website (`server.py`, and the Cloudflare function) stores issued keys and the computers they are activated on. Admin → **Licences** lists them.

## How it fits together

```
License Generator ──(creates keys, uploads with LICENSE_API_KEY)──▶ website /api/licenses/import
Installer ─────────(user types key: ActivateAsync)────────────────▶ website /api/licenses/activate
HeavyPro ──────────(every start: CheckAsync)──────────────────────▶ website /api/licenses/validate
```

* **Key format.** 4-letter product prefix, then 11 random characters and 1 check character from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no I, O, 0 or 1). The check character catches typos without contacting the server. A key only works once the website knows it, so a correctly formed but made-up key is rejected.
* **Activations.** Each key works on `max_activations` computers (default 2). A computer is identified by a hash of the Windows MachineGuid, so reinstalling HeavyPro does not use another activation. Admins can remove a computer from a key in the panel.
* **Signed answers.** A "valid" from the website counts only with a token signed by the website's private key (RS256). The client holds the public key (`https://rotaidem.com/api/licenses/jwks.json`) and checks that the token names this computer, this key and the random nonce it just sent. So a fake licence server, an edited `license.json`, a licence copied to another PC and a replayed old answer are all refused (`LicenseStatus.Unverified`). Pass the public key as the 4th argument of `LicenseClient`; without it nothing is trusted.
* **Offline use.** The signed answer is saved next to the key. Without internet HeavyPro starts on it until its lease runs out (`LICENSE_LEASE_DAYS` on the website, default 14) and within the grace period you choose; then it must reach the website again. A clock turned back is noticed (`ClockTampered`).

## The generator

Build it with `dotnet run --project src/Rotaidem.LicenseGenerator`, or download the ready `.exe` from the **License generator** workflow on GitHub (Actions tab → latest run → Artifacts).

1. Copy the value of `LICENSE_API_KEY` from `/home/www/rotaidem-data/config.env` on the STRATO server (open it in FileZilla). On Render it is in the web service's Environment instead.
2. Copy `generator.example.json` to `generator.json` next to the exe and fill in `siteUrl` and `apiKey`. Alternatively set the environment variables `ROTAIDEM_SITE_URL` and `ROTAIDEM_LICENSE_API_KEY`.
3. Double-click the exe for the menu, or use the command line:

```
RotaidemLicenseGenerator generate --count 10 --email buyer@example.com --note "Launch batch"
RotaidemLicenseGenerator generate --count 1 --email buyer@example.com --no-email   # register only, no email
RotaidemLicenseGenerator generate --count 50 --no-upload --out batch.csv
RotaidemLicenseGenerator verify HPRO-7KQM-2XRT-9HCW
```

When `--email` is given (or an email is typed in the menu), the website emails the keys to that customer, with activation instructions, from `MAIL_FROM` via Resend. Add `--no-email` to skip that. Every run saves a CSV. Keys made with `--no-upload` (or while offline) can be registered later in Admin → Licences → Import.

## Using it in the installer

Reference `Rotaidem.Licensing` (project reference, or copy the four `.cs` files):

```csharp
using Rotaidem.Licensing;

var client = new LicenseClient("https://rotaidem.com", product: "heavypro");
var store = new LicenseStore("heavypro");

if (!LicenseKey.IsWellFormed(keyTextBox.Text, LicenseKey.HeavyProPrefix))
{
    ShowError("That key has a typo. It should look like HPRO-XXXX-XXXX-XXXX.");
    return;
}

var result = await client.ActivateAsync(keyTextBox.Text);
if (result.IsValid)
{
    store.Save(keyTextBox.Text, DateTimeOffset.UtcNow, result.Token);   // the token lets the app start offline
    ContinueInstall();
}
else
{
    ShowError(result.Message);   // e.g. "This key is already active on 2 computer(s)..."
}
```

## Using it in HeavyPro

At start-up:

```csharp
var client = new LicenseClient(LicenseClient.DefaultBaseUrl, "heavypro", null, publicKeyJwk);
var quick = client.CheckOffline(new LicenseStore("heavypro"), TimeSpan.FromDays(14));   // instant start if the saved answer is still good
var result = await client.CheckAsync(new LicenseStore("heavypro"), TimeSpan.FromDays(14));
switch (result.Status)
{
    case LicenseStatus.Valid:
    case LicenseStatus.OfflineGrace:
        break;                                   // run normally; show result.Message for OfflineGrace
    case LicenseStatus.NoKey:
    case LicenseStatus.NotActivated:
        ShowActivationWindow();                  // same flow as the installer
        break;
    default:
        ShowLicenceProblem(result.Message);      // revoked, not found, network error after the grace period...
        break;
}
```

To let users move to a new computer, call `DeactivateAsync(key)` from a menu item and then `store.Clear()`.

## Website API

| Endpoint | Body | Result |
| --- | --- | --- |
| `POST /api/licenses/activate` | `key`, `machine_id`, `machine_name`, `product` | `{ valid, activations_used, max_activations }` or `{ valid: false, reason, error }` |
| `POST /api/licenses/validate` | `key`, `machine_id`, `product` | as above; does not use an activation |
| `POST /api/licenses/deactivate` | `key`, `machine_id` | `{ ok: true }` |
| `POST /api/licenses/import` | `licenses: [{ key, product_id, email, max_activations, note }]`, optional `send_email: true`, header `X-License-Api-Key` | `{ created, skipped, invalid, emailed, email_error }` |

`reason` is one of `invalid_format`, `not_found`, `revoked`, `wrong_product`, `activation_limit`, `not_activated`, `missing_machine`.

## Tests

```
dotnet test
ROTAIDEM_TEST_SITE=http://localhost:8000 ROTAIDEM_TEST_KEY=HPRO-... dotnet test   # also runs the live tests
```
