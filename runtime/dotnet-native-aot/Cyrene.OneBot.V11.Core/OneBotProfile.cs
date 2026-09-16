// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotProfile.cs                                                      │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: Validated configuration and explicit transport-profile routing.   │
// │                                                                         │
// │  模块职责：校验 OneBot 配置，并通过显式注册表路由传输 profile                 │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using System.Text.Json.Serialization;

namespace Cyrene.OneBot.V11.Core;

/// <summary>
/// JSON document accepted from the generic plugin activation environment.
/// <para>通用插件激活环境接受的 JSON 配置文档。</para>
/// </summary>
public sealed class OneBotProfileDocument
{
    [JsonPropertyName("binding_id")]
    public string? BindingId { get; set; }

    [JsonPropertyName("runtime_profile")]
    public string? RuntimeProfile { get; set; }

    [JsonPropertyName("http_base_url")]
    public string? HttpBaseUrl { get; set; }

    [JsonPropertyName("access_token")]
    public string? AccessToken { get; set; }

    [JsonPropertyName("transport_profile")]
    public string? TransportProfile { get; set; }

    [JsonPropertyName("websocket_url")]
    public string? WebSocketUrl { get; set; }

    [JsonPropertyName("reverse_listen_host")]
    public string? ReverseListenHost { get; set; }

    [JsonPropertyName("reverse_listen_port")]
    public int? ReverseListenPort { get; set; }

    [JsonPropertyName("self_account_id")]
    public string? SelfAccountId { get; set; }

    [JsonPropertyName("timeout_seconds")]
    public double? TimeoutSeconds { get; set; }

    [JsonExtensionData]
    public Dictionary<string, JsonElement>? UnknownFields { get; set; }
}

/// <summary>
/// Explicit OneBot transport profile names.
/// <para>显式的 OneBot 传输 profile 名称。</para>
/// </summary>
public enum OneBotTransportProfile
{
    HttpApi,
    ForwardWebSocket,
    ReverseWebSocket
}

/// <summary>
/// Validated, binding-scoped OneBot configuration.
/// <para>经过校验、限定在单个 binding 内的 OneBot 配置。</para>
/// </summary>
public sealed record OneBotProfile(
    string BindingId,
    string RuntimeProfile,
    string? HttpBaseUrl,
    string AccessToken,
    OneBotTransportProfile TransportProfile,
    string? WebSocketUrl,
    string ReverseListenHost,
    int ReverseListenPort,
    string? SelfAccountId,
    double TimeoutSeconds)
{
    public bool IsHttpApi => TransportProfile == OneBotTransportProfile.HttpApi;
}

/// <summary>
/// Configuration failure that is safe to expose across the plugin boundary.
/// <para>可安全跨越插件边界暴露的配置错误。</para>
/// </summary>
public sealed class OneBotConfigurationException : Exception
{
    public OneBotConfigurationException(string domainCode, string message)
        : base(message)
    {
        DomainCode = domainCode;
    }

    public string DomainCode { get; }
}

/// <summary>
/// Loads and validates one binding without reflection or dynamic configuration types.
/// <para>不使用反射或动态配置类型加载并校验单个 binding。</para>
/// </summary>
public static class OneBotProfileLoader
{
    private static readonly HashSet<string> QqntDirectOnlyFields =
        new(StringComparer.Ordinal)
        {
            "host_executable",
            "host_args",
            "data_dir",
            "required_client_version",
            "required_host_abi",
            "installation_manifest",
            "account_id",
            "login_policy",
            "platform",
            "startup_timeout_seconds",
            "shutdown_timeout_seconds",
            "secret_refs",
            "max_restart_attempts",
            "restart_window_seconds",
            "restart_backoff_seconds",
            "restart_backoff_max_seconds",
            "crash_circuit_cooldown_seconds"
        };

