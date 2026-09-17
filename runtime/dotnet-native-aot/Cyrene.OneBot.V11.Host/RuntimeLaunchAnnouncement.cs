// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 RuntimeLaunchAnnouncement.cs                                          │
// │  Namespace: Cyrene.OneBot.V11.Host                                       │
// │  Role: Source-generated startup announcement for package supervision.    │
// │                                                                         │
// │  模块职责：为包管理进程提供源码生成的启动公告                             │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json.Serialization;

namespace Cyrene.OneBot.V11.Host;

/// <summary>Stable stdout record consumed by the package process supervisor.</summary>
public sealed class RuntimeLaunchAnnouncement
{
    [JsonPropertyName("event")]
    public string Event { get; init; } = string.Empty;

    [JsonPropertyName("connection_ref")]
    public string ConnectionRef { get; init; } = string.Empty;

    [JsonPropertyName("capability")]
    public IReadOnlyList<string> Capability { get; init; } = Array.Empty<string>();

    [JsonPropertyName("capabilities")]
    public IReadOnlyList<string> Capabilities { get; init; } = Array.Empty<string>();

    [JsonPropertyName("interface_version")]
    public string InterfaceVersion { get; init; } = string.Empty;

    [JsonPropertyName("interface_versions")]
    public IReadOnlyList<string> InterfaceVersions { get; init; } = Array.Empty<string>();

    [JsonPropertyName("runtime_id")]
    public string RuntimeId { get; init; } = string.Empty;

}

[JsonSourceGenerationOptions(
    PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase,
    GenerationMode = JsonSourceGenerationMode.Metadata)]
[JsonSerializable(typeof(RuntimeLaunchAnnouncement))]
public partial class RuntimeLaunchJsonContext : JsonSerializerContext
{
}
