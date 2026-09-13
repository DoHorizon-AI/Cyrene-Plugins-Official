// ┌─────────────────────────────────────────────────────────────────────────┐
// │ 📄 PluginJsonContext.cs                                                 │
// │ Namespace: Cyrene.Plugin.Core                                           │
// │ Role: Source-generated JsonSerializerContext for zero-reflection JSON.  │
// │                                                                         │
// │ 模块职责：源码生成的 JsonSerializerContext，100% 杜绝运行时反射 (T33)      │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace Cyrene.Plugin.Core;

[JsonSourceGenerationOptions(
    WriteIndented = true,
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase
)]
[JsonSerializable(typeof(PluginManifest))]
[JsonSerializable(typeof(PluginConfig))]
[JsonSerializable(typeof(CapabilityInfo))]
[JsonSerializable(typeof(List<CapabilityInfo>))]
[JsonSerializable(typeof(HealthStatusResponse))]
[JsonSerializable(typeof(UnaryInvokePayload))]
[JsonSerializable(typeof(UnaryInvokeResult))]
[JsonSerializable(typeof(Dictionary<string, string>))]
public partial class PluginJsonContext : JsonSerializerContext
{
}