    public static OneBotProfile FromJson(string json, string? bindingIdOverride = null)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            throw new OneBotConfigurationException(
                "INVALID_CONFIGURATION",
                "OneBot configuration JSON must not be empty.");
        }

        OneBotProfileDocument? document;
        try
        {
            document = JsonSerializer.Deserialize(
                json,
                OneBotJsonContext.Default.OneBotProfileDocument);
        }
        catch (JsonException)
        {
            throw new OneBotConfigurationException(
                "INVALID_CONFIGURATION",
                "OneBot configuration JSON is invalid.");
        }

        if (document is null)
        {
            throw new OneBotConfigurationException(
                "INVALID_CONFIGURATION",
                "OneBot configuration must be a JSON object.");
        }

        if (bindingIdOverride is not null)
        {
            document.BindingId = bindingIdOverride;
        }

        return Validate(document);
    }

    public static OneBotProfile? FromEnvironment()
    {
        string? encoded = Environment.GetEnvironmentVariable(
            "CYRENE_CAPABILITY_CONFIGURATION_JSON");
        encoded ??= Environment.GetEnvironmentVariable(
            "CYRENE_ONEBOT_CONFIGURATION_JSON");
        string? bindingId = Environment.GetEnvironmentVariable(
            "CYRENE_CAPABILITY_BINDING_ID");

        if (string.IsNullOrWhiteSpace(encoded) && string.IsNullOrWhiteSpace(bindingId))
        {
            return null;
        }

        return FromJson(encoded ?? "{}", bindingId);
    }

    private static OneBotProfile Validate(OneBotProfileDocument document)
    {
        if (document.UnknownFields is not null)
        {
            string? directOnly = document.UnknownFields.Keys
                .FirstOrDefault(QqntDirectOnlyFields.Contains);
            if (directOnly is not null)
            {
                throw new OneBotConfigurationException(
                    "QQNT_DIRECT_ONLY_FIELD",
                    $"OneBot configuration contains QQNT-direct-only field '{directOnly}'.");
            }

            string? unknown = document.UnknownFields.Keys.FirstOrDefault();
            if (unknown is not null)
            {
                throw new OneBotConfigurationException(
                    "UNKNOWN_CONFIGURATION_FIELD",
                    $"OneBot configuration contains unknown field '{unknown}'.");
            }
        }

        string bindingId = RequiredIdentifier(document.BindingId, "binding_id");
        string runtimeProfile = (document.RuntimeProfile ?? "onebot-v11").Trim();
        if (!string.Equals(runtimeProfile, "onebot-v11", StringComparison.Ordinal))
        {
            throw new OneBotConfigurationException(
                "UNSUPPORTED_RUNTIME_PROFILE",
                "runtime_profile must be onebot-v11 for this runtime.");
        }

        string? httpBaseUrl = OptionalUrl(document.HttpBaseUrl, "http_base_url", "http", "https");
        string? websocketUrl = OptionalUrl(document.WebSocketUrl, "websocket_url", "ws", "wss");
        string transportName = document.TransportProfile?.Trim() ??
            (websocketUrl is not null ? "forward_websocket" : "http_api");
        OneBotTransportProfile transportProfile = transportName switch
        {
            "http_api" => OneBotTransportProfile.HttpApi,
            "forward_websocket" => OneBotTransportProfile.ForwardWebSocket,
            "reverse_websocket" => OneBotTransportProfile.ReverseWebSocket,
            _ => throw new OneBotConfigurationException(
                "INVALID_TRANSPORT_PROFILE",
                "transport_profile must be http_api, forward_websocket, or reverse_websocket.")
        };

        if (transportProfile == OneBotTransportProfile.HttpApi && httpBaseUrl is null)
        {
            throw new OneBotConfigurationException(
                "REQUIRED_CONFIGURATION_FIELD",
                "http_base_url is required for the http_api transport.");
        }

        if (transportProfile == OneBotTransportProfile.ForwardWebSocket && websocketUrl is null)
        {
            throw new OneBotConfigurationException(
                "REQUIRED_CONFIGURATION_FIELD",
                "websocket_url is required for the forward_websocket transport.");
        }

        string accessToken = document.AccessToken ?? string.Empty;
        if (accessToken.Contains('\r') || accessToken.Contains('\n'))
        {
            throw new OneBotConfigurationException(
                "INVALID_ACCESS_TOKEN",
                "access_token must not contain line breaks.");
        }

        string reverseHost = document.ReverseListenHost?.Trim() ?? "127.0.0.1";
        if (reverseHost.Length == 0)
        {
            throw new OneBotConfigurationException(
                "INVALID_REVERSE_LISTENER",
                "reverse_listen_host must be non-empty.");
        }

        int reversePort = document.ReverseListenPort ?? 6199;
        if (reversePort is < 0 or > 65_535)
        {
            throw new OneBotConfigurationException(
                "INVALID_REVERSE_LISTENER",
                "reverse_listen_port must be between 0 and 65535.");
        }

        string? selfAccountId = document.SelfAccountId is null
            ? null
            : RequiredIdentifier(document.SelfAccountId, "self_account_id");
        double timeoutSeconds = document.TimeoutSeconds ?? 10.0;
        if (double.IsNaN(timeoutSeconds) || double.IsInfinity(timeoutSeconds)
            || timeoutSeconds <= 0 || timeoutSeconds > 300)
        {
            throw new OneBotConfigurationException(
                "INVALID_TIMEOUT",
                "timeout_seconds must be greater than 0 and no more than 300.");
        }

        return new OneBotProfile(
            bindingId,
            runtimeProfile,
            httpBaseUrl,
            accessToken,
            transportProfile,
            websocketUrl,
            reverseHost,
            reversePort,
            selfAccountId,
            timeoutSeconds);
    }

    private static string? OptionalUrl(string? value, string field, params string[] schemes)
    {
        if (value is null)
        {
            return null;
        }

        string trimmed = value.Trim();
        if (!Uri.TryCreate(trimmed, UriKind.Absolute, out Uri? uri)
            || !schemes.Contains(uri.Scheme, StringComparer.OrdinalIgnoreCase)
            || string.IsNullOrEmpty(uri.Host)
            || !string.IsNullOrEmpty(uri.UserInfo)
            || !string.IsNullOrEmpty(uri.Fragment))
        {
            throw new OneBotConfigurationException(
                "INVALID_URL",
                $"{field} must be an absolute URL without userinfo or fragments.");
        }

        return trimmed.TrimEnd('/');
    }

    private static string RequiredIdentifier(string? value, string field)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Trim().Length > 128)
        {
            throw new OneBotConfigurationException(
                "INVALID_IDENTIFIER",
                $"{field} must be non-empty text of at most 128 characters.");
        }

        return value.Trim();
    }
}
