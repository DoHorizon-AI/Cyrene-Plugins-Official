// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqHostOperationValidator.cs                                          │
// │  Namespace: Cyrene.Im.Core                                                │
// │  Role: Bounded public parameter/result validation for QQ operations.     │
// │                                                                         │
// │  模块职责：在 QQ Host IPC 两侧执行固定 operation 的参数与结果边界校验      │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Globalization;
using System.Text;
using System.Text.Json;

namespace Cyrene.Im.Core;

/// <summary>
/// Validates the closed JSON boundary for the public <c>qq.client.v1</c>
/// operation projection.
/// <para>
/// The native Host owns version-specific overload details. This validator
/// prevents unrelated fields, unsafe primitive values, unbounded structures,
/// and credential-bearing results from crossing the stable IPC boundary.
/// </para>
/// </summary>
public static class QqHostOperationValidator
{
    public const int MaxParameterFields = 128;
    public const int MaxCollectionItems = 4_096;
    public const int MaxParameterDepth = 8;
    public const int MaxParameterStringBytes = 64 * 1_024;
    public const int MaxResultReferenceBytes = 4 * 1_024;

    private static readonly HashSet<string> ReservedFields = new(StringComparer.Ordinal)
    {
        "service",
        "method",
        "raw_payload",
        "binding_id",
        "generation"
    };

    private static readonly HashSet<string> IdentifierFields = new(StringComparer.Ordinal)
    {
        "account_id",
        "uin",
        "uid",
        "user_id",
        "user_uid",
        "user_uin",
        "peer_uid",
        "conversation_id",
        "group_id",
        "group_code",
        "message_id",
        "request_id",
        "device_id",
        "like_id",
        "target_id",
        "member_uid",
        "member_uin",
        "file_id",
        "media_id",
        "folder_id",
        "file_uuid",
        "model_id",
        "element_id",
        "notify_id",
        "session_id",
        "login_id",
        "source_id"
    };

    private static readonly HashSet<string> NonNegativeIntegerFields =
        new(StringComparer.Ordinal)
        {
            "sequence",
            "random",
            "timestamp",
            "count",
            "offset",
            "page",
            "page_size",
            "start_time",
            "end_time"
        };

    private static readonly HashSet<string> NonNegativeNumberFields =
        new(StringComparer.Ordinal)
        {
            "duration_seconds",
            "duration"
        };

    private static readonly HashSet<string> PositiveNumberFields = new(StringComparer.Ordinal)
    {
        "poll_interval_seconds"
    };

    private static readonly HashSet<string> BooleanFields = new(StringComparer.Ordinal)
    {
        "approve",
        "download",
        "short_link"
    };

    private static readonly HashSet<string> StringFields = new(StringComparer.Ordinal)
    {
        "comment",
        "request_kind",
        "sub_type",
        "scope",
        "query",
        "keywords",
        "name",
        "remark",
        "nickname",
        "long_nick",
        "birthday",
        "gender",
        "header",
        "status",
        "like_type",
        "file_name",
        "mime_type",
        "media_type",
        "codec",
        "local_result_reference",
        "secret_ref",
        "login_policy",
        "platform",
        "data_dir",
        "client_version",
        "qr_code",
        "folder_name",
        "role"
    };

    private static readonly HashSet<string> StringListFields = new(StringComparer.Ordinal)
    {
        "events"
    };

    private static readonly HashSet<string> IdentifierListFields = new(StringComparer.Ordinal)
    {
        "message_ids"
    };

    private static readonly HashSet<string> ObjectListFields = new(StringComparer.Ordinal)
    {
        "elements",
        "messages"
    };

    private static readonly HashSet<string> ObjectFields = new(StringComparer.Ordinal)
    {
        "peer",
        "source",
        "destination",
        "attributes",
        "reply",
        "message",
        "filter",
        "profile",
        "vendor_request",
        "permissions"
    };

