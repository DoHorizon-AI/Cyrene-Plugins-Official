// ┌─────────────────────────────────────────────────────────────────────────┐
// │ 📄 PluginNativeAbi.cs                                                   │
// │ Namespace: Cyrene.Plugin.NativeLib                                      │
// │ Role: Native AOT C ABI v1 export layer with explicit UnmanagedCallers.  │
// │                                                                         │
// │ 模块职责：Native AOT C ABI v1 导出层，显式声明 UnmanagedCallersOnly       │
// │ · 导出 cyrene_plugin_*_v1 7 个标准符号                                    │
// │ · 全入口通过 try/catch 封装，杜绝任何 C# 异常穿越 ABI 边界                  │
// │ · 跟踪非托管内存分配，防范 double-free 与非法释放                          │
// └─────────────────────────────────────────────────────────────────────────┘

using System;
using System.Collections.Generic;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Cyrene.Plugin.Core;

namespace Cyrene.Plugin.NativeLib;

#region C ABI Struct Definitions Matching cyrene_plugin_abi_v1.h

[StructLayout(LayoutKind.Sequential)]
public unsafe struct CapabilityDescriptorV1
{
    public byte* CapabilityId;
    public uint VersionMajor;
    public uint VersionMinor;
}

[StructLayout(LayoutKind.Sequential)]
public unsafe struct ApiInfoV1
{
    public uint AbiVersion;
    public uint InterfaceVersionMajor;
    public uint InterfaceVersionMinor;
    public byte* ImplementationName;
    public byte* ImplementationVersion;
    public uint CapabilityCount;
    public CapabilityDescriptorV1* Capabilities;
}

[StructLayout(LayoutKind.Sequential)]
public unsafe struct CreateOptionsV1
{
    public byte* ConfigJson;
    public ulong MaxConcurrency;
    public ulong MemoryLimitBytes;
}

[StructLayout(LayoutKind.Sequential)]
public unsafe struct InvokeRequestV1
{
    public byte* Method;
    public byte* InputTypeUrl;
    public byte* InputData;
    public ulong InputSize;
    public ulong DeadlineMs;
}

[StructLayout(LayoutKind.Sequential)]
public unsafe struct InvokeResponseV1
{
    public int StatusCode;
    public byte* ErrorMessage;
    public byte* OutputTypeUrl;
    public byte* OutputData;
    public ulong OutputSize;
}

[StructLayout(LayoutKind.Sequential)]
public unsafe struct StreamEventV1
{
    public ulong SequenceNumber;
    public int EventType;
    public int StatusCode;
    public byte* ErrorMessage;
    public byte* TypeUrl;
    public byte* PayloadData;
    public ulong PayloadSize;
    public byte IsTerminal;
}

[StructLayout(LayoutKind.Sequential)]
public unsafe struct StreamRequestV1
{
    public byte* Method;
    public byte* InputTypeUrl;
    public byte* InputData;
    public ulong InputSize;
    public ulong DeadlineMs;
}

#endregion

internal sealed class NativePluginInstance : IDisposable
{
    private readonly PluginCoreInstance _core;
    private readonly HashSet<IntPtr> _allocations = new();
    private readonly object _allocLock = new();
    private readonly Dictionary<int, CancellationTokenSource> _cancelTokens = new();
    private readonly object _cancelLock = new();
    private int _nextCancelId = 1;
    private int _isDisposed;

    public NativePluginInstance(string? configJson, ulong maxConcurrency, ulong memoryLimitBytes)
    {
        _core = new PluginCoreInstance(configJson, maxConcurrency, memoryLimitBytes);
    }

    public PluginCoreInstance Core => _core;
    public bool IsDisposed => Volatile.Read(ref _isDisposed) != 0;

    public unsafe byte* AllocateTrackedBuffer(ReadOnlySpan<byte> data)
    {
        byte* ptr = (byte*)NativeMemory.Alloc((nuint)data.Length);
        if (ptr == null)
        {
            return null;
        }

        fixed (byte* src = data)
        {
            Buffer.MemoryCopy(src, ptr, data.Length, data.Length);
        }

        lock (_allocLock)
        {
            _allocations.Add((IntPtr)ptr);
        }
        return ptr;
    }

