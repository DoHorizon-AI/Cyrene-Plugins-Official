// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 DirectPluginRuntimeService.cs                                        │
// │  Namespace: Cyrene.Plugin.RuntimeHost                                    │
// │  Role: gRPC server implementation for the canonical plugin runtime.     │
// │                                                                         │
// │  模块职责：实现规范 DirectPluginRuntime gRPC 服务                           │
// └─────────────────────────────────────────────────────────────────────────┘

using Cyrene.Plugin.Runtime.V1;
using Grpc.Core;

namespace Cyrene.Plugin.RuntimeHost;

/// <summary>
/// Serves the canonical DirectPluginRuntime contract without a second transport hop.
/// <para>直接实现规范 DirectPluginRuntime 合约，不增加额外传输中转。</para>
/// </summary>
public sealed class DirectPluginRuntimeService : DirectPluginRuntime.DirectPluginRuntimeBase
{
    private readonly IDirectInvocationDispatcher _dispatcher;
    private readonly RuntimeMetadata _metadata;
    private readonly RuntimeReadiness _readiness;

    public DirectPluginRuntimeService(
        IDirectInvocationDispatcher dispatcher,
        RuntimeMetadata metadata,
        RuntimeReadiness readiness)
    {
        _dispatcher = dispatcher;
        _metadata = metadata;
        _readiness = readiness;
    }

    public override async Task<DirectInvocationResponse> Invoke(
        DirectInvocationRequest request,
        ServerCallContext context)
    {
        try
        {
            InvocationResult result = await _dispatcher.InvokeAsync(request, context.CancellationToken);
            return ToResponse(result);
        }
        catch (OperationCanceledException) when (context.CancellationToken.IsCancellationRequested)
        {
            return ToResponse(InvocationResult.Failure(
                DirectInvocationError.Types.Code.Cancelled,
                "Invocation cancelled by caller.",
                domainCode: "CALLER_CANCELLED"));
        }
    }

    public override async Task InvokeStream(
        DirectInvocationRequest request,
        IServerStreamWriter<DirectStreamItem> responseStream,
        ServerCallContext context)
    {
        try
        {
            await foreach (DirectStreamItem item in _dispatcher.InvokeStreamAsync(
                               request,
                               context.CancellationToken))
            {
                await responseStream.WriteAsync(item);
            }

            await responseStream.WriteAsync(new DirectStreamItem { End = new DirectStreamEnd() });
        }
        catch (OperationCanceledException) when (context.CancellationToken.IsCancellationRequested)
        {
            await responseStream.WriteAsync(new DirectStreamItem
            {
                Error = new DirectInvocationError
                {
                    Code = DirectInvocationError.Types.Code.Cancelled,
                    Message = "Stream cancelled by caller.",
                    DomainCode = "CALLER_CANCELLED"
                }
            });
        }
    }

    public override Task<HealthResponse> Health(
        HealthRequest request,
        ServerCallContext context)
    {
        var response = new HealthResponse
        {
            Status = _readiness.IsServing
                ? HealthResponse.Types.Status.Serving
                : HealthResponse.Types.Status.NotServing,
            PluginId = _metadata.PluginId,
            PluginVersion = _metadata.Version
        };
        response.Capabilities.Add(_metadata.Capabilities);
        return Task.FromResult(response);
    }

    private static DirectInvocationResponse ToResponse(InvocationResult result)
    {
        if (result.Payload is not null)
        {
            return new DirectInvocationResponse { Payload = result.Payload };
        }

        return new DirectInvocationResponse
        {
            Error = result.Error ?? new DirectInvocationError
            {
                Code = DirectInvocationError.Types.Code.ExecutionFailed,
                Message = "Invocation returned no result.",
                DomainCode = "EMPTY_DISPATCH_RESULT"
            }
        };
    }
}
