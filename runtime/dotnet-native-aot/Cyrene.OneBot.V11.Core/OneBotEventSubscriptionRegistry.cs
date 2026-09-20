// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotEventSubscriptionRegistry.cs                                    │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: Binding-local subscription filters and bounded event fan-out.      │
// │                                                                         │
// │  模块职责：binding 独立的订阅过滤与有界事件分发                               │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using System.Threading.Channels;
using Cyrene.Plugin.RuntimeHost;
using Cyrene.Plugin.Runtime.V1;

namespace Cyrene.OneBot.V11.Core;

/// <summary>Invalid subscription request at the direct runtime boundary.</summary>
public sealed class OneBotSubscriptionException : Exception
{
    public OneBotSubscriptionException(string domainCode, string message)
        : base(message)
    {
        DomainCode = domainCode;
    }

    public string DomainCode { get; }
}

/// <summary>
/// One binding-local direct-runtime event subscription.
/// <para>一个 binding 独立的 DirectPluginRuntime 事件订阅。</para>
/// </summary>
public sealed class OneBotEventSubscription : IDisposable
{
    private readonly OneBotEventSubscriptionRegistry _owner;
    private readonly Channel<DirectStreamItem> _channel;
    private int _disposed;

    internal OneBotEventSubscription(
        OneBotEventSubscriptionRegistry owner,
        string subscriptionId,
        OneBotSubscriptionFilter filter)
    {
        _owner = owner;
        SubscriptionId = subscriptionId;
        Filter = filter;
        _channel = Channel.CreateBounded<DirectStreamItem>(
            new BoundedChannelOptions(256)
            {
                FullMode = BoundedChannelFullMode.Wait,
                SingleReader = true,
                SingleWriter = false
            });
    }

    public string SubscriptionId { get; }

    internal OneBotSubscriptionFilter Filter { get; }

    public IAsyncEnumerable<DirectStreamItem> ReadAllAsync(CancellationToken cancellationToken) =>
        _channel.Reader.ReadAllAsync(cancellationToken);

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 0)
        {
            _owner.Remove(this);
            _channel.Writer.TryComplete();
        }
    }

    internal bool TryWrite(DirectStreamItem item) =>
        Volatile.Read(ref _disposed) == 0 && _channel.Writer.TryWrite(item);

    internal void Complete()
    {
        Interlocked.Exchange(ref _disposed, 1);
        _channel.Writer.TryComplete();
    }
}

/// <summary>Validated filter accepted by the v1 subscription boundary.</summary>
internal sealed record OneBotSubscriptionFilter(
    string? EventType,
    string? ConversationId,
    string? Kind,
    string? RequestKind)
{
    public bool Matches(OneBotNormalizedEvent value)
    {
        if (EventType is not null && !string.Equals(
                EventType,
                value.EventType,
                StringComparison.Ordinal))
        {
            return false;
        }

        if (value.ConversationId is null)
        {
            if (ConversationId is not null || Kind is not null)
            {
                return false;
            }

            return RequestKind is null
                || string.Equals(RequestKind, value.RequestKind, StringComparison.Ordinal);
        }

        if (ConversationId is not null
            && !string.Equals(
                ConversationId,
                value.ConversationId,
                StringComparison.Ordinal))
        {
            return false;
        }

        return Kind is null
            || string.Equals(Kind, value.Kind, StringComparison.Ordinal);
    }
}

/// <summary>
/// Owns subscriptions for one configured binding and publishes normalized events.
/// <para>为一个配置 binding 管理订阅，并发布规范化事件。</para>
/// </summary>
public sealed class OneBotEventSubscriptionRegistry : IDisposable
{
    public const string SubscriptionMethod = "events";
    public const string FilterTypeUrl =
        "type.cyrene.io/cyrene.message.connector.v1.Filter";
    public const int MaxFilterBytes = 16 * 1024;

    private readonly OneBotProfile _profile;
    private readonly object _stateLock = new();
    private readonly Dictionary<string, OneBotEventSubscription> _subscriptions = new(
        StringComparer.Ordinal);
    private bool _closed;

    public OneBotEventSubscriptionRegistry(OneBotProfile profile)
    {
        _profile = profile;
    }

