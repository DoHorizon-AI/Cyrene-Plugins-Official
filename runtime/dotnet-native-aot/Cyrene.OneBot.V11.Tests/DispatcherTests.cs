// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 DispatcherTests.cs                                                   │
// │  Namespace: Cyrene.OneBot.V11.Tests                                      │
// │  Role: Contract tests for explicit AOT-safe invocation validation.       │
// │                                                                         │
// │  模块职责：显式 AOT 分派器调用校验的契约测试                                │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using Cyrene.OneBot.V11.Core;
using Cyrene.Plugin.Runtime.V1;
using Google.Protobuf;
using Xunit;

namespace Cyrene.OneBot.V11.Tests;

public sealed class DispatcherTests
{
    private readonly NotConfiguredInvocationDispatcher _dispatcher = new();

    [Fact]
    public async Task RejectsMissingRequiredFields()
    {
        InvocationResult result = await _dispatcher.InvokeAsync(
            new DirectInvocationRequest { Method = "send_message" },
            CancellationToken.None);

        Assert.Equal(DirectInvocationError.Types.Code.InvalidRequest, result.Error?.Code);
        Assert.Equal("REQUIRED_FIELD_MISSING", result.Error?.DomainCode);
    }

    [Fact]
    public async Task RejectsUnknownCapability()
    {
        InvocationResult result = await _dispatcher.InvokeAsync(
            CreateRequest("unknown.capability.v1"),
            CancellationToken.None);

        Assert.Equal(DirectInvocationError.Types.Code.MethodNotFound, result.Error?.Code);
        Assert.Equal("CAPABILITY_NOT_REGISTERED", result.Error?.DomainCode);
    }

    [Fact]
    public async Task RejectsPayloadAboveBound()
    {
        var request = CreateRequest("message.connector.v1");
        request.Payload = ByteString.CopyFrom(new byte[NotConfiguredInvocationDispatcher.MaxPayloadBytes + 1]);

        InvocationResult result = await _dispatcher.InvokeAsync(request, CancellationToken.None);

        Assert.Equal(DirectInvocationError.Types.Code.InvalidRequest, result.Error?.Code);
        Assert.Equal("PAYLOAD_TOO_LARGE", result.Error?.DomainCode);
    }

    [Fact]
    public async Task UnconfiguredMethodFailsClosed()
    {
        InvocationResult result = await _dispatcher.InvokeAsync(
            CreateRequest("message.connector.v1"),
            CancellationToken.None);

        Assert.Equal(DirectInvocationError.Types.Code.MethodNotFound, result.Error?.Code);
        Assert.Equal("CONNECTOR_PROFILE_NOT_CONFIGURED", result.Error?.DomainCode);
    }

    [Fact]
    public async Task QqHostProtocolUsesBoundedBigEndianFrames()
    {
        JsonElement parameters = JsonSerializer.SerializeToElement(
            new Dictionary<string, string> { ["account_id"] = "10001" },
            QqHostJsonContext.Default.DictionaryStringString);
        QqHostRequest request = new()
        {
            RequestId = "qq-main:1:1",
            BindingId = "qq-main",
            Generation = 1,
            Operation = "qq.login.list",
            Params = parameters
        };

        byte[] frame = QqHostProtocol.Encode(
            request,
            QqHostJsonContext.Default.QqHostRequest);
        Assert.Equal(
            frame.Length - QqHostProtocol.FrameHeaderBytes,
            (frame[0] << 24) | (frame[1] << 16) | (frame[2] << 8) | frame[3]);

        await using MemoryStream stream = new(frame);
        byte[] payload = (await QqHostProtocol.ReadFrameAsync(
            stream,
            CancellationToken.None))!;
        QqHostRequest decoded = JsonSerializer.Deserialize(
            payload,
            QqHostJsonContext.Default.QqHostRequest)!;
        Assert.Equal("qq-main:1:1", decoded.RequestId);
        Assert.Equal("qq.login.list", decoded.Operation);
        Assert.Equal("10001", decoded.Params.GetProperty("account_id").GetString());
    }

    [Fact]
    public async Task QqHostProtocolRejectsTruncatedFrames()
    {
        await using MemoryStream stream = new(new byte[]
        {
            0, 0, 0, 3, (byte)'{', (byte)'}'
        });

        await Assert.ThrowsAsync<QqHostProtocolException>(async () =>
            await QqHostProtocol.ReadFrameAsync(stream, CancellationToken.None));
    }

    [Fact]
    public void QqDirectProfileRejectsOneBotTransportFieldsAndKeepsAllowListFixed()
    {
        QqDirectConfigurationException exception = Assert.Throws<QqDirectConfigurationException>(() =>
            QqDirectProfileLoader.FromJson(
                "{\"runtime_profile\":\"qqnt-direct\",\"binding_id\":\"qq-main\","
                + "\"host_executable\":\"/opt/qq-host\",\"data_dir\":\"/var/lib/qq\","
                + "\"required_client_version\":\"9.9.9\",\"required_host_abi\":\"abi\","
                + "\"http_base_url\":\"http://127.0.0.1:18080\"}"));

        Assert.Equal("UNKNOWN_CONFIGURATION_FIELD", exception.DomainCode);
        Assert.Contains(QqHostOperationRegistry.All, operation => operation.Name == "qq.message.send");
        Assert.Contains(QqHostOperationRegistry.All, operation => operation.Name == "qq.group.approve");
        Assert.Contains(QqHostOperationRegistry.All, operation => operation.Name == "qq.online.check_like");
    }