    public unsafe byte* AllocateTrackedString(string str)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(str + "\0");
        return AllocateTrackedBuffer(bytes);
    }

    public unsafe StatusCode FreeBuffer(byte* ptr)
    {
        if (ptr == null)
        {
            return StatusCode.Ok;
        }

        IntPtr intPtr = (IntPtr)ptr;
        lock (_allocLock)
        {
            if (!_allocations.Remove(intPtr))
            {
                // Double free or foreign pointer
                return StatusCode.InvalidArgument;
            }
        }

        NativeMemory.Free(ptr);
        return StatusCode.Ok;
    }

    public (int TokenId, CancellationToken Token) RegisterCancelToken()
    {
        lock (_cancelLock)
        {
            int id = _nextCancelId++;
            var cts = new CancellationTokenSource();
            _cancelTokens[id] = cts;
            return (id, cts.Token);
        }
    }

    public void UnregisterCancelToken(int tokenId)
    {
        lock (_cancelLock)
        {
            if (_cancelTokens.Remove(tokenId, out var cts))
            {
                cts.Dispose();
            }
        }
    }

    public StatusCode Cancel(int tokenId)
    {
        lock (_cancelLock)
        {
            if (_cancelTokens.TryGetValue(tokenId, out var cts))
            {
                cts.Cancel();
            }
            return StatusCode.Ok;
        }
    }

    public unsafe void Dispose()
    {
        if (Interlocked.Exchange(ref _isDisposed, 1) != 0)
        {
            return;
        }

        _core.Dispose();

        lock (_allocLock)
        {
            foreach (var addr in _allocations)
            {
                NativeMemory.Free((void*)addr);
            }
            _allocations.Clear();
        }

        lock (_cancelLock)
        {
            foreach (var cts in _cancelTokens.Values)
            {
                cts.Dispose();
            }
            _cancelTokens.Clear();
        }
    }
}

public static unsafe class PluginNativeAbi
{
    private const uint CyreneAbiVersion1 = 1;
    private const ulong MaxPayloadSize = 64UL * 1024UL * 1024UL;

    private static readonly byte[] ImplementationNameBytes = Encoding.UTF8.GetBytes("cyrene-plugin-dotnet\0");
    private static readonly byte[] ImplementationVersionBytes = Encoding.UTF8.GetBytes("0.1.0\0");

    private static readonly byte[] CapModelBytes = Encoding.UTF8.GetBytes("model.provider.v1\0");

    private static CapabilityDescriptorV1[]? s_capabilities;
    private static GCHandle s_capHandle;
    private static GCHandle s_nameHandle;
    private static GCHandle s_verHandle;
    private static GCHandle s_cap1Handle;

    private static readonly UTF8Encoding StrictUtf8 = new(false, true);

    private static unsafe bool TryValidateUtf8(byte* ptr, out string? result)
    {
        result = null;
        if (ptr == null) return true;
        int len = 0;
        while (ptr[len] != 0)
        {
            len++;
            if (len > 4096) return false; // Guard against unterminated strings
        }
        try
        {
            result = StrictUtf8.GetString(ptr, len);
            return true;
        }
        catch
        {
            return false;
        }
    }

    static PluginNativeAbi()
    {
        s_nameHandle = GCHandle.Alloc(ImplementationNameBytes, GCHandleType.Pinned);
        s_verHandle = GCHandle.Alloc(ImplementationVersionBytes, GCHandleType.Pinned);
        s_cap1Handle = GCHandle.Alloc(CapModelBytes, GCHandleType.Pinned);

        // Only contract-backed capabilities are advertised over the C ABI;
        // speech/rerank have no canonical contract and no implementation (W6-3).
        s_capabilities = new CapabilityDescriptorV1[]
        {
            new() { CapabilityId = (byte*)s_cap1Handle.AddrOfPinnedObject(), VersionMajor = 1, VersionMinor = 0 },
        };
        s_capHandle = GCHandle.Alloc(s_capabilities, GCHandleType.Pinned);
    }

