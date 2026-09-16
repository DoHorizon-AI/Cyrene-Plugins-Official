// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotMessageMapper.cs                                                │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: Canonical message.connector.v1 to OneBot v11 request mapping.     │
// │                                                                         │
// │  模块职责：将规范 message.connector.v1 映射为 OneBot v11 请求               │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using Cyrene.Message.Connector.V1;

namespace Cyrene.OneBot.V11.Core;

/// <summary>Canonical message mapping failure.</summary>
public sealed class OneBotMappingException : Exception
{
    public OneBotMappingException(string message)
        : base(message)
    {
    }
}

/// <summary>One validated OneBot action and its binding-local parameters.</summary>
public sealed record OneBotSendOperation(
    string Action,
    OneBotActionRequest Request);

/// <summary>
/// Maps only the canonical V1 message kinds supported by the Python baseline.
/// <para>仅映射 Python 基线已支持的 V1 规范消息类型。</para>
/// </summary>
/// <summary>Explicit canonical V1 to OneBot v11 message mapper.</summary>
public static class OneBotMessageMapper
{
    public const string CapabilityId = "message.connector.v1";
    public const string InterfaceVersion = "1";
    public const string SendMessageMethod = "send_message";
    public const string Vendor = "onebot.v11";
    public const string SendMessageRequestTypeUrl =
        "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest";
    public const string DeliveryResultTypeUrl =
        "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult";

    public static OneBotSendOperation MapSendMessage(
        SendMessageRequest request,
        OneBotProfile profile)
    {
        if (request.Conversation is null)
        {
            throw new OneBotMappingException("send_message conversation is required.");
        }

        ConversationScope conversation = request.Conversation;
        if (!string.Equals(conversation.Vendor, Vendor, StringComparison.Ordinal))
        {
            throw new OneBotMappingException("conversation.vendor must be onebot.v11.");
        }

        string accountId = RequiredIdentifier(conversation.AccountId, "conversation.account_id");
        if (profile.SelfAccountId is not null
            && !string.Equals(accountId, profile.SelfAccountId, StringComparison.Ordinal))
        {
            throw new OneBotMappingException(
                "conversation account does not match the configured instance.");
        }

        string conversationId = RequiredIdentifier(
            conversation.ConversationId,
            "conversation.conversation_id");
        ulong numericConversationId = NumericIdentifier(
            conversationId,
            "conversation.conversation_id");
        OneBotActionRequest actionRequest = new()
        {
            Message = BuildSegments(request)
        };

        return conversation.Kind switch
        {
            ConversationKind.Private => new OneBotSendOperation(
                "send_private_msg",
                actionRequest with { UserId = numericConversationId }),
            ConversationKind.Group => new OneBotSendOperation(
                "send_group_msg",
                actionRequest with { GroupId = numericConversationId }),
            _ => throw new OneBotMappingException("unsupported conversation kind.")
        };
    }

    public static string? ReadVendorMessageId(OneBotActionResponse response)
    {
        if (response.Data.ValueKind != JsonValueKind.Object
            || !response.Data.TryGetProperty("message_id", out JsonElement messageId))
        {
            return null;
        }

        return messageId.ValueKind switch
        {
            JsonValueKind.String =>
                string.IsNullOrWhiteSpace(messageId.GetString()) ? null : messageId.GetString(),
            JsonValueKind.Number when messageId.TryGetInt64(out long value) =>
                value.ToString(System.Globalization.CultureInfo.InvariantCulture),
            _ => throw new OneBotMappingException("OneBot response message_id is invalid.")
        };
    }

    private static List<OneBotMessageSegment> BuildSegments(SendMessageRequest request)
    {
        List<OneBotMessageSegment> segments = new();
        if (request.Reply is not null)
        {
            segments.Add(new OneBotMessageSegment
            {
                Type = "reply",
                Data = new OneBotSegmentData
                {
                    Id = RequiredIdentifier(request.Reply.MessageId, "reply.message_id")
                }
            });
        }

        foreach (MessageContentPart part in request.Content)
        {
            switch (part.KindCase)
            {
                case MessageContentPart.KindOneofCase.Text:
                    segments.Add(new OneBotMessageSegment
                    {
                        Type = "text",
                        Data = new OneBotSegmentData { Text = part.Text.Text }
                    });
                    break;
                case MessageContentPart.KindOneofCase.Mention:
                    segments.Add(new OneBotMessageSegment
                    {
                        Type = "at",
                        Data = new OneBotSegmentData
                        {
                            Qq = part.Mention.Target switch
                            {
                                MentionTarget.User => RequiredIdentifier(
                                    part.Mention.TargetId,
                                    "mention.target_id"),
                                MentionTarget.Everyone => "all",
                                _ => throw new OneBotMappingException(
                                    "unsupported mention target.")
                            }
                        }
                    });
                    break;
                case MessageContentPart.KindOneofCase.Image:
                    segments.Add(new OneBotMessageSegment
                    {
                        Type = "image",
                        Data = AttachmentData(part.Image.Reference, "image.reference")
                    });
                    break;
                case MessageContentPart.KindOneofCase.File:
                    OneBotSegmentData fileData = AttachmentData(
                        part.File.Reference,
                        "file.reference");
                    fileData.Name = string.IsNullOrWhiteSpace(part.File.FileName)
                        ? null
                        : part.File.FileName.Trim();
                    segments.Add(new OneBotMessageSegment
                    {
                        Type = "file",
                        Data = fileData
                    });
                    break;
                default:
                    throw new OneBotMappingException("message content kind is unsupported.");
            }
        }

        if (segments.Count == 0)
        {
            throw new OneBotMappingException("message content must not be empty.");
        }

        return segments;
    }

    private static OneBotSegmentData AttachmentData(
        AttachmentReference reference,
        string field)
    {
        return reference.LocationCase switch
        {
            AttachmentReference.LocationOneofCase.RemoteUri => new OneBotSegmentData
            {
                Url = ValidRemoteUri(reference.RemoteUri, $"{field}.remote_uri")
            },
            AttachmentReference.LocationOneofCase.VendorMedia => new OneBotSegmentData
            {
                File = RequiredIdentifier(
                    reference.VendorMedia.MediaId,
                    $"{field}.vendor_media.media_id")
            },
            _ => throw new OneBotMappingException($"{field} location is required.")
        };
    }

    private static string ValidRemoteUri(string value, string field)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out Uri? uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps)
            || string.IsNullOrEmpty(uri.Host))
        {
            throw new OneBotMappingException($"{field} must be an absolute HTTP(S) URL.");
        }

        return value;
    }

    private static ulong NumericIdentifier(string value, string field)
    {
        if (!ulong.TryParse(
                value,
                System.Globalization.NumberStyles.None,
                System.Globalization.CultureInfo.InvariantCulture,
                out ulong result)
            || result == 0)
        {
            throw new OneBotMappingException($"{field} must be a numeric OneBot id.");
        }

        return result;
    }

    private static string RequiredIdentifier(string value, string field)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Trim().Length > 128)
        {
            throw new OneBotMappingException($"{field} must be non-empty text.");
        }

        return value.Trim();
    }
}
