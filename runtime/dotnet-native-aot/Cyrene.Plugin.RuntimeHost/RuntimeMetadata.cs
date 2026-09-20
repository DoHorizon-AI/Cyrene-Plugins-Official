// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 RuntimeMetadata.cs                                                   │
// │  Namespace: Cyrene.Plugin.RuntimeHost                                    │
// │  Role: Immutable metadata and readiness state for the native connector.  │
// │                                                                         │
// │  模块职责：定义 Native AOT 连接器的不可变元数据与就绪状态                   │
// └─────────────────────────────────────────────────────────────────────────┘

namespace Cyrene.Plugin.RuntimeHost;

/// <summary>
/// Describes the plugin identity exposed by the DirectPluginRuntime health API.
/// <para>描述 DirectPluginRuntime 健康接口暴露的插件身份。</para>
/// </summary>
public sealed record RuntimeMetadata(
    string PluginId,
    string Version,
    IReadOnlyList<string> Capabilities);

/// <summary>
/// Represents the runtime readiness that is safe to publish to a caller.
/// <para>表示可以安全发布给调用方的运行时就绪状态。</para>
/// </summary>
public sealed record RuntimeReadiness(bool IsServing, string Reason)
{
    public static RuntimeReadiness NotServing(string reason) => new(false, reason);

    public static RuntimeReadiness Serving() => new(true, "READY");
}
