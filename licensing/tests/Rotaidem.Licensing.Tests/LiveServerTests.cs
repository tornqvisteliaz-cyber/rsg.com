using Rotaidem.Licensing;
using Xunit;

namespace Rotaidem.Licensing.Tests;

/// <summary>
/// Runs against a real website when ROTAIDEM_TEST_SITE and ROTAIDEM_TEST_KEY are set
/// (a key that is registered and has no activations yet). Skipped otherwise.
/// </summary>
public class LiveServerTests
{
    private static readonly string? Site = Environment.GetEnvironmentVariable("ROTAIDEM_TEST_SITE");
    private static readonly string? Key = Environment.GetEnvironmentVariable("ROTAIDEM_TEST_KEY");

    [Fact]
    public async Task Activate_validate_deactivate()
    {
        if (Site is null || Key is null)
            return;
        using var http = new HttpClient();
        var jwk = await http.GetStringAsync(Site.TrimEnd('/') + "/api/licenses/jwks.json");
        var client = new LicenseClient(Site, "heavypro", null, jwk);
        var activated = await client.ActivateAsync(Key);
        Assert.Equal(LicenseStatus.Valid, activated.Status);
        Assert.NotNull(activated.Token);
        Assert.Equal(LicenseStatus.Valid, (await client.ValidateAsync(Key)).Status);
        Assert.Equal(LicenseStatus.WrongProduct, (await new LicenseClient(Site, "otherproduct", null, jwk).ValidateAsync(Key)).Status);

        var dir = Path.Combine(Path.GetTempPath(), "rotaidem-live-" + Guid.NewGuid());
        var store = new LicenseStore("heavypro", dir);
        store.Save(Key, DateTimeOffset.UtcNow, activated.Token);
        Assert.Equal(LicenseStatus.OfflineGrace, new LicenseClient("http://127.0.0.1:9", "heavypro", null, jwk).CheckOffline(store, TimeSpan.FromDays(14)).Status);
        Assert.Equal(LicenseStatus.Valid, (await client.CheckAsync(store, TimeSpan.FromDays(14))).Status);
        Directory.Delete(dir, true);

        Assert.Equal(LicenseStatus.Valid, (await client.DeactivateAsync(Key)).Status);
        Assert.Equal(LicenseStatus.NotActivated, (await client.ValidateAsync(Key)).Status);
        Assert.Equal(LicenseStatus.NotFound, (await client.ActivateAsync(LicenseKey.Generate())).Status);
    }
}
