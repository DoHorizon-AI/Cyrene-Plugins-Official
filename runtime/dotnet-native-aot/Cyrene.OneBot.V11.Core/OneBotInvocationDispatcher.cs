// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotInvocationDispatcher.cs                                         │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: AOT-safe direct invocation for the generic OneBot message action.  │
// │                                                                         │
// │  模块职责：为通用 OneBot 消息 action 提供 AOT 安全的直接调用分派               │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using Cyrene.Message.Connector.V1;
using Cyrene.Plugin.RuntimeHost;
using Cyrene.Plugin.Runtime.V1;
using Google.Protobuf;

namespace Cyrene.OneBot.V11.Core;

/// <summary>Dispatches the implemented OneBot V1 action through the direct runtime.</summary>
/// <remarks>中文：通过 direct runtime 分派已实现的 OneBot V1 action。</remarks>
public sealed class OneBotInvocationDispatcher : IDirectInvocationDispatcher
{
    public const int MaxPayloadBytes = 8 * 1024 * 1024;

    private readonly OneBotProfile _profile;
    private readonly IOneBotActionTransport _transport;
    private readonly OneBotEventSubscriptionRegistry _subscriptions;

    public OneBotInvocationDispatcher(
        OneBotProfile profile,
        IOneBotActionTransport transport,
        OneBotEventSubscriptionRegistry? subscriptions = null)
    {
        _profile = profile;
        _transport = transport;
        _subscriptions = subscriptions ?? new OneBotEventSubscriptionRegistry(profile);
    }

    public async ValueTask<InvocationResult> InvokeAsync(
        DirectInvocationRequest request,
        CancellationToken cancellationToken)
    {
        try
        {
            if (request.StreamMode == DirectStreamMode.Subscription)
            {
                return Failure(
                    DirectInvocationError.Types.Code.InvalidRequest,
                    "subscription requires InvokeStream.",
                    "SUBSCRIPTION_REQUIRES_STREAM");
            }

            ValidateEnvelope(request);
            cancellationToken.ThrowIfCancellationRequested();
            if (request.Method == OneBotRequestMapper.RespondRequestMethod)
            {
                return await InvokeRespondRequestAsync(request, cancellationToken);
            }

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
        catch (OneBotRequestMappingException exception)
        {
            return Failure(
                DirectInvocationError.Types.Code.InvalidRequest,
                exception.Message,
                "INVALID_REQUEST_MAPPING");
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
        if (request.StreamMode == DirectStreamMode.Subscription)
        {
            OneBotEventSubscription? subscription = null;
            OneBotSubscriptionException? subscriptionFailure = null;
            try
            {
                subscription = _subscriptions.Subscribe(request);
                _transport.Start();
            }
            catch (OneBotSubscriptionException exception)
            {
                subscriptionFailure = exception;
            }

            if (subscriptionFailure is not null)
            {
                yield return new DirectStreamItem
                {
                    Error = new DirectInvocationError
                    {
                        Code = SubscriptionErrorCode(subscriptionFailure.DomainCode),
                        Message = subscriptionFailure.Message,
                        DomainCode = subscriptionFailure.DomainCode
                    }
                };
                yield break;
            }

            try
            {
                await foreach (DirectStreamItem item in subscription!.ReadAllAsync(
                                   cancellationToken))
                {
                    yield return item;
                }
            }
            finally
            {
                subscription!.Dispose();
            }

            yield break;
        }

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
            || (request.Method != OneBotMessageMapper.SendMessageMethod
                && request.Method != OneBotRequestMapper.RespondRequestMethod))
        {
            throw new OneBotConfigurationException(
                "METHOD_NOT_FOUND",
                "Only message.connector.v1/send_message and respond_request version 1 are registered.");
        }

        string expectedTypeUrl = request.Method == OneBotRequestMapper.RespondRequestMethod
            ? OneBotRequestMapper.RespondRequestTypeUrl
            : OneBotMessageMapper.SendMessageRequestTypeUrl;
        if (!string.Equals(request.PayloadTypeUrl, expectedTypeUrl, StringComparison.Ordinal))
        {
            throw new OneBotConfigurationException(
                "PAYLOAD_TYPE_MISMATCH",
                $"{request.Method} payload_type_url is not the canonical request type.");
        }

        int maxPayloadBytes = request.Method == OneBotRequestMapper.RespondRequestMethod
            ? OneBotRequestMapper.MaxPayloadBytes
            : MaxPayloadBytes;
        if (request.Payload.Length > maxPayloadBytes)
        {
            throw new OneBotConfigurationException(
                "PAYLOAD_TOO_LARGE",
                $"{request.Method} payload exceeds the configured size limit.");
        }
    }

    private async ValueTask<InvocationResult> InvokeRespondRequestAsync(
        DirectInvocationRequest request,
        CancellationToken cancellationToken)
    {
        OneBotRequestOperation operation = OneBotRequestMapper.Map(
            request.Payload.ToByteArray());
        _ = await _transport.CallAsync(
            operation.Action,
            operation.Request,
            cancellationToken);
        cancellationToken.ThrowIfCancellationRequested();
        OneBotRequestResponseResult result = new()
        {
            Status = "accepted",
            RequestId = operation.RequestId,
            RequestKind = operation.RequestKind,
            Decision = operation.Decision
        };
        return InvocationResult.Success(
            OneBotRequestMapper.RespondResultTypeUrl,
            JsonSerializer.SerializeToUtf8Bytes(
                result,
                OneBotJsonContext.Default.OneBotRequestResponseResult));
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

    private static DirectInvocationError.Types.Code SubscriptionErrorCode(string domainCode) =>
        domainCode switch
        {
            "METHOD_NOT_FOUND" => DirectInvocationError.Types.Code.MethodNotFound,
            "CAPABILITY_UNAVAILABLE" => DirectInvocationError.Types.Code.Unavailable,
            _ => DirectInvocationError.Types.Code.InvalidRequest
        };
}
