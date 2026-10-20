using System.Globalization;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json.Serialization;
using Rotaidem.LicenseGenerator;
using Rotaidem.Licensing;

var settings = GeneratorSettings.Load();

if (args.Length == 0)
    return await RunMenu(settings);

switch (args[0].ToLowerInvariant())
{
    case "generate":
        GenerateOptions options;
        try
        {
            options = GenerateOptions.Parse(args.Skip(1).ToArray(), settings);
        }
        catch (Exception ex) when (ex is ArgumentException or FormatException)
        {
            Console.Error.WriteLine(ex.Message);
            return 1;
        }
        return await Generate(settings, options);
    case "verify":
        return Verify(args.Length > 1 ? args[1] : "");
    case "help" or "--help" or "-h" or "/?":
        PrintHelp();
        return 0;
    default:
        Console.Error.WriteLine($"Unknown command '{args[0]}'.");
        PrintHelp();
        return 1;
}

static async Task<int> RunMenu(GeneratorSettings settings)
{
    Console.OutputEncoding = Encoding.UTF8;
    while (true)
    {
        Console.WriteLine();
        Console.WriteLine("ROTAIDEM LICENCE GENERATOR");
        Console.WriteLine($"Site: {settings.SiteUrl}   Upload: {(settings.CanUpload ? "on" : "off (no API key)")}");
        Console.WriteLine();
        Console.WriteLine("  1  Generate licence keys");
        Console.WriteLine("  2  Check a licence key");
        Console.WriteLine("  3  Settings");
        Console.WriteLine("  0  Exit");
        switch (Ask("Choose", "1"))
        {
            case "1":
                var options = new GenerateOptions
                {
                    Count = int.TryParse(Ask("How many keys", "1"), out var n) ? n : 1,
                    Prefix = Ask("Prefix", settings.Prefix),
                    Product = Ask("Product id on the website", settings.Product),
                    Email = Ask("Customer email (optional)", ""),
                    MaxActivations = int.TryParse(Ask("Computers per key", settings.MaxActivations.ToString(CultureInfo.InvariantCulture)), out var m) ? m : settings.MaxActivations,
                    Note = Ask("Note (optional)", ""),
                    Upload = settings.CanUpload && Ask("Register the keys on the website? (y/n)", "y").StartsWith('y'),
                };
                options.SendEmail = options.Upload && options.Email.Length > 0 && Ask($"Email the keys to {options.Email}? (y/n)", "y").StartsWith('y');
                await Generate(settings, options);
                break;
            case "2":
                Verify(Ask("Licence key", ""));
                break;
            case "3":
                Console.WriteLine($"Settings file: {GeneratorSettings.FilePath} ({(File.Exists(GeneratorSettings.FilePath) ? "found" : "not found")})");
                Console.WriteLine("Copy generator.example.json to generator.json and fill in siteUrl and apiKey,");
                Console.WriteLine("or set the environment variables ROTAIDEM_SITE_URL and ROTAIDEM_LICENSE_API_KEY.");
                break;
            case "0" or "q":
                return 0;
        }
    }
}

static async Task<int> Generate(GeneratorSettings settings, GenerateOptions options)
{
    if (options.Count is < 1 or > 1000)
    {
        Console.Error.WriteLine("Count must be between 1 and 1000.");
        return 1;
    }
    if (options.MaxActivations is < 1 or > 100)
    {
        Console.Error.WriteLine("Computers per key must be between 1 and 100.");
        return 1;
    }

    List<string> keys;
    try
    {
        keys = Enumerable.Range(0, options.Count).Select(_ => LicenseKey.Generate(options.Prefix)).Distinct().ToList();
    }
    catch (ArgumentException ex)
    {
        Console.Error.WriteLine(ex.Message);
        return 1;
    }

    var uploaded = new HashSet<string>();
    if (options.Upload)
    {
        if (!settings.CanUpload)
        {
            Console.Error.WriteLine("Cannot upload: no API key. See option 3 (Settings). The keys are still saved to a file.");
        }
        else
        {
            var result = await Upload(settings, keys, options);
            if (result is not null)
            {
                uploaded.UnionWith(result.Created);
                Console.WriteLine($"Registered on {settings.SiteUrl}: {result.Created.Count}. Already existed: {result.Skipped.Count}. Rejected: {result.Invalid.Count}.");
                if (result.Emailed.Count > 0)
                    Console.WriteLine($"Emailed to {string.Join(", ", result.Emailed)}.");
                if (!string.IsNullOrEmpty(result.EmailError))
                    Console.Error.WriteLine($"Email not sent: {result.EmailError}");
            }
        }
    }

    var file = options.OutFile ?? Path.Combine(Environment.CurrentDirectory, $"licences-{DateTime.Now:yyyyMMdd-HHmmss}.csv");
    var csv = new StringBuilder("key,product,email,max_activations,note,registered\n");
    foreach (var key in keys)
        csv.AppendLine(string.Join(',', key, Csv(options.Product), Csv(options.Email), options.MaxActivations, Csv(options.Note), uploaded.Contains(key) ? "yes" : "no"));
    File.WriteAllText(file, csv.ToString());

    Console.WriteLine();
    foreach (var key in keys)
        Console.WriteLine($"  {key}{(options.Upload && !uploaded.Contains(key) ? "   (not registered)" : "")}");
    Console.WriteLine();
    Console.WriteLine($"Saved to {file}");
    if (uploaded.Count == 0)
        Console.WriteLine("These keys only work after they are registered: upload them with this program or import the CSV in Admin > Licences.");
    return 0;
}

