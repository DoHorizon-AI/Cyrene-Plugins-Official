// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqDirectMessageMapper.cs                                             │
// │  Namespace: Cyrene.Im.Core                                                │
// │  Role: QQ-native and canonical message/event mapping authority.           │
// │                                                                         │
// │  模块职责：QQ 原生消息、请求和 callback 与 canonical 合约之间的显式转换      │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Globalization;
using System.Text;
using System.Text.Json;
using Cyrene.Message.Connector.V1;
using Google.Protobuf;

namespace Cyrene.Im.Core;

/// <summary>Canonical constants used by the QQ message connector projection.</summary>
/// <remarks>中文：QQ 消息连接器投影使用的规范常量。</remarks>
public static class QqDirectMessageMapper
{
    public const string CapabilityId = "message.connector.v1";
    public const string InterfaceVersion = "1";
    public const string SendMessageMethod = "send_message";
    public const string RespondRequestMethod = "respond_request";
    public const string EventsMethod = "events";
    public const string Vendor = "qq";
    public const string SendMessageRequestTypeUrl =
        "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest";
    public const string DeliveryResultTypeUrl =
        "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult";
    public const string FilterTypeUrl =
        "type.cyrene.io/cyrene.message.connector.v1.Filter";
    public const string RespondRequestTypeUrl =
        "type.cyrene.io/message.connector.v1.respond_request.request";
    public const string RespondResultTypeUrl =
        "type.cyrene.io/message.connector.v1.respond_request.response";
    public const string InboundMessageEventType = "inbound_message";
    public const string InboundRequestEventType = "inbound_request";
    public const string InboundMessageTypeUrl =
        "type.cyrene.io/cyrene.message.connector.v1.InboundMessagePayload";
    public const string InboundRequestTypeUrl =
        "type.cyrene.io/message.connector.v1.InboundRequestPayload";
    public const string CallbackEventType = "qq_callback";
    public const string CallbackTypeUrl = "type.cyrene.io/qq.client.v1.Callback";

    private const int MaxVendorFacts = 32;
    private const int MaxVendorFactNameBytes = 64;
    private const int MaxVendorFactValueBytes = 2_048;
    private const int MaxVendorFactTotalBytes = 8_192;
    private const int MaxMediaReferenceBytes = 4 * 1_024;

    /// <summary>Maps a canonical send request to fixed native QQ parameters.</summary>
/// <remarks>中文：将规范发送请求映射为固定的原生 QQ 参数。</remarks>
    public static JsonElement BuildSendParameters(
        SendMessageRequest request,
        QqDirectProfile profile)
    {
        ConversationScope conversation = request.Conversation;
        if (conversation is null)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "send_message conversation is required.");
        }

        if (conversation.Vendor != Vendor)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "conversation.vendor must be qq.");
        }

        string accountId = RequiredIdentifier(
            conversation.AccountId,
            "conversation.account_id");
        if (profile.AccountId is not null && accountId != profile.AccountId)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "conversation account does not match the configured instance.");
        }

        QqNativePeer peer = new()
        {
            Kind = conversation.Kind switch
            {
                ConversationKind.Private => "private",
                ConversationKind.Group => "group",
                _ => throw new QqDirectMappingException(
                    "INVALID_REQUEST",
                    "unsupported conversation kind.")
            },
            ConversationId = RequiredIdentifier(
                conversation.ConversationId,
                "conversation.conversation_id")
        };
        ApplyPeerFacts(request, peer);

        QqNativeMessageParameters parameters = new() { Peer = peer };
        foreach (MessageContentPart part in request.Content)
        {
            parameters.Elements.Add(BuildNativeElement(part));
        }

        if (parameters.Elements.Count == 0)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "message content must not be empty.");
        }

        if (request.Reply is not null)
        {
            parameters.Reply = new QqNativeReply
            {
                MessageId = RequiredIdentifier(
                    request.Reply.MessageId,
                    "reply.message_id")
            };
        }

        return JsonSerializer.SerializeToElement(
            parameters,
            QqHostJsonContext.Default.QqNativeMessageParameters);
    }

    /// <summary>Reads a native send result into the canonical delivery payload.</summary>
