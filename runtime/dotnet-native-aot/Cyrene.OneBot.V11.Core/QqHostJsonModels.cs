// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqHostJsonModels.cs                                                  │
// │  Namespace: Cyrene.OneBot.V11.Core                                       │
// │  Role: Bounded QQ Host stdio protocol models and AOT metadata.           │
// │                                                                         │
// │  模块职责：QQ Host stdio 协议模型与 Native AOT JSON 元数据                  │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using System.Text.Json.Serialization;

namespace Cyrene.OneBot.V11.Core;

/// <summary>JSON projection of one qqnt-direct activation configuration.</summary>
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
public sealed class QqHostErrorDocument
{
    [JsonPropertyName("code")]
    public string? Code { get; set; }

    [JsonPropertyName("message")]
    public string? Message { get; set; }
}

/// <summary>Public qq.client.v1 request envelope.</summary>
public sealed class QqClientRequest
{
    [JsonPropertyName("params")]
    public JsonElement Params { get; set; }
}

/// <summary>Public operation-to-native-method projection.</summary>
public sealed class QqOperationMapping
{
    [JsonPropertyName("service")]
    public string Service { get; set; } = string.Empty;

    [JsonPropertyName("method")]
    public string Method { get; set; } = string.Empty;
}

/// <summary>Public qq.client.v1 response envelope.</summary>
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
public partial class QqHostJsonContext : JsonSerializerContext
{
}
