using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

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

    private static readonly HttpClient SharedHttp = new() { Timeout = TimeSpan.FromSeconds(15) };
    private readonly HttpClient _http;
    private readonly string _baseUrl;

    public LicenseClient(string baseUrl = DefaultBaseUrl, string product = "heavypro", HttpClient? http = null)
    {
        _baseUrl = baseUrl.TrimEnd('/');
        Product = product;
        _http = http ?? SharedHttp;
    }

    public string Product { get; }

    /// <summary>Activate the key on this computer. Uses up one activation unless this computer is already activated.</summary>
    public Task<LicenseResult> ActivateAsync(string key, CancellationToken ct = default) => PostAsync("activate", key, ct);

    /// <summary>Confirm the key is still valid and already activated on this computer. Does not use an activation.</summary>
    public Task<LicenseResult> ValidateAsync(string key, CancellationToken ct = default) => PostAsync("validate", key, ct);

    /// <summary>Free this computer's activation, e.g. before moving to a new PC.</summary>
    public Task<LicenseResult> DeactivateAsync(string key, CancellationToken ct = default) => PostAsync("deactivate", key, ct);

    /// <summary>
    /// For app start-up: validate the saved key online. If the server cannot be reached and the key was
    /// confirmed within <paramref name="offlineGrace"/>, the result is <see cref="LicenseStatus.OfflineGrace"/>.
    /// </summary>
    public async Task<LicenseResult> CheckAsync(LicenseStore store, TimeSpan offlineGrace, CancellationToken ct = default)
    {
        var saved = store.Load();
        if (saved is null)
            return new LicenseResult(LicenseStatus.NoKey, "No licence key has been entered on this computer.");

        var result = await ValidateAsync(saved.Key, ct).ConfigureAwait(false);
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

    private async Task<LicenseResult> PostAsync(string action, string key, CancellationToken ct)
    {
        var normalized = LicenseKey.Normalize(key);
        if (normalized is null)
            return new LicenseResult(LicenseStatus.InvalidFormat, "That is not a valid licence key. Check for typing mistakes.");

        var payload = JsonSerializer.Serialize(new { key = normalized, machine_id = MachineId.Get(), machine_name = MachineId.DisplayName(), product = Product });
        HttpResponseMessage response;
        try
        {
            using var content = new StringContent(payload, Encoding.UTF8, "application/json");
            response = await _http.PostAsync($"{_baseUrl}/api/licenses/{action}", content, ct).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return new LicenseResult(LicenseStatus.NetworkError, "Could not reach the licence server. Check your internet connection.");
        }

        using (response)
        {
            Reply? reply = null;
            try
            {
                var text = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                reply = JsonSerializer.Deserialize<Reply>(text);
            }
            catch (JsonException)
            {
            }

            if (reply is null)
                return new LicenseResult(LicenseStatus.ServerError, $"The licence server answered unexpectedly ({(int)response.StatusCode}).");
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

    private sealed class Reply
    {
        [JsonPropertyName("valid")] public bool Valid { get; set; }
        [JsonPropertyName("reason")] public string? Reason { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
        [JsonPropertyName("activations_used")] public int ActivationsUsed { get; set; }
        [JsonPropertyName("max_activations")] public int MaxActivations { get; set; }
    }
}
