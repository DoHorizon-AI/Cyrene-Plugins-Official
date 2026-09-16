// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotEventNormalizer.cs                                              │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: OneBot v11 event to canonical message-connector normalization.     │
// │                                                                         │
// │  模块职责：将 OneBot v11 事件规范化为 message.connector canonical 载荷       │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using Cyrene.Message.Connector.V1;
using Google.Protobuf;

namespace Cyrene.OneBot.V11.Core;

/// <summary>One normalized event ready for the DirectPluginRuntime stream.</summary>
public sealed record OneBotNormalizedEvent(
    string EventType,
    string TypeUrl,
    byte[] Payload,
    string? ConversationId,
    string? Kind,
    string? RequestKind);

/// <summary>Malformed OneBot event data at the protocol boundary.</summary>
public sealed class OneBotEventNormalizationException : Exception
{
    public OneBotEventNormalizationException(string message)
        : base(message)
    {
    }
}

/// <summary>
/// Converts supported OneBot message and request events without reflection.
/// <para>不使用反射转换受支持的 OneBot 消息与请求事件。</para>
/// </summary>
public static class OneBotEventNormalizer
{
    public const string InboundMessageEventType = "inbound_message";
    public const string InboundRequestEventType = "inbound_request";
    public const string InboundMessageTypeUrl =
        "type.cyrene.io/cyrene.message.connector.v1.InboundMessagePayload";
    public const string InboundRequestTypeUrl =
        "type.cyrene.io/message.connector.v1.InboundRequestPayload";

    private const int MaxVendorFacts = 32;
    private const int MaxVendorFactNameBytes = 64;
    private const int MaxVendorFactValueBytes = 2_048;
    private const int MaxVendorFactTotalBytes = 8_192;

    public static OneBotNormalizedEvent? Normalize(
        JsonElement eventPayload,
        OneBotProfile profile)
    {
        if (eventPayload.ValueKind != JsonValueKind.Object)
        {
            throw new OneBotEventNormalizationException(
                "OneBot event must be a JSON object.");
        }

        if (!eventPayload.TryGetProperty("post_type", out JsonElement postType))
        {
            return null;
        }

        string type = RequiredText(postType, "post_type");
        return type switch
        {
            "message" => NormalizeMessage(eventPayload, profile),
            "request" => NormalizeRequest(eventPayload, profile),
            _ => null
        };
    }

