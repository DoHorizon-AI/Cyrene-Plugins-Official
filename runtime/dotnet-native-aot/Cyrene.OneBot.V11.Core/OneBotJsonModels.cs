// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotJsonModels.cs                                                   │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: Bounded OneBot v11 JSON wire models and source-generated metadata. │
// │                                                                         │
// │  模块职责：有界 OneBot v11 JSON 线协议模型与源码生成元数据                    │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using System.Text.Json.Serialization;

namespace Cyrene.OneBot.V11.Core;

/// <summary>Bounded parameters for one OneBot v11 action.</summary>
public sealed record OneBotActionRequest
{
    [JsonPropertyName("flag")]
    public string? Flag { get; init; }

    [JsonPropertyName("group_id")]
    public ulong? GroupId { get; init; }

    [JsonPropertyName("user_id")]
    public ulong? UserId { get; init; }

    [JsonPropertyName("message")]
    public IReadOnlyList<OneBotMessageSegment>? Message { get; init; }

    [JsonPropertyName("sub_type")]
    public string? SubType { get; init; }

    [JsonPropertyName("approve")]
    public bool? Approve { get; init; }

    [JsonPropertyName("remark")]
    public string? Remark { get; init; }

    [JsonPropertyName("reason")]
    public string? Reason { get; init; }
}

/// <summary>One ordered OneBot v11 message segment.</summary>
public sealed class OneBotMessageSegment
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = string.Empty;

    [JsonPropertyName("data")]
    public OneBotSegmentData Data { get; set; } = new();
}

/// <summary>Known scalar data fields for the supported OneBot segments.</summary>
public sealed class OneBotSegmentData
{
    [JsonPropertyName("id")]
    public string? Id { get; set; }

    [JsonPropertyName("text")]
    public string? Text { get; set; }

    [JsonPropertyName("qq")]
    public string? Qq { get; set; }

    [JsonPropertyName("url")]
    public string? Url { get; set; }

    [JsonPropertyName("file")]
    public string? File { get; set; }

    [JsonPropertyName("name")]
    public string? Name { get; set; }
}

/// <summary>One OneBot v11 action response envelope.</summary>
public sealed class OneBotActionResponse
{
    [JsonPropertyName("status")]
    public string? Status { get; set; }

    [JsonPropertyName("retcode")]
    public int Retcode { get; set; }

    [JsonPropertyName("data")]
    public JsonElement Data { get; set; }

    [JsonPropertyName("echo")]
    public string? Echo { get; set; }
}

/// <summary>One forward-WebSocket action envelope.</summary>
public sealed class OneBotWebSocketActionRequest
{
    [JsonPropertyName("action")]
    public string Action { get; set; } = string.Empty;

    [JsonPropertyName("params")]
    public OneBotActionRequest Params { get; set; } = new();

    [JsonPropertyName("echo")]
    public string Echo { get; set; } = string.Empty;
}

/// <summary>Decoded response/event projection for a OneBot WebSocket frame.</summary>
public sealed class OneBotWebSocketFrame
{
    [JsonPropertyName("status")]
    public string? Status { get; set; }

    [JsonPropertyName("retcode")]
    public int Retcode { get; set; }

    [JsonPropertyName("data")]
    public JsonElement Data { get; set; }

    [JsonPropertyName("echo")]
    public string? Echo { get; set; }
}

/// <summary>Canonical JSON payload for one normalized OneBot request event.</summary>
public sealed class OneBotInboundRequestPayload
{
    [JsonPropertyName("account_id")]
    public string AccountId { get; set; } = string.Empty;

    [JsonPropertyName("vendor")]
    public string Vendor { get; set; } = string.Empty;

    [JsonPropertyName("request_id")]
    public string RequestId { get; set; } = string.Empty;

    [JsonPropertyName("request_kind")]
    public string RequestKind { get; set; } = string.Empty;

    [JsonPropertyName("vendor_request")]
    public Dictionary<string, string> VendorRequest { get; set; } = new();
}

/// <summary>JSON result returned after a OneBot request decision.</summary>
public sealed class OneBotRequestResponseResult
{
    [JsonPropertyName("status")]
    public string Status { get; set; } = string.Empty;

    [JsonPropertyName("request_id")]
    public string RequestId { get; set; } = string.Empty;

    [JsonPropertyName("request_kind")]
    public string RequestKind { get; set; } = string.Empty;

    [JsonPropertyName("decision")]
    public string Decision { get; set; } = string.Empty;
}

[JsonSourceGenerationOptions(
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase,
    GenerationMode = JsonSourceGenerationMode.Metadata)]
[JsonSerializable(typeof(OneBotProfileDocument))]
[JsonSerializable(typeof(OneBotActionRequest))]
[JsonSerializable(typeof(OneBotMessageSegment))]
[JsonSerializable(typeof(OneBotSegmentData))]
[JsonSerializable(typeof(OneBotActionResponse))]
[JsonSerializable(typeof(OneBotWebSocketActionRequest))]
[JsonSerializable(typeof(OneBotWebSocketFrame))]
[JsonSerializable(typeof(OneBotInboundRequestPayload))]
[JsonSerializable(typeof(OneBotRequestResponseResult))]
public partial class OneBotJsonContext : JsonSerializerContext
{
}
