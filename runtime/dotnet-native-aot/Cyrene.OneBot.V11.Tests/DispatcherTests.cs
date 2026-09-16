// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 DispatcherTests.cs                                                   │
// │  Namespace: Cyrene.OneBot.V11.Tests                                      │
// │  Role: Contract tests for explicit AOT-safe invocation validation.       │
// │                                                                         │
// │  模块职责：显式 AOT 分派器调用校验的契约测试                                │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using Cyrene.Message.Connector.V1;
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

        string fixture = RepositoryPath(
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
    public async Task QqHostClientDeliversCurrentGenerationEvents()
    {
        if (!OperatingSystem.IsLinux() || !File.Exists("/usr/bin/python3"))
        {
            return;
        }

        string fixture = RepositoryPath(
            "plugins/connectors/onebot-v11/tests/fixtures/fake_qq_host.py");
        if (!File.Exists(fixture))
        {
            return;
        }

        string dataDirectory = Path.Combine(
            Path.GetTempPath(),
            $"cyrene-qq-host-event-test-{Guid.NewGuid():N}");
        TaskCompletionSource<QqHostEvent> received = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        QqDirectProfile profile = CreateQqProfile(
            fixture,
            dataDirectory,
            "--mode=semantic_mapping");
        await using QqHostClient client = new(
            profile.HostLaunch,
            @event =>
            {
                if (@event.Payload.TryGetProperty("message_id", out JsonElement messageId)
                    && messageId.GetString() == "native-private-message-1")
                {
                    received.TrySetResult(@event);
                }
            });

        await client.StartAsync(CancellationToken.None);
        JsonElement parameters = JsonSerializer.SerializeToElement(
            new QqSubscribeParameters
            {
                Events = new List<string> { "message.received" }
            },
            QqHostJsonContext.Default.QqSubscribeParameters);
        await client.RequestAsync("qq.message.subscribe", parameters, CancellationToken.None);

        QqHostEvent @event = await received.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal("message.received", @event.Event);
        Assert.Equal(client.Generation, @event.Generation);
        Assert.Equal("native-private-message-1", @event.Payload.GetProperty("message_id").GetString());
        Assert.Equal(
            "20003",
            @event.Payload.GetProperty("peer").GetProperty("user_uid").GetString());
        Assert.Equal("private", @event.Payload.GetProperty("peer").GetProperty("kind").GetString());
        QqDirectNormalizedEvent normalized = QqDirectMessageMapper.NormalizeMessage(
            @event.Payload,
            profile,
            @event.Generation,
            profile.BindingId);
        Assert.Equal(QqDirectMessageMapper.InboundMessageEventType, normalized.EventType);
    }

    [Fact]
    public async Task QqDispatcherBootstrapsTheSessionBeforeAReadyOperation()
    {
        if (!OperatingSystem.IsLinux() || !File.Exists("/usr/bin/python3"))
        {
            return;
        }

        string fixture = RepositoryPath(
            "plugins/connectors/onebot-v11/tests/fixtures/fake_qq_host.py");
        if (!File.Exists(fixture))
        {
            return;
        }

        string dataDirectory = Path.Combine(
            Path.GetTempPath(),
            $"cyrene-qq-dispatch-test-{Guid.NewGuid():N}");
        string operationLog = Path.Combine(dataDirectory, "operations.log");
        QqDirectProfile profile = CreateQqProfile(
            fixture,
            dataDirectory,
            $"--operation-log={operationLog}");
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
        string payloadText = result.Payload!.Value.ToStringUtf8();
        Assert.True(payloadText.StartsWith('{'), payloadText);
        using JsonDocument response = JsonDocument.Parse(payloadText);
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

    [Theory]
    [InlineData("{\"account_id\":true}")]
    [InlineData("{\"account_id\":[]}")]
    [InlineData("{\"account_id\":\"10001\",\"count\":-1}")]
    [InlineData("{\"account_id\":\"10001\",\"binding_id\":\"other\"}")]
    public async Task QqDispatcherRejectsInvalidParametersBeforeHostStart(string parameters)
    {
        string fixture = RepositoryPath(
            "plugins/connectors/onebot-v11/tests/fixtures/fake_qq_host.py");
        string dataDirectory = Path.Combine(
            Path.GetTempPath(),
            $"cyrene-qq-invalid-params-test-{Guid.NewGuid():N}");
        QqDirectProfile profile = CreateQqProfile(fixture, dataDirectory);
        await using QqHostClient host = new(profile.HostLaunch);
        QqDirectInvocationDispatcher dispatcher = new(profile, host);

        InvocationResult result = await dispatcher.InvokeAsync(
            new DirectInvocationRequest
            {
                Capability = QqDirectInvocationDispatcher.CapabilityId,
                InterfaceVersion = QqDirectInvocationDispatcher.InterfaceVersion,
                Method = "qq.group.list",
                PayloadTypeUrl = QqDirectInvocationDispatcher.RequestTypeUrl,
                Payload = ByteString.CopyFromUtf8($"{{\"params\":{parameters}}}")
            },
            CancellationToken.None);

        Assert.Equal(DirectInvocationError.Types.Code.InvalidRequest, result.Error?.Code);
        Assert.Equal("INVALID_REQUEST", result.Error?.DomainCode);
        Assert.Equal(0, host.Generation);
    }

    [Theory]
    [InlineData("sensitive_result", "qq.group.list", "{\"account_id\":\"10001\"}", "sensitive field")]
    [InlineData("invalid_media_reference", "qq.media.download", "{\"account_id\":\"10001\",\"media_id\":\"media-1\"}", "must be http(s)")]
    [InlineData("mismatched_result", "qq.group.list", "{\"account_id\":\"10001\"}", "does not match qq.group.list")]
    public async Task QqDispatcherRejectsUnsafeHostResults(
        string mode,
        string operation,
        string parameters,
        string expectedMessage)
    {
        string fixture = RepositoryPath(
            "plugins/connectors/onebot-v11/tests/fixtures/fake_qq_host.py");
        string dataDirectory = Path.Combine(
            Path.GetTempPath(),
            $"cyrene-qq-invalid-result-test-{Guid.NewGuid():N}");
        QqDirectProfile profile = CreateQqProfile(fixture, dataDirectory, $"--mode={mode}");
        await using QqHostClient host = new(profile.HostLaunch);
        QqDirectInvocationDispatcher dispatcher = new(profile, host);

        InvocationResult result = await dispatcher.InvokeAsync(
            new DirectInvocationRequest
            {
                Capability = QqDirectInvocationDispatcher.CapabilityId,
                InterfaceVersion = QqDirectInvocationDispatcher.InterfaceVersion,
                Method = operation,
                PayloadTypeUrl = QqDirectInvocationDispatcher.RequestTypeUrl,
                Payload = ByteString.CopyFromUtf8($"{{\"params\":{parameters}}}")
            },
            CancellationToken.None);

        Assert.Equal(DirectInvocationError.Types.Code.ExecutionFailed, result.Error?.Code);
        Assert.Equal("PROTOCOL_MISMATCH", result.Error?.DomainCode);
        Assert.Contains(expectedMessage, result.Error?.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task QqDispatcherMapsCanonicalSendMessageAndDelivery()
    {
        if (!OperatingSystem.IsLinux() || !File.Exists("/usr/bin/python3"))
        {
            return;
        }

        string fixture = RepositoryPath(
            "plugins/connectors/onebot-v11/tests/fixtures/fake_qq_host.py");
        if (!File.Exists(fixture))
        {
            return;
        }

        string dataDirectory = Path.Combine(
            Path.GetTempPath(),
            $"cyrene-qq-send-test-{Guid.NewGuid():N}");
        QqDirectProfile profile = CreateQqProfile(fixture, dataDirectory);
        await using QqHostClient host = new(profile.HostLaunch);
        QqDirectInvocationDispatcher dispatcher = new(profile, host);
        SendMessageRequest message = new()
        {
            Conversation = new ConversationScope
            {
                Vendor = QqDirectMessageMapper.Vendor,
                AccountId = "10001",
                ConversationId = "20001",
                Kind = ConversationKind.Group
            },
            Reply = new ReplyReference { MessageId = "reply-1" },
            VendorExtension = new VendorExtension { Vendor = QqDirectMessageMapper.Vendor }
        };
        message.VendorExtension.Facts.Add(new VendorFact
        {
            Name = "qq_peer_uid",
            Value = "group-peer-1"
        });
        message.Content.Add(new MessageContentPart
        {
            Text = new TextContent { Text = "hello" }
        });
        message.Content.Add(new MessageContentPart
        {
            Mention = new MentionContent { Target = MentionTarget.Everyone }
        });
        message.Content.Add(new MessageContentPart
        {
            Image = new ImageContent
            {
                Reference = new AttachmentReference
                {
                    RemoteUri = "https://cdn.example/image.png"
                }
            }
        });

        InvocationResult result = await dispatcher.InvokeAsync(
            new DirectInvocationRequest
            {
                Capability = QqDirectMessageMapper.CapabilityId,
                InterfaceVersion = QqDirectMessageMapper.InterfaceVersion,
                Method = QqDirectMessageMapper.SendMessageMethod,
                PayloadTypeUrl = QqDirectMessageMapper.SendMessageRequestTypeUrl,
                Payload = ByteString.CopyFrom(message.ToByteArray())
            },
            CancellationToken.None);

        Assert.Null(result.Error);
        DeliveryResult delivery = DeliveryResult.Parser.ParseFrom(result.Payload!.Value);
        Assert.Equal(DeliveryStatus.Accepted, delivery.Status);
        Assert.Equal("qq-test-1-message-1", delivery.VendorMessageId);
        Assert.Equal(QqDirectMessageMapper.Vendor, delivery.VendorExtension.Vendor);
    }

    [Fact]
    public async Task QqDispatcherFiltersNativeInboundMessageEvents()
    {
        if (!OperatingSystem.IsLinux() || !File.Exists("/usr/bin/python3"))
        {
            return;
        }

        string fixture = RepositoryPath(
            "plugins/connectors/onebot-v11/tests/fixtures/fake_qq_host.py");
        if (!File.Exists(fixture))
        {
            return;
        }

        string dataDirectory = Path.Combine(
            Path.GetTempPath(),
            $"cyrene-qq-event-test-{Guid.NewGuid():N}");
        QqDirectProfile profile = CreateQqProfile(
            fixture,
            dataDirectory,
            "--mode=semantic_mapping");
        await using QqHostClient host = new(profile.HostLaunch);
        QqDirectInvocationDispatcher dispatcher = new(profile, host);
        DirectInvocationRequest request = CreateSubscriptionRequest(
            "qq-events",
            "{\"event_type\":\"inbound_message\",\"kind\":\"private\"}");

        await using IAsyncEnumerator<DirectStreamItem> events = dispatcher
            .InvokeStreamAsync(request, CancellationToken.None)
            .GetAsyncEnumerator();
        Assert.True(
            await events.MoveNextAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(5)));
        DirectStreamItem item = events.Current;
        Assert.NotNull(item.Payload);
        Assert.Equal(QqDirectMessageMapper.InboundMessageEventType, item.Payload.EventType);
        InboundMessagePayload message = InboundMessagePayload.Parser.ParseFrom(item.Payload.Value);
        Assert.Equal(ConversationKind.Private, message.Conversation.Kind);
        Assert.Equal("native-private-message-1", message.MessageId);
        Assert.Equal("20003", message.Conversation.ConversationId);
    }

    [Fact]
    public async Task QqDispatcherCorrelatesSendCompletionCallbackToTheOriginatingSend()
    {
        if (!OperatingSystem.IsLinux() || !File.Exists("/usr/bin/python3"))
        {
            return;
        }

        string fixture = RepositoryPath(
            "plugins/connectors/onebot-v11/tests/fixtures/fake_qq_host.py");
        if (!File.Exists(fixture))
        {
            return;
        }

        string dataDirectory = Path.Combine(
            Path.GetTempPath(),
            $"cyrene-qq-callback-test-{Guid.NewGuid():N}");
        QqDirectProfile profile = CreateQqProfile(fixture, dataDirectory, "--mode=callbacks");
        await using QqHostClient host = new(profile.HostLaunch);
        QqDirectInvocationDispatcher dispatcher = new(profile, host);
        await using IAsyncEnumerator<DirectStreamItem> events = dispatcher
            .InvokeStreamAsync(
                CreateSubscriptionRequest("qq-callbacks", "{}"),
                CancellationToken.None)
            .GetAsyncEnumerator();

        Assert.True(
            await events.MoveNextAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(5)));
        Assert.Equal(QqDirectMessageMapper.InboundMessageEventType, events.Current.Payload.EventType);

        SendMessageRequest message = new()
        {
            Conversation = new ConversationScope
            {
                Vendor = QqDirectMessageMapper.Vendor,
                AccountId = "10001",
                ConversationId = "20001",
                Kind = ConversationKind.Group
            }
        };
        message.Content.Add(new MessageContentPart
        {
            Text = new TextContent { Text = "callback" }
        });
        InvocationResult send = await dispatcher.InvokeAsync(
            new DirectInvocationRequest
            {
                Capability = QqDirectMessageMapper.CapabilityId,
                InterfaceVersion = QqDirectMessageMapper.InterfaceVersion,
                Method = QqDirectMessageMapper.SendMessageMethod,
                PayloadTypeUrl = QqDirectMessageMapper.SendMessageRequestTypeUrl,
                Payload = ByteString.CopyFrom(message.ToByteArray())
            },
            CancellationToken.None);
        Assert.Null(send.Error);

        Assert.True(await events.MoveNextAsync());
        DirectStreamItem callback = events.Current;
        Assert.Equal(QqDirectMessageMapper.CallbackEventType, callback.Payload.EventType);
        QqCallbackPayload payload = JsonSerializer.Deserialize(
            callback.Payload.Value.ToByteArray(),
            QqHostJsonContext.Default.QqCallbackPayload)!;
        Assert.Equal("qq.message.send_completion", payload.Operation);
        Assert.Equal("completed", payload.Status?.GetString());
    }

    private static QqDirectProfile CreateQqProfile(
        string fixture,
        string dataDirectory,
        params string[] extraArguments)
    {
        string[] hostArguments = new[] { fixture }.Concat(extraArguments).ToArray();
        string json = "{\"runtime_profile\":\"qqnt-direct\","
            + "\"binding_id\":\"qq-test\","
            + "\"host_executable\":\"/usr/bin/python3\","
            + $"\"host_args\":[{string.Join(',', hostArguments.Select(JsonString))}],"
            + $"\"data_dir\":{JsonString(dataDirectory)},"
            + "\"required_client_version\":\"fixture-client\","
            + "\"required_host_abi\":\"fake-qqnt-linux-x86_64\","
            + "\"account_id\":\"10001\"}";
        return QqDirectProfileLoader.FromJson(json);
    }

    private static string JsonString(string value) =>
        $"\"{value.Replace("\\", "\\\\").Replace("\"", "\\\"")}\"";

    private static string RepositoryPath(params string[] parts)
    {
        DirectoryInfo? directory = new(AppContext.BaseDirectory);
        while (directory is not null)
        {
            if (File.Exists(Path.Combine(directory.FullName, "source-manifest.json")))
            {
                return Path.Combine(new[] { directory.FullName }.Concat(parts).ToArray());
            }

            directory = directory.Parent;
        }

        throw new InvalidOperationException("repository root was not found");
    }

    private static DirectInvocationRequest CreateSubscriptionRequest(
        string requestId,
        string filter) => new()
        {
            Capability = QqDirectMessageMapper.CapabilityId,
            InterfaceVersion = QqDirectMessageMapper.InterfaceVersion,
            Method = QqDirectMessageMapper.EventsMethod,
            PayloadTypeUrl = QqDirectMessageMapper.FilterTypeUrl,
            RequestId = requestId,
            StreamMode = DirectStreamMode.Subscription,
            Payload = ByteString.CopyFromUtf8(filter)
        };

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