    private static readonly Dictionary<string, IReadOnlySet<string>> OperationFields =
        new Dictionary<string, IReadOnlySet<string>>(StringComparer.Ordinal)
        {
            ["qq.session.create"] = Fields(
                "account_id", "platform", "client_version", "data_dir", "login_policy", "session_id"),
            ["qq.session.init"] = Fields(
                "account_id", "platform", "client_version", "data_dir", "login_policy", "session_id"),
            ["qq.session.start_nt"] = Fields("account_id", "login_policy", "session_id"),
            ["qq.login.connect"] = Fields("account_id", "uin", "uid", "login_id"),
            ["qq.login.online"] = Fields("account_id", "login_id"),
            ["qq.login.offline"] = Fields("account_id", "login_id"),
            ["qq.login.list"] = Fields("account_id"),
            ["qq.login.quick"] = Fields("account_id", "uin", "login_id"),
            ["qq.login.password"] = Fields("account_id", "uin", "secret_ref"),
            ["qq.login.qr"] = Fields("account_id", "login_id"),
            ["qq.login.poll"] = Fields(
                "account_id", "login_id", "qr_code", "poll_interval_seconds"),
            ["qq.login.self_status"] = Fields("account_id"),
            ["qq.account.core"] = Fields("account_id", "uid", "uin", "user_uid", "user_uin"),
            ["qq.account.simple"] = Fields("account_id", "uid", "uin", "user_uid", "user_uin"),
            ["qq.message.subscribe"] = Fields("account_id", "events", "filter"),
            ["qq.message.send"] = Fields(
                "account_id", "peer", "elements", "attributes", "reply", "message"),
            ["qq.message.send_completion"] = Fields(),
            ["qq.peer.uid_by_uin"] = Fields("account_id", "uin", "user_uin"),
            ["qq.peer.uin_by_uid"] = Fields("account_id", "uid", "user_uid"),
            ["qq.peer.uid"] = Fields("account_id", "uin", "user_uin"),
            ["qq.peer.uin"] = Fields("account_id", "uid", "user_uid"),
            ["qq.message.history_include_self"] = Fields(
                "account_id", "peer", "offset", "count", "page", "page_size"),
            ["qq.message.history_by_seq"] = Fields(
                "account_id", "peer", "sequence", "count", "offset"),
            ["qq.message.by_id"] = Fields("account_id", "peer", "message_id"),
            ["qq.message.single"] = Fields("account_id", "peer", "message_id"),
            ["qq.message.search"] = Fields(
                "account_id", "peer", "filter", "query", "offset", "count", "page", "page_size",
                "start_time", "end_time"),
            ["qq.message.recall"] = Fields(
                "account_id", "peer", "message_id", "sequence", "random"),
            ["qq.message.forward"] = Fields(
                "account_id", "source", "destination", "message_id"),
            ["qq.message.forward_comment"] = Fields(
                "account_id", "source", "destination", "message_id", "comment"),
            ["qq.message.multi_forward"] = Fields(
                "account_id", "source", "destination", "messages", "message_ids"),
            ["qq.message.read"] = Fields(
                "account_id", "peer", "message_id", "message_ids", "sequence"),
            ["qq.message.read_all"] = Fields("account_id"),
            ["qq.message.emoji_likes"] = Fields(
                "account_id", "peer", "message_id", "like_id", "like_type"),
            ["qq.message.emoji_likes_list"] = Fields(
                "account_id", "peer", "message_id", "like_id", "like_type"),
            ["qq.group.list"] = Fields(
                "account_id", "offset", "count", "page", "page_size"),
            ["qq.group.detail"] = Fields("account_id", "group_id", "group_code"),
            ["qq.group.members"] = Fields(
                "account_id", "group_id", "group_code", "offset", "count", "page", "page_size"),
            ["qq.group.member"] = Fields(
                "account_id", "group_id", "group_code", "member_uid", "member_uin"),
            ["qq.friend.list"] = Fields(
                "account_id", "offset", "count", "page", "page_size"),
            ["qq.friend.cached"] = Fields(
                "account_id", "offset", "count", "page", "page_size"),
            ["qq.friend.requests"] = Fields(
                "account_id", "offset", "count", "page", "page_size"),
            ["qq.media.element"] = Fields(
                "account_id", "peer", "message_id", "element_id", "media_id", "media_type"),
            ["qq.media.download"] = Fields(
                "account_id", "peer", "message_id", "element_id", "media_id", "media_type",
                "download", "model_id", "file_uuid", "local_result_reference"),
            ["qq.media.video_url"] = Fields(
                "account_id", "peer", "message_id", "element_id", "media_id", "codec", "download"),
            ["qq.media.download_complete"] = Fields(),
            ["qq.file.list"] = Fields(
                "account_id", "group_id", "folder_id", "offset", "count", "page", "page_size"),
            ["qq.file.search"] = Fields(
                "account_id", "group_id", "folder_id", "query", "file_name", "offset", "count",
                "page", "page_size"),
            ["qq.file.download"] = Fields(
                "account_id", "group_id", "file_id", "file_uuid", "file_name", "local_result_reference"),
            ["qq.file.forward"] = Fields(
                "account_id", "group_id", "file_id", "file_uuid", "source", "destination"),
            ["qq.file.save"] = Fields(
                "account_id", "group_id", "file_id", "file_uuid", "file_name", "folder_id"),
            ["qq.group.modify_name"] = Fields("account_id", "group_id", "group_code", "name"),
            ["qq.group.modify_remark"] = Fields("account_id", "group_id", "group_code", "remark"),
            ["qq.group.mute_member"] = Fields(
                "account_id", "group_id", "group_code", "member_uid", "member_uin",
                "duration_seconds", "duration"),
            ["qq.group.mute"] = Fields(
                "account_id", "group_id", "group_code", "duration_seconds", "duration"),
            ["qq.group.kick"] = Fields(
                "account_id", "group_id", "group_code", "member_uid", "member_uin", "user_id", "comment"),
            ["qq.group.quit"] = Fields("account_id", "group_id", "group_code"),
            ["qq.group.approve"] = Fields(
                "account_id", "request_id", "group_id", "group_code", "user_id", "approve", "comment",
                "sub_type", "notify_id", "vendor_request"),
            ["qq.friend.approve"] = Fields(
                "account_id", "request_id", "uid", "uin", "user_id", "approve", "comment", "vendor_request"),
            ["qq.friend.approve_doubt"] = Fields(
                "account_id", "request_id", "uid", "uin", "user_id", "approve", "comment", "vendor_request"),
            ["qq.friend.doubt_requests"] = Fields(
                "account_id", "offset", "count", "page", "page_size"),
            ["qq.friend.add"] = Fields("account_id", "uid", "uin", "user_id", "comment"),
            ["qq.friend.delete"] = Fields("account_id", "uid", "uin", "user_id"),
            ["qq.friend.set_remark"] = Fields("account_id", "uid", "uin", "user_id", "remark"),
            ["qq.profile.modify"] = Fields("account_id", "profile"),
            ["qq.profile.nickname"] = Fields("account_id", "nickname"),
            ["qq.profile.long_nick"] = Fields("account_id", "long_nick"),
            ["qq.profile.birthday"] = Fields("account_id", "birthday"),
            ["qq.profile.gender"] = Fields("account_id", "gender"),
            ["qq.profile.header"] = Fields("account_id", "header"),
            ["qq.search.stranger"] = SearchFields(),
            ["qq.search.group"] = SearchFields(),
            ["qq.search.contact"] = SearchFields(),
            ["qq.search.message"] = SearchFields(),
            ["qq.search.file"] = SearchFields(),
            ["qq.online.status"] = Fields("account_id", "status"),
            ["qq.online.devices"] = Fields("account_id", "device_id"),
            ["qq.online.likes"] = Fields("account_id", "target_id", "like_id", "like_type"),
            ["qq.online.set_like"] = Fields("account_id", "target_id", "like_id", "like_type"),
            ["qq.online.check_like"] = Fields("account_id", "target_id", "like_id", "like_type")
        };

