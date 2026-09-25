// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqHostJsonModels.cs                                                  │
// │  Namespace: Cyrene.Im.Core                                                │
// │  Role: Bounded QQ Host stdio protocol models and AOT metadata.           │
// │                                                                         │
// │  模块职责：QQ Host stdio 协议模型与 Native AOT JSON 元数据                  │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using System.Text.Json.Serialization;

namespace Cyrene.Im.Core;

/// <summary>JSON projection of one qqnt-direct activation configuration.</summary>
/// <remarks>中文：JSON 形式的 qqnt-direct 激活配置投影。</remarks>
public sealed class QqDirectProfileDocument
{
    [JsonPropertyName("runtime_profile")]
    public string? RuntimeProfile { get; set; }

    [JsonPropertyName("binding_id")]
    public string? BindingId { get; set; }

    [JsonPropertyName("host_executable")]
    public string? HostExecutable { get; set; }

    [JsonPropertyName("host_args")]
    public List<string>? HostArguments { get; set; }

    [JsonPropertyName("data_dir")]
    public string? DataDirectory { get; set; }

    [JsonPropertyName("required_client_version")]
    public string? RequiredClientVersion { get; set; }

    [JsonPropertyName("required_host_abi")]
    public string? RequiredHostAbi { get; set; }

    [JsonPropertyName("account_id")]
    public string? AccountId { get; set; }

    [JsonPropertyName("self_account_id")]
    public string? SelfAccountId { get; set; }

    [JsonPropertyName("login_policy")]
    public string? LoginPolicy { get; set; }

    [JsonPropertyName("platform")]
    public string? Platform { get; set; }

    [JsonPropertyName("timeout_seconds")]
    public double? TimeoutSeconds { get; set; }

    [JsonPropertyName("startup_timeout_seconds")]
    public double? StartupTimeoutSeconds { get; set; }

    [JsonPropertyName("shutdown_timeout_seconds")]
    public double? ShutdownTimeoutSeconds { get; set; }

    [JsonPropertyName("secret_refs")]
    public List<string>? SecretReferences { get; set; }

    [JsonPropertyName("max_restart_attempts")]
    public int? MaxRestartAttempts { get; set; }

    [JsonPropertyName("restart_window_seconds")]
    public double? RestartWindowSeconds { get; set; }

    [JsonPropertyName("restart_backoff_seconds")]
    public double? RestartBackoffSeconds { get; set; }

    [JsonPropertyName("restart_backoff_max_seconds")]
    public double? RestartBackoffMaxSeconds { get; set; }

    [JsonPropertyName("crash_circuit_cooldown_seconds")]
    public double? CrashCircuitCooldownSeconds { get; set; }

    [JsonPropertyName("installation_manifest")]
    public string? InstallationManifest { get; set; }

    [JsonExtensionData]
    public Dictionary<string, JsonElement>? UnknownFields { get; set; }
}

/// <summary>One request sent to the configured QQ Host child.</summary>
/// <remarks>中文：发送给已配置 QQ Host 子进程的单个请求。</remarks>
public sealed class QqHostHelloParams
{
    [JsonPropertyName("protocol")]
    public string Protocol { get; set; } = string.Empty;

    [JsonPropertyName("protocol_version")]
    public string ProtocolVersion { get; set; } = string.Empty;

    [JsonPropertyName("platform")]
    public string Platform { get; set; } = string.Empty;

    [JsonPropertyName("required_client_version")]
    public string RequiredClientVersion { get; set; } = string.Empty;

    [JsonPropertyName("required_host_abi")]
    public string RequiredHostAbi { get; set; } = string.Empty;
}

/// <summary>One request sent to the configured QQ Host child.</summary>
/// <remarks>中文：发送给已配置 QQ Host 子进程的单个请求。</remarks>
public sealed class QqHostRequest
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = "request";

    [JsonPropertyName("request_id")]
    public string RequestId { get; set; } = string.Empty;

    [JsonPropertyName("binding_id")]
    public string BindingId { get; set; } = string.Empty;

    [JsonPropertyName("generation")]
    public int Generation { get; set; }

    [JsonPropertyName("operation")]
    public string Operation { get; set; } = string.Empty;

    [JsonPropertyName("params")]
    public JsonElement Params { get; set; }
}