/// <remarks>中文：将原生发送结果读取并转换为规范投递负载。</remarks>
    public static DeliveryResult BuildDeliveryResult(JsonElement result)
    {
        if (result.ValueKind != JsonValueKind.Object)
        {
            throw new QqDirectMappingException(
                "PROTOCOL_MISMATCH",
                "QQ send result must be an object.");
        }

        DeliveryResult delivery = new()
        {
            Status = DeliveryStatus.Accepted,
            VendorMessageId = RequiredPropertyIdentifier(result, "message_id")
        };
        delivery.VendorExtension = new VendorExtension { Vendor = Vendor };
        foreach (string name in new[]
                 {
                     "sequence",
                     "random",
                     "peer_uid",
                     "peer_uin",
                     "group_code",
                     "user_uid",
                     "user_uin"
                 })
        {
            if (result.TryGetProperty(name, out JsonElement value)
                && value.ValueKind is not JsonValueKind.Null)
            {
                AddFact(
                    delivery.VendorExtension.Facts,
                    $"qq_{name}",
                    RequiredIdentifier(value, $"result.{name}"));
            }
        }

        return delivery;
    }

    /// <summary>Maps the canonical JSON approval request to one fixed QQ operation.</summary>
/// <remarks>中文：将规范 JSON 审批请求映射为一个固定的 QQ 操作。</remarks>
    public static QqRespondOperation BuildRespondOperation(
        string json,
        QqDirectProfile profile)
    {
        QqRespondRequestDocument? request;
        try
        {
            request = JsonSerializer.Deserialize(
                json,
                QqHostJsonContext.Default.QqRespondRequestDocument);
        }
        catch (JsonException)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "respond_request must be valid UTF-8 JSON.");
        }

        if (request is null || request.UnknownFields is { Count: > 0 })
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "respond_request contains unknown or invalid fields.");
        }

        string requestId = RequiredText(request.RequestId, "request_id");
        string requestKind = RequiredText(request.RequestKind, "request_kind");
        if (requestKind is not ("friend" or "group_invite"))
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "unsupported request_kind.");
        }

        string decision = RequiredText(request.Decision, "decision");
        if (decision is not ("approve" or "reject"))
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "decision must be approve or reject.");
        }

        string comment = request.Comment ?? string.Empty;
        if (Encoding.UTF8.GetByteCount(comment) > 2_048)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "comment must be at most 2048 UTF-8 bytes.");
        }

        Dictionary<string, string> vendorRequest = request.VendorRequest ?? new();
        QqRespondParameters parameters = new()
        {
            RequestId = requestId,
            Approve = decision == "approve",
            Comment = comment,
            VendorRequest = vendorRequest
        };
        string operation = requestKind == "friend"
            ? "qq.friend.approve"
            : "qq.group.approve";
        JsonElement encoded = JsonSerializer.SerializeToElement(
            parameters,
            QqHostJsonContext.Default.QqRespondParameters);
        QqRespondResult response = new()
        {
            RequestId = requestId,
            RequestKind = requestKind,
            Decision = decision,
            AccountId = profile.AccountId ?? string.Empty
        };
        return new QqRespondOperation(operation, encoded, response);
    }

    /// <summary>Normalizes one current-generation native message event.</summary>
