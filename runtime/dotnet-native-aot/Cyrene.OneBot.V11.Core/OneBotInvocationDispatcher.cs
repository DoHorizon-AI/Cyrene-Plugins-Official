// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotInvocationDispatcher.cs                                         │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: AOT-safe direct invocation for the generic OneBot message action.  │
// │                                                                         │
// │  模块职责：为通用 OneBot 消息 action 提供 AOT 安全的直接调用分派               │
// └─────────────────────────────────────────────────────────────────────────┘

using Cyrene.Message.Connector.V1;
using Cyrene.Plugin.Runtime.V1;
using Google.Protobuf;

namespace Cyrene.OneBot.V11.Core;

/// <summary>Dispatches the implemented OneBot V1 action through the direct runtime.</summary>
public sealed class OneBotInvocationDispatcher : IDirectInvocationDispatcher
{
    public const int MaxPayloadBytes = 8 * 1024 * 1024;

    private readonly OneBotProfile _profile;
    private readonly IOneBotActionTransport _transport;

    public OneBotInvocationDispatcher(
        OneBotProfile profile,
        IOneBotActionTransport transport)
    {
        _profile = profile;
        _transport = transport;
    }

    public async ValueTask<InvocationResult> InvokeAsync(
        DirectInvocationRequest request,
        CancellationToken cancellationToken)
    {
        try
        {
            ValidateEnvelope(request);
            cancellationToken.ThrowIfCancellationRequested();
            SendMessageRequest message = SendMessageRequest.Parser.ParseFrom(request.Payload);
            OneBotSendOperation operation = OneBotMessageMapper.MapSendMessage(message, _profile);
            OneBotActionResponse response = await _transport.CallAsync(
                operation.Action,
                operation.Request,
                cancellationToken);
            cancellationToken.ThrowIfCancellationRequested();

            DeliveryResult delivery = new()
            {
                Status = DeliveryStatus.Accepted
            };
            string? vendorMessageId = OneBotMessageMapper.ReadVendorMessageId(response);
            if (vendorMessageId is not null)
            {
                delivery.VendorMessageId = vendorMessageId;
            }

            return InvocationResult.Success(
                OneBotMessageMapper.DeliveryResultTypeUrl,
                delivery.ToByteArray());
        }
        catch (InvalidProtocolBufferException)
        {
            return Failure(
                DirectInvocationError.Types.Code.InvalidRequest,
                "send_message payload is invalid protobuf.",
                "INVALID_PROTOBUF");
        }
        catch (OneBotMappingException exception)
        {
            return Failure(
                DirectInvocationError.Types.Code.InvalidRequest,
                exception.Message,
                "INVALID_MESSAGE_MAPPING");
        }
        catch (OneBotConfigurationException exception) when (exception.DomainCode == "METHOD_NOT_FOUND")
        {
            return Failure(
                DirectInvocationError.Types.Code.MethodNotFound,
                exception.Message,
                exception.DomainCode);
        }
        catch (OneBotConfigurationException exception)
        {
            return Failure(
                DirectInvocationError.Types.Code.InvalidRequest,
                exception.Message,
                exception.DomainCode);
        }
        catch (OneBotTransportException exception) when (exception.DomainCode == "TIMEOUT")
        {
            return Failure(
                DirectInvocationError.Types.Code.DeadlineExceeded,
                exception.Message,
                exception.DomainCode,
                retryable: true);
        }
        catch (OneBotTransportException exception) when (exception.DomainCode == "CAPABILITY_UNAVAILABLE")
        {
            return Failure(
                DirectInvocationError.Types.Code.Unavailable,
                exception.Message,
                exception.DomainCode,
                retryable: true);
        }
        catch (OneBotTransportException exception)
        {
            return Failure(
                DirectInvocationError.Types.Code.ExecutionFailed,
                exception.Message,
                exception.DomainCode);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            return Failure(
                DirectInvocationError.Types.Code.Cancelled,
                "Invocation cancelled by caller.",
                "CALLER_CANCELLED");
        }
        catch (Exception exception)
        {
            return Failure(
                DirectInvocationError.Types.Code.ExecutionFailed,
                "OneBot invocation failed.",
                "UNHANDLED_INVOCATION_FAILURE",
                detail: exception.GetType().Name);
        }
    }

    public async IAsyncEnumerable<DirectStreamItem> InvokeStreamAsync(
        DirectInvocationRequest request,
        [System.Runtime.CompilerServices.EnumeratorCancellation]
        CancellationToken cancellationToken)
    {
        InvocationResult result = await InvokeAsync(request, cancellationToken);
        if (result.Payload is not null)
        {
            yield return new DirectStreamItem { Payload = result.Payload };
        }
        else if (result.Error is not null)
        {
            yield return new DirectStreamItem { Error = result.Error };
        }
    }

    private static void ValidateEnvelope(DirectInvocationRequest request)
    {
        if (!string.Equals(request.Capability, OneBotMessageMapper.CapabilityId, StringComparison.Ordinal)
            || !string.Equals(request.InterfaceVersion, OneBotMessageMapper.InterfaceVersion, StringComparison.Ordinal)
            || !string.Equals(request.Method, OneBotMessageMapper.SendMessageMethod, StringComparison.Ordinal))
        {
            throw new OneBotConfigurationException(
                "METHOD_NOT_FOUND",
                "Only message.connector.v1/send_message version 1 is registered.");
        }

        if (!string.Equals(
                request.PayloadTypeUrl,
                OneBotMessageMapper.SendMessageRequestTypeUrl,
                StringComparison.Ordinal))
        {
            throw new OneBotConfigurationException(
                "PAYLOAD_TYPE_MISMATCH",
                "send_message payload_type_url is not the canonical request type.");
        }

        if (request.Payload.Length > MaxPayloadBytes)
        {
            throw new OneBotConfigurationException(
                "PAYLOAD_TOO_LARGE",
                "send_message payload exceeds the 8 MiB limit.");
        }
    }

    private static InvocationResult Failure(
        DirectInvocationError.Types.Code code,
        string message,
        string domainCode,
        bool retryable = false,
        string? detail = null)
    {
        _ = detail;
        return InvocationResult.Failure(code, message, retryable, domainCode);
    }
}
