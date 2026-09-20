// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotParityTests.cs                                                  │
// │  Namespace: Cyrene.OneBot.V11.Tests                                      │
// │  Role: Generic OneBot behavior parity and fail-closed boundary tests.    │
// │                                                                         │
// │  模块职责：通用 OneBot 行为等价性与 fail-closed 边界测试                     │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Cyrene.Message.Connector.V1;
using Cyrene.Plugin.RuntimeHost;
using Cyrene.OneBot.V11.Core;
using Cyrene.Plugin.Runtime.V1;
using Google.Protobuf;
using Xunit;

namespace Cyrene.OneBot.V11.Tests;

/// <summary>
/// Covers the generic OneBot behavior classes that previously existed only in
/// the Python reference suite.
/// <para>覆盖此前只存在于 Python 参考测试中的通用 OneBot 行为类别。</para>
/// </summary>
public sealed class OneBotParityTests
{
    [Fact]
    public void DefaultsToForwardWebSocketWhenWebSocketUrlIsPresent()
    {
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            "{\"binding_id\":\"qq-main\",\"websocket_url\":\"ws://127.0.0.1:18080\"}");

        Assert.Equal(OneBotTransportProfile.ForwardWebSocket, profile.TransportProfile);
        Assert.Equal("ws://127.0.0.1:18080", profile.WebSocketUrl);
    }

    [Theory]
    [InlineData("{\"binding_id\":\"main\"}", "REQUIRED_CONFIGURATION_FIELD")]
    [InlineData("{\"binding_id\":\"main\",\"http_base_url\":\"not-a-url\"}", "INVALID_URL")]
    [InlineData("{\"binding_id\":\"main\",\"http_base_url\":\"http://127.0.0.1\",\"reverse_listen_port\":65536}", "INVALID_REVERSE_LISTENER")]
    [InlineData("{\"binding_id\":\"main\",\"access_token\":\"a\\nb\",\"http_base_url\":\"http://127.0.0.1\"}", "INVALID_ACCESS_TOKEN")]
    public void RejectsInvalidGenericProfiles(string json, string domainCode)
    {
        OneBotConfigurationException exception = Assert.Throws<OneBotConfigurationException>(
            () => OneBotProfileLoader.FromJson(json));

        Assert.Equal(domainCode, exception.DomainCode);
    }

    [Fact]
    public void RejectsUnknownConfigurationFields()
    {
        OneBotConfigurationException unknown = Assert.Throws<OneBotConfigurationException>(
            () => OneBotProfileLoader.FromJson(
                "{\"binding_id\":\"main\",\"http_base_url\":\"http://127.0.0.1\",\"future\":true}"));
        Assert.Equal("UNKNOWN_CONFIGURATION_FIELD", unknown.DomainCode);

        OneBotConfigurationException hostField = Assert.Throws<OneBotConfigurationException>(
            () => OneBotProfileLoader.FromJson(
                "{\"binding_id\":\"main\",\"http_base_url\":\"http://127.0.0.1\",\"host_executable\":\"/opt/qq\"}"));
        Assert.Equal("UNKNOWN_CONFIGURATION_FIELD", hostField.DomainCode);
    }

    [Fact]
    public void MapsPrivateMessageAndPreservesTypedAttachmentSources()
    {
        OneBotProfile profile = HttpProfile();
        SendMessageRequest message = new()
        {
            Conversation = new ConversationScope
            {
                Vendor = OneBotMessageMapper.Vendor,
                AccountId = "10001",
                ConversationId = "10002",
                Kind = ConversationKind.Private
            }
        };
        message.Content.Add(new MessageContentPart
        {
            Mention = new MentionContent
            {
                Target = MentionTarget.Everyone
            }
        });
        message.Content.Add(new MessageContentPart
        {
            File = new FileContent
            {
                Reference = new AttachmentReference
                {
                    VendorMedia = new VendorMediaReference { MediaId = "media-1" }
                },
                FileName = "notes.txt"
            }
        });

        OneBotSendOperation operation = OneBotMessageMapper.MapSendMessage(message, profile);

        Assert.Equal("send_private_msg", operation.Action);
        Assert.Equal((ulong)10002, operation.Request.UserId);
        Assert.Equal("all", operation.Request.Message![0].Data.Qq);
        Assert.Equal("media-1", operation.Request.Message[1].Data.File);
        Assert.Equal("notes.txt", operation.Request.Message[1].Data.Name);
    }

    [Theory]
    [InlineData(ConversationKind.Unspecified)]
    [InlineData(ConversationKind.Channel)]
    public void RejectsUnsupportedConversationKinds(ConversationKind kind)
    {
        SendMessageRequest message = GroupMessage();
        message.Conversation.Kind = kind;

        OneBotMappingException exception = Assert.Throws<OneBotMappingException>(
            () => OneBotMessageMapper.MapSendMessage(message, HttpProfile()));

        Assert.Contains("conversation kind", exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void RejectsInvalidMessageContentAndAccountIdentity()
    {
        SendMessageRequest empty = GroupMessage();
        empty.Content.Clear();
        Assert.Throws<OneBotMappingException>(
            () => OneBotMessageMapper.MapSendMessage(empty, HttpProfile()));

        SendMessageRequest wrongAccount = GroupMessage();
        wrongAccount.Conversation.AccountId = "10002";
        OneBotMappingException accountException = Assert.Throws<OneBotMappingException>(
            () => OneBotMessageMapper.MapSendMessage(wrongAccount, HttpProfile()));
        Assert.Contains("account", accountException.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("{\"status\":\"ok\",\"retcode\":0,\"data\":[]}", "PROTOCOL_MISMATCH")]
    [InlineData("{\"status\":\"ok\",\"retcode\":1,\"data\":{}}", "ACTION_REJECTED")]
    [InlineData("not-json", "PROTOCOL_MISMATCH")]
    public async Task HttpTransportClassifiesMalformedAndRejectedResponses(
        string body,
        string domainCode)
    {
        StaticHttpMessageHandler handler = new(body, HttpStatusCode.OK);
        using HttpClient client = new(handler);
        using OneBotHttpTransport transport = new(HttpProfile(), client);

        OneBotTransportException exception = await Assert.ThrowsAsync<OneBotTransportException>(
            () => transport.CallAsync(
                "send_private_msg",
                new OneBotActionRequest { UserId = 10001 },
                CancellationToken.None));

        Assert.Equal(domainCode, exception.DomainCode);
    }

    [Fact]
    public async Task HttpTransportRejectsHttpFailureAndSendsExpectedHeaders()
    {
        StaticHttpMessageHandler handler = new("{}", HttpStatusCode.Unauthorized);
        using HttpClient client = new(handler);
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            "{\"binding_id\":\"main\",\"http_base_url\":\"http://127.0.0.1:18080\",\"access_token\":\"token\"}");
        using OneBotHttpTransport transport = new(profile, client);

        OneBotTransportException exception = await Assert.ThrowsAsync<OneBotTransportException>(
            () => transport.CallAsync(
                "send_private_msg",
                new OneBotActionRequest { UserId = 10001 },
                CancellationToken.None));

        Assert.Equal("HTTP_STATUS", exception.DomainCode);
        Assert.Equal("Bearer", handler.Authorization!.Scheme);
        Assert.Equal("token", handler.Authorization.Parameter);
        Assert.Equal("application/json", handler.ContentType);
    }

    [Fact]
    public async Task ForwardWebSocketClassifiesUnauthorizedHandshake()
    {
        using UnauthorizedWebSocketPeer peer = new();
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            $"{{\"binding_id\":\"main\",\"transport_profile\":\"forward_websocket\","
            + $"\"websocket_url\":\"ws://127.0.0.1:{peer.Port}/onebot\","
            + "\"access_token\":\"wrong\",\"timeout_seconds\":1}");
        using OneBotWebSocketTransport transport = new(profile);

        OneBotTransportException exception = await Assert.ThrowsAsync<OneBotTransportException>(
            () => transport.CallAsync(
                "send_private_msg",
                new OneBotActionRequest { UserId = 10001 },
                CancellationToken.None));

        Assert.True(peer.RequestReceived, "The unauthorized peer did not receive a WebSocket request.");
        Assert.Equal("AUTHENTICATION_FAILED", transport.LastErrorDomainCode);
        Assert.Equal("AUTHENTICATION_FAILED", exception.DomainCode);
    }

    [Fact]
    public void NormalizesPrivateEventsAndRejectsDuplicateReplyMetadata()
    {
        OneBotNormalizedEvent normalized = OneBotEventNormalizer.Normalize(
            ParseJson(
                "{\"post_type\":\"message\",\"message_type\":\"private\","
                + "\"self_id\":10001,\"user_id\":10002,\"message_id\":30001,"
                + "\"sender\":{\"user_id\":10002},"
                + "\"message\":[{\"type\":\"text\",\"data\":{\"text\":\"hi\"}}]}"),
            HttpProfile())!;
        InboundMessagePayload payload = InboundMessagePayload.Parser.ParseFrom(normalized.Payload);

        Assert.Equal(ConversationKind.Private, payload.Conversation.Kind);
        Assert.Equal("10002", payload.Conversation.ConversationId);
        Assert.Equal("10002", payload.SenderDisplayName);
        Assert.Equal("hi", payload.Content[0].Text.Text);

        Assert.Throws<OneBotEventNormalizationException>(() => OneBotEventNormalizer.Normalize(
            ParseJson(
                "{\"post_type\":\"message\",\"message_type\":\"group\","
                + "\"self_id\":10001,\"group_id\":20001,\"message_id\":30001,"
                + "\"sender\":{\"user_id\":10002},\"message\":["
                + "{\"type\":\"reply\",\"data\":{\"id\":1}},"
                + "{\"type\":\"reply\",\"data\":{\"id\":2}}]}"),
            HttpProfile()));
    }

    [Theory]
    [InlineData("{\"post_type\":\"notice\"}")]
    [InlineData("{\"post_type\":\"message\",\"message_type\":\"channel\"}")]
    [InlineData("{\"post_type\":\"message\",\"message_type\":\"group\",\"self_id\":1,\"group_id\":2,\"message_id\":3,\"sender\":{\"user_id\":4},\"message\":[]}")]
    public void RejectsUnsupportedOrEmptyInboundEvents(string json)
    {
        if (json.Contains("notice", StringComparison.Ordinal))
        {
            Assert.Null(OneBotEventNormalizer.Normalize(ParseJson(json), HttpProfile()));
            return;
        }

        Assert.Throws<OneBotEventNormalizationException>(
            () => OneBotEventNormalizer.Normalize(ParseJson(json), HttpProfile()));
    }

    [Fact]
    public async Task NormalizesFriendRequestsAndRoutesRequestFilters()
    {
        OneBotNormalizedEvent normalized = OneBotEventNormalizer.Normalize(
            ParseJson(
                "{\"post_type\":\"request\",\"request_type\":\"friend\","
                + "\"self_id\":10001,\"flag\":\"friend-1\",\"user_id\":10002}"),
            HttpProfile())!;
        using JsonDocument document = JsonDocument.Parse(normalized.Payload);
        Assert.Equal("friend", normalized.RequestKind);
        Assert.Equal("add", document.RootElement.GetProperty("vendor_request")
            .GetProperty("sub_type").GetString());

        using OneBotEventSubscriptionRegistry registry = new(HttpProfile());
        using OneBotEventSubscription subscription = registry.Subscribe(
            SubscriptionRequest("request-filter", "{\"request_kind\":\"friend\"}"));
        registry.Publish(ParseJson(
            "{\"post_type\":\"request\",\"request_type\":\"friend\","
            + "\"self_id\":10001,\"flag\":\"friend-2\",\"user_id\":10002}"));
        registry.Publish(ParseJson(
            "{\"post_type\":\"request\",\"request_type\":\"group\","
            + "\"sub_type\":\"invite\",\"self_id\":10001,\"flag\":\"group-1\","
            + "\"group_id\":20001,\"user_id\":10002}"));

        IAsyncEnumerator<DirectStreamItem> enumerator = subscription
            .ReadAllAsync(CancellationToken.None)
            .GetAsyncEnumerator();
        Assert.True(await enumerator.MoveNextAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(1)));
        Assert.Equal("inbound_request", enumerator.Current.Payload.EventType);
        using CancellationTokenSource cancellation = new(TimeSpan.FromMilliseconds(50));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () =>
            await enumerator.MoveNextAsync().AsTask().WaitAsync(cancellation.Token));
        subscription.Dispose();
    }

    [Theory]
    [InlineData("{\"event_type\":\"unknown\"}")]
    [InlineData("{\"unknown\":\"field\"}")]
    [InlineData("[]")]
    [InlineData("{\"kind\":true}")]
    public void SubscriptionFiltersFailClosed(string filter)
    {
        OneBotSubscriptionException exception = Assert.Throws<OneBotSubscriptionException>(() =>
            new OneBotEventSubscriptionRegistry(HttpProfile()).Subscribe(
                SubscriptionRequest("invalid", filter)));

        Assert.Equal("INVALID_REQUEST", exception.DomainCode);
    }

    [Fact]
    public async Task FullSubscriptionQueueClosesInsteadOfGrowingUnbounded()
    {
        using OneBotEventSubscriptionRegistry registry = new(HttpProfile());
        using OneBotEventSubscription subscription = registry.Subscribe(
            SubscriptionRequest("bounded", "{}"));
        for (int index = 0; index < 300; index++)
        {
            registry.Publish(ParseJson(
                "{\"post_type\":\"message\",\"message_type\":\"private\","
                + "\"self_id\":10001,\"user_id\":10002,\"message_id\":"
                + index
                + ",\"message\":[{\"type\":\"text\",\"data\":{\"text\":\"x\"}}]}"));
        }

        int count = 0;
        await foreach (DirectStreamItem _ in subscription.ReadAllAsync(CancellationToken.None))
        {
            count++;
        }

        Assert.Equal(256, count);
    }

    [Fact]
    public async Task DispatcherPreservesCancellationBeforeTransportDispatch()
    {
        RecordingTransport transport = new();
        OneBotInvocationDispatcher dispatcher = new(HttpProfile(), transport);
        using CancellationTokenSource cancellation = new();
        cancellation.Cancel();

        InvocationResult result = await dispatcher.InvokeAsync(
            new DirectInvocationRequest
            {
                Capability = OneBotMessageMapper.CapabilityId,
                InterfaceVersion = OneBotMessageMapper.InterfaceVersion,
                Method = OneBotMessageMapper.SendMessageMethod,
                PayloadTypeUrl = OneBotMessageMapper.SendMessageRequestTypeUrl,
                Payload = ByteString.CopyFrom(GroupMessage().ToByteArray())
            },
            cancellation.Token);

        Assert.Equal(DirectInvocationError.Types.Code.Cancelled, result.Error?.Code);
        Assert.Empty(transport.Calls);
    }

    [Fact]
    public async Task DispatcherMapsTransportFailuresWithoutLeakingSecrets()
    {
        RecordingTransport transport = new()
        {
            Failure = new OneBotTransportException("CAPABILITY_UNAVAILABLE", "runtime unavailable")
        };
        OneBotInvocationDispatcher dispatcher = new(HttpProfile(), transport);

        InvocationResult result = await dispatcher.InvokeAsync(
            new DirectInvocationRequest
            {
                Capability = OneBotMessageMapper.CapabilityId,
                InterfaceVersion = OneBotMessageMapper.InterfaceVersion,
                Method = OneBotMessageMapper.SendMessageMethod,
                PayloadTypeUrl = OneBotMessageMapper.SendMessageRequestTypeUrl,
                Payload = ByteString.CopyFrom(GroupMessage().ToByteArray())
            },
            CancellationToken.None);

        Assert.Equal(DirectInvocationError.Types.Code.Unavailable, result.Error?.Code);
        Assert.DoesNotContain("token", result.Error?.Message ?? string.Empty, StringComparison.OrdinalIgnoreCase);
    }

    private static OneBotProfile HttpProfile() => OneBotProfileLoader.FromJson(
        "{\"binding_id\":\"main\",\"http_base_url\":\"http://127.0.0.1:18080\","
        + "\"access_token\":\"token\",\"self_account_id\":\"10001\",\"timeout_seconds\":2}");

    private static SendMessageRequest GroupMessage()
    {
        SendMessageRequest request = new()
        {
            Conversation = new ConversationScope
            {
                Vendor = OneBotMessageMapper.Vendor,
                AccountId = "10001",
                ConversationId = "20001",
                Kind = ConversationKind.Group
            }
        };
        request.Content.Add(new MessageContentPart
        {
            Text = new TextContent { Text = "hello" }
        });
        return request;
    }

    private static DirectInvocationRequest SubscriptionRequest(string id, string filter) => new()
    {
        Capability = OneBotMessageMapper.CapabilityId,
        InterfaceVersion = OneBotMessageMapper.InterfaceVersion,
        Method = OneBotEventSubscriptionRegistry.SubscriptionMethod,
        PayloadTypeUrl = OneBotEventSubscriptionRegistry.FilterTypeUrl,
        Payload = ByteString.CopyFromUtf8(filter),
        RequestId = id,
        StreamMode = DirectStreamMode.Subscription
    };

    private static JsonElement ParseJson(string json)
    {
        using JsonDocument document = JsonDocument.Parse(json);
        return document.RootElement.Clone();
    }

    private sealed class RecordingTransport : IOneBotActionTransport
    {
        public List<string> Calls { get; } = new();
        public OneBotTransportException? Failure { get; init; }

        public Task<OneBotActionResponse> CallAsync(
            string action,
            OneBotActionRequest request,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            Calls.Add(action);
            if (Failure is not null)
            {
                throw Failure;
            }

            return Task.FromResult(new OneBotActionResponse
            {
                Status = "ok",
                Retcode = 0,
                Data = ParseJson("{\"message_id\":\"recorded-1\"}")
            });
        }

        public void SetEventHandler(Action<JsonElement>? eventHandler)
        {
            _ = eventHandler;
        }

        public void Start()
        {
        }

        public void Close()
        {
        }
    }

    private sealed class StaticHttpMessageHandler : HttpMessageHandler
    {
        private readonly string _body;
        private readonly HttpStatusCode _statusCode;

        public StaticHttpMessageHandler(string body, HttpStatusCode statusCode)
        {
            _body = body;
            _statusCode = statusCode;
        }

        public AuthenticationHeaderValue? Authorization { get; private set; }
        public string? ContentType { get; private set; }

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            Authorization = request.Headers.Authorization;
            ContentType = request.Content?.Headers.ContentType?.MediaType;
            return Task.FromResult(new HttpResponseMessage(_statusCode)
            {
                Content = new StringContent(_body, Encoding.UTF8, "application/json")
            });
        }
    }

    private sealed class UnauthorizedWebSocketPeer : IDisposable
    {
        private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource _shutdown = new();
        private readonly Task _worker;

        public UnauthorizedWebSocketPeer()
        {
            _listener.Start();
            Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            _worker = RejectAsync();
        }

        public int Port { get; }
        public bool RequestReceived { get; private set; }

        public void Dispose()
        {
            _shutdown.Cancel();
            _listener.Stop();
            try
            {
                _worker.Wait(TimeSpan.FromSeconds(1));
            }
            catch (AggregateException)
            {
            }

            _shutdown.Dispose();
        }

        private async Task RejectAsync()
        {
            try
            {
                using TcpClient client = await _listener.AcceptTcpClientAsync(_shutdown.Token);
                await using NetworkStream stream = client.GetStream();
                byte[] buffer = new byte[4 * 1024];
                int length = 0;
                while (length < buffer.Length)
                {
                    int read = await stream.ReadAsync(
                        buffer.AsMemory(length, buffer.Length - length),
                        _shutdown.Token);
                    if (read == 0)
                    {
                        return;
                    }

                    length += read;
                    if (Encoding.ASCII.GetString(buffer, 0, length).Contains(
                            "\r\n\r\n",
                            StringComparison.Ordinal))
                    {
                        RequestReceived = true;
                        await stream.WriteAsync(
                            Encoding.ASCII.GetBytes(
                                "HTTP/1.1 401 Unauthorized\r\n"
                                + "Content-Length: 0\r\nConnection: close\r\n\r\n"),
                            _shutdown.Token);
                        return;
                    }
                }
            }
            catch (OperationCanceledException) when (_shutdown.IsCancellationRequested)
            {
            }
            catch (ObjectDisposedException) when (_shutdown.IsCancellationRequested)
            {
            }
            catch (SocketException) when (_shutdown.IsCancellationRequested)
            {
            }
        }
    }
}
