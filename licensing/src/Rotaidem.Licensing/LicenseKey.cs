using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace Rotaidem.Licensing;

/// <summary>
/// Licence key format: PREFIX-XXXX-XXXX-XXXX, for example HPRO-7KQM-2XRT-9HCW.
/// The 12 characters after the prefix are 11 random characters plus 1 check character, all taken
/// from a 32-letter alphabet without look-alikes (no I, O, 0 or 1). The check character catches
/// typing mistakes before anything is sent to the server; whether a key is real is decided by the
/// server, which keeps the list of issued keys.
/// The same rules are implemented in server.py and cloudflare/functions/api/[[path]].js.
/// </summary>
public static class LicenseKey
{
    public const string Alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    public const string HeavyProPrefix = "HPRO";

    private static readonly Regex KeyPattern = new("^([A-Z0-9]{2,8})-([A-Z2-9]{4})-([A-Z2-9]{4})-([A-Z2-9]{4})$", RegexOptions.CultureInvariant);

    /// <summary>Create a new random key with a cryptographically secure generator.</summary>
    public static string Generate(string prefix = HeavyProPrefix)
    {
        prefix = NormalizePrefix(prefix);
        var random = new byte[11];
        using (var rng = RandomNumberGenerator.Create())
            rng.GetBytes(random);
        // The alphabet has exactly 32 letters, so the low 5 bits of each byte pick a letter without bias.
        var body = new string(random.Select(b => Alphabet[b & 31]).ToArray());
        var full = body + CheckCharacter(prefix, body);
        return $"{prefix}-{full.Substring(0, 4)}-{full.Substring(4, 4)}-{full.Substring(8, 4)}";
    }

    /// <summary>Upper-cases the input and removes spaces. Returns null if the format or check character is wrong.</summary>
    public static string? Normalize(string? input)
    {
        if (input is null || string.IsNullOrWhiteSpace(input))
            return null;
        var key = new string(input.Where(c => !char.IsWhiteSpace(c)).ToArray()).ToUpperInvariant();
        var match = KeyPattern.Match(key);
        if (!match.Success)
            return null;
        var chars = match.Groups[2].Value + match.Groups[3].Value + match.Groups[4].Value;
        if (chars.Any(c => Alphabet.IndexOf(c) < 0))
            return null;
        return CheckCharacter(match.Groups[1].Value, chars.Substring(0, 11)) == chars[11] ? key : null;
    }

    /// <summary>True when the key is well formed and its check character matches. Does not contact the server.</summary>
    public static bool IsWellFormed(string? input) => Normalize(input) is not null;

    /// <summary>True when the key is well formed and starts with the given product prefix.</summary>
    public static bool IsWellFormed(string? input, string prefix) =>
        Normalize(input) is { } key && key.StartsWith(NormalizePrefix(prefix) + "-", StringComparison.Ordinal);

    public static char CheckCharacter(string prefix, string body11)
    {
        var total = 0;
        foreach (var c in prefix)
            total += c;
        for (var i = 0; i < body11.Length; i++)
            total += (2 * i + 1) * Alphabet.IndexOf(body11[i]);
        return Alphabet[total % Alphabet.Length];
    }

    private static string NormalizePrefix(string prefix)
    {
        var clean = new StringBuilder();
        foreach (var c in prefix.ToUpperInvariant())
            if (c is >= 'A' and <= 'Z' or >= '0' and <= '9')
                clean.Append(c);
        if (clean.Length is < 2 or > 8)
            throw new ArgumentException("Prefix must be 2 to 8 letters or digits, for example HPRO.", nameof(prefix));
        return clean.ToString();
    }
}
