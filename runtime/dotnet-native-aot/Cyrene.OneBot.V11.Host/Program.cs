// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 Program.cs                                                           │
// │  Namespace: Cyrene.OneBot.V11.Host                                       │
// │  Role: AOT-safe process entrypoint for the OneBot DirectPluginRuntime.   │
// │                                                                         │
// │  模块职责：OneBot DirectPluginRuntime 的 Native AOT 进程入口                 │
// └─────────────────────────────────────────────────────────────────────────┘

using Cyrene.OneBot.V11.Core;

namespace Cyrene.OneBot.V11.Host;

public static class Program
{
    private static readonly IReadOnlyList<string> Capabilities =
        new[] { "message.connector.v1", "qq.client.v1" };

    public static Task Main(string[] args)
    {
        WebApplicationBuilder builder = WebApplication.CreateSlimBuilder(args);
        builder.Services.AddGrpc();
        builder.Services.AddSingleton<IDirectInvocationDispatcher, NotConfiguredInvocationDispatcher>();
        builder.Services.AddSingleton(CreateMetadata());
        builder.Services.AddSingleton(CreateReadiness());

        WebApplication app = builder.Build();
        app.MapGrpcService<DirectPluginRuntimeService>();
        return app.RunAsync();
    }

    private static RuntimeMetadata CreateMetadata() => new(
        Environment.GetEnvironmentVariable("CYRENE_PLUGIN_ID")
            ?? "cyrene.connectors.onebot-v11",
        Environment.GetEnvironmentVariable("CYRENE_PLUGIN_VERSION")
            ?? "0.3.0",
        Capabilities);

    private static RuntimeReadiness CreateReadiness() =>
        RuntimeReadiness.NotServing("CONNECTOR_PROFILE_NOT_CONFIGURED");
}
