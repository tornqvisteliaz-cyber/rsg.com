using System.Globalization;

namespace Rotaidem.Licensing;

/// <param name="Token">The server's signed answer from the last successful check, or null (older versions, or a rejected key).</param>
/// <param name="LastSeen">The latest time this computer has seen.</param>
public sealed record SavedLicense(string Key, DateTimeOffset LastValidatedUtc, string? Token = null, DateTimeOffset? LastSeen = null);

/// <summary>
/// Remembers the activated key on this computer, in %LOCALAPPDATA%\Rotaidem\{product}\license.json.
/// The installer saves the key after a successful activation; the app reads it at start-up.
/// </summary>
public sealed class LicenseStore
{
    public LicenseStore(string product = "heavypro", string? folder = null)
    {
        var root = folder ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Rotaidem", product);
        FilePath = Path.Combine(root, "license.json");
    }

    public string FilePath { get; }

    public SavedLicense? Load()
    {
        try
        {
            if (!File.Exists(FilePath))
                return null;
            var saved = Json.Deserialize<SavedLicenseFile>(File.ReadAllText(FilePath));
            if (saved is null || !LicenseKey.IsWellFormed(saved.Key) ||
                !DateTimeOffset.TryParse(saved.LastValidatedUtc, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var when))
                return null;
            return new SavedLicense(LicenseKey.Normalize(saved.Key)!, when, string.IsNullOrEmpty(saved.Token) ? null : saved.Token,
                saved.LastSeen > 0 ? DateTimeOffset.FromUnixTimeSeconds(saved.LastSeen) : null);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    /// <summary>Saves the key; <paramref name="token"/> is the server's signed answer (null forgets it, so the next start must check online).</summary>
    public void Save(string key, DateTimeOffset lastValidatedUtc, string? token = null)
    {
        var normalized = LicenseKey.Normalize(key) ?? throw new ArgumentException("Not a valid licence key.", nameof(key));
        var seen = Math.Max(Load()?.LastSeen?.ToUnixTimeSeconds() ?? 0, DateTimeOffset.UtcNow.ToUnixTimeSeconds());
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        var temp = FilePath + ".tmp";
        File.WriteAllText(temp, Json.Serialize(new SavedLicenseFile
        {
            Key = normalized,
            LastValidatedUtc = lastValidatedUtc.ToUniversalTime().ToString("o", CultureInfo.InvariantCulture),
            Token = token,
            LastSeen = seen,
        }));
        if (File.Exists(FilePath))
            File.Delete(FilePath);
        File.Move(temp, FilePath);
    }

    /// <summary>Remembers that this computer has seen the current time (used to notice a clock turned back).</summary>
    public void Touch()
    {
        if (Load() is { } saved)
            Save(saved.Key, saved.LastValidatedUtc, saved.Token);
    }

    public void Clear()
    {
        if (File.Exists(FilePath))
            File.Delete(FilePath);
    }
}
