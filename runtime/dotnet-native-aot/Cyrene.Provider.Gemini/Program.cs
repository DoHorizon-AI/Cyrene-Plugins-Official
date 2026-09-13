// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 Program.cs                                                      │
// │  Namespace: Cyrene.Provider.Gemini                                  │
// │  Role: Standalone Native AOT Gemini provider entrypoint.            │
// └─────────────────────────────────────────────────────────────────────┘

namespace Cyrene.Provider.Gemini;

public static class Program
{
    public static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--readiness")
        {
            Console.WriteLine("{\"status\":\"NOT_SERVING\",\"provider\":\"gemini\",\"reason\":\"DIRECT_RUNTIME_HOST_NOT_CONFIGURED\",\"aot\":true}");
            return 2;
        }

        Console.Error.WriteLine("DirectPluginRuntime host configuration is required.");
        return 2;
    }
}
