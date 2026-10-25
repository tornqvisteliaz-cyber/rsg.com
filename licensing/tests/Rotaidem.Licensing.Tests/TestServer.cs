using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Rotaidem.Licensing.Tests;

/// <summary>Plays the licence server's signing side: its own RSA key, and signed answers like server.py makes.</summary>
internal sealed class TestSigner : IDisposable
{
    private readonly RSA _rsa = RSA.Create(2048);

    public string Jwk
    {
        get
        {
            var p = _rsa.ExportParameters(false);
            return JsonSerializer.Serialize(new { kty = "RSA", alg = "RS256", kid = "test", n = B64(p.Modulus!), e = B64(p.Exponent!) });
        }
    }

    public static string B64(byte[] data) => Convert.ToBase64String(data).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    public string Token(string key, string machine, string? nonce, string product = "heavypro", TimeSpan? lease = null, DateTimeOffset? issued = null)
    {
        var now = issued ?? DateTimeOffset.UtcNow;
        var claims = new JsonObject
        {
            ["aud"] = product, ["key"] = key, ["mid"] = machine, ["iat"] = now.ToUnixTimeSeconds(),
            ["exp"] = now.Add(lease ?? TimeSpan.FromDays(14)).ToUnixTimeSeconds(), ["activations_used"] = 1, ["max_activations"] = 2,
        };
        if (nonce != null) claims["nonce"] = nonce;
        return Sign(claims);
    }

    public string Sign(JsonObject claims)
    {
        var data = B64(Encoding.UTF8.GetBytes("{\"alg\":\"RS256\",\"typ\":\"JWT\",\"kid\":\"test\"}")) + "." + B64(Encoding.UTF8.GetBytes(claims.ToJsonString()));
        return data + "." + B64(_rsa.SignData(Encoding.ASCII.GetBytes(data), HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1));
    }

    public static JsonObject Claims(string token) =>
        JsonNode.Parse(Encoding.UTF8.GetString(Convert.FromBase64String(Pad(token.Split('.')[1]))))!.AsObject();

    /// <summary>The same token with changed claims but the old signature (what an editor of license.json would produce).</summary>
    public static string Edit(string token, Action<JsonObject> change)
    {
        var parts = token.Split('.');
        var claims = Claims(token);
        change(claims);
        return parts[0] + "." + B64(Encoding.UTF8.GetBytes(claims.ToJsonString())) + "." + parts[2];
    }

    private static string Pad(string s) { s = s.Replace('-', '+').Replace('_', '/'); return s + new string('=', (4 - s.Length % 4) % 4); }

    /// <summary>A reply like the real server's: valid, with a token for the key, machine and nonce of the request.</summary>
    public Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> SignedOk(Func<string, string, string?, string>? token = null) =>
        async (request, _) =>
        {
            var body = JsonNode.Parse(await request.Content!.ReadAsStringAsync())!;
            var key = body["key"]!.GetValue<string>();
            var machine = body["machine_id"]!.GetValue<string>();
            var nonce = body["nonce"]?.GetValue<string>();
            var reply = new { valid = true, activations_used = 1, max_activations = 2, token = (token ?? ((k, m, n) => Token(k, m, n)))(key, machine, nonce) };
            return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(JsonSerializer.Serialize(reply), Encoding.UTF8, "application/json") };
        };

    public void Dispose() => _rsa.Dispose();
}

internal sealed class FakeServer(params Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>>[] replies) : HttpMessageHandler
{
    public int Calls { get; private set; }

    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct) =>
        replies[Math.Min(Calls++, replies.Length - 1)](request, ct);
}