    [UnmanagedCallersOnly(EntryPoint = "cyrene_plugin_get_api_v1")]
    public static int GetApiV1(uint requestedAbiVersion, ApiInfoV1* outInfo)
    {
        try
        {
            if (outInfo == null)
            {
                return (int)StatusCode.InvalidArgument;
            }

            if (requestedAbiVersion != CyreneAbiVersion1)
            {
                outInfo->AbiVersion = CyreneAbiVersion1;
                return (int)StatusCode.FailedPrecondition;
            }

            outInfo->AbiVersion = CyreneAbiVersion1;
            outInfo->InterfaceVersionMajor = 1;
            outInfo->InterfaceVersionMinor = 0;
            outInfo->ImplementationName = (byte*)s_nameHandle.AddrOfPinnedObject();
            outInfo->ImplementationVersion = (byte*)s_verHandle.AddrOfPinnedObject();
            outInfo->CapabilityCount = (uint)s_capabilities!.Length;
            outInfo->Capabilities = (CapabilityDescriptorV1*)s_capHandle.AddrOfPinnedObject();

            return (int)StatusCode.Ok;
        }
        catch
        {
            return (int)StatusCode.Internal;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "cyrene_plugin_create_v1")]
    public static int CreateV1(CreateOptionsV1* options, void** outHandle)
    {
        try
        {
            if (outHandle == null)
            {
                return (int)StatusCode.InvalidArgument;
            }

            string? config = null;
            ulong maxConcurrency = 0;
            ulong memoryLimit = 0;

            if (options != null)
            {
                if (options->ConfigJson != null)
                {
                    config = Marshal.PtrToStringUTF8((IntPtr)options->ConfigJson);
                }
                maxConcurrency = options->MaxConcurrency;
                memoryLimit = options->MemoryLimitBytes;
            }

            var instance = new NativePluginInstance(config, maxConcurrency, memoryLimit);
            GCHandle handle = GCHandle.Alloc(instance);
            *outHandle = (void*)GCHandle.ToIntPtr(handle);

            return (int)StatusCode.Ok;
        }
        catch
        {
            return (int)StatusCode.Internal;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "cyrene_plugin_invoke_v1")]
    public static int InvokeV1(void* handle, InvokeRequestV1* request, InvokeResponseV1* outResponse)
    {
        try
        {
            if (handle == null || request == null || outResponse == null)
            {
                return (int)StatusCode.InvalidArgument;
            }

            var gch = GCHandle.FromIntPtr((IntPtr)handle);
            if (gch.Target is not NativePluginInstance instance || instance.IsDisposed)
            {
                return (int)StatusCode.FailedPrecondition;
            }

            if (request->InputSize > MaxPayloadSize)
            {
                outResponse->StatusCode = (int)StatusCode.ResourceExhausted;
                outResponse->ErrorMessage = instance.AllocateTrackedString("Payload size exceeds 64 MB maximum limit");
                outResponse->OutputTypeUrl = null;
                outResponse->OutputData = null;
                outResponse->OutputSize = 0;
                return (int)StatusCode.ResourceExhausted;
            }

            if (request->Method == null)
            {
                return (int)StatusCode.InvalidArgument;
            }

            if (!TryValidateUtf8(request->Method, out string? method) || method == null)
            {
                return (int)StatusCode.InvalidArgument;
            }

            string? typeUrl = null;
            if (request->InputTypeUrl != null)
            {
                if (!TryValidateUtf8(request->InputTypeUrl, out typeUrl))
                {
                    return (int)StatusCode.InvalidArgument;
                }
            }

            ReadOnlySpan<byte> inputSpan = request->InputData != null && request->InputSize > 0
                ? new ReadOnlySpan<byte>(request->InputData, (int)request->InputSize)
                : ReadOnlySpan<byte>.Empty;

            var result = instance.Core.Invoke(method, typeUrl, inputSpan, request->DeadlineMs);

            outResponse->StatusCode = (int)result.Status;
            outResponse->ErrorMessage = result.ErrorMessage != null ? instance.AllocateTrackedString(result.ErrorMessage) : null;
            outResponse->OutputTypeUrl = result.OutputTypeUrl != null ? instance.AllocateTrackedString(result.OutputTypeUrl) : null;
            outResponse->OutputData = result.OutputData != null ? instance.AllocateTrackedBuffer(result.OutputData) : null;
            outResponse->OutputSize = (ulong)(result.OutputData?.Length ?? 0);

            return (int)result.Status;
        }
        catch
        {
            return (int)StatusCode.Internal;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "cyrene_plugin_invoke_stream_v1")]
    public static int InvokeStreamV1(
        void* handle,
        StreamRequestV1* request,
        delegate* unmanaged[Cdecl]<StreamEventV1*, void*, void> callback,
        void* userData,
        void** outCancelHandle)
    {
        try
        {
            if (handle == null || request == null || callback == null)
            {
                return (int)StatusCode.InvalidArgument;
            }

            var gch = GCHandle.FromIntPtr((IntPtr)handle);
            if (gch.Target is not NativePluginInstance instance || instance.IsDisposed)
            {
                return (int)StatusCode.FailedPrecondition;
            }

            if (request->InputSize > MaxPayloadSize)
            {
                return (int)StatusCode.ResourceExhausted;
            }

            var (tokenId, cancelToken) = instance.RegisterCancelToken();
            if (outCancelHandle != null)
            {
                *outCancelHandle = (void*)(IntPtr)tokenId;
            }

            string method = Marshal.PtrToStringUTF8((IntPtr)request->Method) ?? "Stream";
            string? typeUrl = request->InputTypeUrl != null ? Marshal.PtrToStringUTF8((IntPtr)request->InputTypeUrl) : null;

            byte[] inputBytes = request->InputData != null && request->InputSize > 0
                ? new ReadOnlySpan<byte>(request->InputData, (int)request->InputSize).ToArray()
                : Array.Empty<byte>();

            var chunks = instance.Core.InvokeStream(method, typeUrl, inputBytes, request->DeadlineMs, () => cancelToken.IsCancellationRequested);

            byte[]? typeUrlUtf8 = typeUrl != null ? Encoding.UTF8.GetBytes(typeUrl + "\0") : null;

            foreach (var chunk in chunks)
            {
                byte[]? errUtf8 = chunk.ErrorMessage != null ? Encoding.UTF8.GetBytes(chunk.ErrorMessage + "\0") : null;

                fixed (byte* pErr = errUtf8)
                fixed (byte* pType = typeUrlUtf8)
                fixed (byte* pData = chunk.PayloadData)
                {
                    var evt = new StreamEventV1
                    {
                        SequenceNumber = chunk.SequenceNumber,
                        EventType = chunk.EventType,
                        StatusCode = (int)chunk.Status,
                        ErrorMessage = pErr,
                        TypeUrl = pType,
                        PayloadData = pData,
                        PayloadSize = (ulong)(chunk.PayloadData?.Length ?? 0),
                        IsTerminal = (byte)(chunk.IsTerminal ? 1 : 0)
                    };

                    callback(&evt, userData);
                }
            }

            instance.UnregisterCancelToken(tokenId);
            return (int)StatusCode.Ok;
        }
        catch
        {
            return (int)StatusCode.Internal;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "cyrene_plugin_cancel_v1")]
    public static int CancelV1(void* handle, void* cancelHandle)
    {
        try
        {
            if (handle == null)
            {
                return (int)StatusCode.InvalidArgument;
            }

            var gch = GCHandle.FromIntPtr((IntPtr)handle);
            if (gch.Target is not NativePluginInstance instance || instance.IsDisposed)
            {
                return (int)StatusCode.FailedPrecondition;
            }

            int tokenId = (int)(IntPtr)cancelHandle;
            return (int)instance.Cancel(tokenId);
        }
        catch
        {
            return (int)StatusCode.Internal;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "cyrene_plugin_free_buffer_v1")]
    public static int FreeBufferV1(void* handle, void* buffer)
    {
        try
        {
            if (buffer == null)
            {
                return (int)StatusCode.Ok;
            }

            if (handle == null)
            {
                return (int)StatusCode.InvalidArgument;
            }

            var gch = GCHandle.FromIntPtr((IntPtr)handle);
            if (gch.Target is not NativePluginInstance instance)
            {
                return (int)StatusCode.InvalidArgument;
            }

            return (int)instance.FreeBuffer((byte*)buffer);
        }
        catch
        {
            return (int)StatusCode.Internal;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "cyrene_plugin_destroy_v1")]
    public static int DestroyV1(void* handle)
    {
        try
        {
            if (handle == null)
            {
                return (int)StatusCode.InvalidArgument;
            }

            var gch = GCHandle.FromIntPtr((IntPtr)handle);
            if (gch.Target is not NativePluginInstance instance || instance.IsDisposed)
            {
                return (int)StatusCode.InvalidArgument;
            }

            instance.Dispose();
            gch.Free();

            return (int)StatusCode.Ok;
        }
        catch
        {
            return (int)StatusCode.Internal;
        }
    }
}
