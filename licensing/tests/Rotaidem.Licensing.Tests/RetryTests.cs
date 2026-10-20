using System.Net;
using System.Text;
using Rotaidem.Licensing;
using Xunit;

namespace Rotaidem.Licensing.Tests;

/// <summary>Simulates a hosting service that sleeps when idle and needs time to wake up.</summary>
public class RetryTests
{
    private const string Ok = """{"valid": true, "activations_used": 1, "max_activations": 2}""";

    private sealed class FakeServer(params Func<CancellationToken, Task<HttpResponseMessage>>[] replies) : HttpMessageHandler
    {
        public int Calls { get; private set; }

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct) =>
            replies[Math.Min(Calls++, replies.Length - 1)](ct);
    }

    private static Func<CancellationToken, Task<HttpResponseMessage>> Reply(HttpStatusCode code, string body) =>
        _ => Task.FromResult(new HttpResponseMessage(code) { Content = new StringContent(body, Encoding.UTF8, "application/json") });

    private static Func<CancellationToken, Task<HttpResponseMessage>> Hang() =>
        async ct => { await Task.Delay(Timeout.Infinite, ct); throw new InvalidOperationException(); };

    private static LicenseClient Client(FakeServer server) =>
        new("https://example.test", "heavypro", new HttpClient(server)) { AttemptTimeout = TimeSpan.FromMilliseconds(300) };

    [Fact]
    public async Task Activation_survives_a_server_that_is_waking_up()
    {
        var server = new FakeServer(Hang(), Reply(HttpStatusCode.ServiceUnavailable, "starting"), Reply(HttpStatusCode.OK, Ok));
        var result = await Client(server).ActivateAsync(LicenseKey.Generate());
        Assert.Equal(LicenseStatus.Valid, result.Status);
        Assert.Equal(3, server.Calls);
    }

    [Fact]
    public async Task Gives_a_clear_message_when_the_server_never_answers()
    {
        var server = new FakeServer(Hang());
        var result = await Client(server).ActivateAsync(LicenseKey.Generate());
        Assert.Equal(LicenseStatus.NetworkError, result.Status);
        Assert.Contains("https://example.test", result.Message);
        Assert.Contains("did not answer", result.Message);
        Assert.Equal(3, server.Calls);
    }

    [Fact]
    public async Task Shows_the_real_connection_error()
    {
        var server = new FakeServer(_ => throw new HttpRequestException("outer", new IOException("No such host is known.")));
        var result = await Client(server).ActivateAsync(LicenseKey.Generate());
        Assert.Contains("No such host is known", result.Message);
    }

    [Fact]
    public async Task Wrong_address_is_reported_as_such()
    {
        var server = new FakeServer(Reply(HttpStatusCode.NotFound, "<html>Not Found</html>"));
        var result = await Client(server).ActivateAsync(LicenseKey.Generate());
        Assert.Equal(LicenseStatus.ServerError, result.Status);
        Assert.Contains("server address", result.Message);
    }

    [Fact]
    public async Task Start_up_check_does_not_wait_for_retries()
    {
        var dir = Path.Combine(Path.GetTempPath(), "rotaidem-test-" + Guid.NewGuid());
        var store = new LicenseStore("heavypro", dir);
        store.Save(LicenseKey.Generate(), DateTimeOffset.UtcNow.AddDays(-1));
        var server = new FakeServer(Hang());
        var result = await Client(server).CheckAsync(store, TimeSpan.FromDays(14));
        Assert.Equal(LicenseStatus.OfflineGrace, result.Status);
        Assert.Equal(1, server.Calls);
        Directory.Delete(dir, true);
    }
}
