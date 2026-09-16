// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotRequestMapper.cs                                                │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: Canonical request approval JSON to OneBot v11 action mapping.      │
// │                                                                         │
// │  模块职责：将规范请求审批 JSON 映射为 OneBot v11 action                      │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;

namespace Cyrene.OneBot.V11.Core;

/// <summary>Validated OneBot request-approval action.</summary>
public sealed record OneBotRequestOperation(
    string Action,
    OneBotActionRequest Request,
    string RequestId,
    string RequestKind,
    string Decision);

/// <summary>Maps the existing message.connector.v1 request approval contract.</summary>
public static class OneBotRequestMapper
{
    public const string RespondRequestMethod = "respond_request";
    public const string RespondRequestTypeUrl =
        "type.cyrene.io/message.connector.v1.respond_request.request";
    public const string RespondResultTypeUrl =
        "type.cyrene.io/message.connector.v1.respond_request.response";
    public const int MaxPayloadBytes = 16 * 1024;

    public static OneBotRequestOperation Map(byte[] encoded)
    {
        if (encoded.Length > MaxPayloadBytes)
        {
            throw new OneBotRequestMappingException(
                "respond_request payload exceeds the 16 KiB limit.");
        }

        try
        {
            using JsonDocument document = JsonDocument.Parse(encoded);
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                throw new OneBotRequestMappingException(
                    "respond_request must be a JSON object.");
            }

            string? requestId = null;
            string? requestKind = null;
            string? decision = null;
            string comment = string.Empty;
            JsonElement vendorRequest = default;
            bool hasVendorRequest = false;
            foreach (JsonProperty property in root.EnumerateObject())
            {
                switch (property.Name)
                {
                    case "request_id":
                        requestId = RequiredText(property.Value, "request_id", 512);
                        break;
                    case "request_kind":
                        requestKind = RequiredText(property.Value, "request_kind", 64);
                        break;
                    case "decision":
                        decision = RequiredText(property.Value, "decision", 32);
                        break;
                    case "comment":
                        comment = RequiredText(property.Value, "comment", 2_048, allowEmpty: true);
                        break;
                    case "vendor_request":
                        if (property.Value.ValueKind != JsonValueKind.Object)
                        {
                            throw new OneBotRequestMappingException(
                                "vendor_request must be an object.");
                        }

                        vendorRequest = property.Value;
                        hasVendorRequest = true;
                        break;
                    default:
                        throw new OneBotRequestMappingException(
                            "respond_request contains unknown fields.");
                }
            }

            if (requestId is null || requestKind is null || decision is null)
            {
                throw new OneBotRequestMappingException(
                    "request_id, request_kind, and decision are required.");
            }

            if (requestKind is not ("friend" or "group_invite"))
            {
                throw new OneBotRequestMappingException(
                    "request_kind must be friend or group_invite.");
            }

            if (decision is not ("approve" or "reject"))
            {
                throw new OneBotRequestMappingException(
                    "decision must be approve or reject.");
            }

            Dictionary<string, string> vendorFields = hasVendorRequest
                ? ParseVendorRequest(vendorRequest)
                : new Dictionary<string, string>(StringComparer.Ordinal);
            string subType = vendorFields.TryGetValue("sub_type", out string? configuredSubType)
                ? configuredSubType
                : requestKind == "group_invite" ? "invite" : "add";
            if (subType is not ("add" or "invite"))
            {
                throw new OneBotRequestMappingException(
                    "vendor_request.sub_type must be add or invite.");
            }

            bool approved = decision == "approve";
            OneBotActionRequest request = requestKind == "friend"
                ? new OneBotActionRequest
                {
                    Flag = requestId,
                    Approve = approved,
                    Remark = comment.Length == 0 ? null : comment
                }
                : new OneBotActionRequest
                {
                    Flag = requestId,
                    SubType = subType,
                    Approve = approved,
                    Reason = comment.Length == 0 ? null : comment
                };
            string action = requestKind == "friend"
                ? "set_friend_add_request"
                : "set_group_add_request";
            return new OneBotRequestOperation(
                action,
                request,
                requestId,
                requestKind,
                decision);
        }
        catch (JsonException)
        {
            throw new OneBotRequestMappingException(
                "respond_request JSON is invalid.");
        }
    }

    private static Dictionary<string, string> ParseVendorRequest(JsonElement value)
    {
        Dictionary<string, string> fields = new(StringComparer.Ordinal);
        foreach (JsonProperty property in value.EnumerateObject())
        {
            if (property.Name is not ("sub_type" or "group_id" or "user_id"))
            {
                throw new OneBotRequestMappingException(
                    "vendor_request contains unknown fields.");
            }

            fields[property.Name] = RequiredText(
                property.Value,
                $"vendor_request.{property.Name}",
                512);
        }

        return fields;
    }

    private static string RequiredText(
        JsonElement value,
        string field,
        int maxBytes,
        bool allowEmpty = false)
    {
        if (value.ValueKind != JsonValueKind.String)
        {
            throw new OneBotRequestMappingException($"{field} must be text.");
        }

        string text = value.GetString() ?? string.Empty;
        if (!allowEmpty && text.Trim().Length == 0)
        {
            throw new OneBotRequestMappingException($"{field} must be non-empty text.");
        }

        if (System.Text.Encoding.UTF8.GetByteCount(text) > maxBytes)
        {
            throw new OneBotRequestMappingException(
                $"{field} exceeds {maxBytes} UTF-8 bytes.");
        }

        return allowEmpty ? text : text.Trim();
    }
}

/// <summary>Request approval JSON mapping failure.</summary>
public sealed class OneBotRequestMappingException : Exception
{
    public OneBotRequestMappingException(string message)
        : base(message)
    {
    }
}
