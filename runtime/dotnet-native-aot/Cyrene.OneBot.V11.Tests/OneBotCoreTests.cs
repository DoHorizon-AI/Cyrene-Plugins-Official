// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotCoreTests.cs                                                    │
// │  Namespace: Cyrene.OneBot.V11.Tests                                       │
// │  Role: Configuration, HTTP transport, and canonical message mapping tests.│
// │                                                                         │
// │  模块职责：配置、HTTP 传输与规范消息映射测试                                 │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Net;
using System.Net.Http.Headers;
using System.Text;
using Cyrene.Message.Connector.V1;
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
    public void RejectsQqntOnlyConfigurationFields()
    {
        OneBotConfigurationException exception = Assert.Throws<OneBotConfigurationException>(
            () => OneBotProfileLoader.FromJson(
                "{\"binding_id\":\"qq-main\",\"http_base_url\":\"http://127.0.0.1\",\"host_executable\":\"/opt/qq\"}"));

        Assert.Equal("QQNT_DIRECT_ONLY_FIELD", exception.DomainCode);
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
}
