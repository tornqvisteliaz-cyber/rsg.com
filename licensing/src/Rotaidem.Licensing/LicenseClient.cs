using System.Net.Http;
using System.Text;

namespace Rotaidem.Licensing;

public enum LicenseStatus
{
    /// <summary>The server confirmed the key for this computer.</summary>
    Valid,
    /// <summary>The server could not be reached, but the key was confirmed recently enough to keep working.</summary>
    OfflineGrace,
    NoKey,
    InvalidFormat,
    NotFound,
    Revoked,
    WrongProduct,
    ActivationLimit,
    NotActivated,
    NetworkError,
    ServerError,
}

public sealed record LicenseResult(LicenseStatus Status, string Message, int ActivationsUsed = 0, int MaxActivations = 0)
{
    public bool IsValid => Status is LicenseStatus.Valid or LicenseStatus.OfflineGrace;
}

/// <summary>
/// Talks to the licence endpoints on the Rotaidem website.
/// Installer: <see cref="ActivateAsync"/> when the user enters a key, then save it with <see cref="LicenseStore"/>.
/// App start-up: <see cref="CheckAsync"/>, which also allows a period of offline use.
/// </summary>
public sealed class LicenseClient
{
    public const string DefaultBaseUrl = "https://rotaidem.com";

    // Timeouts are applied per attempt below, so the shared client itself never times out.
    private static readonly HttpClient SharedHttp = new() { Timeout = Timeout.InfiniteTimeSpan };
    private readonly HttpClient _http;
    private readonly string _baseUrl;

    static LicenseClient()
    {
#if NETFRAMEWORK
        // .NET Framework can default to old TLS versions that hosting providers refuse.
        System.Net.ServicePointManager.SecurityProtocol |= System.Net.SecurityProtocolType.Tls12;
#endif
    }

    public LicenseClient(string baseUrl = DefaultBaseUrl, string product = "heavypro", HttpClient? http = null)
    {
        _baseUrl = baseUrl.TrimEnd('/');
        Product = product;
        _http = http ?? SharedHttp;
    }

    public string Product { get; }

    /// <summary>
    /// How long one attempt may take. A server on a free hosting plan sleeps when idle and can need
    /// up to a minute to wake, so activation retries (see <see cref="ActivationAttempts"/>).
    /// </summary>
    public TimeSpan AttemptTimeout { get; set; } = TimeSpan.FromSeconds(35);

    /// <summary>Attempts for activate/deactivate, which the user waits for and which must not fail just because the server was asleep.</summary>
    public int ActivationAttempts { get; set; } = 3;

    /// <summary>Activate the key on this computer. Uses up one activation unless this computer is already activated.</summary>
    public Task<LicenseResult> ActivateAsync(string key, CancellationToken ct = default) => PostAsync("activate", key, ActivationAttempts, ct);

    /// <summary>Confirm the key is still valid and already activated on this computer. Does not use an activation.</summary>
    public Task<LicenseResult> ValidateAsync(string key, CancellationToken ct = default) => PostAsync("validate", key, ActivationAttempts, ct);

    /// <summary>Free this computer's activation, e.g. before moving to a new PC.</summary>
    public Task<LicenseResult> DeactivateAsync(string key, CancellationToken ct = default) => PostAsync("deactivate", key, ActivationAttempts, ct);

