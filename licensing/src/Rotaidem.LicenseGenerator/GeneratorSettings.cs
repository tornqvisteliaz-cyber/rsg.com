using System.Text.Json;
using System.Text.Json.Serialization;

namespace Rotaidem.LicenseGenerator;

/// <summary>
/// Read from generator.json next to the program. Environment variables ROTAIDEM_SITE_URL and
/// ROTAIDEM_LICENSE_API_KEY override the file, so the API key never has to be written to disk.
/// </summary>
public sealed class GeneratorSettings
{
    [JsonPropertyName("siteUrl")] public string SiteUrl { get; set; } = "https://rotaidem.com";
    [JsonPropertyName("apiKey")] public string ApiKey { get; set; } = "";
    [JsonPropertyName("prefix")] public string Prefix { get; set; } = "HPRO";
    [JsonPropertyName("product")] public string Product { get; set; } = "heavypro";
    [JsonPropertyName("maxActivations")] public int MaxActivations { get; set; } = 2;

    [JsonIgnore] public string Source { get; private set; } = "defaults";

    public bool CanUpload => ApiKey.Length >= 16 && !ApiKey.StartsWith("paste ", StringComparison.OrdinalIgnoreCase);

    public static string FilePath => Path.Combine(AppContext.BaseDirectory, "generator.json");

    public static GeneratorSettings Load()
    {
        var settings = new GeneratorSettings();
        if (File.Exists(FilePath))
        {
            try
            {
                settings = JsonSerializer.Deserialize<GeneratorSettings>(File.ReadAllText(FilePath)) ?? settings;
                settings.Source = FilePath;
            }
            catch (JsonException ex)
            {
                Console.Error.WriteLine($"Could not read {FilePath}: {ex.Message}");
            }
        }
        if (Environment.GetEnvironmentVariable("ROTAIDEM_SITE_URL") is { Length: > 0 } url)
            settings.SiteUrl = url;
        if (Environment.GetEnvironmentVariable("ROTAIDEM_LICENSE_API_KEY") is { Length: > 0 } key)
            settings.ApiKey = key;
        settings.SiteUrl = settings.SiteUrl.TrimEnd('/');
        return settings;
    }
}