    private static OneBotNormalizedEvent NormalizeMessage(
        JsonElement eventPayload,
        OneBotProfile profile)
    {
        string messageType = RequiredPropertyText(eventPayload, "message_type");
        if (messageType is not ("private" or "group"))
        {
            throw new OneBotEventNormalizationException(
                "unsupported OneBot message type.");
        }

        string selfAccountId = RequiredPropertyIdentifier(eventPayload, "self_id");
        if (profile.SelfAccountId is not null
            && !string.Equals(
                selfAccountId,
                profile.SelfAccountId,
                StringComparison.Ordinal))
        {
            throw new OneBotEventNormalizationException(
                "inbound event self account does not match configured instance.");
        }

        JsonElement sender = default;
        if (eventPayload.TryGetProperty("sender", out JsonElement senderValue))
        {
            if (senderValue.ValueKind != JsonValueKind.Object)
            {
                throw new OneBotEventNormalizationException("sender must be an object.");
            }

            sender = senderValue;
        }

        JsonElement senderIdValue;
        if (sender.ValueKind == JsonValueKind.Object
            && sender.TryGetProperty("user_id", out JsonElement nestedSenderId))
        {
            senderIdValue = nestedSenderId;
        }
        else
        {
            senderIdValue = RequiredProperty(eventPayload, "user_id");
        }

        string senderId = RequiredIdentifier(senderIdValue, "sender.user_id");
        string? groupId = OptionalPropertyIdentifier(eventPayload, "group_id");
        string conversationId = messageType == "group" ? groupId
            ?? throw new OneBotEventNormalizationException("group_id is required.")
            : senderId;

        JsonElement rawMessage = RequiredProperty(eventPayload, "message");
        if (rawMessage.ValueKind != JsonValueKind.Array)
        {
            throw new OneBotEventNormalizationException(
                "OneBot message must be an ordered list.");
        }

        InboundMessagePayload payload = new()
        {
            MessageId = RequiredPropertyIdentifier(eventPayload, "message_id"),
            Conversation = new ConversationScope
            {
                Vendor = OneBotMessageMapper.Vendor,
                AccountId = selfAccountId,
                ConversationId = conversationId,
                Kind = messageType == "private"
                    ? ConversationKind.Private
                    : ConversationKind.Group
            },
            SenderId = senderId,
            SenderDisplayName = SenderDisplayName(sender, senderId)
        };

        List<VendorFact> facts = new();
        string? replyId = null;
        int index = 0;
        foreach (JsonElement rawSegment in rawMessage.EnumerateArray())
        {
            JsonElement segment = RequireObject(rawSegment, $"message[{index}]");
            string segmentType = RequiredPropertyText(segment, "type");
            JsonElement data = OptionalObject(segment, "data", $"message[{index}].data");
            switch (segmentType)
            {
                case "text":
                    payload.Content.Add(new MessageContentPart
                    {
                        Text = new TextContent
                        {
                            Text = RequiredMessageText(data, "text")
                        }
                    });
                    break;
                case "at":
                    string target = RequiredPropertyIdentifier(data, "qq");
                    payload.Content.Add(new MessageContentPart
                    {
                        Mention = target == "all"
                            ? new MentionContent { Target = MentionTarget.Everyone }
                            : new MentionContent
                            {
                                Target = MentionTarget.User,
                                TargetId = target,
                                DisplayName = OptionalPropertyText(data, "name") ?? string.Empty
                            }
                    });
                    break;
                case "image":
                case "file":
                    payload.Content.Add(BuildAttachmentPart(
                        segmentType,
                        data,
                        selfAccountId));
                    break;
                case "reply":
                    if (replyId is not null)
                    {
                        throw new OneBotEventNormalizationException(
                            "OneBot event contains duplicate replies.");
                    }

                    replyId = RequiredPropertyIdentifier(data, "id");
                    break;
                default:
                    CollectVendorFacts(facts, index, segmentType, data);
                    break;
            }

            index++;
        }

        if (payload.Content.Count == 0 && replyId is null)
        {
            throw new OneBotEventNormalizationException(
                "OneBot event has no supported content.");
        }

        if (replyId is not null)
        {
            payload.Reply = new ReplyReference { MessageId = replyId };
        }

        if (facts.Count > 0)
        {
            payload.VendorExtension = new VendorExtension
            {
                Vendor = OneBotMessageMapper.Vendor
            };
            payload.VendorExtension.Facts.AddRange(facts);
        }

        return new OneBotNormalizedEvent(
            InboundMessageEventType,
            InboundMessageTypeUrl,
            payload.ToByteArray(),
            conversationId,
            messageType,
            null);
    }

