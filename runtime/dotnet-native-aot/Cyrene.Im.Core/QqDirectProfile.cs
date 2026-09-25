// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqDirectProfile.cs                                                    │
// │  Namespace: Cyrene.Im.Core                                                 │
// │  Role: Binding-scoped qqnt-direct configuration validation.                │
// │                                                                         │
// │  模块职责：校验 qqnt-direct binding 配置并生成 Host 启动参数                 │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;

namespace Cyrene.Im.Core;

/// <summary>Validated QQNT direct configuration for one binding.</summary>
/// <remarks>中文：经过校验、供单个 binding 使用的 QQNT direct 配置。</remarks>
public sealed record QqDirectProfile(
    string BindingId,
    string HostExecutable,
    IReadOnlyList<string> HostArguments,
    string DataDirectory,
    string RequiredClientVersion,
    string RequiredHostAbi,
    string? AccountId,
    string LoginPolicy,
    string Platform,
    double TimeoutSeconds,
    double StartupTimeoutSeconds,
    double ShutdownTimeoutSeconds,
    IReadOnlyList<string> SecretReferences,
    int MaxRestartAttempts,
    double RestartWindowSeconds,
    double RestartBackoffSeconds,
    double RestartBackoffMaxSeconds,
    double CrashCircuitCooldownSeconds,
    string? InstallationManifest)
{
    public QqHostLaunchConfiguration HostLaunch => new(
        BindingId,
        HostExecutable,
        HostArguments,
        DataDirectory,
        RequiredClientVersion,
        RequiredHostAbi,
        Platform,
        TimeoutSeconds,
        StartupTimeoutSeconds,
        ShutdownTimeoutSeconds,
        MaxRestartAttempts,
        RestartWindowSeconds,
        RestartBackoffSeconds,
        RestartBackoffMaxSeconds,
        CrashCircuitCooldownSeconds,
        InstallationManifest);
}

/// <summary>Configuration failure safe to expose across the plugin boundary.</summary>
/// <remarks>中文：可安全跨插件边界公开的配置错误。</remarks>
public sealed class QqDirectConfigurationException : Exception
{
    public QqDirectConfigurationException(string domainCode, string message)
        : base(message)
    {
        DomainCode = domainCode;
    }

    public string DomainCode { get; }
}

/// <summary>Loads qqnt-direct configuration without runtime reflection.</summary>
/// <remarks>中文：无需运行时反射即可加载 qqnt-direct 配置。</remarks>
public static class QqDirectProfileLoader
{
    private const string RuntimeProfile = "qqnt-direct";
    private const string DefaultPlatform = "linux-x86_64";

    public static bool IsQqntDirect(string? json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return false;
        }

