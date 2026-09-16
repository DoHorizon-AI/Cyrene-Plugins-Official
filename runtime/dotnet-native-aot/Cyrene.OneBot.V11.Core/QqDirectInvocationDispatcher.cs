// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqDirectInvocationDispatcher.cs                                      │
// │  Namespace: Cyrene.OneBot.V11.Core                                       │
// │  Role: Explicit qq.client.v1 dispatch over the QQ Host bridge.            │
// │                                                                         │
// │  模块职责：通过固定 operation 注册表将 qq.client.v1 调用送入 QQ Host         │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using Cyrene.Plugin.Runtime.V1;

namespace Cyrene.OneBot.V11.Core;

/// <summary>Dispatches fixed QQ extension operations through one Host client.</summary>
public sealed class QqDirectInvocationDispatcher : IDirectInvocationDispatcher
{
    public const string CapabilityId = "qq.client.v1";
    public const string InterfaceVersion = "1";
    public const string RequestTypeUrl = "type.cyrene.io/qq.client.v1.Request";
    public const string ResponseTypeUrl = "type.cyrene.io/qq.client.v1.Response";

    private readonly QqDirectProfile _profile;
    private readonly QqHostClient _host;

    public QqDirectInvocationDispatcher(QqDirectProfile profile, QqHostClient host)
    {
        _profile = profile;
        _host = host;
    }

    public async ValueTask<InvocationResult> InvokeAsync(
        DirectInvocationRequest request,
        CancellationToken cancellationToken)
    {
        try
        {
            ValidateEnvelope(request);
            using JsonDocument document = JsonDocument.Parse(request.Payload.ToByteArray());
            JsonElement root = document.RootElement;
            JsonElement parameters = ParseParameters(root);
            await _host.StartAsync(cancellationToken);
            JsonElement result = await _host.RequestAsync(
                request.Method,
                parameters,
                cancellationToken);
            QqHostOperationRegistry.TryGet(request.Method, out QqHostOperation? operation);
            QqClientResponse response = new()
            {
                Operation = request.Method,
                Status = "accepted",
                Result = result,
                Priority = operation!.Priority,
                Mapping = new QqOperationMapping
                {
                    Service = operation.Service,
                    Method = operation.Method
                }
            };
            return InvocationResult.Success(
                ResponseTypeUrl,
                JsonSerializer.SerializeToUtf8Bytes(
                    response,
                    QqHostJsonContext.Default.QqClientResponse));
        }
        catch (QqHostException exception)
        {
            return Failure(exception);
        }
        catch (JsonException)
        {
            return InvocationResult.Failure(
                DirectInvocationError.Types.Code.InvalidRequest,
                "qq.client.v1 payload must be valid JSON.",
                domainCode: "INVALID_REQUEST");
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            return InvocationResult.Failure(
                DirectInvocationError.Types.Code.Cancelled,
                "Invocation cancelled by caller.",
                domainCode: "CALLER_CANCELLED");
        }
    }

    public async IAsyncEnumerable<DirectStreamItem> InvokeStreamAsync(
        DirectInvocationRequest request,
        [System.Runtime.CompilerServices.EnumeratorCancellation]
        CancellationToken cancellationToken)
    {
        InvocationResult result = await InvokeAsync(request, cancellationToken);
        if (result.Error is not null)
        {
            yield return new DirectStreamItem { Error = result.Error };
        }
        else if (result.Payload is not null)
        {
            yield return new DirectStreamItem { Payload = result.Payload };
        }

        yield return new DirectStreamItem { End = new DirectStreamEnd() };
    }

    private static void ValidateEnvelope(DirectInvocationRequest request)
    {
        if (request.Capability != CapabilityId
            || request.InterfaceVersion != InterfaceVersion)
        {
            throw new QqHostException(
                "METHOD_NOT_FOUND",
                "Only qq.client.v1 version 1 is registered.");
        }

        if (!QqHostOperationRegistry.TryGet(request.Method, out QqHostOperation? operation))
        {
            throw new QqHostException(
                "UNKNOWN_OPERATION",
                $"unsupported QQ operation {request.Method}");
        }

        if (!operation.Requestable)
        {
            throw new QqHostException(
                "UNSUPPORTED_OPERATION",
                $"QQ operation {request.Method} is callback-only");
        }

        if (request.PayloadTypeUrl != RequestTypeUrl)
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                "qq.client.v1 request type URL is invalid");
        }

        if (request.Payload.Length is <= 0 or > QqHostProtocol.MaxFrameBytes)
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                "qq.client.v1 request payload exceeds the 8 MiB limit");
        }
    }

    private static JsonElement ParseParameters(JsonElement root)
    {
        if (root.ValueKind != JsonValueKind.Object
            || root.EnumerateObject().Count() != 1
            || !root.TryGetProperty("params", out JsonElement parameters)
            || parameters.ValueKind != JsonValueKind.Object)
        {
            throw new QqHostException(
                "INVALID_REQUEST",
                "qq.client.v1 request must contain only an object params field");
        }

        return parameters.Clone();
    }

    private static InvocationResult Failure(QqHostException exception)
    {
        DirectInvocationError.Types.Code code = exception.Code switch
        {
            "METHOD_NOT_FOUND" or "UNKNOWN_OPERATION" or "UNSUPPORTED_OPERATION" =>
                DirectInvocationError.Types.Code.MethodNotFound,
            "CANCELLED" => DirectInvocationError.Types.Code.Cancelled,
            "TIMEOUT" => DirectInvocationError.Types.Code.DeadlineExceeded,
            "CAPABILITY_UNAVAILABLE" => DirectInvocationError.Types.Code.Unavailable,
            "INVALID_REQUEST" => DirectInvocationError.Types.Code.InvalidRequest,
            _ => DirectInvocationError.Types.Code.ExecutionFailed
        };
        return InvocationResult.Failure(
            code,
            exception.Message,
            retryable: code is DirectInvocationError.Types.Code.Unavailable
                or DirectInvocationError.Types.Code.DeadlineExceeded,
            domainCode: exception.Code);
    }
}