    private static OneBotNormalizedEvent NormalizeRequest(
        JsonElement eventPayload,
        OneBotProfile profile)
    {
        string requestType = RequiredPropertyText(eventPayload, "request_type");
        string? subType = OptionalPropertyText(eventPayload, "sub_type");
        string requestKind;
        string normalizedSubType;
        if (requestType == "friend")
        {
            requestKind = "friend";
            normalizedSubType = "add";
        }
        else if (requestType == "group" && subType is "add" or "invite")
        {
            requestKind = "group_invite";
            normalizedSubType = subType;
        }
        else
        {
            throw new OneBotEventNormalizationException(
                "only friend and group add/invite requests are supported.");
        }

        string selfAccountId = RequiredPropertyIdentifier(eventPayload, "self_id");
        if (profile.SelfAccountId is not null
            && !string.Equals(
                selfAccountId,
                profile.SelfAccountId,
                StringComparison.Ordinal))
        {
            throw new OneBotEventNormalizationException(
                "inbound event self account does not match configured instance.");
        }

        string requestId = RequiredPropertyIdentifier(eventPayload, "flag");
        Dictionary<string, string> vendorRequest = new()
        {
            ["sub_type"] = normalizedSubType
        };
        AddOptionalIdentifier(eventPayload, vendorRequest, "group_id");
        AddOptionalIdentifier(eventPayload, vendorRequest, "user_id");
        OneBotInboundRequestPayload payload = new()
        {
            AccountId = selfAccountId,
            Vendor = OneBotMessageMapper.Vendor,
            RequestId = requestId,
            RequestKind = requestKind,
            VendorRequest = vendorRequest
        };

        return new OneBotNormalizedEvent(
            InboundRequestEventType,
            InboundRequestTypeUrl,
            JsonSerializer.SerializeToUtf8Bytes(
                payload,
                OneBotJsonContext.Default.OneBotInboundRequestPayload),
            null,
            null,
            requestKind);
    }

    private static MessageContentPart BuildAttachmentPart(
        string segmentType,
        JsonElement data,
        string accountId)
    {
        string? url = OptionalPropertyText(data, "url");
        string? file = OptionalPropertyText(data, "file");
        string source = (!string.IsNullOrEmpty(url) ? url : file)
            ?? throw new OneBotEventNormalizationException(
                $"OneBot {segmentType} segment has no attachment reference.");
        AttachmentReference reference = new();
        if (Uri.TryCreate(source, UriKind.Absolute, out Uri? uri)
            && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps)
            && !string.IsNullOrEmpty(uri.Host))
        {
            reference.RemoteUri = source;
        }
        else
        {
            reference.VendorMedia = new VendorMediaReference
            {
                Vendor = OneBotMessageMapper.Vendor,
                AccountId = accountId,
                MediaId = source
            };
        }

        if (segmentType == "image")
        {
            return new MessageContentPart
            {
                Image = new ImageContent { Reference = reference }
            };
        }