    private static readonly string[] SensitiveMarkers =
    {
        "password",
        "token",
        "secret",
        "ticket",
        "cookie"
    };

    public static void ValidateParameters(string operation, JsonElement parameters)
    {
        QqHostOperation operationSpec = GetOperation(operation);
        if (!operationSpec.Requestable)
        {
            throw InvalidRequest($"QQ operation {operation} is callback-only");
        }

        if (parameters.ValueKind != JsonValueKind.Object)
        {
            throw InvalidRequest("QQ operation params must be an object");
        }

        if (parameters.EnumerateObject().Count() > MaxParameterFields)
        {
            throw InvalidRequest("QQ operation params have too many fields");
        }

        IReadOnlySet<string> allowed = GetAllowedFields(operationSpec);
        foreach (JsonProperty property in parameters.EnumerateObject())
        {
            if (ReservedFields.Contains(property.Name))
            {
                throw InvalidRequest("QQ operation params contain reserved fields");
            }

            if (!allowed.Contains(property.Name))
            {
                throw InvalidRequest(
                    $"QQ operation params contain undeclared fields: {property.Name}");
            }

            ValidateParameterValue(property.Name, property.Value);
        }
    }

    public static void ValidateResult(string operation, JsonElement result)
    {
        QqHostOperation operationSpec = GetOperation(operation);
        if (!operationSpec.Requestable)
        {
            throw ProtocolMismatch($"QQ operation {operation} is callback-only");
        }

        if (result.ValueKind != JsonValueKind.Object)
        {
            throw ProtocolMismatch($"QQ operation result for {operation} must be an object");
        }

        try
        {
            ValidateJsonValue(result, "result", 0, allowNull: true);
        }
        catch (QqHostException exception) when (exception.Code == "INVALID_REQUEST")
        {
            throw ProtocolMismatch(exception.Message);
        }
        RejectSensitiveFields(result, "result");
        if (result.TryGetProperty("operation", out JsonElement reportedOperation)
            && (reportedOperation.ValueKind != JsonValueKind.String
                || reportedOperation.GetString() != operation))
        {
            throw ProtocolMismatch(
                $"QQ operation result operation does not match {operation}");
        }

        if (operation == "qq.message.send"
            && (!result.TryGetProperty("message_id", out JsonElement messageId)
                || !IsIdentifier(messageId)))
        {
            throw ProtocolMismatch(
                "QQ operation result for qq.message.send must contain message_id");
        }

        if (operationSpec.Mapping is "media" or "file")
        {
            ValidateResultReferences(result);
        }
    }

