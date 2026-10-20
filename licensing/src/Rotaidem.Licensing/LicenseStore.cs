using System.Globalization;

namespace Rotaidem.Licensing;

public sealed record SavedLicense(string Key, DateTimeOffset LastValidatedUtc);

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
            return new SavedLicense(LicenseKey.Normalize(saved.Key)!, when);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    public void Save(string key, DateTimeOffset lastValidatedUtc)
    {
        var normalized = LicenseKey.Normalize(key) ?? throw new ArgumentException("Not a valid licence key.", nameof(key));
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        File.WriteAllText(FilePath, Json.Serialize(new SavedLicenseFile
        {
            Key = normalized,
            LastValidatedUtc = lastValidatedUtc.ToUniversalTime().ToString("o", CultureInfo.InvariantCulture),
        }));
    }

    public void Clear()
    {
        if (File.Exists(FilePath))
            File.Delete(FilePath);
    }
}
