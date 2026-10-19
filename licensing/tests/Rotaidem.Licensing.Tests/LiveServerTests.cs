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
        var client = new LicenseClient(Site);
        Assert.Equal(LicenseStatus.Valid, (await client.ActivateAsync(Key)).Status);
        Assert.Equal(LicenseStatus.Valid, (await client.ValidateAsync(Key)).Status);
        Assert.Equal(LicenseStatus.WrongProduct, (await new LicenseClient(Site, "otherproduct").ValidateAsync(Key)).Status);
        Assert.Equal(LicenseStatus.Valid, (await client.DeactivateAsync(Key)).Status);
        Assert.Equal(LicenseStatus.NotActivated, (await client.ValidateAsync(Key)).Status);
        Assert.Equal(LicenseStatus.NotFound, (await client.ActivateAsync(LicenseKey.Generate())).Status);
    }

    [Fact]
    public async Task Offline_grace_only_within_the_window()
    {
        var dir = Path.Combine(Path.GetTempPath(), "rotaidem-test-" + Guid.NewGuid());
        var store = new LicenseStore("heavypro", dir);
        var offline = new LicenseClient("http://127.0.0.1:9");
        Assert.Equal(LicenseStatus.NoKey, (await offline.CheckAsync(store, TimeSpan.FromDays(14))).Status);
        store.Save(LicenseKey.Generate(), DateTimeOffset.UtcNow.AddDays(-3));
        Assert.Equal(LicenseStatus.OfflineGrace, (await offline.CheckAsync(store, TimeSpan.FromDays(14))).Status);
        store.Save(store.Load()!.Key, DateTimeOffset.UtcNow.AddDays(-30));
        Assert.Equal(LicenseStatus.NetworkError, (await offline.CheckAsync(store, TimeSpan.FromDays(14))).Status);
        Directory.Delete(dir, true);
    }
}