    private static QqHostOperation GetOperation(string operation)
    {
        if (!QqHostOperationRegistry.TryGet(operation, out QqHostOperation? operationSpec))
        {
            throw InvalidRequest($"unsupported QQ operation {operation}");
        }

        return operationSpec;
    }

    private static IReadOnlySet<string> GetAllowedFields(QqHostOperation operation)
    {
        if (OperationFields.TryGetValue(operation.Name, out IReadOnlySet<string>? fields))
        {
            return fields;
        }

        throw InvalidRequest($"QQ operation {operation.Name} has no parameter schema");
    }

    private static void ValidateParameterValue(string field, JsonElement value)
    {
        ValidateJsonValue(value, field, 0, allowNull: false);
        if (IdentifierFields.Contains(field))
        {
            if (!IsIdentifier(value))
            {
                throw InvalidRequest(
                    $"QQ operation parameter {field} must be a string or integer");
            }
        }
        else if (NonNegativeIntegerFields.Contains(field))
        {
            if (!IsInteger(value) || value.GetInt64() < 0)
            {
                throw InvalidRequest(
                    $"QQ operation parameter {field} must be a non-negative integer");
            }
        }
        else if (NonNegativeNumberFields.Contains(field))
        {
            if (!TryGetFiniteNumber(value, out double number) || number < 0)
            {
                throw InvalidRequest(
                    $"QQ operation parameter {field} must be a non-negative number");
            }
        }
        else if (PositiveNumberFields.Contains(field))
        {
            if (!TryGetFiniteNumber(value, out double number) || number <= 0)
            {
                throw InvalidRequest(
                    $"QQ operation parameter {field} must be a positive number");
            }
        }
        else if (BooleanFields.Contains(field))
        {
            if (value.ValueKind != JsonValueKind.True && value.ValueKind != JsonValueKind.False)
            {
                throw InvalidRequest(
                    $"QQ operation parameter {field} must be boolean");
            }
        }
        else if (StringFields.Contains(field))
        {
            if (value.ValueKind != JsonValueKind.String)
            {
                throw InvalidRequest($"QQ operation parameter {field} must be text");
            }

            if (field == "comment" && value.GetString()?.Length > 2_048)
            {
                throw InvalidRequest(
                    "QQ operation parameter comment exceeds 2048 characters");
            }
        }
        else if (StringListFields.Contains(field))
        {
            ValidateStringList(field, value);
        }
        else if (IdentifierListFields.Contains(field))
        {
            if (value.ValueKind != JsonValueKind.Array)
            {
                throw InvalidRequest($"QQ operation parameter {field} must be a list");
            }

            foreach (JsonElement item in value.EnumerateArray())
            {
                if (!IsIdentifier(item))
                {
                    throw InvalidRequest(
                        $"QQ operation parameter {field} must contain identifiers");
                }
            }
        }
        else if (ObjectListFields.Contains(field))
        {
            if (value.ValueKind != JsonValueKind.Array)
            {
                throw InvalidRequest(
                    $"QQ operation parameter {field} must be a list of objects");
            }

            foreach (JsonElement item in value.EnumerateArray())
            {
                if (item.ValueKind != JsonValueKind.Object)
                {
                    throw InvalidRequest(
                        $"QQ operation parameter {field} must be a list of objects");
                }
            }
        }
        else if (ObjectFields.Contains(field) && value.ValueKind != JsonValueKind.Object)
        {
            throw InvalidRequest($"QQ operation parameter {field} must be an object");
        }
    }

