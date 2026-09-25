// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 Program.cs                                                      │
// │  Namespace: Cyrene.Provider.Anthropic                               │
// │  Role: Standalone Native AOT Provider entrypoint (T87, T88).        │
// └─────────────────────────────────────────────────────────────────────┘
// 中文：文件：Program.cs
// 中文：命名空间：Cyrene.Provider.Anthropic
// 中文：职责：独立的 Native AOT Provider 入口点（T87、T88）。

namespace Cyrene.Provider.Anthropic;

public static class Program
{
    public static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--readiness")
        {
            Console.WriteLine("{\"status\":\"NOT_SERVING\",\"provider\":\"anthropic\",\"reason\":\"DIRECT_RUNTIME_HOST_NOT_CONFIGURED\",\"aot\":true}");
            return 2;
        }

        Console.Error.WriteLine("DirectPluginRuntime host configuration is required.");
        return 2;
    }
}
