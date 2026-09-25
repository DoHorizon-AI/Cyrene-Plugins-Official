// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqHostInstallation.cs                                                 │
// │  Namespace: Cyrene.Im.Core                                                 │
// │  Role: Deterministic QQ Host installation and manifest validation.        │
// │                                                                         │
// │  模块职责：校验操作员选择的 QQ Host 安装，并拒绝模糊或漂移的路径声明            │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;

namespace Cyrene.Im.Core;

internal sealed record QqHostInstallation(string HostExecutable, string DataDirectory);

/// <summary>Resolves exactly one binding-local QQ Host installation.</summary>
/// <remarks>中文：解析且只解析一个 binding 本地的 QQ Host 安装项。</remarks>
internal static class QqHostInstallationResolver
{
    private const string ManifestSchema = "cyrene.qq.installation.v1";
    private const string SupportedPlatform = "linux-x86_64";
    private const int MaxManifestBytes = 64 * 1024;

    public static QqHostInstallation Resolve(QqHostLaunchConfiguration configuration)
    {
        if (configuration.Platform != SupportedPlatform)
        {
            throw new QqHostException(
                "UNSUPPORTED_VERSION",
                "only linux-x86_64 QQ Host installations are supported");
        }

        QqHostInstallation explicitInstallation = ResolveExplicit(
            configuration.HostExecutable,
            configuration.DataDirectory,
            configuration.RequiredClientVersion);
        if (string.IsNullOrWhiteSpace(configuration.InstallationManifest))
        {
            return explicitInstallation;
        }

        QqHostInstallation manifestInstallation = ResolveManifest(
            configuration.InstallationManifest,
            configuration.RequiredClientVersion);
        if (!StringComparer.Ordinal.Equals(
                explicitInstallation.HostExecutable,
                manifestInstallation.HostExecutable)
            || !StringComparer.Ordinal.Equals(
                explicitInstallation.DataDirectory,
                manifestInstallation.DataDirectory))
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                "QQ installation manifest does not match configured paths");
        }

        return manifestInstallation;
    }

    private static QqHostInstallation ResolveManifest(
        string manifestPath,
        string requiredClientVersion)
    {
        string canonicalManifest = ResolveRegularFile(
            manifestPath,
            "installation_manifest",
            "NO_INSTALLATION");
        byte[] raw;
        try
        {
            raw = File.ReadAllBytes(canonicalManifest);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            throw new QqHostException(
                "NO_INSTALLATION",
                "QQ installation manifest cannot be read");
        }

        if (raw.Length > MaxManifestBytes)
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                "QQ installation manifest exceeds the size limit");
        }

        using JsonDocument document = ParseManifest(raw);
        JsonElement root = document.RootElement;
        HashSet<string> expectedFields = new(StringComparer.Ordinal)
        {
            "schema",
            "installation_id",
            "host_executable",
            "data_dir",
            "client_version",
            "platform",
            "architecture"
        };
        foreach (JsonProperty property in root.EnumerateObject())
        {
            if (!expectedFields.Contains(property.Name))
            {
                throw new QqHostException(
                    "INVALID_REQUEST",
                    $"QQ installation manifest has unknown field '{property.Name}'");
            }
        }

        if (RequiredPropertyText(root, "schema") != ManifestSchema)
        {
            throw new QqHostException(
                "UNSUPPORTED_VERSION",
                "QQ installation manifest schema is unsupported");
        }

        string manifestVersion = RequiredPropertyText(root, "client_version");
        if (manifestVersion != requiredClientVersion)
        {
            throw new QqHostException(
                "UNSUPPORTED_VERSION",
                "QQ installation manifest build does not match the allow-list");
        }

        if (RequiredPropertyText(root, "platform") != SupportedPlatform)
        {
            throw new QqHostException(
                "UNSUPPORTED_VERSION",
                "QQ installation platform is unsupported");
        }

        string architecture = RequiredPropertyText(root, "architecture").ToLowerInvariant() switch
        {
            "amd64" or "x86-64" => "x86_64",
            var value => value
        };
        if (architecture != "x86_64")
        {
            throw new QqHostException(
                "UNSUPPORTED_VERSION",
                "QQ installation architecture is unsupported");
        }

        if (root.TryGetProperty("installation_id", out JsonElement installationId))
        {
            _ = BoundedText(installationId, "installation_id");
        }

        return ResolveExplicit(
            RequiredPropertyText(root, "host_executable"),
            RequiredPropertyText(root, "data_dir"),
            manifestVersion);
    }

    private static QqHostInstallation ResolveExplicit(
        string hostExecutable,
        string dataDirectory,
        string clientVersion)
    {
        if (!Path.IsPathRooted(hostExecutable) || !Path.IsPathRooted(dataDirectory))
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                "QQ Host paths must be absolute");
        }

        string canonicalExecutable = ResolveExecutable(hostExecutable);
        string canonicalDataDirectory = ResolveDataDirectory(dataDirectory);
        if (string.IsNullOrWhiteSpace(clientVersion))
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                "client_version must be non-empty text");
        }

        return new QqHostInstallation(canonicalExecutable, canonicalDataDirectory);
    }

    private static string ResolveExecutable(string path)
    {
        if (!OperatingSystem.IsLinux())
        {
            throw new QqHostException(
                "UNSUPPORTED_VERSION",
                "QQNT direct discovery requires Linux x86_64");
        }

        string canonicalPath = ResolveRegularFile(
            path,
            "host_executable",
            "NO_INSTALLATION");
        UnixFileMode mode = File.GetUnixFileMode(canonicalPath);
        UnixFileMode executeBits = UnixFileMode.UserExecute
            | UnixFileMode.GroupExecute
            | UnixFileMode.OtherExecute;
        if ((mode & executeBits) == 0)
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                "QQ Host executable must be executable");
        }
        return canonicalPath;
    }

    private static string ResolveDataDirectory(string path)
    {
        string fullPath = Path.GetFullPath(path);
        try
        {
            if (File.Exists(fullPath) || Directory.Exists(fullPath))
            {
                FileAttributes attributes = File.GetAttributes(fullPath);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                {
                    throw new QqHostException(
                        "CAPABILITY_UNAVAILABLE",
                        "data_dir must not be a symlink");
                }

                if ((attributes & FileAttributes.Directory) == 0)
                {
                    throw new QqHostException(
                        "CAPABILITY_UNAVAILABLE",
                        "data_dir must be a directory");
                }
            }
        }
        catch (QqHostException)
        {
            throw;
        }
        catch (IOException)
        {
            throw new QqHostException(
                "CAPABILITY_UNAVAILABLE",
                "data_dir is unusable");
        }

        return fullPath;
    }

    private static string ResolveRegularFile(
        string path,
        string field,
        string missingCode)
    {
        if (!Path.IsPathRooted(path))
        {
            throw new QqHostException("INVALID_REQUEST", $"{field} must be absolute");
        }

        string fullPath = Path.GetFullPath(path);
        try
        {
            FileInfo file = new(fullPath);
            FileSystemInfo? resolved = file.ResolveLinkTarget(returnFinalTarget: true);
            string canonicalPath = resolved?.FullName ?? file.FullName;
            FileAttributes attributes = File.GetAttributes(canonicalPath);
            if ((attributes & FileAttributes.Directory) != 0)
            {
                throw new QqHostException(
                    "INVALID_REQUEST",
                    $"{field} must be a regular file");
            }

            return canonicalPath;
        }
        catch (QqHostException)
        {
            throw;
        }
        catch (FileNotFoundException)
        {
            throw new QqHostException(missingCode, $"QQ {field} does not exist");
        }
        catch (DirectoryNotFoundException)
        {
            throw new QqHostException(missingCode, $"QQ {field} does not exist");
        }
        catch (IOException)
        {
            throw new QqHostException(missingCode, $"QQ {field} cannot be read");
        }
        catch (UnauthorizedAccessException)
        {
            throw new QqHostException(missingCode, $"QQ {field} cannot be read");
        }
    }

    private static JsonDocument ParseManifest(byte[] raw)
    {
        try
        {
            JsonDocument document = JsonDocument.Parse(raw);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                document.Dispose();
                throw new QqHostException(
                    "INVALID_REQUEST",
                    "QQ installation manifest must be an object");
            }

            return document;
        }
        catch (QqHostException)
        {
            throw;
        }
        catch (JsonException)
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                "QQ installation manifest is not valid UTF-8 JSON");
        }
    }

    private static string RequiredPropertyText(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out JsonElement value))
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                $"QQ installation manifest field {name} must be bounded text");
        }

        return BoundedText(value, name);
    }

    private static string BoundedText(JsonElement value, string name)
    {
        if (value.ValueKind != JsonValueKind.String)
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                $"{name} must be bounded text");
        }

        string? text = value.GetString();
        if (string.IsNullOrWhiteSpace(text) || text.Length > 512)
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                $"{name} must be bounded text");
        }

        return text.Trim();
    }
}
