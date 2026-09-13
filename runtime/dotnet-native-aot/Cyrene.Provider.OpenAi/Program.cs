// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 Program.cs                                                      │
// │  Namespace: Cyrene.Provider.OpenAi                                  │
// │  Role: Standalone Native AOT Provider entrypoint (T87, T88).        │
// └─────────────────────────────────────────────────────────────────────┘

namespace Cyrene.Provider.OpenAi;

public static class Program
{
    public static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--readiness")
        {
            Console.WriteLine("{\"status\":\"NOT_SERVING\",\"provider\":\"openai\",\"reason\":\"DIRECT_RUNTIME_HOST_NOT_CONFIGURED\",\"aot\":true}");
            return 2;
        }

        Console.Error.WriteLine("DirectPluginRuntime host configuration is required.");
        return 2;
    }
}