    /// <summary>
    /// For app start-up: validate the saved key online. If the server cannot be reached and the key was
    /// confirmed within <paramref name="offlineGrace"/>, the result is <see cref="LicenseStatus.OfflineGrace"/>.
    /// </summary>
    public async Task<LicenseResult> CheckAsync(LicenseStore store, TimeSpan offlineGrace, CancellationToken ct = default)
    {
        var saved = store.Load();
        if (saved is null)
            return new LicenseResult(LicenseStatus.NoKey, "No licence key has been entered on this computer.");

        // One attempt only: at start-up the offline grace period covers a sleeping or unreachable server.
        var result = await PostAsync("validate", saved.Key, 1, ct).ConfigureAwait(false);
        if (result.Status == LicenseStatus.Valid)
        {
            store.Save(saved.Key, DateTimeOffset.UtcNow);
            return result;
        }
        if (result.Status is LicenseStatus.NetworkError or LicenseStatus.ServerError &&
            DateTimeOffset.UtcNow - saved.LastValidatedUtc <= offlineGrace)
        {
            var left = offlineGrace - (DateTimeOffset.UtcNow - saved.LastValidatedUtc);
            return new LicenseResult(LicenseStatus.OfflineGrace,
                $"Could not reach the licence server. HeavyPro keeps working offline for {Math.Max(0, (int)left.TotalDays)} more day(s).");
        }
        return result;
    }

    private async Task<LicenseResult> PostAsync(string action, string key, int attempts, CancellationToken ct)
    {
        var normalized = LicenseKey.Normalize(key);
        if (normalized is null)
            return new LicenseResult(LicenseStatus.InvalidFormat, "That is not a valid licence key. Check for typing mistakes.");

        var payload = Json.Serialize(new LicenseRequest { Key = normalized, MachineId = MachineId.Get(), MachineName = MachineId.DisplayName(), Product = Product });
        HttpResponseMessage? response = null;
        var problem = "";
        for (var attempt = 1; attempt <= Math.Max(1, attempts) && response is null; attempt++)
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(AttemptTimeout);
            try
            {
                using var content = new StringContent(payload, Encoding.UTF8, "application/json");
                response = await _http.PostAsync($"{_baseUrl}/api/licenses/{action}", content, timeout.Token).ConfigureAwait(false);
                // Render answers 502/503 while a sleeping service starts; treat that like no answer and try again.
                if ((int)response.StatusCode is 502 or 503 or 504 && attempt < attempts)
                {
                    response.Dispose();
                    response = null;
                    problem = "the server is starting up";
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (OperationCanceledException)
            {
                problem = $"it did not answer within {(int)AttemptTimeout.TotalSeconds} seconds";
            }
            catch (HttpRequestException ex)
            {
                problem = InnermostMessage(ex);
            }
            if (response is null && attempt < attempts)
                await Task.Delay(TimeSpan.FromSeconds(2), ct).ConfigureAwait(false);
        }
        if (response is null)
            return new LicenseResult(LicenseStatus.NetworkError,
                $"Could not reach the licence server ({_baseUrl}): {problem}. Check your internet connection and try again.");

        using (response)
        {
            var reply = Json.Deserialize<LicenseReply>(await response.Content.ReadAsStringAsync().ConfigureAwait(false));

            if (reply is null)
                return new LicenseResult(LicenseStatus.ServerError,
                    $"{_baseUrl} answered, but not like the Rotaidem licence server (HTTP {(int)response.StatusCode}). Check that the server address is right.");
            if (action == "deactivate" && response.IsSuccessStatusCode)
                return new LicenseResult(LicenseStatus.Valid, "This computer has been deactivated.");
            if (reply.Valid)
                return new LicenseResult(LicenseStatus.Valid, "Licence confirmed.", reply.ActivationsUsed, reply.MaxActivations);

            var status = reply.Reason switch
            {
                "invalid_format" => LicenseStatus.InvalidFormat,
                "not_found" => LicenseStatus.NotFound,
                "revoked" => LicenseStatus.Revoked,
                "wrong_product" => LicenseStatus.WrongProduct,
                "activation_limit" => LicenseStatus.ActivationLimit,
                "not_activated" => LicenseStatus.NotActivated,
                _ => (int)response.StatusCode >= 500 ? LicenseStatus.ServerError : LicenseStatus.NotFound,
            };
            return new LicenseResult(status, reply.Error ?? "The licence could not be confirmed.", reply.ActivationsUsed, reply.MaxActivations);
        }
    }

    private static string InnermostMessage(Exception ex)
    {
        while (ex.InnerException is not null)
            ex = ex.InnerException;
        return ex.Message.TrimEnd('.');
    }
}
