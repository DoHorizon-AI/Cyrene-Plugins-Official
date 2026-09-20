// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 DirectInvocationDispatcher.cs                                        │
// │  Namespace: Cyrene.Plugin.RuntimeHost                                    │
// │  Role: Explicit capability dispatch for the DirectPluginRuntime seam.    │
// │                                                                         │
// │  模块职责：通过显式注册表分派 DirectPluginRuntime 能力调用                   │
// └─────────────────────────────────────────────────────────────────────────┘

using Cyrene.Plugin.Runtime.V1;

namespace Cyrene.Plugin.RuntimeHost;

/// <summary>
/// The result of an invocation after capability validation and dispatch.
/// <para>表示能力校验和分派完成后的调用结果。</para>
/// </summary>
public sealed record InvocationResult(
    DirectPayload? Payload,
    DirectInvocationError? Error)
{
    public static InvocationResult Success(string typeUrl, byte[] value, string eventType = "") =>
        new(new DirectPayload
        {
            TypeUrl = typeUrl,
            Value = Google.Protobuf.ByteString.CopyFrom(value),
            EventType = eventType
        }, null);

    public static InvocationResult Failure(
        DirectInvocationError.Types.Code code,
        string message,
        bool retryable = false,
        string domainCode = "") =>
        new(null, new DirectInvocationError
        {
            Code = code,
            Message = message,
            Retryable = retryable,
            DomainCode = domainCode
        });
}

/// <summary>
/// Dispatches direct invocations without runtime reflection or dynamic loading.
/// <para>不使用运行时反射或动态加载来分派 DirectPluginRuntime 调用。</para>
/// </summary>
public interface IDirectInvocationDispatcher
{
    ValueTask<InvocationResult> InvokeAsync(
        DirectInvocationRequest request,
        CancellationToken cancellationToken);

    IAsyncEnumerable<DirectStreamItem> InvokeStreamAsync(
        DirectInvocationRequest request,
        CancellationToken cancellationToken);
}

/// <summary>
/// Initial dispatcher used before a concrete OneBot profile is configured.
/// <para>在具体 OneBot profile 配置完成前使用的初始分派器。</para>
/// </summary>
public sealed class NotConfiguredInvocationDispatcher : IDirectInvocationDispatcher
{
    public const int MaxPayloadBytes = 8 * 1024 * 1024;

    private static readonly HashSet<string> SupportedCapabilities =
        new HashSet<string>(StringComparer.Ordinal)
        {
            "message.connector.v1",
            "qq.client.v1"
        };

    public ValueTask<InvocationResult> InvokeAsync(
        DirectInvocationRequest request,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        InvocationResult validation = Validate(request);
        if (validation.Error is not null)
        {
            return ValueTask.FromResult(validation);
        }

        return ValueTask.FromResult(InvocationResult.Failure(
            DirectInvocationError.Types.Code.MethodNotFound,
            $"Method '{request.Method}' is not configured for capability '{request.Capability}'.",
            domainCode: "CONNECTOR_PROFILE_NOT_CONFIGURED"));
    }

    public async IAsyncEnumerable<DirectStreamItem> InvokeStreamAsync(
        DirectInvocationRequest request,
        [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        InvocationResult validation = Validate(request);
        if (validation.Error is not null)
        {
            yield return ToStreamItem(validation.Error);
            yield break;
        }

        await Task.CompletedTask;
        yield return ToStreamItem(new DirectInvocationError
        {
            Code = DirectInvocationError.Types.Code.MethodNotFound,
            Message = $"Streaming method '{request.Method}' is not configured for capability '{request.Capability}'.",
            DomainCode = "CONNECTOR_PROFILE_NOT_CONFIGURED"
        });
    }

    private static InvocationResult Validate(DirectInvocationRequest request)
    {
        if (string.IsNullOrWhiteSpace(request.Capability)
            || string.IsNullOrWhiteSpace(request.InterfaceVersion)
            || string.IsNullOrWhiteSpace(request.Method))
        {
            return InvocationResult.Failure(
                DirectInvocationError.Types.Code.InvalidRequest,
                "capability, interface_version, and method are required.",
                domainCode: "REQUIRED_FIELD_MISSING");
        }

        if (!SupportedCapabilities.Contains(request.Capability))
        {
            return InvocationResult.Failure(
                DirectInvocationError.Types.Code.MethodNotFound,
                $"Capability '{request.Capability}' is not registered.",
                domainCode: "CAPABILITY_NOT_REGISTERED");
        }

        if (request.Payload.Length > MaxPayloadBytes)
        {
            return InvocationResult.Failure(
                DirectInvocationError.Types.Code.InvalidRequest,
                $"Payload exceeds the {MaxPayloadBytes}-byte limit.",
                domainCode: "PAYLOAD_TOO_LARGE");
        }

        return new InvocationResult(null, null);
    }

    private static DirectStreamItem ToStreamItem(DirectInvocationError error) => new()
    {
        Error = error
    };
}
