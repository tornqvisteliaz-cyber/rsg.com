using System.Net.Http;
using System.Security.Cryptography;
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
    /// <summary>The answer was not signed by the Rotaidem server, was edited, or belongs to another computer or request.</summary>
    Unverified,
    /// <summary>Offline longer than the server's lease allows: the app must reach the server again.</summary>
    LeaseExpired,
    /// <summary>The computer's clock was turned back.</summary>
    ClockTampered,
    /// <summary>Too many wrong keys from this internet address; try again later.</summary>
    RateLimited,
}

/// <param name="Token">The server's signed answer (save it with the key, see <see cref="LicenseStore.Save"/>).</param>
/// <param name="LeaseUntil">Until when the app may run offline on this answer.</param>
public sealed record LicenseResult(LicenseStatus Status, string Message, int ActivationsUsed = 0, int MaxActivations = 0,
    string? Token = null, DateTimeOffset? LeaseUntil = null)
{
    public bool IsValid => Status is LicenseStatus.Valid or LicenseStatus.OfflineGrace;
}

/// <summary>
/// Talks to the licence endpoints on the Rotaidem website.
/// Installer: <see cref="ActivateAsync"/> when the user enters a key, then save it and its token with <see cref="LicenseStore"/>.
/// App start-up: <see cref="CheckOffline"/> for a quick start, <see cref="CheckAsync"/> to confirm online.
/// A "valid" answer counts only if it carries a token signed by the server (see <see cref="LicenseToken"/>)
/// that names this computer and repeats the random nonce of this request.
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

    private readonly LicenseToken? _verifier;

    /// <param name="publicKeyJwk">The server's public key (https://rotaidem.com/api/licenses/jwks.json). Without it no answer can be trusted.</param>
    public LicenseClient(string baseUrl = DefaultBaseUrl, string product = "heavypro", HttpClient? http = null, string? publicKeyJwk = null)
    {
        _baseUrl = baseUrl.TrimEnd('/');
        Product = product;
        _http = http ?? SharedHttp;
        _verifier = LicenseToken.FromJwk(publicKeyJwk);
    }

    /// <summary>The computer id sent to the server and expected in its tokens. Normally <see cref="Licensing.MachineId.Get"/>.</summary>
    public string MachineId { get; set; } = Licensing.MachineId.Get();

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

        // One attempt only: at start-up the saved signed answer covers a sleeping or unreachable server.
        var result = await PostAsync("validate", saved.Key, 1, ct).ConfigureAwait(false);
        if (result.Status == LicenseStatus.Valid)
        {
            store.Save(saved.Key, DateTimeOffset.UtcNow, result.Token);
            return result;
        }
        if (result.Status is LicenseStatus.NetworkError or LicenseStatus.ServerError or LicenseStatus.RateLimited)
        {
            var offline = CheckOffline(store, offlineGrace);
            return offline.IsValid ? offline : offline.Status is LicenseStatus.NoKey ? result : offline;
        }
        return result;
    }

    /// <summary>
    /// Without the network: is the saved signed answer still good on this computer? Valid while its lease
    /// (set by the server) and <paramref name="offlineGrace"/> since the last online check both last.
    /// </summary>
    public LicenseResult CheckOffline(LicenseStore store, TimeSpan? offlineGrace = null)
    {
        var saved = store.Load();
        if (saved is null)
            return new LicenseResult(LicenseStatus.NoKey, "No licence key has been entered on this computer.");
        if (saved.Token is null)
            return new LicenseResult(LicenseStatus.NoKey, "The licence has to be confirmed online first.");
        if (_verifier is null)
            return NoPublicKey();
        var now = DateTimeOffset.UtcNow;
        var claims = _verifier.Verify(saved.Token, Product, MachineId, saved.Key, null, now, saved.LastSeen, out var status, out var problem);
        if (claims is null)
            return new LicenseResult(status, problem);
        if (offlineGrace is { } grace && now - saved.LastValidatedUtc > grace)
            return new LicenseResult(LicenseStatus.LeaseExpired, "HeavyPro has been offline too long. Connect to the internet once so the licence can be checked.");
        store.Touch();
        var until = offlineGrace is { } g && saved.LastValidatedUtc + g < claims.LeaseUntil ? saved.LastValidatedUtc + g : claims.LeaseUntil;
        var days = Math.Max(0, (int)(until - now).TotalDays);
        return new LicenseResult(LicenseStatus.OfflineGrace,
            $"Could not reach the licence server. HeavyPro keeps working offline for {days} more day(s).",
            claims.ActivationsUsed, claims.MaxActivations, saved.Token, until);
    }

    private static LicenseResult NoPublicKey() =>
        new(LicenseStatus.Unverified, "This copy of HeavyPro was built without the licence server's public key. Download it again from rotaidem.com.");

    private static string NewNonce()
    {
        var bytes = new byte[16];
        using (var rng = RandomNumberGenerator.Create())
            rng.GetBytes(bytes);
        return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant();
    }

    private async Task<LicenseResult> PostAsync(string action, string key, int attempts, CancellationToken ct)
    {
        var normalized = LicenseKey.Normalize(key);
        if (normalized is null)
            return new LicenseResult(LicenseStatus.InvalidFormat, "That is not a valid licence key. Check for typing mistakes.");

        var nonce = action == "deactivate" ? null : NewNonce();
        var payload = Json.Serialize(new LicenseRequest { Key = normalized, MachineId = MachineId, MachineName = Licensing.MachineId.DisplayName(), Product = Product, Nonce = nonce });
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
            {
                // Only an answer signed by the server, for this computer, key and request, counts.
                if (_verifier is null)
                    return NoPublicKey();
                var claims = _verifier.Verify(reply.Token, Product, MachineId, normalized, nonce, DateTimeOffset.UtcNow, null, out var verdict, out var why);
                if (claims is null)
                    return new LicenseResult(verdict, why);
                return new LicenseResult(LicenseStatus.Valid, "Licence confirmed.", claims.ActivationsUsed, claims.MaxActivations, reply.Token, claims.LeaseUntil);
            }

            var status = reply.Reason switch
            {
                "invalid_format" => LicenseStatus.InvalidFormat,
                "not_found" => LicenseStatus.NotFound,
                "revoked" => LicenseStatus.Revoked,
                "wrong_product" => LicenseStatus.WrongProduct,
                "activation_limit" => LicenseStatus.ActivationLimit,
                "not_activated" => LicenseStatus.NotActivated,
                "rate_limited" => LicenseStatus.RateLimited,
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