    [Fact]
    public async Task QqHostClientNegotiatesWithTheIndependentFixture()
    {
        if (!OperatingSystem.IsLinux() || !File.Exists("/usr/bin/python3"))
        {
            return;
        }

        string fixture = Path.GetFullPath(
            "plugins/connectors/onebot-v11/tests/fixtures/fake_qq_host.py");
        if (!File.Exists(fixture))
        {
            return;
        }

        string dataDirectory = Path.Combine(
            Path.GetTempPath(),
            $"cyrene-qq-host-test-{Guid.NewGuid():N}");
        await using QqHostClient client = new(
            new QqHostLaunchConfiguration(
                "qq-test",
                "/usr/bin/python3",
                new[] { fixture },
                dataDirectory,
                "fixture-client",
                "fake-qqnt-linux-x86_64"));

        await client.StartAsync(CancellationToken.None);
        Assert.Equal("NATIVE_READY", client.State);
        Assert.Equal(1, client.Generation);
        Assert.Equal("fake-qqnt-linux-x86_64", client.Compatibility?.HostAbi);

        JsonElement parameters = JsonSerializer.SerializeToElement(
            new Dictionary<string, string> { ["account_id"] = "10001" },
            QqHostJsonContext.Default.DictionaryStringString);
        JsonElement result = await client.RequestAsync(
            "qq.login.list",
            parameters,
            CancellationToken.None);
        Assert.Equal("ready", result.GetProperty("state").GetString());
        await client.CloseAsync(CancellationToken.None);
        Assert.Equal("STOPPED", client.State);
    }

    [Fact]
    public async Task QqDispatcherBootstrapsTheSessionBeforeAReadyOperation()
    {
        if (!OperatingSystem.IsLinux() || !File.Exists("/usr/bin/python3"))
        {
            return;
        }

        string fixture = Path.GetFullPath(
            "plugins/connectors/onebot-v11/tests/fixtures/fake_qq_host.py");
        if (!File.Exists(fixture))
        {
            return;
        }

        string dataDirectory = Path.Combine(
            Path.GetTempPath(),
            $"cyrene-qq-dispatch-test-{Guid.NewGuid():N}");
        string operationLog = Path.Combine(dataDirectory, "operations.log");
        QqDirectProfile profile = QqDirectProfileLoader.FromJson(
            $$"""
            {
                "runtime_profile":"qqnt-direct",
                "binding_id":"qq-dispatch",
                "host_executable":"/usr/bin/python3",
                "host_args":["{{fixture}}","--operation-log={{operationLog}}"],
                "data_dir":"{{dataDirectory}}",
                "required_client_version":"fixture-client",
                "required_host_abi":"fake-qqnt-linux-x86_64",
                "account_id":"10001"
            }
            """
        );
        await using QqHostClient host = new(profile.HostLaunch);
        QqDirectInvocationDispatcher dispatcher = new(profile, host);

        InvocationResult result = await dispatcher.InvokeAsync(
            new DirectInvocationRequest
            {
                Capability = QqDirectInvocationDispatcher.CapabilityId,
                InterfaceVersion = QqDirectInvocationDispatcher.InterfaceVersion,
                Method = "qq.group.list",
                PayloadTypeUrl = QqDirectInvocationDispatcher.RequestTypeUrl,
                Payload = ByteString.CopyFromUtf8("{\"params\":{\"account_id\":\"10001\"}}")
            },
            CancellationToken.None);

        Assert.Null(result.Error);
        using JsonDocument response = JsonDocument.Parse(result.Payload!.ToByteArray());
        Assert.Equal("qq.group.list", response.RootElement.GetProperty("operation").GetString());
        Assert.Equal("NodeIKernelGroupService", response.RootElement.GetProperty("mapping").GetProperty("service").GetString());
        string[] operations = File.ReadAllLines(operationLog);
        Assert.Equal(
            new List<string>
            {
                "qq.session.create",
                "qq.session.init",
                "qq.session.start_nt",
                "qq.group.list"
            },
            operations);
        await host.CloseAsync(CancellationToken.None);
    }

    [Fact]
    public async Task StreamReturnsErrorAndTerminalEnd()
    {
        var items = new List<DirectStreamItem>();
        await foreach (DirectStreamItem item in _dispatcher.InvokeStreamAsync(
                           CreateRequest("message.connector.v1"),
                           CancellationToken.None))
        {
            items.Add(item);
        }

        Assert.Single(items);
        Assert.Equal(DirectInvocationError.Types.Code.MethodNotFound, items[0].Error?.Code);
    }

    private static DirectInvocationRequest CreateRequest(string capability) => new()
    {
        Capability = capability,
        InterfaceVersion = "1",
        Method = "send_message",
        PayloadTypeUrl = "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest",
        Payload = ByteString.Empty
    };
}
