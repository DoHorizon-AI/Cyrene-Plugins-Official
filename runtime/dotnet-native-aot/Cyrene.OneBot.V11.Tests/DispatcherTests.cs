// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 DispatcherTests.cs                                                   │
// │  Namespace: Cyrene.OneBot.V11.Tests                                      │
// │  Role: Contract tests for explicit AOT-safe invocation validation.       │
// │                                                                         │
// │  模块职责：显式 AOT 分派器调用校验的契约测试                                │
// └─────────────────────────────────────────────────────────────────────────┘

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