/// <summary>One response returned by the configured QQ Host child.</summary>
/// <remarks>中文：由已配置 QQ Host 子进程返回的单个响应。</remarks>
public sealed class QqHostResponse
{
    [JsonPropertyName("type")]
    public string? Type { get; set; }

    [JsonPropertyName("request_id")]
    public string? RequestId { get; set; }

    [JsonPropertyName("binding_id")]
    public string? BindingId { get; set; }

    [JsonPropertyName("generation")]
    public int Generation { get; set; }

    [JsonPropertyName("ok")]
    public bool Ok { get; set; }

    [JsonPropertyName("result")]
    public JsonElement Result { get; set; }

    [JsonPropertyName("error")]
    public QqHostErrorDocument? Error { get; set; }
}

/// <summary>One event emitted by the configured QQ Host child.</summary>
/// <remarks>中文：由已配置 QQ Host 子进程发出的单个事件。</remarks>
public sealed class QqHostEvent
{
    [JsonPropertyName("type")]
    public string? Type { get; set; }

    [JsonPropertyName("event")]
    public string? Event { get; set; }

    [JsonPropertyName("event_id")]
    public string? EventId { get; set; }

    [JsonPropertyName("request_id")]
    public string? RequestId { get; set; }

    [JsonPropertyName("binding_id")]
    public string? BindingId { get; set; }

    [JsonPropertyName("generation")]
    public int Generation { get; set; }

    [JsonPropertyName("payload")]
    public JsonElement Payload { get; set; }
}

/// <summary>Control message used to request a bounded Host shutdown.</summary>
/// <remarks>中文：用于请求 Host 在有界时间内关闭的控制消息。</remarks>
public sealed class QqHostShutdown
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = "shutdown";

    [JsonPropertyName("binding_id")]
    public string BindingId { get; set; } = string.Empty;

    [JsonPropertyName("generation")]
    public int Generation { get; set; }
}

/// <summary>Bounded error record crossing the QQ Host boundary.</summary>
/// <remarks>中文：穿过 QQ Host 边界的有界错误记录。</remarks>
public sealed class QqHostErrorDocument
{
    [JsonPropertyName("code")]
    public string? Code { get; set; }

    [JsonPropertyName("message")]
    public string? Message { get; set; }
}

/// <summary>Public qq.client.v1 request envelope.</summary>
/// <remarks>中文：公共 qq.client.v1 请求封套。</remarks>
public sealed class QqClientRequest
{
    [JsonPropertyName("params")]
    public JsonElement Params { get; set; }
}

/// <summary>Public operation-to-native-method projection.</summary>
/// <remarks>中文：公共的 operation 到 native method 投影。</remarks>
public sealed class QqOperationMapping
{
    [JsonPropertyName("service")]
    public string Service { get; set; } = string.Empty;

    [JsonPropertyName("method")]
    public string Method { get; set; } = string.Empty;
}

/// <summary>Public qq.client.v1 response envelope.</summary>
/// <remarks>中文：公共 qq.client.v1 响应封套。</remarks>
public sealed class QqClientResponse
{
    [JsonPropertyName("operation")]
    public string Operation { get; set; } = string.Empty;

    [JsonPropertyName("status")]
    public string Status { get; set; } = "accepted";

    [JsonPropertyName("result")]
    public JsonElement Result { get; set; }

    [JsonPropertyName("priority")]
    public string Priority { get; set; } = string.Empty;

    [JsonPropertyName("mapping")]
    public QqOperationMapping Mapping { get; set; } = new();
}

/// <summary>Native QQ peer identity used by canonical message sends.</summary>
/// <remarks>中文：规范消息发送操作使用的原生 QQ 对端身份。</remarks>
public sealed class QqNativePeer
{
    [JsonPropertyName("kind")]
    public string Kind { get; set; } = string.Empty;

    [JsonPropertyName("conversation_id")]
    public string ConversationId { get; set; } = string.Empty;

    [JsonPropertyName("peer_uid")]
    public string? PeerUid { get; set; }

    [JsonPropertyName("peer_uin")]
    public string? PeerUin { get; set; }

    [JsonPropertyName("group_code")]
    public string? GroupCode { get; set; }

    [JsonPropertyName("user_uid")]
    public string? UserUid { get; set; }

    [JsonPropertyName("user_uin")]
    public string? UserUin { get; set; }
}

/// <summary>Remote or binding-owned media reference for a native element.</summary>
/// <remarks>中文：原生消息元素的远程媒体引用或 binding 所有的媒体引用。</remarks>
public sealed class QqNativeReference
{
    [JsonPropertyName("remote_uri")]
    public string? RemoteUri { get; set; }

