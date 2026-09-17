// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 Program.cs                                                           │
// │  Namespace: Cyrene.OneBot.V11.Host                                       │
// │  Role: AOT-safe process entrypoint for the OneBot DirectPluginRuntime.   │
// │                                                                         │
// │  模块职责：OneBot DirectPluginRuntime 的 Native AOT 进程入口                 │
// └─────────────────────────────────────────────────────────────────────────┘

using Cyrene.OneBot.V11.Core;
using Microsoft.AspNetCore.Server.Kestrel.Core;
using System.Text.Json;

namespace Cyrene.OneBot.V11.Host;

public static class Program
{
    private static readonly IReadOnlyList<string> Capabilities =
        new[] { "message.connector.v1", "qq.client.v1" };

    public static async Task<int> Main(string[] args)
    {
        try
        {
            string listenAddress = ResolveListenAddress(args);
            WebApplicationBuilder builder = WebApplication.CreateSlimBuilder(args);
            builder.WebHost.UseUrls(ToHttpUrl(listenAddress));
            builder.WebHost.ConfigureKestrel(options =>
            {
                // DirectPluginRuntime uses h2c on the loopback endpoint.  The
                // endpoint must therefore be HTTP/2-only, not HTTP/1.1 fallback.
                options.ConfigureEndpointDefaults(endpoint =>
                    endpoint.Protocols = HttpProtocols.Http2);
            });
            builder.Services.AddGrpc();
            builder.Services.AddSingleton(CreateMetadata());

            QqDirectProfile? qqProfile = QqDirectProfileLoader.FromEnvironment();
            OneBotProfile? profile = qqProfile is null
                ? OneBotProfileLoader.FromEnvironment()
                : null;
            if (qqProfile is not null)
            {
                builder.Services.AddSingleton(qqProfile);
                builder.Services.AddSingleton<QqHostClient>(serviceProvider =>
                    new QqHostClient(qqProfile.HostLaunch));
                builder.Services.AddSingleton<IDirectInvocationDispatcher>(serviceProvider =>
                    new QqDirectInvocationDispatcher(
                        qqProfile,
                        serviceProvider.GetRequiredService<QqHostClient>()));
                builder.Services.AddSingleton(RuntimeReadiness.Serving());
            }
            else if (profile is null)
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

            await app.StartAsync();
            if (profile?.TransportProfile == OneBotTransportProfile.ReverseWebSocket)
            {
                // Start the reverse listener only after Kestrel has completed
                // application startup.  This keeps the readiness announcement
                // deterministic on all Native AOT architectures and prevents
                // the listener's accept loop from running during host startup.
                app.Services.GetRequiredService<OneBotReverseWebSocketServer>();
            }
            WriteReadyAnnouncement(app);
            await app.WaitForShutdownAsync();
            await app.StopAsync();
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"OneBot Native AOT host failed to start: {exception.Message}");
            return 2;
        }
    }

    private static RuntimeMetadata CreateMetadata() => new(
        Environment.GetEnvironmentVariable("CYRENE_PLUGIN_ID")
            ?? "cyrene.connectors.onebot-v11",
        Environment.GetEnvironmentVariable("CYRENE_PLUGIN_VERSION")
            ?? "0.3.0",
        Capabilities);

    private static string ResolveListenAddress(string[] args)
    {
        for (int index = 0; index < args.Length - 1; index++)
        {
            if (string.Equals(args[index], "--listen", StringComparison.Ordinal))
            {
                return ValidateListenAddress(args[index + 1]);
            }
        }

        return ValidateListenAddress(
            Environment.GetEnvironmentVariable("CYRENE_PLUGIN_LISTEN")
                ?? "127.0.0.1:0");
    }

    private static string ValidateListenAddress(string value)
    {
        string candidate = value.Trim();
        if (candidate.Length == 0 || candidate.Contains('/'))
        {
            throw new ArgumentException("--listen must be a host:port address.");
        }

        int separator = candidate.LastIndexOf(':');
        if (separator <= 0
            || separator == candidate.Length - 1
            || !int.TryParse(candidate[(separator + 1)..], out int port)
            || port is < 0 or > 65535)
        {
            throw new ArgumentException("--listen must use a valid host:port address.");
        }

        return candidate;
    }

    private static string ToHttpUrl(string listenAddress) => $"http://{listenAddress}";

    private static void WriteReadyAnnouncement(WebApplication app)
    {
        Uri address = app.Urls
            .Select(url => new Uri(url))
            .First(uri => uri.Scheme == Uri.UriSchemeHttp);
        string authority = address.Host.Contains(':')
            ? $"[{address.Host}]:{address.Port}"
            : $"{address.Host}:{address.Port}";
        RuntimeMetadata metadata = app.Services.GetRequiredService<RuntimeMetadata>();
        RuntimeLaunchAnnouncement announcement = new()
        {
            Event = "direct_plugin_ready",
            ConnectionRef = $"grpc://{authority}",
            Capability = metadata.Capabilities,
            Capabilities = metadata.Capabilities,
            InterfaceVersion = "1",
            InterfaceVersions = new[] { "1" },
            RuntimeId = Guid.NewGuid().ToString("D")
        };
        Console.WriteLine(JsonSerializer.Serialize(
            announcement,
            RuntimeLaunchJsonContext.Default.RuntimeLaunchAnnouncement));
        Console.Out.Flush();
    }

}
