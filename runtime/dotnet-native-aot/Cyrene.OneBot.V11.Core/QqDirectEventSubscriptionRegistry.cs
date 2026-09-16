// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqDirectEventSubscriptionRegistry.cs                                  │
// │  Namespace: Cyrene.OneBot.V11.Core                                       │
// │  Role: Binding-local QQ event subscriptions and bounded fan-out.          │
// │                                                                         │
// │  模块职责：按 binding 隔离 QQ 事件订阅，并将规范事件有界地分发给调用方      │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using System.Threading.Channels;
using Cyrene.Plugin.Runtime.V1;

namespace Cyrene.OneBot.V11.Core;

/// <summary>One normalized QQ event ready for DirectPluginRuntime streaming.</summary>
public sealed record QqDirectNormalizedEvent(
    string EventType,
    string TypeUrl,
    byte[] Payload,
    string? ConversationId,
    string? Kind,
    string? RequestKind);

/// <summary>Invalid QQ subscription request at the direct runtime boundary.</summary>
public sealed class QqDirectSubscriptionException : Exception
{
    public QqDirectSubscriptionException(string domainCode, string message)
        : base(message)
    {
        DomainCode = domainCode;
    }

    public string DomainCode { get; }
}

/// <summary>One binding-local QQ event stream.</summary>
public sealed class QqDirectEventSubscription : IDisposable
{
    private readonly QqDirectEventSubscriptionRegistry _owner;
    private readonly Channel<DirectStreamItem> _channel;
    private int _disposed;

    internal QqDirectEventSubscription(
        QqDirectEventSubscriptionRegistry owner,
        string subscriptionId,
        QqDirectSubscriptionFilter filter)
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

