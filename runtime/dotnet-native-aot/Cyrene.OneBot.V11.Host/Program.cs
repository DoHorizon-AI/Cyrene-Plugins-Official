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
        builder.Services.AddSingleton(CreateMetadata());

        OneBotProfile? profile = OneBotProfileLoader.FromEnvironment();
        if (profile is null)
        {
            builder.Services.AddSingleton<IDirectInvocationDispatcher,
                NotConfiguredInvocationDispatcher>();
            builder.Services.AddSingleton(RuntimeReadiness.NotServing(
                "CONNECTOR_PROFILE_NOT_CONFIGURED"));
        }
        else
        {
            builder.Services.AddSingleton(profile);
            builder.Services.AddSingleton<OneBotEventSubscriptionRegistry>();
            builder.Services.AddSingleton<HttpClient>(_ => new HttpClient
            {
                Timeout = Timeout.InfiniteTimeSpan
            });
            builder.Services.AddSingleton<IOneBotActionTransport>(serviceProvider =>
            {
                IOneBotActionTransport transport = OneBotTransportFactory.Create(
                    profile,
                    serviceProvider.GetRequiredService<HttpClient>());
                transport.SetEventHandler(
                    serviceProvider
                        .GetRequiredService<OneBotEventSubscriptionRegistry>()
                        .Publish);
                return transport;
            });
            if (profile.TransportProfile == OneBotTransportProfile.ReverseWebSocket)
            {
                builder.Services.AddSingleton(serviceProvider =>
                {
                    OneBotWebSocketTransport transport =
                        (OneBotWebSocketTransport)serviceProvider
                            .GetRequiredService<IOneBotActionTransport>();
                    OneBotReverseWebSocketServer server = new(profile, transport);
                    server.Start();
                    return server;
                });
            }
            builder.Services.AddSingleton<IDirectInvocationDispatcher,
                OneBotInvocationDispatcher>();
            builder.Services.AddSingleton(RuntimeReadiness.Serving());
        }

        WebApplication app = builder.Build();
        app.MapGrpcService<DirectPluginRuntimeService>();
        if (profile?.TransportProfile == OneBotTransportProfile.ReverseWebSocket)
        {
            app.Services.GetRequiredService<OneBotReverseWebSocketServer>();
        }

        return app.RunAsync();
    }

    private static RuntimeMetadata CreateMetadata() => new(
        Environment.GetEnvironmentVariable("CYRENE_PLUGIN_ID")
            ?? "cyrene.connectors.onebot-v11",
        Environment.GetEnvironmentVariable("CYRENE_PLUGIN_VERSION")
            ?? "0.3.0",
        Capabilities);

}
