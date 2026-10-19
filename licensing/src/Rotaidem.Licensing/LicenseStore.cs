using System.Text.Json;

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
            var saved = JsonSerializer.Deserialize<SavedLicense>(File.ReadAllText(FilePath));
            return saved is not null && LicenseKey.IsWellFormed(saved.Key) ? saved : null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            return null;
        }
    }

    public void Save(string key, DateTimeOffset lastValidatedUtc)
    {
        var normalized = LicenseKey.Normalize(key) ?? throw new ArgumentException("Not a valid licence key.", nameof(key));
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        File.WriteAllText(FilePath, JsonSerializer.Serialize(new SavedLicense(normalized, lastValidatedUtc)));
    }

    public void Clear()
    {
        if (File.Exists(FilePath))
            File.Delete(FilePath);
    }
}
