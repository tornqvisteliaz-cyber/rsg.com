#if !NET5_0_OR_GREATER
// Lets records and init-only properties compile on .NET Framework 4.8.
namespace System.Runtime.CompilerServices
{
    internal static class IsExternalInit
    {
    }
}
#endif
