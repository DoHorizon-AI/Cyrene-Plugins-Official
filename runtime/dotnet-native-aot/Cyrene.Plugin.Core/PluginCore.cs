// ┌─────────────────────────────────────────────────────────────────────────┐
// │ 📄 PluginCore.cs                                                        │
// │ Namespace: Cyrene.Plugin.Core                                           │
// │ Role: Shared business and capability dispatch logic for C# plugins.    │
// │                                                                         │
// │ 模块职责：C# 原生插件共享业务与能力调度逻辑                              │
// └─────────────────────────────────────────────────────────────────────────┘

using System;
using System.Collections.Generic;
using System.Text;

namespace Cyrene.Plugin.Core;

public record CapabilityInfo(string CapabilityId, uint VersionMajor, uint VersionMinor);

public record InvokeResult(
    StatusCode Status,
    string? ErrorMessage,
    string? OutputTypeUrl,
    byte[]? OutputData
);

public record StreamChunk(
    ulong SequenceNumber,
    int EventType,
    StatusCode Status,
    string? ErrorMessage,
    string? TypeUrl,
    byte[]? PayloadData,
    bool IsTerminal
);

public interface IPluginInstance : IDisposable
{
    InvokeResult Invoke(string method, string? inputTypeUrl, ReadOnlySpan<byte> inputData, ulong deadlineMs);
    IEnumerable<StreamChunk> InvokeStream(string method, string? inputTypeUrl, byte[] inputData, ulong deadlineMs, Func<bool> isCancelled);
}

/// <summary>
/// Core implementation of the plugin capability provider.
/// </summary>
public class PluginCoreInstance : IPluginInstance
{
    private readonly string? _configJson;
    private readonly ulong _maxConcurrency;
    private readonly ulong _memoryLimitBytes;
    private bool _disposed;

    public PluginCoreInstance(string? configJson, ulong maxConcurrency, ulong memoryLimitBytes)
    {
        _configJson = configJson;
        _maxConcurrency = maxConcurrency;
        _memoryLimitBytes = memoryLimitBytes;
    }

    public static IReadOnlyList<CapabilityInfo> SupportedCapabilities { get; } = new List<CapabilityInfo>
    {
        new("model.provider.v1", 1, 0),
        new("speech.provider.v1", 1, 0),
        new("rerank.provider.v1", 1, 0)
    };

    public InvokeResult Invoke(string method, string? inputTypeUrl, ReadOnlySpan<byte> inputData, ulong deadlineMs)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);

        if (deadlineMs > 0 && (ulong)DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() > deadlineMs)
        {
            return new InvokeResult(StatusCode.DeadlineExceeded, "Deadline exceeded prior to invocation", null, null);
        }

        switch (method)
        {
            case "Echo":
                return new InvokeResult(StatusCode.Ok, null, inputTypeUrl ?? "", inputData.ToArray());

            case "model.provider.v1/Chat":
                return new InvokeResult(
                    StatusCode.Unavailable,
                    "Model provider binding is not configured",
                    null,
                    null);

            case "health":
                return new InvokeResult(
                    StatusCode.Ok,
                    null,
                    "type.cyrene.io/cyrene.plugin.runtime.v1.HealthResponse",
                    new byte[] { 0x08, 0x01 });

            default:
                return new InvokeResult(StatusCode.Unimplemented, $"Method '{method}' is not registered", null, null);
        }
    }

    public IEnumerable<StreamChunk> InvokeStream(string method, string? inputTypeUrl, byte[] inputData, ulong deadlineMs, Func<bool> isCancelled)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);

        if (!string.Equals(method, "EchoStream", StringComparison.Ordinal)
            && !string.Equals(method, "StreamEcho", StringComparison.Ordinal))
        {
            yield return new StreamChunk(
                0,
                3,
                StatusCode.Unimplemented,
                $"Streaming method '{method}' is not registered",
                null,
                null,
                true);
            yield break;
        }

        const int chunkCount = 3;
        byte[] payload = inputData.Length > 0 ? inputData : Encoding.UTF8.GetBytes("chunk");

        for (ulong i = 0; i < chunkCount; i++)
        {
            if (isCancelled())
            {
                yield return new StreamChunk(i, 2, StatusCode.Cancelled, "Stream cancelled by caller", null, null, true);
                yield break;
            }

            if (deadlineMs > 0 && (ulong)DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() > deadlineMs)
            {
                yield return new StreamChunk(i, 3, StatusCode.DeadlineExceeded, "Stream deadline exceeded", null, null, true);
                yield break;
            }

            yield return new StreamChunk(i, 0, StatusCode.Ok, null, inputTypeUrl, payload, false);
        }

        yield return new StreamChunk(chunkCount, 1, StatusCode.Ok, null, null, null, true);
    }

    public void Dispose()
    {
        _disposed = true;
        GC.SuppressFinalize(this);
    }
}