        try
        {
            using JsonDocument document = JsonDocument.Parse(json);
            return document.RootElement.ValueKind == JsonValueKind.Object
                && document.RootElement.TryGetProperty(
                    "runtime_profile",
                    out JsonElement profile)
                && profile.ValueKind == JsonValueKind.String
                && profile.GetString() == RuntimeProfile;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    public static QqDirectProfile FromJson(string json, string? bindingIdOverride = null)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                "QQNT direct configuration JSON must not be empty.");
        }

        QqDirectProfileDocument? document;
        try
        {
            document = JsonSerializer.Deserialize(
                json,
                QqHostJsonContext.Default.QqDirectProfileDocument);
        }
        catch (JsonException)
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                "QQNT direct configuration JSON is invalid.");
        }

        if (document is null)
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                "QQNT direct configuration must be a JSON object.");
        }

        if (document.UnknownFields is not null && document.UnknownFields.Count > 0)
        {
            string unknown = document.UnknownFields.Keys.First();
            throw new QqDirectConfigurationException(
                "UNKNOWN_CONFIGURATION_FIELD",
                $"QQNT direct configuration contains unknown field '{unknown}'.");
        }

        string runtimeProfile = document.RuntimeProfile?.Trim() ?? RuntimeProfile;
        if (runtimeProfile != RuntimeProfile)
        {
            throw new QqDirectConfigurationException(
                "UNSUPPORTED_RUNTIME_PROFILE",
                "runtime_profile must be qqnt-direct for this runtime.");
        }

        string bindingId = RequiredIdentifier(
            bindingIdOverride ?? document.BindingId,
            "binding_id");
        string hostExecutable = RequiredAbsolutePath(document.HostExecutable, "host_executable");
        string dataDirectory = RequiredAbsolutePath(document.DataDirectory, "data_dir");
        string requiredClientVersion = RequiredText(
            document.RequiredClientVersion,
            "required_client_version");
        string requiredHostAbi = RequiredText(document.RequiredHostAbi, "required_host_abi");
        string platform = RequiredText(document.Platform ?? DefaultPlatform, "platform");
        if (platform != DefaultPlatform)
        {
            throw new QqDirectConfigurationException(
                "UNSUPPORTED_VERSION",
                "the first qqnt-direct target is linux-x86_64");
        }

        string? accountId = OptionalIdentifier(document.AccountId, "account_id");
        string? legacyAccountId = OptionalIdentifier(document.SelfAccountId, "self_account_id");
        if (accountId is not null && legacyAccountId is not null && accountId != legacyAccountId)
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                "account_id and self_account_id must match.");
        }

        string loginPolicy = document.LoginPolicy?.Trim() ?? "existing_session";
        if (loginPolicy is not ("existing_session" or "qr"))
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                "login_policy must be existing_session or qr.");
        }

        double timeout = PositiveBounded(document.TimeoutSeconds ?? 10, "timeout_seconds", 300);
        double startupTimeout = PositiveBounded(
            document.StartupTimeoutSeconds ?? 30,
            "startup_timeout_seconds",
            300);
        double shutdownTimeout = PositiveBounded(
            document.ShutdownTimeoutSeconds ?? 2,
            "shutdown_timeout_seconds",
            300);
        List<string> secretReferences = document.SecretReferences ?? new List<string>();
        if (secretReferences.Any(item => string.IsNullOrWhiteSpace(item)))
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                "secret_refs must contain non-empty names.");
        }

        int maxRestartAttempts = BoundedInteger(
            document.MaxRestartAttempts ?? 2,
            "max_restart_attempts",
            0,
            5);
        double restartWindow = Bounded(document.RestartWindowSeconds ?? 60, "restart_window_seconds", 0.1, 3_600);
        double restartBackoff = Bounded(document.RestartBackoffSeconds ?? 0.25, "restart_backoff_seconds", 0, 60);
        double restartBackoffMax = Bounded(
            document.RestartBackoffMaxSeconds ?? 5,
            "restart_backoff_max_seconds",
            0,
            300);
        if (restartBackoffMax < restartBackoff)
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                "restart_backoff_max_seconds must not be below restart_backoff_seconds.");
        }

        double crashCooldown = Bounded(
            document.CrashCircuitCooldownSeconds ?? 60,
            "crash_circuit_cooldown_seconds",
            0.1,
            3_600);
        string? installationManifest = document.InstallationManifest is null
            ? null
            : RequiredAbsolutePath(document.InstallationManifest, "installation_manifest");

        return new QqDirectProfile(
            bindingId,
            hostExecutable,
            document.HostArguments ?? new List<string>(),
            dataDirectory,
            requiredClientVersion,
            requiredHostAbi,
            accountId ?? legacyAccountId,
            loginPolicy,
            platform,
            timeout,
            startupTimeout,
            shutdownTimeout,
            secretReferences,
            maxRestartAttempts,
            restartWindow,
            restartBackoff,
            restartBackoffMax,
            crashCooldown,
            installationManifest);
    }

    public static QqDirectProfile? FromEnvironment()
    {
        string? encoded = Environment.GetEnvironmentVariable(
            "CYRENE_CAPABILITY_CONFIGURATION_JSON");
        encoded ??= Environment.GetEnvironmentVariable(
            "CYRENE_ONEBOT_CONFIGURATION_JSON");
        string? bindingId = Environment.GetEnvironmentVariable("CYRENE_CAPABILITY_BINDING_ID");
        if (string.IsNullOrWhiteSpace(encoded) && string.IsNullOrWhiteSpace(bindingId))
        {
            return null;
        }

        if (!IsQqntDirect(encoded))
        {
            return null;
        }

        return FromJson(encoded!, bindingId);
    }

    private static string RequiredText(string? value, string field)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new QqDirectConfigurationException(
                "REQUIRED_CONFIGURATION_FIELD",
                $"{field} is required.");
        }

        return value.Trim();
    }

    private static string RequiredIdentifier(string? value, string field)
    {
        string identifier = RequiredText(value, field);
        if (identifier.Length > 512
            || !char.IsLetterOrDigit(identifier[0])
            || identifier.Any(character => !char.IsLetterOrDigit(character)
                && character is not ('.' or '_' or '-')))
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                $"{field} has an invalid format.");
        }

        return identifier;
    }

    private static string? OptionalIdentifier(string? value, string field) =>
        value is null ? null : RequiredIdentifier(value, field);

    private static string RequiredAbsolutePath(string? value, string field)
    {
        string path = RequiredText(value, field);
        if (!Path.IsPathRooted(path))
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                $"{field} must be an absolute path.");
        }

        return path;
    }

    private static double PositiveBounded(double value, string field, double maximum) =>
        Bounded(value, field, double.Epsilon, maximum);

    private static double Bounded(double value, string field, double minimum, double maximum)
    {
        if (double.IsNaN(value) || double.IsInfinity(value) || value < minimum || value > maximum)
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                $"{field} is outside its allowed range.");
        }

        return value;
    }

    private static int BoundedInteger(int value, string field, int minimum, int maximum)
    {
        if (value < minimum || value > maximum)
        {
            throw new QqDirectConfigurationException(
                "INVALID_CONFIGURATION",
                $"{field} is outside its allowed range.");
        }

        return value;
    }
}
