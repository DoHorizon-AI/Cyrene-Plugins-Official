// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotCoreTests.cs                                                    │
// │  Namespace: Cyrene.OneBot.V11.Tests                                       │
// │  Role: Configuration, HTTP transport, and canonical message mapping tests.│
// │                                                                         │
// │  模块职责：配置、HTTP 传输与规范消息映射测试                                 │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using Cyrene.Message.Connector.V1;
using Cyrene.Plugin.RuntimeHost;
using Cyrene.OneBot.V11.Core;
using Cyrene.Plugin.Runtime.V1;
using Google.Protobuf;
using Xunit;

namespace Cyrene.OneBot.V11.Tests;

public sealed class OneBotCoreTests
{
    [Fact]
    public void LoadsHttpProfileAndOverlaysActivationBinding()
    {
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            "{\"http_base_url\":\"http://127.0.0.1:18080/\",\"self_account_id\":\"10001\"}",
            "qq-main");

        Assert.Equal("qq-main", profile.BindingId);
        Assert.Equal("http://127.0.0.1:18080", profile.HttpBaseUrl);
        Assert.Equal(OneBotTransportProfile.HttpApi, profile.TransportProfile);
        Assert.Equal("10001", profile.SelfAccountId);
    }

    [Fact]
    public void RejectsUnknownConfigurationFields()
    {
        OneBotConfigurationException exception = Assert.Throws<OneBotConfigurationException>(
            () => OneBotProfileLoader.FromJson(
                "{\"binding_id\":\"qq-main\",\"http_base_url\":\"http://127.0.0.1\",\"host_executable\":\"/opt/qq\"}"));

        Assert.Equal("UNKNOWN_CONFIGURATION_FIELD", exception.DomainCode);
    }

    [Fact]
    public async Task MapsCanonicalGroupMessageAndReadsDeliveryId()
    {
        RecordingHandler handler = new(
            "{\"status\":\"ok\",\"retcode\":0,\"data\":{\"message_id\":\"m-1\"}}");
        using HttpClient client = new(handler);
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            "{\"binding_id\":\"qq-main\",\"http_base_url\":\"http://127.0.0.1:18080\",\"access_token\":\"secret-token\",\"self_account_id\":\"10001\"}");
        OneBotInvocationDispatcher dispatcher = new(
            profile,
            new OneBotHttpTransport(profile, client));

        SendMessageRequest message = new();
        message.Conversation = new ConversationScope
        {
            Vendor = OneBotMessageMapper.Vendor,
            AccountId = "10001",
            ConversationId = "20001",
            Kind = ConversationKind.Group
        };
        MessageContentPart text = new() { Text = new TextContent { Text = "hello" } };
        message.Content.Add(text);
        MessageContentPart mention = new() { Mention = new MentionContent { Target = MentionTarget.Everyone } };
        message.Content.Add(mention);
        MessageContentPart image = new()
        {
            Image = new ImageContent
            {
                Reference = new AttachmentReference { RemoteUri = "https://cdn.example/image.png" }
            }
        };
        message.Content.Add(image);
        message.Reply = new ReplyReference { MessageId = "30001" };

        InvocationResult result = await dispatcher.InvokeAsync(
            new DirectInvocationRequest
            {
                Capability = OneBotMessageMapper.CapabilityId,
                InterfaceVersion = OneBotMessageMapper.InterfaceVersion,
                Method = OneBotMessageMapper.SendMessageMethod,
                PayloadTypeUrl = OneBotMessageMapper.SendMessageRequestTypeUrl,
                Payload = Google.Protobuf.ByteString.CopyFrom(message.ToByteArray())
            },
            CancellationToken.None);

        Assert.Null(result.Error);
        Assert.NotNull(result.Payload);
        DeliveryResult delivery = DeliveryResult.Parser.ParseFrom(result.Payload!.Value);
        Assert.Equal(DeliveryStatus.Accepted, delivery.Status);
        Assert.Equal("m-1", delivery.VendorMessageId);
        Assert.Equal("http://127.0.0.1:18080/send_group_msg", handler.RequestUri!.ToString());
        Assert.Equal("Bearer", handler.Authorization!.Scheme);
        Assert.Equal("secret-token", handler.Authorization.Parameter);
        Assert.Contains("\"group_id\":20001", handler.Body, StringComparison.Ordinal);
        Assert.Contains("\"type\":\"reply\"", handler.Body, StringComparison.Ordinal);
        Assert.Contains("\"type\":\"text\"", handler.Body, StringComparison.Ordinal);
        Assert.Contains("\"qq\":\"all\"", handler.Body, StringComparison.Ordinal);
        Assert.Contains("\"url\":\"https://cdn.example/image.png\"", handler.Body, StringComparison.Ordinal);
    }

    [Fact]
    public async Task MapsHttpTimeoutToDeadlineExceeded()
    {
        RecordingHandler handler = new(null, delay: TimeSpan.FromSeconds(1));
        using HttpClient client = new(handler);
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            "{\"binding_id\":\"qq-main\",\"http_base_url\":\"http://127.0.0.1:18080\",\"timeout_seconds\":0.01}");
        OneBotInvocationDispatcher dispatcher = new(
            profile,
            new OneBotHttpTransport(profile, client));

        InvocationResult result = await dispatcher.InvokeAsync(
            CreateMessageRequest("10001", "20001"),
            CancellationToken.None);

        Assert.Equal(DirectInvocationError.Types.Code.DeadlineExceeded, result.Error?.Code);
        Assert.Equal("TIMEOUT", result.Error?.DomainCode);
        Assert.True(result.Error?.Retryable);
    }

    [Fact]
    public async Task MapsRequestApprovalToFriendAndGroupActions()
    {
        RecordingHandler friendHandler = new(
            "{\"status\":\"ok\",\"retcode\":0,\"data\":{}}");
        using HttpClient friendClient = new(friendHandler);
        OneBotProfile friendProfile = OneBotProfileLoader.FromJson(
            "{\"binding_id\":\"qq-main\",\"http_base_url\":\"http://127.0.0.1:18080\"}");
        OneBotInvocationDispatcher friendDispatcher = new(
            friendProfile,
            new OneBotHttpTransport(friendProfile, friendClient));

        InvocationResult friendResult = await friendDispatcher.InvokeAsync(
            CreateRequestApproval("friend", "approve", "flag-friend", "hello"),
            CancellationToken.None);

        Assert.Null(friendResult.Error);
        Assert.Equal(
            OneBotRequestMapper.RespondResultTypeUrl,
            friendResult.Payload?.TypeUrl);
        Assert.Equal("http://127.0.0.1:18080/set_friend_add_request", friendHandler.RequestUri!.ToString());
        Assert.Contains("\"flag\":\"flag-friend\"", friendHandler.Body, StringComparison.Ordinal);
        Assert.Contains("\"approve\":true", friendHandler.Body, StringComparison.Ordinal);
        Assert.Contains("\"remark\":\"hello\"", friendHandler.Body, StringComparison.Ordinal);

        RecordingHandler groupHandler = new(
            "{\"status\":\"ok\",\"retcode\":0,\"data\":{}}");
        using HttpClient groupClient = new(groupHandler);
        OneBotProfile groupProfile = OneBotProfileLoader.FromJson(
            "{\"binding_id\":\"qq-main\",\"http_base_url\":\"http://127.0.0.1:18080\"}");
        OneBotInvocationDispatcher groupDispatcher = new(
            groupProfile,
            new OneBotHttpTransport(groupProfile, groupClient));

        InvocationResult groupResult = await groupDispatcher.InvokeAsync(
            CreateRequestApproval("group_invite", "reject", "flag-group", "nope"),
            CancellationToken.None);

        Assert.Null(groupResult.Error);
        Assert.Equal("http://127.0.0.1:18080/set_group_add_request", groupHandler.RequestUri!.ToString());
        Assert.Contains("\"flag\":\"flag-group\"", groupHandler.Body, StringComparison.Ordinal);
        Assert.Contains("\"sub_type\":\"invite\"", groupHandler.Body, StringComparison.Ordinal);
        Assert.Contains("\"approve\":false", groupHandler.Body, StringComparison.Ordinal);
        Assert.Contains("\"reason\":\"nope\"", groupHandler.Body, StringComparison.Ordinal);
    }

    [Fact]
    public async Task ForwardWebSocketCorrelatesActionAndDeliversEvent()
    {
        using WebSocketPeer peer = new();
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            $"{{\"binding_id\":\"qq-main\",\"websocket_url\":\"ws://127.0.0.1:{peer.Port}/onebot\",\"transport_profile\":\"forward_websocket\",\"timeout_seconds\":2}}",
            "qq-main");
        using OneBotWebSocketTransport transport = new(profile);
        TaskCompletionSource<JsonElement> eventReceived = new(
            TaskCreationOptions.RunContinuationsAsynchronously);
        transport.SetEventHandler(eventReceived.SetResult);

        OneBotActionResponse response = await transport.CallAsync(
            "send_private_msg",
            new OneBotActionRequest
            {
                UserId = 10001,
                Message = new[]
                {
                    new OneBotMessageSegment
                    {
                        Type = "text",
                        Data = new OneBotSegmentData { Text = "hello" }
                    }
                }
            },
            CancellationToken.None);

        Assert.Equal("ok", response.Status);
        Assert.Equal(0, response.Retcode);
        Assert.Equal("ws-message-1", response.Data.GetProperty("message_id").GetString());
        JsonElement eventPayload = await eventReceived.Task.WaitAsync(TimeSpan.FromSeconds(2));
        Assert.Equal("meta_event", eventPayload.GetProperty("post_type").GetString());
        await peer.Completed.WaitAsync(TimeSpan.FromSeconds(2));
        Assert.Equal("send_private_msg", peer.Action);
        Assert.Contains("\"user_id\":10001", peer.ActionBody, StringComparison.Ordinal);
    }

    [Fact]
    public async Task ReverseWebSocketAcceptsAuthorizedPeerAndCorrelatesAction()
    {
        int port = GetFreePort();
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            $"{{\"binding_id\":\"qq-main\",\"transport_profile\":\"reverse_websocket\",\"reverse_listen_host\":\"127.0.0.1\",\"reverse_listen_port\":{port},\"access_token\":\"reverse-token\",\"timeout_seconds\":2}}",
            "qq-main");
        using OneBotWebSocketTransport transport = new(profile);
        using OneBotReverseWebSocketServer server = new(profile, transport);
        using OneBotEventSubscriptionRegistry registry = new(profile);
        transport.SetEventHandler(registry.Publish);
        using OneBotEventSubscription subscription = registry.Subscribe(
            new DirectInvocationRequest
            {
                Capability = OneBotMessageMapper.CapabilityId,
                InterfaceVersion = OneBotMessageMapper.InterfaceVersion,
                Method = OneBotEventSubscriptionRegistry.SubscriptionMethod,
                PayloadTypeUrl = OneBotEventSubscriptionRegistry.FilterTypeUrl,
                Payload = Google.Protobuf.ByteString.CopyFromUtf8(
                    "{\"event_type\":\"inbound_message\"}"),
                RequestId = "reverse-subscription",
                StreamMode = DirectStreamMode.Subscription
            });
        server.Start();
        using ClientWebSocket client = new();
        client.Options.SetRequestHeader("Authorization", "Bearer reverse-token");
        await client.ConnectAsync(
            new Uri($"ws://127.0.0.1:{server.ListenPort}/onebot"),
            CancellationToken.None);

        await client.SendAsync(
            Encoding.UTF8.GetBytes(
                "{\"post_type\":\"message\",\"message_type\":\"private\","
                + "\"self_id\":10001,\"user_id\":10002,\"message_id\":\"reverse-event-1\","
                + "\"sender\":{\"user_id\":10002,\"nickname\":\"Alice\"},"
                + "\"message\":[{\"type\":\"text\",\"data\":{\"text\":\"from reverse\"}}]}"),
            WebSocketMessageType.Text,
            true,
            CancellationToken.None);
        await using IAsyncEnumerator<DirectStreamItem> events =
            subscription.ReadAllAsync(CancellationToken.None).GetAsyncEnumerator();
        Assert.True(await events.MoveNextAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(2)));
        InboundMessagePayload inbound = InboundMessagePayload.Parser.ParseFrom(
            events.Current.Payload.Value);
        Assert.Equal("reverse-event-1", inbound.MessageId);
        Assert.Equal("from reverse", inbound.Content[0].Text.Text);

        Task<OneBotActionResponse> call = transport.CallAsync(
            "send_private_msg",
            new OneBotActionRequest { UserId = 10001 },
            CancellationToken.None);
        string actionBody = await ReceiveWebSocketTextAsync(client);
        using JsonDocument action = JsonDocument.Parse(actionBody);
        string echo = action.RootElement.GetProperty("echo").GetString()!;
        await client.SendAsync(
            Encoding.UTF8.GetBytes(
                $"{{\"status\":\"ok\",\"retcode\":0,\"data\":{{\"message_id\":\"reverse-1\"}},\"echo\":\"{echo}\"}}"),
            WebSocketMessageType.Text,
            true,
            CancellationToken.None);

        OneBotActionResponse response = await call;
        Assert.Equal("ok", response.Status);
        Assert.Equal("reverse-1", response.Data.GetProperty("message_id").GetString());
        Assert.Equal("send_private_msg", action.RootElement.GetProperty("action").GetString());
    }

    [Fact]
    public void NormalizesInboundMessageToCanonicalPayload()
    {
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            "{\"binding_id\":\"qq-main\",\"http_base_url\":\"http://127.0.0.1:18080\",\"self_account_id\":\"10001\"}");
        using JsonDocument document = JsonDocument.Parse(
            "{\"post_type\":\"message\",\"message_type\":\"group\","
            + "\"self_id\":10001,\"group_id\":20001,\"message_id\":30001,"
            + "\"sender\":{\"user_id\":10002,\"card\":\"Card\",\"nickname\":\"Nick\"},"
            + "\"message\":["
            + "{\"type\":\"text\",\"data\":{\"text\":\"hello\"}},"
            + "{\"type\":\"at\",\"data\":{\"qq\":\"10003\",\"name\":\"Bob\"}},"
            + "{\"type\":\"image\",\"data\":{\"url\":\"https://cdn.example/a.png\"}},"
            + "{\"type\":\"file\",\"data\":{\"file\":\"media-1\",\"name\":\"a.txt\"}},"
            + "{\"type\":\"reply\",\"data\":{\"id\":\"29999\"}},"
            + "{\"type\":\"json\",\"data\":{\"foo\":\"bar\"}}]}");

        OneBotNormalizedEvent normalized = OneBotEventNormalizer.Normalize(
            document.RootElement,
            profile)!;
        InboundMessagePayload payload = InboundMessagePayload.Parser.ParseFrom(
            normalized.Payload);

        Assert.Equal(OneBotEventNormalizer.InboundMessageEventType, normalized.EventType);
        Assert.Equal(OneBotEventNormalizer.InboundMessageTypeUrl, normalized.TypeUrl);
        Assert.Equal("30001", payload.MessageId);
        Assert.Equal("20001", payload.Conversation.ConversationId);
        Assert.Equal(ConversationKind.Group, payload.Conversation.Kind);
        Assert.Equal("10002", payload.SenderId);
        Assert.Equal("Card", payload.SenderDisplayName);
        Assert.Equal(4, payload.Content.Count);
        Assert.Equal("hello", payload.Content[0].Text.Text);
        Assert.Equal(MentionTarget.User, payload.Content[1].Mention.Target);
        Assert.Equal("10003", payload.Content[1].Mention.TargetId);
        Assert.Equal("https://cdn.example/a.png", payload.Content[2].Image.Reference.RemoteUri);
        Assert.Equal("media-1", payload.Content[3].File.Reference.VendorMedia.MediaId);
        Assert.Equal("29999", payload.Reply.MessageId);
        Assert.Equal("onebot.v11", payload.VendorExtension.Vendor);
        Assert.Contains(payload.VendorExtension.Facts, fact =>
            fact.Name == "segment_5_type" && fact.Value == "json");
        Assert.Contains(payload.VendorExtension.Facts, fact =>
            fact.Name == "segment_5_foo" && fact.Value == "bar");
    }

    [Fact]
    public void NormalizesInboundRequestToExistingJsonContract()
    {
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            "{\"binding_id\":\"qq-main\",\"http_base_url\":\"http://127.0.0.1:18080\",\"self_account_id\":\"10001\"}");
        using JsonDocument document = JsonDocument.Parse(
            "{\"post_type\":\"request\",\"request_type\":\"group\","
            + "\"sub_type\":\"invite\",\"self_id\":\"10001\",\"flag\":\"flag-1\","
            + "\"group_id\":20001,\"user_id\":10002}");

        OneBotNormalizedEvent normalized = OneBotEventNormalizer.Normalize(
            document.RootElement,
            profile)!;
        using JsonDocument payload = JsonDocument.Parse(normalized.Payload);
        JsonElement root = payload.RootElement;

        Assert.Equal(OneBotEventNormalizer.InboundRequestEventType, normalized.EventType);
        Assert.Equal("group_invite", normalized.RequestKind);
        Assert.Equal("flag-1", root.GetProperty("request_id").GetString());
        Assert.Equal("group_invite", root.GetProperty("request_kind").GetString());
        Assert.Equal("invite", root.GetProperty("vendor_request").GetProperty("sub_type").GetString());
        Assert.Equal("20001", root.GetProperty("vendor_request").GetProperty("group_id").GetString());
        Assert.Equal("10002", root.GetProperty("vendor_request").GetProperty("user_id").GetString());
    }

    [Fact]
    public async Task SubscriptionFiltersAndPublishesCanonicalInboundEvents()
    {
        OneBotProfile profile = OneBotProfileLoader.FromJson(
            "{\"binding_id\":\"qq-main\",\"http_base_url\":\"http://127.0.0.1:18080\",\"self_account_id\":\"10001\"}");
        using OneBotEventSubscriptionRegistry registry = new(profile);
        using OneBotEventSubscription subscription = registry.Subscribe(
            new DirectInvocationRequest
            {
                Capability = OneBotMessageMapper.CapabilityId,
                InterfaceVersion = OneBotMessageMapper.InterfaceVersion,
                Method = OneBotEventSubscriptionRegistry.SubscriptionMethod,
                PayloadTypeUrl = OneBotEventSubscriptionRegistry.FilterTypeUrl,
                Payload = Google.Protobuf.ByteString.CopyFromUtf8(
                    "{\"event_type\":\"inbound_message\",\"conversation_id\":\"20001\",\"kind\":\"group\"}"),
                RequestId = "subscription-1",
                StreamMode = DirectStreamMode.Subscription
            });
        using JsonDocument document = JsonDocument.Parse(
            "{\"post_type\":\"message\",\"message_type\":\"group\","
            + "\"self_id\":10001,\"group_id\":20001,\"message_id\":30001,"
            + "\"sender\":{\"user_id\":10002},"
            + "\"message\":[{\"type\":\"text\",\"data\":{\"text\":\"hello\"}}]}");
        registry.Publish(document.RootElement);

        await using IAsyncEnumerator<DirectStreamItem> events =
            subscription.ReadAllAsync(CancellationToken.None).GetAsyncEnumerator();
        Assert.True(await events.MoveNextAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(2)));
        DirectStreamItem item = events.Current;
        Assert.Equal(OneBotEventNormalizer.InboundMessageEventType, item.Payload.EventType);
        InboundMessagePayload payload = InboundMessagePayload.Parser.ParseFrom(
            item.Payload.Value);
        Assert.Equal("20001", payload.Conversation.ConversationId);
        Assert.Equal("hello", payload.Content[0].Text.Text);
    }

    private static int GetFreePort()
    {
        TcpListener probe = new(IPAddress.Loopback, 0);
        probe.Start();
        int port = ((IPEndPoint)probe.LocalEndpoint).Port;
        probe.Stop();
        return port;
    }

    private static async Task<string> ReceiveWebSocketTextAsync(ClientWebSocket client)
    {
        byte[] buffer = new byte[16 * 1024];
        using MemoryStream output = new();
        WebSocketReceiveResult result;
        do
        {
            result = await client.ReceiveAsync(
                new ArraySegment<byte>(buffer),
                CancellationToken.None);
            Assert.Equal(WebSocketMessageType.Text, result.MessageType);
            output.Write(buffer, 0, result.Count);
        }
        while (!result.EndOfMessage);

        return Encoding.UTF8.GetString(output.ToArray());
    }

    private static DirectInvocationRequest CreateMessageRequest(
        string accountId,
        string conversationId)
    {
        SendMessageRequest message = new();
        message.Conversation = new ConversationScope
        {
            Vendor = OneBotMessageMapper.Vendor,
            AccountId = accountId,
            ConversationId = conversationId,
            Kind = ConversationKind.Private
        };
        MessageContentPart text = new() { Text = new TextContent { Text = "hello" } };
        message.Content.Add(text);
        return new DirectInvocationRequest
        {
            Capability = OneBotMessageMapper.CapabilityId,
            InterfaceVersion = OneBotMessageMapper.InterfaceVersion,
            Method = OneBotMessageMapper.SendMessageMethod,
            PayloadTypeUrl = OneBotMessageMapper.SendMessageRequestTypeUrl,
            Payload = Google.Protobuf.ByteString.CopyFrom(message.ToByteArray())
        };
    }

    private static DirectInvocationRequest CreateRequestApproval(
        string requestKind,
        string decision,
        string requestId,
        string comment) => new()
        {
            Capability = OneBotMessageMapper.CapabilityId,
            InterfaceVersion = OneBotMessageMapper.InterfaceVersion,
            Method = OneBotRequestMapper.RespondRequestMethod,
            PayloadTypeUrl = OneBotRequestMapper.RespondRequestTypeUrl,
            Payload = Google.Protobuf.ByteString.CopyFromUtf8(
                $"{{\"request_id\":\"{requestId}\",\"request_kind\":\"{requestKind}\","
                + $"\"decision\":\"{decision}\",\"comment\":\"{comment}\"}}")
        };

    private sealed class RecordingHandler : HttpMessageHandler
    {
        private readonly string? _responseBody;
        private readonly TimeSpan _delay;

        public RecordingHandler(string? responseBody, TimeSpan? delay = null)
        {
            _responseBody = responseBody;
            _delay = delay ?? TimeSpan.Zero;
        }

        public Uri? RequestUri { get; private set; }
        public AuthenticationHeaderValue? Authorization { get; private set; }
        public string Body { get; private set; } = string.Empty;

        protected override async Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            RequestUri = request.RequestUri;
            Authorization = request.Headers.Authorization;
            Body = await request.Content!.ReadAsStringAsync(cancellationToken);
            if (_delay > TimeSpan.Zero)
            {
                await Task.Delay(_delay, cancellationToken);
            }

            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(
                    _responseBody ?? "{\"status\":\"ok\",\"retcode\":0,\"data\":{}}",
                    Encoding.UTF8,
                    "application/json")
            };
        }
    }

    private sealed class WebSocketPeer : IDisposable
    {
        private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource _shutdown = new();
        private TcpClient? _client;

        public WebSocketPeer()
        {
            _listener.Start();
            Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            Completed = RunAsync();
        }

        public int Port { get; }
        public Task Completed { get; }
        public string? Action { get; private set; }
        public string ActionBody { get; private set; } = string.Empty;

        public void Dispose()
        {
            _shutdown.Cancel();
            _listener.Stop();
            _client?.Close();
            try
            {
                Completed.Wait(TimeSpan.FromSeconds(2));
            }
            catch (AggregateException)
            {
            }
            _shutdown.Dispose();
        }

        private async Task RunAsync()
        {
            try
            {
                using TcpClient client = await _listener.AcceptTcpClientAsync(_shutdown.Token);
                _client = client;
                using NetworkStream stream = client.GetStream();
                string headers = await ReadHeadersAsync(stream, _shutdown.Token);
                string key = HeaderValue(headers, "Sec-WebSocket-Key");
                string accept = ComputeWebSocketAccept(key);
                string response =
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    + "Upgrade: websocket\r\n"
                    + "Connection: Upgrade\r\n"
                    + $"Sec-WebSocket-Accept: {accept}\r\n\r\n";
                await stream.WriteAsync(Encoding.ASCII.GetBytes(response), _shutdown.Token);

                await WriteServerTextAsync(
                    stream,
                    "{\"post_type\":\"meta_event\",\"meta_event_type\":\"heartbeat\",\"self_id\":\"10001\"}",
                    _shutdown.Token);
                string actionBody = await ReadClientTextAsync(stream, _shutdown.Token);
                ActionBody = actionBody;
                using JsonDocument request = JsonDocument.Parse(actionBody);
                Action = request.RootElement.GetProperty("action").GetString();
                string echo = request.RootElement.GetProperty("echo").GetString()!;
                await WriteServerTextAsync(
                    stream,
                    $"{{\"status\":\"ok\",\"retcode\":0,\"data\":{{\"message_id\":\"ws-message-1\"}},\"echo\":\"{echo}\"}}",
                    _shutdown.Token);
            }
            catch (OperationCanceledException) when (_shutdown.IsCancellationRequested)
            {
            }
            finally
            {
                _client?.Close();
            }
        }

        private static async Task<string> ReadHeadersAsync(
            NetworkStream stream,
            CancellationToken cancellationToken)
        {
            using MemoryStream output = new();
            byte[] buffer = new byte[1];
            while (output.Length < 64 * 1024)
            {
                int read = await stream.ReadAsync(buffer, cancellationToken);
                if (read == 0)
                {
                    throw new InvalidOperationException("WebSocket peer closed during handshake.");
                }

                output.WriteByte(buffer[0]);
                if (output.Length >= 4)
                {
                    byte[] bytes = output.GetBuffer();
                    int length = checked((int)output.Length);
                    if (bytes[length - 4] == '\r'
                        && bytes[length - 3] == '\n'
                        && bytes[length - 2] == '\r'
                        && bytes[length - 1] == '\n')
                    {
                        return Encoding.ASCII.GetString(bytes, 0, length);
                    }
                }
            }

            throw new InvalidOperationException("WebSocket peer handshake is too large.");
        }

        private static string HeaderValue(string headers, string name)
        {
            foreach (string line in headers.Split("\r\n", StringSplitOptions.RemoveEmptyEntries))
            {
                int separator = line.IndexOf(':');
                if (separator > 0 && string.Equals(
                        line[..separator].Trim(),
                        name,
                        StringComparison.OrdinalIgnoreCase))
                {
                    return line[(separator + 1)..].Trim();
                }
            }

            throw new InvalidOperationException($"WebSocket header {name} is missing.");
        }

        private static string ComputeWebSocketAccept(string key)
        {
#pragma warning disable CA5350 // RFC 6455 requires SHA-1 for Sec-WebSocket-Accept.
            return Convert.ToBase64String(
                System.Security.Cryptography.SHA1.HashData(
                    Encoding.ASCII.GetBytes(
                        key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")));
#pragma warning restore CA5350
        }

        private static async Task<string> ReadClientTextAsync(
            NetworkStream stream,
            CancellationToken cancellationToken)
        {
            byte[] header = await ReadExactAsync(stream, 2, cancellationToken);
            int opcode = header[0] & 0x0F;
            if (opcode != 1)
            {
                throw new InvalidOperationException("WebSocket peer expected a text frame.");
            }

            int length = header[1] & 0x7F;
            if (length == 126)
            {
                byte[] extended = await ReadExactAsync(stream, 2, cancellationToken);
                length = (extended[0] << 8) | extended[1];
            }
            else if (length == 127)
            {
                byte[] extended = await ReadExactAsync(stream, 8, cancellationToken);
                long longLength = 0;
                foreach (byte value in extended)
                {
                    longLength = (longLength << 8) | value;
                }
                length = checked((int)longLength);
            }

            if ((header[1] & 0x80) == 0)
            {
                throw new InvalidOperationException("WebSocket client frame was not masked.");
            }

            byte[] mask = await ReadExactAsync(stream, 4, cancellationToken);
            byte[] payload = await ReadExactAsync(stream, length, cancellationToken);
            for (int index = 0; index < payload.Length; index++)
            {
                payload[index] ^= mask[index % 4];
            }

            return Encoding.UTF8.GetString(payload);
        }

        private static async Task WriteServerTextAsync(
            NetworkStream stream,
            string payload,
            CancellationToken cancellationToken)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(payload);
            if (bytes.Length >= 126)
            {
                throw new InvalidOperationException("Test frame must remain short.");
            }

            await stream.WriteAsync(new[] { (byte)0x81, (byte)bytes.Length }, cancellationToken);
            await stream.WriteAsync(bytes, cancellationToken);
        }

        private static async Task<byte[]> ReadExactAsync(
            NetworkStream stream,
            int length,
            CancellationToken cancellationToken)
        {
            byte[] result = new byte[length];
            int offset = 0;
            while (offset < length)
            {
                int read = await stream.ReadAsync(
                    result.AsMemory(offset, length - offset),
                    cancellationToken);
                if (read == 0)
                {
                    throw new InvalidOperationException("WebSocket peer closed unexpectedly.");
                }

                offset += read;
            }

            return result;
        }
    }
}