    public OneBotEventSubscription Subscribe(DirectInvocationRequest request)
    {
        if (_closed)
        {
            throw new OneBotSubscriptionException(
                "CAPABILITY_UNAVAILABLE",
                "OneBot event subscription registry is closed.");
        }

        if (!string.Equals(
                request.Capability,
                OneBotMessageMapper.CapabilityId,
                StringComparison.Ordinal)
            || !string.Equals(
                request.InterfaceVersion,
                OneBotMessageMapper.InterfaceVersion,
                StringComparison.Ordinal)
            || !string.Equals(request.Method, SubscriptionMethod, StringComparison.Ordinal))
        {
            throw new OneBotSubscriptionException(
                "METHOD_NOT_FOUND",
                "Only message.connector.v1/events version 1 is registered for subscriptions.");
        }

        if (!string.Equals(request.PayloadTypeUrl, FilterTypeUrl, StringComparison.Ordinal))
        {
            throw new OneBotSubscriptionException(
                "PAYLOAD_TYPE_MISMATCH",
                "events payload_type_url is not the canonical filter type.");
        }

        if (string.IsNullOrWhiteSpace(request.RequestId))
        {
            throw new OneBotSubscriptionException(
                "REQUIRED_FIELD_MISSING",
                "subscription request_id must be non-empty.");
        }

        if (request.Payload.Length > MaxFilterBytes)
        {
            throw new OneBotSubscriptionException(
                "PAYLOAD_TOO_LARGE",
                "subscription filter exceeds the 16 KiB limit.");
        }

        OneBotSubscriptionFilter filter = OneBotSubscriptionFilterParser.Parse(
            request.Payload.ToByteArray());
        OneBotEventSubscription subscription = new(this, request.RequestId, filter);
        lock (_stateLock)
        {
            if (_closed)
            {
                subscription.Complete();
                throw new OneBotSubscriptionException(
                    "CAPABILITY_UNAVAILABLE",
                    "OneBot event subscription registry is closed.");
            }

            if (_subscriptions.Remove(
                    request.RequestId,
                    out OneBotEventSubscription? previous))
            {
                previous.Complete();
            }

            _subscriptions.Add(request.RequestId, subscription);
        }

        return subscription;
    }

    public void Publish(JsonElement eventPayload)
    {
        OneBotNormalizedEvent? normalized;
        try
        {
            normalized = OneBotEventNormalizer.Normalize(eventPayload, _profile);
        }
        catch (OneBotEventNormalizationException)
        {
            return;
        }

        if (normalized is null)
        {
            return;
        }

        OneBotEventSubscription[] subscriptions;
        lock (_stateLock)
        {
            if (_closed)
            {
                return;
            }

            subscriptions = _subscriptions.Values.ToArray();
        }

        foreach (OneBotEventSubscription subscription in subscriptions)
        {
            if (!subscription.Filter.Matches(normalized))
            {
                continue;
            }

            bool accepted = subscription.TryWrite(new DirectStreamItem
            {
                Payload = new DirectPayload
                {
                    EventType = normalized.EventType,
                    TypeUrl = normalized.TypeUrl,
                    Value = Google.Protobuf.ByteString.CopyFrom(normalized.Payload)
                }
            });
            if (!accepted)
            {
                // A full bounded queue is terminal for this subscription.
                // 有界队列满表示该订阅终止；若只移除而不完成 channel，
                // DirectPluginRuntime stream 会永久等待。
                subscription.Dispose();
            }
        }
    }

    public void Dispose()
    {
        OneBotEventSubscription[] subscriptions;
        lock (_stateLock)
        {
            if (_closed)
            {
                return;
            }

            _closed = true;
            subscriptions = _subscriptions.Values.ToArray();
            _subscriptions.Clear();
        }

        foreach (OneBotEventSubscription subscription in subscriptions)
        {
            subscription.Complete();
        }
    }

    internal void Remove(OneBotEventSubscription subscription)
    {
        lock (_stateLock)
        {
            if (_subscriptions.TryGetValue(
                    subscription.SubscriptionId,
                    out OneBotEventSubscription? current)
                && ReferenceEquals(current, subscription))
            {
                _subscriptions.Remove(subscription.SubscriptionId);
            }
        }
    }
}

internal static class OneBotSubscriptionFilterParser
{
    public static OneBotSubscriptionFilter Parse(byte[] payload)
    {
        if (payload.Length == 0)
        {
            return new OneBotSubscriptionFilter(null, null, null, null);
        }

        try
        {
            using JsonDocument document = JsonDocument.Parse(payload);
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                throw new OneBotSubscriptionException(
                    "INVALID_REQUEST",
                    "subscription filter must be an object.");
            }

            string? eventType = null;
            string? conversationId = null;
            string? kind = null;
            string? requestKind = null;
            foreach (JsonProperty property in root.EnumerateObject())
            {
                string? value = property.Value.ValueKind == JsonValueKind.String
                    ? property.Value.GetString()?.Trim()
                    : null;
                if (value is null)
                {
                    throw new OneBotSubscriptionException(
                        "INVALID_REQUEST",
                        $"subscription filter {property.Name} must be text.");
                }

                switch (property.Name)
                {
                    case "event_type":
                        if (value is not (OneBotEventNormalizer.InboundMessageEventType
                            or OneBotEventNormalizer.InboundRequestEventType))
                        {
                            throw new OneBotSubscriptionException(
                                "INVALID_REQUEST",
                                "subscription filter event_type is unsupported.");
                        }

                        eventType = value;
                        break;
                    case "conversation_id":
                        conversationId = value;
                        break;
                    case "kind":
                        kind = value;
                        break;
                    case "request_kind":
                        if (value is not ("friend" or "group_invite"))
                        {
                            throw new OneBotSubscriptionException(
                                "INVALID_REQUEST",
                                "subscription filter request_kind is unsupported.");
                        }

                        requestKind = value;
                        break;
                    default:
                        throw new OneBotSubscriptionException(
                            "INVALID_REQUEST",
                            "subscription filter contains unknown fields.");
                }
            }

            return new OneBotSubscriptionFilter(
                eventType,
                conversationId,
                kind,
                requestKind);
        }
        catch (JsonException)
        {
            throw new OneBotSubscriptionException(
                "INVALID_REQUEST",
                "subscription filter is invalid JSON.");
        }
    }
}