/// <remarks>中文：规范化当前代次的单个原生消息事件。</remarks>
    public static QqDirectNormalizedEvent NormalizeMessage(
        JsonElement payload,
        QqDirectProfile profile,
        int generation,
        string bindingId)
    {
        string accountId = RequiredPropertyIdentifier(payload, "account_id");
        EnsureAccount(profile, accountId, "native message account");
        JsonElement peer = RequiredObject(payload, "peer");
        string kind = RequiredPropertyText(peer, "kind");
        if (kind is not ("private" or "group"))
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "native message peer kind is unsupported.");
        }

        _ = RequiredPropertyIdentifier(peer, "peer_uid");
        string conversationId;
        if (kind == "group")
        {
            conversationId = RequiredPropertyIdentifier(peer, "group_code");
        }
        else
        {
            JsonElement userIdentity = peer.TryGetProperty(
                "user_uid",
                out JsonElement userUid)
                ? userUid
                : RequiredProperty(peer, "user_uin");
            conversationId = RequiredIdentifier(userIdentity, "peer.user_uid");
        }
        JsonElement sender = RequiredObject(payload, "sender");
        string? senderUid = OptionalPropertyIdentifier(sender, "uid");
        string? senderUin = OptionalPropertyIdentifier(sender, "uin");
        string senderId = senderUid ?? senderUin
            ?? throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "native message sender identity is missing.");

        InboundMessagePayload normalized = new()
        {
            MessageId = RequiredPropertyIdentifier(payload, "message_id"),
            Conversation = new ConversationScope
            {
                Vendor = Vendor,
                AccountId = accountId,
                ConversationId = conversationId,
                Kind = kind == "private"
                    ? ConversationKind.Private
                    : ConversationKind.Group
            },
            SenderId = senderId,
            SenderDisplayName = OptionalPropertyText(sender, "display_name") ?? senderId
        };

        List<VendorFact> facts = new();
        AddFact(facts, "qq_binding_id", bindingId);
        AddFact(facts, "qq_worker_generation", generation.ToString(CultureInfo.InvariantCulture));
        AddFact(facts, "qq_peer_uid", RequiredPropertyIdentifier(peer, "peer_uid"));
        AddOptionalFact(facts, peer, "qq_peer_uin", "peer_uin");
        AddOptionalFact(facts, peer, "qq_group_code", "group_code");
        AddOptionalFact(facts, peer, "qq_user_uid", "user_uid");
        AddOptionalFact(facts, peer, "qq_user_uin", "user_uin");
        AddOptionalFact(facts, sender, "qq_sender_uid", "uid");
        AddOptionalFact(facts, sender, "qq_sender_uin", "uin");
        AddOptionalFact(facts, payload, "qq_sequence", "sequence");
        AddOptionalFact(facts, payload, "qq_random", "random");
        AddOptionalFact(facts, payload, "qq_timestamp", "timestamp");

        JsonElement elements = RequiredArray(payload, "elements");
        string? replyId = null;
        foreach (JsonElement rawElement in elements.EnumerateArray())
        {
            JsonElement element = RequireObject(rawElement, "native message element");
            string type = RequiredPropertyText(element, "type");
            switch (type)
            {
                case "text":
                    normalized.Content.Add(new MessageContentPart
                    {
                        Text = new TextContent
                        {
                            Text = RequiredPropertyText(element, "text")
                        }
                    });
                    break;
                case "mention":
                    string target = OptionalPropertyText(element, "target") ?? string.Empty;
                    if (target == "everyone")
                    {
                        normalized.Content.Add(new MessageContentPart
                        {
                            Mention = new MentionContent
                            {
                                Target = MentionTarget.Everyone
                            }
                        });
                    }
                    else
                    {
                        normalized.Content.Add(new MessageContentPart
                        {
                            Mention = new MentionContent
                            {
                                Target = MentionTarget.User,
                                TargetId = RequiredPropertyIdentifier(element, "target_id"),
                                DisplayName = OptionalPropertyText(element, "display_name")
                                    ?? string.Empty
                            }
                        });
                    }

                    break;
                case "image":
                case "file":
                    normalized.Content.Add(BuildInboundAttachment(
                        type,
                        element,
                        accountId));
                    break;
                case "reply":
                    if (replyId is not null)
                    {
                        throw new QqDirectMappingException(
                            "INVALID_REQUEST",
                            "native message contains duplicate replies.");
                    }

                    replyId = RequiredPropertyIdentifier(element, "message_id");
                    break;
                default:
                    throw new QqDirectMappingException(
                        "UNSUPPORTED_OPERATION",
                        $"native message element type is not supported: {type}");
            }
        }

        if (normalized.Content.Count == 0 && replyId is null)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "native message has no supported content.");
        }

        if (replyId is not null)
        {
            normalized.Reply = new ReplyReference { MessageId = replyId };
        }

        normalized.VendorExtension = new VendorExtension { Vendor = Vendor };
        normalized.VendorExtension.Facts.Add(facts);
        return new QqDirectNormalizedEvent(
            InboundMessageEventType,
            InboundMessageTypeUrl,
            normalized.ToByteArray(),
            conversationId,
            kind,
            null);
    }

    /// <summary>Normalizes one current-generation native friend/group request.</summary>