static async Task<ImportReply?> Upload(GeneratorSettings settings, List<string> keys, GenerateOptions options)
{
    using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
    http.DefaultRequestHeaders.Add("X-License-Api-Key", settings.ApiKey);
    var payload = new
    {
        send_email = options.SendEmail,
        licenses = keys.Select(k => new { key = k, product_id = options.Product, email = options.Email, max_activations = options.MaxActivations, note = options.Note }),
    };
    try
    {
        using var response = await http.PostAsJsonAsync($"{settings.SiteUrl}/api/licenses/import", payload);
        if (!response.IsSuccessStatusCode)
        {
            Console.Error.WriteLine($"Upload failed ({(int)response.StatusCode}): {await response.Content.ReadAsStringAsync()}");
            return null;
        }
        return await response.Content.ReadFromJsonAsync<ImportReply>();
    }
    catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
    {
        Console.Error.WriteLine($"Could not reach {settings.SiteUrl}: {ex.Message}");
        return null;
    }
}

static int Verify(string key)
{
    var normalized = LicenseKey.Normalize(key);
    Console.WriteLine(normalized is null
        ? "Not a valid key: wrong format or a typing mistake."
        : $"{normalized} is well formed. Whether it has been issued is checked by the website.");
    return normalized is null ? 1 : 0;
}

static string Ask(string question, string fallback)
{
    Console.Write(fallback.Length > 0 ? $"{question} [{fallback}]: " : $"{question}: ");
    var answer = Console.ReadLine()?.Trim();
    return string.IsNullOrEmpty(answer) ? fallback : answer;
}

static string Csv(string value) => value.Contains(',') || value.Contains('"') ? $"\"{value.Replace("\"", "\"\"")}\"" : value;

static void PrintHelp()
{
    Console.WriteLine("""
        Rotaidem licence generator

          RotaidemLicenseGenerator                      interactive menu
          RotaidemLicenseGenerator generate [options]   create keys
          RotaidemLicenseGenerator verify KEY           check a key's format

        Options for generate:
          --count N          number of keys (default 1)
          --prefix HPRO      key prefix
          --product ID       product id on the website (default heavypro)
          --email ADDRESS    customer email to link the keys to
          --max N            computers per key (default 2)
          --note TEXT        note shown in the admin panel
          --no-upload        only save the CSV, do not register on the website
          --no-email         do not email the keys to --email (emailing is on by default)
          --out FILE         CSV file to write
        """);
}

sealed class GenerateOptions
{
    public int Count { get; set; } = 1;
    public string Prefix { get; set; } = LicenseKey.HeavyProPrefix;
    public string Product { get; set; } = "heavypro";
    public string Email { get; set; } = "";
    public int MaxActivations { get; set; } = 2;
    public string Note { get; set; } = "";
    public bool Upload { get; set; } = true;
    public bool SendEmail { get; set; } = true;
    public string? OutFile { get; set; }

    public static GenerateOptions Parse(string[] args, GeneratorSettings settings)
    {
        var o = new GenerateOptions { Prefix = settings.Prefix, Product = settings.Product, MaxActivations = settings.MaxActivations, Upload = settings.CanUpload };
        for (var i = 0; i < args.Length; i++)
        {
            string Next() => i + 1 < args.Length ? args[++i] : throw new ArgumentException($"{args[i]} needs a value");
            int Number() { var name = args[i]; return int.TryParse(Next(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var n) ? n : throw new ArgumentException($"{name} must be a whole number"); }
            switch (args[i])
            {
                case "--count": o.Count = Number(); break;
                case "--prefix": o.Prefix = Next(); break;
                case "--product": o.Product = Next(); break;
                case "--email": o.Email = Next(); break;
                case "--max": o.MaxActivations = Number(); break;
                case "--note": o.Note = Next(); break;
                case "--no-upload": o.Upload = false; break;
                case "--no-email": o.SendEmail = false; break;
                case "--out": o.OutFile = Next(); break;
                default: throw new ArgumentException($"Unknown option {args[i]}");
            }
        }
        o.SendEmail = o.SendEmail && o.Upload && o.Email.Length > 0;
        return o;
    }
}

sealed class ImportReply
{
    [JsonPropertyName("created")] public List<string> Created { get; set; } = [];
    [JsonPropertyName("skipped")] public List<string> Skipped { get; set; } = [];
    [JsonPropertyName("invalid")] public List<string> Invalid { get; set; } = [];
    [JsonPropertyName("emailed")] public List<string> Emailed { get; set; } = [];
    [JsonPropertyName("email_error")] public string? EmailError { get; set; }
}
