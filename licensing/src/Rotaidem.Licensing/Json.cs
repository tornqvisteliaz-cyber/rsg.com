using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;

namespace Rotaidem.Licensing;

/// <summary>
/// JSON through DataContractJsonSerializer, which is built into both .NET Framework and .NET,
/// so the library has no package dependencies and can be compiled straight into a small
/// .NET Framework installer.
/// </summary>
internal static class Json
{
    public static string Serialize<T>(T value)
    {
        using var stream = new MemoryStream();
        new DataContractJsonSerializer(typeof(T)).WriteObject(stream, value);
        return Encoding.UTF8.GetString(stream.ToArray());
    }

    /// <summary>Returns null for empty or malformed input instead of throwing.</summary>
    public static T? Deserialize<T>(string text) where T : class
    {
        if (string.IsNullOrWhiteSpace(text))
            return null;
        try
        {
            using var stream = new MemoryStream(Encoding.UTF8.GetBytes(text));
            return new DataContractJsonSerializer(typeof(T)).ReadObject(stream) as T;
        }
        catch (Exception ex) when (ex is SerializationException or InvalidCastException or ArgumentException or FormatException)
        {
            return null;
        }
    }
}

[DataContract]
internal sealed class LicenseRequest
{
    [DataMember(Name = "key", Order = 0)] public string Key { get; set; } = "";
    [DataMember(Name = "machine_id", Order = 1)] public string MachineId { get; set; } = "";
    [DataMember(Name = "machine_name", Order = 2)] public string MachineName { get; set; } = "";
    [DataMember(Name = "product", Order = 3)] public string Product { get; set; } = "";
    [DataMember(Name = "nonce", Order = 4, EmitDefaultValue = false)] public string? Nonce { get; set; }
}

[DataContract]
internal sealed class LicenseReply
{
    [DataMember(Name = "valid")] public bool Valid { get; set; }
    [DataMember(Name = "reason")] public string? Reason { get; set; }
    [DataMember(Name = "error")] public string? Error { get; set; }
    [DataMember(Name = "activations_used")] public int ActivationsUsed { get; set; }
    [DataMember(Name = "max_activations")] public int MaxActivations { get; set; }
    [DataMember(Name = "token")] public string? Token { get; set; }
}

[DataContract]
internal sealed class Jwk
{
    [DataMember(Name = "kty")] public string? Kty { get; set; }
    [DataMember(Name = "n")] public string? N { get; set; }
    [DataMember(Name = "e")] public string? E { get; set; }
    [DataMember(Name = "kid")] public string? Kid { get; set; }
}

[DataContract]
internal sealed class JwkSet
{
    [DataMember(Name = "keys")] public Jwk[]? Keys { get; set; }
}

[DataContract]
internal sealed class TokenHeader
{
    [DataMember(Name = "alg")] public string? Alg { get; set; }
    [DataMember(Name = "kid")] public string? Kid { get; set; }
}

[DataContract]
internal sealed class TokenClaims
{
    [DataMember(Name = "aud")] public string? Aud { get; set; }
    [DataMember(Name = "key")] public string? Key { get; set; }
    [DataMember(Name = "mid")] public string? Mid { get; set; }
    [DataMember(Name = "nonce")] public string? Nonce { get; set; }
    [DataMember(Name = "iat")] public long Iat { get; set; }
    [DataMember(Name = "exp")] public long Exp { get; set; }
    [DataMember(Name = "activations_used")] public int ActivationsUsed { get; set; }
    [DataMember(Name = "max_activations")] public int MaxActivations { get; set; }
}

/// <summary>On-disk shape of license.json. Same property names the earlier System.Text.Json version wrote.</summary>
[DataContract]
internal sealed class SavedLicenseFile
{
    [DataMember(Name = "Key", Order = 0)] public string Key { get; set; } = "";
    [DataMember(Name = "LastValidatedUtc", Order = 1)] public string LastValidatedUtc { get; set; } = "";
    /// <summary>The server's signed answer: what lets HeavyPro start offline (until its lease ends).</summary>
    [DataMember(Name = "Token", Order = 2, EmitDefaultValue = false)] public string? Token { get; set; }
    /// <summary>The latest time this computer has seen (Unix seconds); a clock earlier than that was turned back.</summary>
    [DataMember(Name = "LastSeen", Order = 3, EmitDefaultValue = false)] public long LastSeen { get; set; }
}
