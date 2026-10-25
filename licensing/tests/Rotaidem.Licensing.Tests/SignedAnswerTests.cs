using System.Net;
using System.Text;
using System.Text.Json;
using Rotaidem.Licensing;
using Xunit;

namespace Rotaidem.Licensing.Tests;

/// <summary>A cracked or faked licence must not pass: every "valid" needs the server's signature.</summary>
public sealed class SignedAnswerTests : IDisposable
{
    private readonly TestSigner _server = new();
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "rotaidem-test-" + Guid.NewGuid());
    private readonly string _key = LicenseKey.Generate();

    private LicenseStore Store() => new("heavypro", _dir);

    private LicenseClient Client(FakeServer fake, string? jwk = "server", string? machine = null)
    {
        var client = new LicenseClient("https://rotaidem.test", "heavypro", new HttpClient(fake), jwk == "server" ? _server.Jwk : jwk)
            { AttemptTimeout = TimeSpan.FromSeconds(5), ActivationAttempts = 1 };
        if (machine != null) client.MachineId = machine;
        return client;
    }

    private static Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> Json(object reply) =>
        (_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            { Content = new StringContent(JsonSerializer.Serialize(reply), Encoding.UTF8, "application/json") });

    private static readonly Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> Offline =
        (_, _) => throw new HttpRequestException("No such host is known.");

    [Fact]
    public async Task Real_signed_answer_is_accepted_and_carries_the_lease()
    {
        var result = await Client(new FakeServer(_server.SignedOk())).ActivateAsync(_key);
        Assert.Equal(LicenseStatus.Valid, result.Status);
        Assert.NotNull(result.Token);
        Assert.True(result.LeaseUntil > DateTimeOffset.UtcNow.AddDays(13));
    }

    [Fact]
    public async Task Fake_server_saying_valid_without_a_token_is_refused()
    {
        var result = await Client(new FakeServer(Json(new { valid = true, activations_used = 1, max_activations = 2 }))).ActivateAsync(_key);
        Assert.Equal(LicenseStatus.Unverified, result.Status);
    }

    [Fact]
    public async Task Token_signed_with_another_key_is_refused()
    {
        using var other = new TestSigner();
        var result = await Client(new FakeServer(other.SignedOk())).ActivateAsync(_key);
        Assert.Equal(LicenseStatus.Unverified, result.Status);
    }

    [Fact]
    public async Task Replayed_old_answer_is_refused()
    {
        var client = Client(new FakeServer(_server.SignedOk((k, m, _) => _server.Token(k, m, "an-old-nonce"))));
        Assert.Equal(LicenseStatus.Unverified, (await client.ActivateAsync(_key)).Status);
    }

    [Fact]
    public async Task Answer_for_another_computer_or_key_is_refused()
    {
        Assert.Equal(LicenseStatus.Unverified, (await Client(new FakeServer(_server.SignedOk((k, _, n) => _server.Token(k, "other-pc", n)))).ActivateAsync(_key)).Status);
        Assert.Equal(LicenseStatus.Unverified, (await Client(new FakeServer(_server.SignedOk((_, m, n) => _server.Token(LicenseKey.Generate(), m, n)))).ActivateAsync(_key)).Status);
    }

    [Fact]
    public async Task Edited_token_is_refused()
    {
        var edited = _server.SignedOk((k, m, n) => TestSigner.Edit(_server.Token(k, m, n), c => c["max_activations"] = 99));
        Assert.Equal(LicenseStatus.Unverified, (await Client(new FakeServer(edited)).ActivateAsync(_key)).Status);
    }

    [Fact]
    public async Task Without_a_public_key_nothing_is_trusted()
    {
        var result = await Client(new FakeServer(_server.SignedOk()), jwk: null).ActivateAsync(_key);
        Assert.Equal(LicenseStatus.Unverified, result.Status);
        Assert.Contains("public key", result.Message);
    }

    [Fact]
    public async Task Offline_start_uses_the_saved_signed_answer_until_its_lease_ends()
    {
        var store = Store();
        var client = Client(new FakeServer(_server.SignedOk(), Offline));
        var activated = await client.ActivateAsync(_key);
        store.Save(_key, DateTimeOffset.UtcNow, activated.Token);

        var offline = client.CheckOffline(store, TimeSpan.FromDays(14));
        Assert.Equal(LicenseStatus.OfflineGrace, offline.Status);
        Assert.Equal(LicenseStatus.OfflineGrace, (await client.CheckAsync(store, TimeSpan.FromDays(14))).Status);

        store.Save(_key, DateTimeOffset.UtcNow.AddDays(-15), _server.Token(_key, client.MachineId, null, issued: DateTimeOffset.UtcNow.AddDays(-15)));
        Assert.Equal(LicenseStatus.LeaseExpired, client.CheckOffline(store, TimeSpan.FromDays(14)).Status);
    }

    [Fact]
    public void Saved_licence_cannot_be_edited_copied_or_outlived_by_turning_the_clock_back()
    {
        var store = Store();
        var client = Client(new FakeServer(Offline));
        var token = _server.Token(_key, client.MachineId, null);

        store.Save(_key, DateTimeOffset.UtcNow, TestSigner.Edit(token, c => c["exp"] = DateTimeOffset.UtcNow.AddYears(5).ToUnixTimeSeconds()));
        Assert.Equal(LicenseStatus.Unverified, client.CheckOffline(store).Status);

        store.Save(_key, DateTimeOffset.UtcNow, token);
        Assert.Equal(LicenseStatus.OfflineGrace, client.CheckOffline(store).Status);
        Assert.Equal(LicenseStatus.Unverified, Client(new FakeServer(Offline), machine: "another-pc").CheckOffline(store).Status);

        // license.json says this PC already saw a later time: the clock has been turned back.
        var file = File.ReadAllText(store.FilePath);
        var seen = DateTimeOffset.UtcNow.AddDays(5).ToUnixTimeSeconds();
        File.WriteAllText(store.FilePath, System.Text.RegularExpressions.Regex.Replace(file, "\"LastSeen\":\\d+", $"\"LastSeen\":{seen}"));
        Assert.Equal(LicenseStatus.ClockTampered, client.CheckOffline(store).Status);
    }

    [Fact]
    public void Key_saved_by_an_older_version_must_be_confirmed_online_first()
    {
        var store = Store();
        store.Save(_key, DateTimeOffset.UtcNow);
        var result = Client(new FakeServer(Offline)).CheckOffline(store);
        Assert.False(result.IsValid);
        Assert.Equal(LicenseStatus.NoKey, result.Status);
    }

    [Fact]
    public void Product_ids_match_like_on_the_server()
    {
        Assert.True(LicenseToken.SameProduct("heavypro", "heavypro"));
        Assert.True(LicenseToken.SameProduct("heavypro", "heavy-pro-msfs-2024"));
        Assert.True(LicenseToken.SameProduct("rotaidem-heavypro", "rotaidem-heavypro"));
        Assert.False(LicenseToken.SameProduct("heavypro", "heavypro2"));
        Assert.False(LicenseToken.SameProduct("heavypro", "otherproduct"));
    }

    public void Dispose()
    {
        _server.Dispose();
        if (Directory.Exists(_dir)) Directory.Delete(_dir, true);
    }
}