/// <remarks>中文：规范化当前代次的单个原生好友／群组请求。</remarks>
    public static QqDirectNormalizedEvent NormalizeRequest(
        JsonElement payload,
        QqDirectProfile profile)
    {
        string accountId = RequiredPropertyIdentifier(payload, "account_id");
        EnsureAccount(profile, accountId, "request account");
        string requestKind = RequiredPropertyText(payload, "request_kind");
        if (requestKind is not ("friend" or "group_invite"))
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "unsupported native request kind.");
        }

        Dictionary<string, string> vendorRequest = new();
        foreach (string name in new[] { "group_id", "user_id", "sub_type" })
        {
            if (payload.TryGetProperty(name, out JsonElement value)
                && value.ValueKind is not JsonValueKind.Null)
            {
                vendorRequest[name] = RequiredIdentifier(value, name);
            }
        }

        QqInboundRequestPayload normalized = new()
        {
            AccountId = accountId,
            RequestId = RequiredPropertyIdentifier(payload, "request_id"),
            RequestKind = requestKind,
            VendorRequest = vendorRequest
        };
        return new QqDirectNormalizedEvent(
            InboundRequestEventType,
            InboundRequestTypeUrl,
            JsonSerializer.SerializeToUtf8Bytes(
                normalized,
                QqHostJsonContext.Default.QqInboundRequestPayload),
            null,
            null,
            requestKind);
    }

    /// <summary>Normalizes one correlated native completion callback.</summary>