        return new MessageContentPart
        {
            File = new FileContent
            {
                Reference = reference,
                FileName = OptionalPropertyText(data, "name") ?? string.Empty
            }
        };
    }

    private static void CollectVendorFacts(
        List<VendorFact> facts,
        int index,
        string segmentType,
        JsonElement data)
    {
        AddVendorFact(facts, $"segment_{index}_type", segmentType);
        foreach (JsonProperty property in data.EnumerateObject())
        {
            if (property.Value.ValueKind is JsonValueKind.Object
                or JsonValueKind.Array
                or JsonValueKind.Null)
            {
                continue;
            }

            string? value = property.Value.ValueKind == JsonValueKind.String
                ? property.Value.GetString()
                : property.Value.GetRawText();
            if (value is not null)
            {
                AddVendorFact(facts, $"segment_{index}_{property.Name}", value);
            }
        }
    }

    private static void AddVendorFact(List<VendorFact> facts, string name, string value)
    {
        if (facts.Count >= MaxVendorFacts)
        {
            return;
        }

        string boundedName = TruncateUtf8(name, MaxVendorFactNameBytes);
        string boundedValue = TruncateUtf8(value, MaxVendorFactValueBytes);
        int proposed = facts.Sum(fact =>
                System.Text.Encoding.UTF8.GetByteCount(fact.Name)
                + System.Text.Encoding.UTF8.GetByteCount(fact.Value))
            + System.Text.Encoding.UTF8.GetByteCount(boundedName)
            + System.Text.Encoding.UTF8.GetByteCount(boundedValue);
        if (proposed > MaxVendorFactTotalBytes)
        {
            return;
        }

        facts.Add(new VendorFact { Name = boundedName, Value = boundedValue });
    }

    private static string TruncateUtf8(string value, int maxBytes)
    {
        if (System.Text.Encoding.UTF8.GetByteCount(value) <= maxBytes)
        {
            return value;
        }

        int length = value.Length;
        while (length > 0
            && System.Text.Encoding.UTF8.GetByteCount(value[..length]) > maxBytes)
        {
            length--;
        }

        return value[..length];
    }

    private static string SenderDisplayName(JsonElement sender, string senderId)
    {
        string? card = OptionalPropertyText(sender, "card");
        if (!string.IsNullOrEmpty(card))
        {
            return card;
        }

        string? nickname = OptionalPropertyText(sender, "nickname");
        return string.IsNullOrEmpty(nickname) ? senderId : nickname;
    }

    private static void AddOptionalIdentifier(
        JsonElement source,
        Dictionary<string, string> destination,
        string property)
    {
        string? value = OptionalPropertyIdentifier(source, property);
        if (value is not null)
        {
            destination[property] = value;
        }
    }

    private static JsonElement OptionalObject(
        JsonElement source,
        string property,
        string field)
    {
        if (!source.TryGetProperty(property, out JsonElement value))
        {
            using JsonDocument empty = JsonDocument.Parse("{}");
            return empty.RootElement.Clone();
        }

        return RequireObject(value, field);
    }

    private static JsonElement RequireObject(JsonElement value, string field)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw new OneBotEventNormalizationException($"{field} must be an object.");
        }

        return value;
    }

    private static JsonElement RequiredProperty(JsonElement source, string property)
    {
        if (!source.TryGetProperty(property, out JsonElement value))
        {
            throw new OneBotEventNormalizationException($"{property} is required.");
        }

        return value;
    }

    private static string RequiredPropertyText(JsonElement source, string property) =>
        RequiredText(RequiredProperty(source, property), property);

    private static string RequiredMessageText(JsonElement source, string property)
    {
        JsonElement value = RequiredProperty(source, property);
        if (value.ValueKind != JsonValueKind.String)
        {
            throw new OneBotEventNormalizationException($"{property} must be text.");
        }

        return value.GetString() ?? string.Empty;
    }

    private static string RequiredPropertyIdentifier(JsonElement source, string property) =>
        RequiredIdentifier(RequiredProperty(source, property), property);

    private static string? OptionalPropertyText(JsonElement source, string property)
    {
        return source.ValueKind == JsonValueKind.Object
            && source.TryGetProperty(property, out JsonElement value)
            && value.ValueKind == JsonValueKind.String
            ? value.GetString()?.Trim()
            : null;
    }

    private static string? OptionalPropertyIdentifier(JsonElement source, string property)
    {
        if (!source.TryGetProperty(property, out JsonElement value)
            || value.ValueKind == JsonValueKind.Null)
        {
            return null;
        }

        return RequiredIdentifier(value, property);
    }

    private static string RequiredText(JsonElement value, string field)
    {
        if (value.ValueKind != JsonValueKind.String)
        {
            throw new OneBotEventNormalizationException($"{field} must be text.");
        }

        string text = value.GetString()?.Trim() ?? string.Empty;
        if (text.Length == 0)
        {
            throw new OneBotEventNormalizationException($"{field} must be non-empty text.");
        }

        return text;
    }

    private static string RequiredIdentifier(JsonElement value, string field)
    {
        string? text = value.ValueKind switch
        {
            JsonValueKind.String => value.GetString(),
            JsonValueKind.Number when value.TryGetUInt64(out ulong number) =>
                number.ToString(System.Globalization.CultureInfo.InvariantCulture),
            _ => null
        };
        text = text?.Trim();
        if (string.IsNullOrEmpty(text))
        {
            throw new OneBotEventNormalizationException(
                $"{field} must be a string or integer.");
        }

        return text;
    }
}
