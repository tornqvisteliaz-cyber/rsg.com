using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace Rotaidem.Licensing;

/// <summary>What a signed licence answer says (the claims of the server's token).</summary>
public sealed record LicenseClaims(string Product, string Key, string MachineId, string? Nonce, DateTimeOffset IssuedAt,
    DateTimeOffset LeaseUntil, int ActivationsUsed, int MaxActivations);

/// <summary>
/// Checks the signed token in the licence server's answers (RS256, the same scheme KeyForge uses).
/// The server signs with a private key that never leaves it; this class holds only the public key,
/// so nobody can make a token it accepts. That is what stops a fake licence server, an edited
/// saved licence and an activation copied to another PC.
/// </summary>
public sealed class LicenseToken
{
    private readonly RSAParameters _key;
    private readonly string? _keyId;

    private LicenseToken(RSAParameters key, string? keyId)
    {
        _key = key;
        _keyId = keyId;
    }

    /// <summary>The public key as JSON (JWK), from https://rotaidem.com/api/licenses/jwks.json. Null if it is not a usable RSA key.</summary>
    public static LicenseToken? FromJwk(string? jwkJson)
    {
        if (string.IsNullOrWhiteSpace(jwkJson))
            return null;
        // Accept the JWK set the server publishes ({"keys":[...]}) as well as a single key.
        var set = Json.Deserialize<JwkSet>(jwkJson!);
        var jwk = set?.Keys is { Length: > 0 } ? set.Keys[0] : Json.Deserialize<Jwk>(jwkJson!);
        if (jwk is null || jwk.Kty != "RSA" || string.IsNullOrEmpty(jwk.N) || string.IsNullOrEmpty(jwk.E))
            return null;
        try
        {
            return new LicenseToken(new RSAParameters { Modulus = Base64Url(jwk.N!), Exponent = Base64Url(jwk.E!) }, jwk.Kid);
        }
        catch (FormatException)
        {
            return null;
        }
    }

    /// <summary>
    /// The claims, or null with a reason in <paramref name="problem"/>. Checks the signature, product,
    /// computer, key, nonce (when given), lease and clock. <paramref name="lastSeen"/> is the latest
    /// time this computer has seen; a clock earlier than that means it was turned back.
    /// </summary>
    public LicenseClaims? Verify(string? token, string product, string machineId, string? key, string? nonce, DateTimeOffset now,
        DateTimeOffset? lastSeen, out LicenseStatus status, out string problem)
    {
        status = LicenseStatus.Unverified;
        problem = "The licence answer is not signed by the Rotaidem licence server.";
        var parts = (token ?? "").Split('.');
        if (parts.Length != 3)
            return null;
        TokenHeader? header;
        TokenClaims? claims;
        try
        {
            header = Json.Deserialize<TokenHeader>(Encoding.UTF8.GetString(Base64Url(parts[0])));
            claims = Json.Deserialize<TokenClaims>(Encoding.UTF8.GetString(Base64Url(parts[1])));
            if (header?.Alg != "RS256" || claims is null)
                return null;
            if (!string.IsNullOrEmpty(_keyId) && !string.IsNullOrEmpty(header.Kid) && header.Kid != _keyId)
                return null;
            using var rsa = RSA.Create();
            rsa.ImportParameters(_key);
            if (!rsa.VerifyData(Encoding.ASCII.GetBytes(parts[0] + "." + parts[1]), Base64Url(parts[2]), HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1))
                return null;
        }
        catch (Exception ex) when (ex is FormatException or CryptographicException or ArgumentException)
        {
            return null;
        }

        if (!SameProduct(product, claims.Aud))
        {
            status = LicenseStatus.WrongProduct;
            problem = "This licence is for another product.";
            return null;
        }
        if (claims.Mid != machineId)
        {
            problem = "This licence was activated on another computer. Activate it on this one.";
            return null;
        }
        if (key is not null && claims.Key != LicenseKey.Normalize(key))
        {
            problem = "The licence answer is for another key.";
            return null;
        }
        if (nonce is not null && claims.Nonce != nonce)
        {
            problem = "The licence answer does not belong to this request.";
            return null;
        }
        var issued = DateTimeOffset.FromUnixTimeSeconds(claims.Iat);
        var until = DateTimeOffset.FromUnixTimeSeconds(claims.Exp);
        if (now.AddHours(1) < issued || (lastSeen is { } seen && now.AddHours(1) < seen))
        {
            status = LicenseStatus.ClockTampered;
            problem = "The computer's clock is wrong. Set the correct date and time, then start HeavyPro again.";
            return null;
        }
        if (now >= until)
        {
            status = LicenseStatus.LeaseExpired;
            problem = "HeavyPro has been offline too long. Connect to the internet once so the licence can be checked.";
            return null;
        }
        status = LicenseStatus.Valid;
        problem = "";
        return new LicenseClaims(claims.Aud ?? "", claims.Key ?? "", claims.Mid ?? "", claims.Nonce, issued, until, claims.ActivationsUsed, claims.MaxActivations);
    }

    /// <summary>Same rule as the server's same_product: ids match ignoring case and punctuation, or one is the other plus words ("heavypro" ~ "heavypro-msfs-2024").</summary>
    public static bool SameProduct(string? a, string? b)
    {
        static (HashSet<string> All, string Joined) Prefixes(string? value)
        {
            var words = Regex.Matches((value ?? "").ToLowerInvariant(), "[a-z0-9]+").Cast<Match>().Select(m => m.Value).ToList();
            var all = new HashSet<string>();
            for (var i = 1; i <= words.Count; i++)
                all.Add(string.Concat(words.Take(i)));
            return (all, string.Concat(words));
        }
        var (aAll, aJoined) = Prefixes(a);
        var (bAll, bJoined) = Prefixes(b);
        return aJoined.Length > 0 && bJoined.Length > 0 && (aAll.Contains(bJoined) || bAll.Contains(aJoined));
    }

    internal static byte[] Base64Url(string text)
    {
        var s = text.Replace('-', '+').Replace('_', '/');
        return Convert.FromBase64String(s + new string('=', (4 - s.Length % 4) % 4));
    }
}