    private static void ValidateStringList(string field, JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Array)
        {
            throw InvalidRequest(
                $"QQ operation parameter {field} must be a list of non-empty text");
        }

        foreach (JsonElement item in value.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.String
                || string.IsNullOrEmpty(item.GetString()))
            {
                throw InvalidRequest(
                    $"QQ operation parameter {field} must be a list of non-empty text");
            }
        }
    }

    private static void ValidateJsonValue(
        JsonElement value,
        string field,
        int depth,
        bool allowNull)
    {
        if (depth > MaxParameterDepth)
        {
            throw InvalidRequest($"QQ operation parameter {field} is nested too deeply");
        }

        switch (value.ValueKind)
        {
            case JsonValueKind.Null:
                if (!allowNull)
                {
                    throw InvalidRequest(
                        $"QQ operation parameter {field} must not be null");
                }

                return;
            case JsonValueKind.String:
                if (Encoding.UTF8.GetByteCount(value.GetString() ?? string.Empty)
                    > MaxParameterStringBytes)
                {
                    throw InvalidRequest($"QQ operation parameter {field} is too large");
                }

                return;
            case JsonValueKind.Number:
                if (!TryGetFiniteNumber(value, out _))
                {
                    throw InvalidRequest(
                        $"QQ operation parameter {field} must be finite");
                }

                return;
            case JsonValueKind.Object:
                if (value.EnumerateObject().Count() > MaxCollectionItems)
                {
                    throw InvalidRequest(
                        $"QQ operation parameter {field} has too many object fields");
                }

                foreach (JsonProperty property in value.EnumerateObject())
                {
                    if (property.Name.Length == 0)
                    {
                        throw InvalidRequest(
                            $"QQ operation parameter {field} has an invalid object key");
                    }

                    ValidateJsonValue(
                        property.Value,
                        $"{field}.{property.Name}",
                        depth + 1,
                        allowNull);
                }

                return;
            case JsonValueKind.Array:
                if (value.GetArrayLength() > MaxCollectionItems)
                {
                    throw InvalidRequest(
                        $"QQ operation parameter {field} has too many list items");
                }

                int index = 0;
                foreach (JsonElement item in value.EnumerateArray())
                {
                    ValidateJsonValue(item, $"{field}[{index}]", depth + 1, allowNull);
                    index++;
                }

                return;
            case JsonValueKind.True:
            case JsonValueKind.False:
                return;
            default:
                throw InvalidRequest(
                    $"QQ operation parameter {field} contains an invalid JSON value");
        }
    }

    private static void RejectSensitiveFields(JsonElement value, string path)
    {
        if (value.ValueKind == JsonValueKind.Object)
        {
            foreach (JsonProperty property in value.EnumerateObject())
            {
                string normalized = property.Name
                    .ToLowerInvariant()
                    .Replace("_", string.Empty, StringComparison.Ordinal)
                    .Replace("-", string.Empty, StringComparison.Ordinal);
                if (SensitiveMarkers.Any(normalized.Contains))
                {
                    throw ProtocolMismatch(
                        $"QQ operation result contains a sensitive field: {path}.{property.Name}");
                }

                RejectSensitiveFields(property.Value, $"{path}.{property.Name}");
            }
        }
        else if (value.ValueKind == JsonValueKind.Array)
        {
            int index = 0;
            foreach (JsonElement item in value.EnumerateArray())
            {
                RejectSensitiveFields(item, $"{path}[{index}]");
                index++;
            }
        }
    }

    private static void ValidateResultReferences(JsonElement result)
    {
        if (result.TryGetProperty("remote_uri", out JsonElement remoteUri)
            && remoteUri.ValueKind != JsonValueKind.Null)
        {
            string value = RequiredText(remoteUri, "QQ operation result remote_uri");
            if (Encoding.UTF8.GetByteCount(value) > MaxResultReferenceBytes
                || !Uri.TryCreate(value, UriKind.Absolute, out Uri? uri)
                || uri is null
                || uri.Host.Length == 0
                || uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps)
            {
                throw ProtocolMismatch(
                    "QQ operation result remote_uri must be http(s)");
            }
        }

        if (result.TryGetProperty("local_result_reference", out JsonElement localReference)
            && localReference.ValueKind != JsonValueKind.Null)
        {
            string value = RequiredText(
                localReference,
                "QQ operation result local_result_reference");
            if (Encoding.UTF8.GetByteCount(value) > MaxResultReferenceBytes
                || !value.StartsWith("qq://", StringComparison.Ordinal)
                    && !value.StartsWith("staging://", StringComparison.Ordinal))
            {
                throw ProtocolMismatch(
                    "QQ operation result local_result_reference must be binding-private");
            }
        }
    }

    private static bool IsIdentifier(JsonElement value)
    {
        if (value.ValueKind == JsonValueKind.String)
        {
            return !string.IsNullOrWhiteSpace(value.GetString());
        }

        return value.ValueKind == JsonValueKind.Number
            && value.TryGetUInt64(out ulong number)
            && number > 0;
    }

    private static bool IsInteger(JsonElement value) =>
        value.ValueKind == JsonValueKind.Number && value.TryGetInt64(out _);

    private static bool TryGetFiniteNumber(JsonElement value, out double number)
    {
        number = 0;
        if (value.ValueKind != JsonValueKind.Number
            || !value.TryGetDouble(out number))
        {
            return false;
        }

        return double.IsFinite(number);
    }

    private static string RequiredText(JsonElement value, string field)
    {
        if (value.ValueKind != JsonValueKind.String
            || string.IsNullOrWhiteSpace(value.GetString()))
        {
            throw ProtocolMismatch($"{field} must be text");
        }

        return value.GetString()!;
    }

    private static HashSet<string> Fields(params string[] names) =>
        new(names, StringComparer.Ordinal);

    private static HashSet<string> SearchFields() => Fields(
        "account_id", "query", "keywords", "scope", "offset", "count", "page", "page_size", "filter");

    private static QqHostException InvalidRequest(string message) =>
        new("INVALID_REQUEST", message);

    private static QqHostException ProtocolMismatch(string message) =>
        new("PROTOCOL_MISMATCH", message);
}