/// <remarks>中文：规范化与请求相关联的单个原生完成回调。</remarks>
    public static QqDirectNormalizedEvent NormalizeCallback(
        string operation,
        string requestId,
        string? eventId,
        JsonElement payload)
    {
        QqCallbackPayload normalized = new()
        {
            Operation = operation,
            RequestId = RequiredText(requestId, "callback.request_id"),
            EventId = eventId is null ? null : RequiredIdentifier(eventId, "callback.event_id")
        };
        SetOptionalIdentifier(normalized, payload, "account_id", value => normalized.AccountId = value);
        SetOptionalIdentifier(normalized, payload, "message_id", value => normalized.MessageId = value);
        SetOptionalIdentifier(normalized, payload, "peer_uid", value => normalized.PeerUid = value);
        SetOptionalIdentifier(normalized, payload, "peer_uin", value => normalized.PeerUin = value);
        SetOptionalIdentifier(normalized, payload, "group_code", value => normalized.GroupCode = value);
        SetOptionalIdentifier(normalized, payload, "user_uid", value => normalized.UserUid = value);
        SetOptionalIdentifier(normalized, payload, "user_uin", value => normalized.UserUin = value);
        SetOptionalIdentifier(normalized, payload, "group_id", value => normalized.GroupId = value);
        SetOptionalIdentifier(normalized, payload, "user_id", value => normalized.UserId = value);
        SetOptionalIdentifier(normalized, payload, "member_uid", value => normalized.MemberUid = value);
        SetOptionalIdentifier(normalized, payload, "member_uin", value => normalized.MemberUin = value);
        SetOptionalIdentifier(normalized, payload, "media_id", value => normalized.MediaId = value);
        SetOptionalIdentifier(normalized, payload, "file_id", value => normalized.FileId = value);
        SetOptionalIdentifier(normalized, payload, "file_uuid", value => normalized.FileUuid = value);
        SetOptionalIdentifier(normalized, payload, "element_id", value => normalized.ElementId = value);
        SetOptionalIdentifier(normalized, payload, "sequence", value => normalized.Sequence = value);
        SetOptionalIdentifier(normalized, payload, "random", value => normalized.Random = value);
        if (payload.TryGetProperty("status", out JsonElement status))
        {
            normalized.Status = Scalar(status, "callback.status");
        }

        if (payload.TryGetProperty("progress", out JsonElement progress))
        {
            normalized.Progress = Scalar(progress, "callback.progress");
        }

        if (payload.TryGetProperty("remote_uri", out JsonElement remote)
            && remote.ValueKind is not JsonValueKind.Null)
        {
            normalized.RemoteUri = RemoteUri(remote, "callback.remote_uri");
        }

        if (payload.TryGetProperty("local_result_reference", out JsonElement local)
            && local.ValueKind is not JsonValueKind.Null)
        {
            string reference = RequiredText(local, "callback.local_result_reference");
            if (Encoding.UTF8.GetByteCount(reference) > MaxMediaReferenceBytes
                || !reference.StartsWith("qq://", StringComparison.Ordinal)
                    && !reference.StartsWith("staging://", StringComparison.Ordinal))
            {
                throw new QqDirectMappingException(
                    "INVALID_REQUEST",
                    "callback.local_result_reference must be binding-private.");
            }

            normalized.LocalResultReference = reference;
        }

        if (payload.TryGetProperty("error", out JsonElement error)
            && error.ValueKind is not JsonValueKind.Null)
        {
            JsonElement errorObject = RequireObject(error, "callback.error");
            normalized.Error = new QqCallbackError
            {
                Code = TruncateUtf8(
                    RequiredPropertyText(errorObject, "code"),
                    128),
                Message = TruncateUtf8(
                    RequiredPropertyText(errorObject, "message"),
                    512)
            };
        }

        return new QqDirectNormalizedEvent(
            QqDirectMessageMapper.CallbackEventType,
            CallbackTypeUrl,
            JsonSerializer.SerializeToUtf8Bytes(
                normalized,
                QqHostJsonContext.Default.QqCallbackPayload),
            null,
            null,
            null);
    }

    private static QqNativeElement BuildNativeElement(MessageContentPart part) =>
        part.KindCase switch
        {
            MessageContentPart.KindOneofCase.Text => new QqNativeElement
            {
                Type = "text",
                Text = RequiredText(part.Text.Text, "text.text")
            },
            MessageContentPart.KindOneofCase.Mention => BuildMention(part.Mention),
            MessageContentPart.KindOneofCase.Image => BuildAttachment(
                "image",
                part.Image.Reference),
            MessageContentPart.KindOneofCase.File => BuildAttachment(
                "file",
                part.File.Reference,
                part.File.FileName),
            _ => throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "message content kind is unsupported.")
        };

    private static QqNativeElement BuildMention(MentionContent mention)
    {
        return mention.Target switch
        {
            MentionTarget.Everyone => new QqNativeElement
            {
                Type = "mention",
                Target = "everyone"
            },
            MentionTarget.User => new QqNativeElement
            {
                Type = "mention",
                Target = "user",
                TargetId = RequiredIdentifier(mention.TargetId, "mention.target_id"),
                FileName = null
            },
            _ => throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "unsupported mention target.")
        };
    }

    private static QqNativeElement BuildAttachment(
        string type,
        AttachmentReference reference,
        string? fileName = null)
    {
        if (reference is null)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{type}.reference is required.");
        }

        QqNativeReference nativeReference = reference.LocationCase switch
        {
            AttachmentReference.LocationOneofCase.RemoteUri => new QqNativeReference
            {
                RemoteUri = RemoteUri(reference.RemoteUri, $"{type}.reference.remote_uri")
            },
            AttachmentReference.LocationOneofCase.VendorMedia =>
                BuildVendorMediaReference(type, reference.VendorMedia),
            _ => throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{type}.reference is empty.")
        };
        return new QqNativeElement
        {
            Type = type,
            Reference = nativeReference,
            FileName = type == "file" ? fileName ?? string.Empty : null
        };
    }

    private static QqNativeReference BuildVendorMediaReference(
        string type,
        VendorMediaReference media)
    {
        if (media.Vendor != Vendor)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{type}.reference.vendor_media.vendor must be qq.");
        }

        return new QqNativeReference
        {
            Vendor = Vendor,
            AccountId = RequiredIdentifier(media.AccountId, $"{type}.reference.account_id"),
            MediaId = RequiredIdentifier(media.MediaId, $"{type}.reference.media_id")
        };
    }

    private static MessageContentPart BuildInboundAttachment(
        string type,
        JsonElement element,
        string accountId)
    {
        string? remote = OptionalPropertyText(element, "remote_uri");
        AttachmentReference reference;
        if (remote is not null)
        {
            reference = new AttachmentReference { RemoteUri = RemoteUri(remote, "remote_uri") };
        }
        else
        {
            JsonElement media = element.TryGetProperty("media_id", out JsonElement mediaIdValue)
                ? mediaIdValue
                : RequiredProperty(element, "file_id");
            string mediaId = RequiredIdentifier(media, "media_id");
            reference = new AttachmentReference
            {
                VendorMedia = new VendorMediaReference
                {
                    Vendor = Vendor,
                    AccountId = accountId,
                    MediaId = mediaId
                }
            };
        }

        return type == "image"
            ? new MessageContentPart
            {
                Image = new ImageContent
                {
                    Reference = reference,
                    MimeType = OptionalPropertyText(element, "mime_type") ?? string.Empty
                }
            }
            : new MessageContentPart
            {
                File = new FileContent
                {
                    Reference = reference,
                    FileName = OptionalPropertyText(element, "file_name") ?? string.Empty,
                    MimeType = OptionalPropertyText(element, "mime_type") ?? string.Empty
                }
            };
    }

    private static void ApplyPeerFacts(SendMessageRequest request, QqNativePeer peer)
    {
        if (request.VendorExtension is null)
        {
            return;
        }

        if (request.VendorExtension.Vendor != Vendor)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                "vendor_extension.vendor must be qq.");
        }

        HashSet<string> mappedFacts = new(StringComparer.Ordinal);
        foreach (VendorFact fact in request.VendorExtension.Facts)
        {
            string? nativeField = fact.Name switch
            {
                "qq_peer_uid" => nameof(QqNativePeer.PeerUid),
                "qq_peer_uin" => nameof(QqNativePeer.PeerUin),
                "qq_group_code" => nameof(QqNativePeer.GroupCode),
                "qq_user_uid" => nameof(QqNativePeer.UserUid),
                "qq_user_uin" => nameof(QqNativePeer.UserUin),
                _ => null
            };
            if (nativeField is null)
            {
                continue;
            }

            if (!mappedFacts.Add(nativeField))
            {
                throw new QqDirectMappingException(
                    "INVALID_REQUEST",
                    $"duplicate QQ identity fact {fact.Name}.");
            }

            string value = RequiredIdentifier(
                fact.Value,
                $"vendor_extension.facts.{fact.Name}");
            switch (nativeField)
            {
                case nameof(QqNativePeer.PeerUid):
                    peer.PeerUid = value;
                    break;
                case nameof(QqNativePeer.PeerUin):
                    peer.PeerUin = value;
                    break;
                case nameof(QqNativePeer.GroupCode):
                    peer.GroupCode = value;
                    break;
                case nameof(QqNativePeer.UserUid):
                    peer.UserUid = value;
                    break;
                case nameof(QqNativePeer.UserUin):
                    peer.UserUin = value;
                    break;
            }
        }
    }

    private static void EnsureAccount(QqDirectProfile profile, string accountId, string subject)
    {
        if (profile.AccountId is not null && accountId != profile.AccountId)
        {
            throw new QqDirectMappingException(
                "ACCOUNT_MISMATCH",
                $"{subject} does not match binding.");
        }
    }

    private static void AddOptionalFact(
        List<VendorFact> facts,
        JsonElement source,
        string factName,
        string property)
    {
        if (source.TryGetProperty(property, out JsonElement value)
            && value.ValueKind is not JsonValueKind.Null)
        {
            AddFact(facts, factName, RequiredIdentifier(value, property));
        }
    }

    private static void AddFact(ICollection<VendorFact> facts, string name, string value)
    {
        if (facts.Count >= MaxVendorFacts)
        {
            return;
        }

        string boundedName = TruncateUtf8(name, MaxVendorFactNameBytes);
        string boundedValue = TruncateUtf8(value, MaxVendorFactValueBytes);
        int total = facts.Sum(fact =>
            Encoding.UTF8.GetByteCount(fact.Name)
            + Encoding.UTF8.GetByteCount(fact.Value));
        if (total
            + Encoding.UTF8.GetByteCount(boundedName)
            + Encoding.UTF8.GetByteCount(boundedValue) > MaxVendorFactTotalBytes)
        {
            return;
        }

        facts.Add(new VendorFact { Name = boundedName, Value = boundedValue });
    }

    private static void SetOptionalIdentifier(
        QqCallbackPayload target,
        JsonElement source,
        string property,
        Action<string> setter)
    {
        if (source.TryGetProperty(property, out JsonElement value)
            && value.ValueKind is not JsonValueKind.Null)
        {
            setter(RequiredIdentifier(value, $"callback.{property}"));
        }
    }

    private static JsonElement Scalar(JsonElement value, string field)
    {
        if (value.ValueKind is not (JsonValueKind.String
            or JsonValueKind.Number
            or JsonValueKind.True
            or JsonValueKind.False))
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{field} must be a scalar.");
        }

        return value.Clone();
    }

    private static string RemoteUri(JsonElement value, string field) =>
        RemoteUri(RequiredText(value, field), field);

    private static string RemoteUri(string value, string field)
    {
        if (Encoding.UTF8.GetByteCount(value) > MaxMediaReferenceBytes
            || !Uri.TryCreate(value, UriKind.Absolute, out Uri? uri)
            || uri is null
            || uri.Host.Length == 0
            || uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{field} must be a bounded http(s) URI.");
        }

        return value;
    }

    private static JsonElement RequiredArray(JsonElement source, string property)
    {
        JsonElement value = RequiredProperty(source, property);
        if (value.ValueKind != JsonValueKind.Array)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{property} must be an array.");
        }

        return value;
    }

    private static JsonElement RequiredObject(JsonElement source, string property) =>
        RequireObject(RequiredProperty(source, property), property);

    private static JsonElement RequireObject(JsonElement value, string field)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{field} must be an object.");
        }

        return value;
    }

    private static JsonElement RequiredProperty(JsonElement source, string property)
    {
        if (!source.TryGetProperty(property, out JsonElement value))
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{property} is required.");
        }

        return value;
    }

    private static string RequiredPropertyText(JsonElement source, string property) =>
        RequiredText(RequiredProperty(source, property), property);

    private static string? OptionalPropertyText(JsonElement source, string property) =>
        source.TryGetProperty(property, out JsonElement value)
            && value.ValueKind == JsonValueKind.String
            ? value.GetString()?.Trim()
            : null;

    private static string RequiredText(string? value, string field)
    {
        string text = value?.Trim() ?? string.Empty;
        if (text.Length == 0)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{field} must be non-empty text.");
        }

        return text;
    }

    private static string RequiredText(JsonElement value, string field)
    {
        if (value.ValueKind != JsonValueKind.String)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{field} must be text.");
        }

        return RequiredText(value.GetString(), field);
    }

    private static string RequiredIdentifier(string? value, string field) =>
        RequiredText(value, field);

    private static string RequiredIdentifier(JsonElement value, string field)
    {
        string? result = value.ValueKind switch
        {
            JsonValueKind.String => value.GetString(),
            JsonValueKind.Number when value.TryGetUInt64(out ulong number) =>
                number.ToString(CultureInfo.InvariantCulture),
            _ => null
        };
        return RequiredIdentifier(result, field);
    }

    private static string RequiredPropertyIdentifier(JsonElement source, string property) =>
        RequiredIdentifier(RequiredProperty(source, property), property);

    private static string? OptionalPropertyIdentifier(JsonElement source, string property) =>
        source.TryGetProperty(property, out JsonElement value)
            && value.ValueKind is not JsonValueKind.Null
            ? RequiredIdentifier(value, property)
            : null;

    private static string TruncateUtf8(string value, int maxBytes)
    {
        if (Encoding.UTF8.GetByteCount(value) <= maxBytes)
        {
            return value;
        }

        int length = value.Length;
        while (length > 0
            && Encoding.UTF8.GetByteCount(value[..length]) > maxBytes)
        {
            length--;
        }

        return value[..length];
    }
}

/// <summary>Mapping failure safe to expose at the direct runtime boundary.</summary>
/// <remarks>中文：可在 Direct runtime 边界安全公开的映射失败。</remarks>
public sealed class QqDirectMappingException : Exception
{
    public QqDirectMappingException(string domainCode, string message)
        : base(message)
    {
        DomainCode = domainCode;
    }

    public string DomainCode { get; }
}

/// <summary>Fixed operation and result for canonical request approval.</summary>
/// <remarks>中文：规范请求审批操作及其固定结果。</remarks>
public sealed record QqRespondOperation(
    string Operation,
    JsonElement Parameters,
    QqRespondResult Result);