    [JsonPropertyName("vendor")]
    public string? Vendor { get; set; }

    [JsonPropertyName("account_id")]
    public string? AccountId { get; set; }

    [JsonPropertyName("media_id")]
    public string? MediaId { get; set; }
}

/// <summary>One canonical content part mapped to a native QQ message element.</summary>
/// <remarks>中文：映射到原生 QQ 消息元素的单个规范内容部分。</remarks>
public sealed class QqNativeElement
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = string.Empty;

    [JsonPropertyName("text")]
    public string? Text { get; set; }

    [JsonPropertyName("target")]
    public string? Target { get; set; }

    [JsonPropertyName("target_id")]
    public string? TargetId { get; set; }

    [JsonPropertyName("reference")]
    public QqNativeReference? Reference { get; set; }

    [JsonPropertyName("file_name")]
    public string? FileName { get; set; }
}

/// <summary>Canonical reply mapped to the native QQ send operation.</summary>
/// <remarks>中文：映射到原生 QQ 发送操作的规范回复。</remarks>
public sealed class QqNativeReply
{
    [JsonPropertyName("message_id")]
    public string MessageId { get; set; } = string.Empty;
}

/// <summary>Fixed parameter shape for the native QQ message send operation.</summary>
/// <remarks>中文：原生 QQ 消息发送操作的固定参数结构。</remarks>
public sealed class QqNativeMessageParameters
{
    [JsonPropertyName("peer")]
    public QqNativePeer Peer { get; set; } = new();

    [JsonPropertyName("elements")]
    public List<QqNativeElement> Elements { get; set; } = new();

    [JsonPropertyName("reply")]
    public QqNativeReply? Reply { get; set; }
}

/// <summary>Fixed native listener subscription parameter shape.</summary>
/// <remarks>中文：原生 listener 订阅的固定参数结构。</remarks>
public sealed class QqSubscribeParameters
{
    [JsonPropertyName("events")]
    public List<string> Events { get; set; } = new();
}

/// <summary>JSON request shape for canonical friend/group approval.</summary>
/// <remarks>中文：规范好友／群组审批的 JSON 请求结构。</remarks>
public sealed class QqRespondRequestDocument
{
    [JsonPropertyName("request_id")]
    public string? RequestId { get; set; }

    [JsonPropertyName("request_kind")]
    public string? RequestKind { get; set; }

    [JsonPropertyName("decision")]
    public string? Decision { get; set; }

    [JsonPropertyName("comment")]
    public string? Comment { get; set; }

    [JsonPropertyName("vendor_request")]
    public Dictionary<string, string>? VendorRequest { get; set; }

    [JsonExtensionData]
    public Dictionary<string, JsonElement>? UnknownFields { get; set; }
}

/// <summary>Fixed native parameters for a QQ friend or group approval.</summary>
/// <remarks>中文：QQ 好友或群组审批操作的固定原生参数。</remarks>
public sealed class QqRespondParameters
{
    [JsonPropertyName("request_id")]
    public string RequestId { get; set; } = string.Empty;

    [JsonPropertyName("approve")]
    public bool Approve { get; set; }

    [JsonPropertyName("comment")]
    public string Comment { get; set; } = string.Empty;

    [JsonPropertyName("vendor_request")]
    public Dictionary<string, string> VendorRequest { get; set; } = new();
}

/// <summary>Canonical approval result returned to the Product caller.</summary>
/// <remarks>中文：返回给 Product 调用方的规范审批结果。</remarks>
public sealed class QqRespondResult
{
    [JsonPropertyName("status")]
    public string Status { get; set; } = "accepted";

    [JsonPropertyName("request_id")]
    public string RequestId { get; set; } = string.Empty;

    [JsonPropertyName("request_kind")]
    public string RequestKind { get; set; } = string.Empty;

    [JsonPropertyName("decision")]
    public string Decision { get; set; } = string.Empty;

    [JsonPropertyName("account_id")]
    public string AccountId { get; set; } = string.Empty;

    [JsonPropertyName("result")]
    public JsonElement? Result { get; set; }
}

/// <summary>Canonical friend/group request event JSON shape.</summary>
/// <remarks>中文：规范好友／群组请求事件的 JSON 结构。</remarks>
public sealed class QqInboundRequestPayload
{
    [JsonPropertyName("account_id")]
    public string AccountId { get; set; } = string.Empty;