    internal QqDirectSubscriptionFilter Filter { get; }

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

/// <summary>Validated filter for the v1 QQ subscription boundary.</summary>
internal sealed record QqDirectSubscriptionFilter(
    string? EventType,
    string? ConversationId,
    string? Kind,
    string? RequestKind)
{
    public bool Matches(QqDirectNormalizedEvent value)
    {
        if (EventType is not null
            && !string.Equals(EventType, value.EventType, StringComparison.Ordinal))
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

/// <summary>Owns QQ subscriptions for one configured direct binding.</summary>
public sealed class QqDirectEventSubscriptionRegistry : IDisposable
{
    public const int MaxFilterBytes = 16 * 1024;

    private readonly object _stateLock = new();
    private readonly Dictionary<string, QqDirectEventSubscription> _subscriptions = new(
        StringComparer.Ordinal);
    private bool _closed;

    public QqDirectEventSubscription Subscribe(DirectInvocationRequest request)
    {
        if (_closed)
        {
            throw new QqDirectSubscriptionException(
                "CAPABILITY_UNAVAILABLE",
                "QQ event subscription registry is closed.");
        }

        if (request.Capability is not (
                QqDirectMessageMapper.CapabilityId
                or QqDirectInvocationDispatcher.CapabilityId)
            || request.InterfaceVersion != QqDirectMessageMapper.InterfaceVersion
            || request.Method != QqDirectMessageMapper.EventsMethod)
        {
            throw new QqDirectSubscriptionException(
                "METHOD_NOT_FOUND",
                "Only message.connector.v1/events and qq.client.v1/events are registered.");
        }

        if (request.PayloadTypeUrl != QqDirectMessageMapper.FilterTypeUrl)
        {
            throw new QqDirectSubscriptionException(
                "PAYLOAD_TYPE_MISMATCH",
                "events payload_type_url is not the canonical filter type.");
        }

        if (string.IsNullOrWhiteSpace(request.RequestId))
        {
            throw new QqDirectSubscriptionException(
                "REQUIRED_FIELD_MISSING",
                "subscription request_id must be non-empty.");
        }

        if (request.Payload.Length > MaxFilterBytes)
        {
            throw new QqDirectSubscriptionException(
                "PAYLOAD_TOO_LARGE",
                "subscription filter exceeds the 16 KiB limit.");
        }

        QqDirectSubscriptionFilter filter = ParseFilter(request.Payload.ToByteArray());
        QqDirectEventSubscription subscription = new(this, request.RequestId, filter);
        lock (_stateLock)
        {
            if (_closed)
            {
                subscription.Complete();
                throw new QqDirectSubscriptionException(
                    "CAPABILITY_UNAVAILABLE",
                    "QQ event subscription registry is closed.");
            }

            if (_subscriptions.Remove(
                    request.RequestId,
                    out QqDirectEventSubscription? previous))
            {
                previous.Complete();
            }

            _subscriptions.Add(request.RequestId, subscription);
        }

        return subscription;
    }

    public void Publish(QqDirectNormalizedEvent normalized)
    {
        QqDirectEventSubscription[] subscriptions;
        lock (_stateLock)
        {
            if (_closed)
            {
                return;
            }

            subscriptions = _subscriptions.Values.ToArray();
        }

        foreach (QqDirectEventSubscription subscription in subscriptions)
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
                Remove(subscription);
            }
        }
    }

    public bool HasSubscriptions
    {
        get
        {
            lock (_stateLock)
            {
                return _subscriptions.Count > 0;
            }
        }
    }

    public void Dispose()
    {
        QqDirectEventSubscription[] subscriptions;
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

        foreach (QqDirectEventSubscription subscription in subscriptions)
        {
            subscription.Complete();
        }
    }

    internal void Remove(QqDirectEventSubscription subscription)
    {
        lock (_stateLock)
        {
            if (_subscriptions.TryGetValue(
                    subscription.SubscriptionId,
                    out QqDirectEventSubscription? current)
                && ReferenceEquals(current, subscription))
            {
                _subscriptions.Remove(subscription.SubscriptionId);
            }
        }
    }

    private static QqDirectSubscriptionFilter ParseFilter(byte[] payload)
    {
        if (payload.Length == 0)
        {
            return new QqDirectSubscriptionFilter(null, null, null, null);
        }

        try
        {
            using JsonDocument document = JsonDocument.Parse(payload);
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                throw new QqDirectSubscriptionException(
                    "INVALID_REQUEST",
                    "subscription filter must be an object.");
            }

            string? eventType = null;
            string? conversationId = null;
            string? kind = null;
            string? requestKind = null;
            foreach (JsonProperty property in root.EnumerateObject())
            {
                if (property.Value.ValueKind != JsonValueKind.String)
                {
                    throw new QqDirectSubscriptionException(
                        "INVALID_REQUEST",
                        $"subscription filter {property.Name} must be text.");
                }

                string value = property.Value.GetString()?.Trim() ?? string.Empty;
                if (value.Length == 0)
                {
                    throw new QqDirectSubscriptionException(
                        "INVALID_REQUEST",
                        $"subscription filter {property.Name} must be non-empty text.");
                }

                switch (property.Name)
                {
                    case "event_type":
                        if (value is not (
                                QqDirectMessageMapper.InboundMessageEventType
                                or QqDirectMessageMapper.InboundRequestEventType))
                        {
                            throw new QqDirectSubscriptionException(
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
                            throw new QqDirectSubscriptionException(
                                "INVALID_REQUEST",
                                "subscription filter request_kind is unsupported.");
                        }

                        requestKind = value;
                        break;
                    default:
                        throw new QqDirectSubscriptionException(
                            "INVALID_REQUEST",
                            "subscription filter contains unknown fields.");
                }
            }

            return new QqDirectSubscriptionFilter(
                eventType,
                conversationId,
                kind,
                requestKind);
        }
        catch (JsonException)
        {
            throw new QqDirectSubscriptionException(
                "INVALID_REQUEST",
                "subscription filter is invalid JSON.");
        }
    }
}
