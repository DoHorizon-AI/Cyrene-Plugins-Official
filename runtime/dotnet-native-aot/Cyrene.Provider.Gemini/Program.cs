// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 Program.cs                                                      │
// │  Namespace: Cyrene.Provider.Gemini                                  │
// │  Role: Standalone Native AOT Gemini provider entrypoint.            │
// └─────────────────────────────────────────────────────────────────────┘
// 中文：文件：Program.cs
// 中文：命名空间：Cyrene.Provider.Gemini
// 中文：职责：独立的 Native AOT Gemini Provider 入口点。

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