    [JsonPropertyName("vendor")]
    public string Vendor { get; set; } = "qq";

    [JsonPropertyName("request_id")]
    public string RequestId { get; set; } = string.Empty;

    [JsonPropertyName("request_kind")]
    public string RequestKind { get; set; } = string.Empty;

    [JsonPropertyName("vendor_request")]
    public Dictionary<string, string> VendorRequest { get; set; } = new();
}

/// <summary>Typed callback error facts emitted by a correlated native event.</summary>
/// <remarks>中文：由关联的原生事件发出的类型化回调错误事实。</remarks>
public sealed class QqCallbackError
{
    [JsonPropertyName("code")]
    public string Code { get; set; } = string.Empty;

    [JsonPropertyName("message")]
    public string Message { get; set; } = string.Empty;
}

/// <summary>Bounded callback payload correlated to one originating operation.</summary>
/// <remarks>中文：与一个发起操作关联的有界回调负载。</remarks>
public sealed class QqCallbackPayload
{
    [JsonPropertyName("operation")]
    public string Operation { get; set; } = string.Empty;

    [JsonPropertyName("request_id")]
    public string RequestId { get; set; } = string.Empty;

    [JsonPropertyName("event_id")]
    public string? EventId { get; set; }

    [JsonPropertyName("account_id")]
    public string? AccountId { get; set; }

    [JsonPropertyName("message_id")]
    public string? MessageId { get; set; }

    [JsonPropertyName("peer_uid")]
    public string? PeerUid { get; set; }

    [JsonPropertyName("peer_uin")]
    public string? PeerUin { get; set; }

    [JsonPropertyName("group_code")]
    public string? GroupCode { get; set; }

    [JsonPropertyName("user_uid")]
    public string? UserUid { get; set; }

    [JsonPropertyName("user_uin")]
    public string? UserUin { get; set; }

    [JsonPropertyName("group_id")]
    public string? GroupId { get; set; }

    [JsonPropertyName("user_id")]
    public string? UserId { get; set; }

    [JsonPropertyName("member_uid")]
    public string? MemberUid { get; set; }

    [JsonPropertyName("member_uin")]
    public string? MemberUin { get; set; }

    [JsonPropertyName("media_id")]
    public string? MediaId { get; set; }

    [JsonPropertyName("file_id")]
    public string? FileId { get; set; }

    [JsonPropertyName("file_uuid")]
    public string? FileUuid { get; set; }

    [JsonPropertyName("element_id")]
    public string? ElementId { get; set; }

    [JsonPropertyName("sequence")]
    public string? Sequence { get; set; }

    [JsonPropertyName("random")]
    public string? Random { get; set; }

    [JsonPropertyName("status")]
    public JsonElement? Status { get; set; }

    [JsonPropertyName("progress")]
    public JsonElement? Progress { get; set; }

    [JsonPropertyName("remote_uri")]
    public string? RemoteUri { get; set; }

    [JsonPropertyName("local_result_reference")]
    public string? LocalResultReference { get; set; }

    [JsonPropertyName("error")]
    public QqCallbackError? Error { get; set; }
}

[JsonSourceGenerationOptions(
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase,
    GenerationMode = JsonSourceGenerationMode.Metadata)]
[JsonSerializable(typeof(QqHostRequest))]
[JsonSerializable(typeof(QqHostHelloParams))]
[JsonSerializable(typeof(QqDirectProfileDocument))]
[JsonSerializable(typeof(QqHostResponse))]
[JsonSerializable(typeof(QqHostEvent))]
[JsonSerializable(typeof(QqHostShutdown))]
[JsonSerializable(typeof(QqClientRequest))]
[JsonSerializable(typeof(QqClientResponse))]
[JsonSerializable(typeof(Dictionary<string, string>))]
[JsonSerializable(typeof(QqNativeMessageParameters))]
[JsonSerializable(typeof(QqSubscribeParameters))]
[JsonSerializable(typeof(QqRespondRequestDocument))]
[JsonSerializable(typeof(QqRespondParameters))]
[JsonSerializable(typeof(QqRespondResult))]
[JsonSerializable(typeof(QqInboundRequestPayload))]
[JsonSerializable(typeof(QqCallbackPayload))]
public partial class QqHostJsonContext : JsonSerializerContext
{
}
