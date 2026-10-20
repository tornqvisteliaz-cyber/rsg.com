using System.Security.Cryptography;
using System.Text;
using Microsoft.Win32;

namespace Rotaidem.Licensing;

/// <summary>
/// A stable, anonymous id for this computer, used to count activations. On Windows it is derived
/// from the MachineGuid that Windows creates at install time, hashed so the raw value never leaves
/// the computer. It survives reinstalling HeavyPro but changes if Windows is reinstalled.
/// </summary>
public static class MachineId
{
    private const string Salt = "rotaidem-licensing-v1";

    public static string Get()
    {
        var raw = ReadWindowsMachineGuid() ?? $"{Environment.MachineName}|{Environment.UserName}|{Environment.OSVersion.Platform}";
        using var sha = SHA256.Create();
        var hash = sha.ComputeHash(Encoding.UTF8.GetBytes(Salt + "|" + raw));
        return BitConverter.ToString(hash, 0, 16).Replace("-", "").ToLowerInvariant();
    }

    /// <summary>A readable name shown in the admin panel next to the activation, e.g. "EliazPC".</summary>
    public static string DisplayName() => Environment.MachineName;

    private static string? ReadWindowsMachineGuid()
    {
#if NET5_0_OR_GREATER
        if (!OperatingSystem.IsWindows())
            return null;
#else
        if (Environment.OSVersion.Platform != PlatformID.Win32NT)
            return null;
#endif
        try
        {
            using var key = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64)
                .OpenSubKey(@"SOFTWARE\Microsoft\Cryptography");
            return key?.GetValue("MachineGuid") as string;
        }
        catch (Exception)
        {
            return null;
        }
    }
}
