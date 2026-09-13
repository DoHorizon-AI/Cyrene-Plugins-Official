// ┌─────────────────────────────────────────────────────────────────────────┐
// │ 📄 PluginJsonModels.cs                                                  │
// │ Namespace: Cyrene.Plugin.Core                                           │
// │ Role: JSON data transfer models for manifests, configs, and health.     │
// │                                                                         │
// │ 模块职责：用于 Manifest、配置、健康检查的强类型 JSON 数据模型 (T33)          │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Collections.Generic;

namespace Cyrene.Plugin.Core;

public class PluginEntrypoint
{
    public string Type { get; set; } = "native_executable";
    public string Path { get; set; } = "./cyrene-plugin-host";
}

public class PluginCapabilityDeclaration
{
    public string Id { get; set; } = "";
    public string Version { get; set; } = "1.0";
}

public class PluginManifest
{
    public string ManifestVersion { get; set; } = "1.0.0";
    public string PluginId { get; set; } = "";
    public string Version { get; set; } = "0.1.0";
    public string Name { get; set; } = "";
    public PluginEntrypoint Entrypoint { get; set; } = new();
    public List<PluginCapabilityDeclaration> Capabilities { get; set; } = new();
}

public class PluginConfig
{
    public ulong MaxConcurrency { get; set; }
    public ulong MemoryLimitBytes { get; set; }
    public Dictionary<string, string> Settings { get; set; } = new();
}

public class HealthStatusResponse
{
    public string Status { get; set; } = "SERVING";
    public double UptimeSeconds { get; set; }
    public uint ActiveOperations { get; set; }
}

public class UnaryInvokePayload
{
    public string Method { get; set; } = "";
    public string? TypeUrl { get; set; }
    public string? PayloadBase64 { get; set; }
    public ulong DeadlineMs { get; set; }
}

public class UnaryInvokeResult
{
    public int StatusCode { get; set; }
    public string? ErrorMessage { get; set; }
    public string? TypeUrl { get; set; }
    public string? OutputBase64 { get; set; }
}
