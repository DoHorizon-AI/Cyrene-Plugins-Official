// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqDirectInvocationDispatcher.cs                                      │
// │  Namespace: Cyrene.Im.Core                                                │
// │  Role: Explicit qq.client.v1 dispatch over the QQ Host bridge.            │
// │                                                                         │
// │  模块职责：通过固定 operation 注册表将 qq.client.v1 调用送入 QQ Host         │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using System.Text;
using Cyrene.Message.Connector.V1;
using Cyrene.Plugin.RuntimeHost;
using Cyrene.Plugin.Runtime.V1;
using Google.Protobuf;

namespace Cyrene.Im.Core;

/// <summary>Dispatches fixed QQ extension operations through one Host client.</summary>
/// <remarks>中文：通过一个 Host 客户端分派固定的 QQ 扩展操作。</remarks>
public sealed class QqDirectInvocationDispatcher : IDirectInvocationDispatcher, IDisposable
{
    public const string CapabilityId = "qq.client.v1";
    public const string InterfaceVersion = "1";
    public const string RequestTypeUrl = "type.cyrene.io/qq.client.v1.Request";
    public const string ResponseTypeUrl = "type.cyrene.io/qq.client.v1.Response";

    private readonly QqDirectProfile _profile;
    private readonly QqHostClient _host;
    private readonly SemaphoreSlim _sessionGate = new(1, 1);
    private int _sessionGeneration;
    private int _sessionBootstrapStage;
    private string _sessionState = "CREATED";
    private int _nativeSubscriptionGeneration;
    private readonly object _callbackGate = new();
    private readonly Dictionary<string, string> _callbackRequests = new(StringComparer.Ordinal);
    private readonly object _eventGate = new();
    private readonly HashSet<string> _seenEventIds = new(StringComparer.Ordinal);
    private readonly QqDirectEventSubscriptionRegistry _subscriptions;

    public QqDirectInvocationDispatcher(
        QqDirectProfile profile,
        QqHostClient host,
        QqDirectEventSubscriptionRegistry? subscriptions = null)
    {
        _profile = profile;
        _host = host;
        _subscriptions = subscriptions ?? new QqDirectEventSubscriptionRegistry();
        _host.EventHandler = HandleHostEvent;
    }

    public void Dispose()
    {
        _subscriptions.Dispose();
        _sessionGate.Dispose();
    }

    public async ValueTask<InvocationResult> InvokeAsync(
        DirectInvocationRequest request,
        CancellationToken cancellationToken)
    {
        try
        {
            if (request.Capability == QqDirectMessageMapper.CapabilityId)
            {
                return await InvokeCanonicalMessageAsync(request, cancellationToken);
            }

            ValidateEnvelope(request);
            using JsonDocument document = JsonDocument.Parse(request.Payload.ToByteArray());
            JsonElement root = document.RootElement;
            JsonElement parameters = ParseParameters(request.Method, root);
            QqHostOperationRegistry.TryGet(request.Method, out QqHostOperation? operation);
            await EnsureHostStartedAsync(cancellationToken);
            if (operation!.Mapping != "session")
            {
                await EnsureSessionStartedAsync(cancellationToken);
            }

            if (operation.Priority != "P0" || operation.Mapping is not ("session" or "login"))
            {
                EnsureSessionReady();
            }

            JsonElement result = await _host.RequestAsync(
                request.Method,
                parameters,
                cancellationToken);
            QqHostOperationValidator.ValidateResult(request.Method, result);
            UpdateSessionState(request.Method, result);
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
        catch (QqDirectMappingException exception)
        {
            return Failure(exception);
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
        if (request.StreamMode == DirectStreamMode.Subscription)
        {
            QqDirectEventSubscription? subscription = null;
            DirectInvocationError? setupError = null;
            try
            {
                subscription = _subscriptions.Subscribe(request);
                await EnsureHostStartedAsync(cancellationToken);
                await EnsureSessionStartedAsync(cancellationToken);
                EnsureSessionReady();
                await EnsureNativeSubscriptionAsync(cancellationToken);
            }
            catch (QqDirectSubscriptionException exception)
            {
                setupError = SubscriptionFailure(exception);
            }
            catch (QqHostException exception)
            {
                setupError = Failure(exception).Error;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                setupError = new DirectInvocationError
                {
                    Code = DirectInvocationError.Types.Code.Cancelled,
                    Message = "Subscription cancelled by caller.",
                    DomainCode = "CALLER_CANCELLED"
                };
            }

            if (setupError is not null)
            {
                yield return new DirectStreamItem { Error = setupError };
                subscription?.Dispose();
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

    private async Task<InvocationResult> InvokeCanonicalMessageAsync(
        DirectInvocationRequest request,
        CancellationToken cancellationToken)
    {
        if (request.StreamMode == DirectStreamMode.Subscription)
        {
            return InvocationResult.Failure(
                DirectInvocationError.Types.Code.InvalidRequest,
                "subscription requires InvokeStream.",
                domainCode: "SUBSCRIPTION_REQUIRES_STREAM");
        }

        ValidateCanonicalMessageEnvelope(request);
        cancellationToken.ThrowIfCancellationRequested();
        if (request.Method == QqDirectMessageMapper.RespondRequestMethod)
        {
            return await InvokeCanonicalRespondAsync(request, cancellationToken);
        }

        SendMessageRequest message;
        try
        {
            message = SendMessageRequest.Parser.ParseFrom(request.Payload);
        }
        catch (InvalidProtocolBufferException)
        {
            return InvocationResult.Failure(
                DirectInvocationError.Types.Code.InvalidRequest,
                "send_message payload is invalid protobuf.",
                domainCode: "INVALID_PROTOBUF");
        }

        JsonElement parameters = QqDirectMessageMapper.BuildSendParameters(message, _profile);
        await EnsureReadyForOperationAsync(cancellationToken);
        string? callbackRequestId = null;
        try
        {
            JsonElement result = await _host.RequestAsync(
                "qq.message.send",
                parameters,
                cancellationToken,
                requestId =>
                {
                    callbackRequestId = requestId;
                    RememberCallbackRequest(requestId, "qq.message.send");
                });
            DeliveryResult delivery = QqDirectMessageMapper.BuildDeliveryResult(result);
            return InvocationResult.Success(
                QqDirectMessageMapper.DeliveryResultTypeUrl,
                delivery.ToByteArray());
        }
        catch
        {
            RemoveCallbackRequest(callbackRequestId);
            throw;
        }
    }

    private async Task<InvocationResult> InvokeCanonicalRespondAsync(
        DirectInvocationRequest request,
        CancellationToken cancellationToken)
    {
        QqRespondOperation operation = QqDirectMessageMapper.BuildRespondOperation(
            DecodeUtf8(request.Payload, "respond_request"),
            _profile);
        await EnsureReadyForOperationAsync(cancellationToken);
        JsonElement nativeResult = await _host.RequestAsync(
            operation.Operation,
            operation.Parameters,
            cancellationToken);
        operation.Result.Result = nativeResult.Clone();
        return InvocationResult.Success(
            QqDirectMessageMapper.RespondResultTypeUrl,
            JsonSerializer.SerializeToUtf8Bytes(
                operation.Result,
                QqHostJsonContext.Default.QqRespondResult));
    }

    private async Task EnsureReadyForOperationAsync(CancellationToken cancellationToken)
    {
        await EnsureHostStartedAsync(cancellationToken);
        await EnsureSessionStartedAsync(cancellationToken);
        EnsureSessionReady();
    }

    private async Task EnsureNativeSubscriptionAsync(CancellationToken cancellationToken)
    {
        if (_nativeSubscriptionGeneration == _host.Generation)
        {
            return;
        }

        QqSubscribeParameters parameters = new()
        {
            Events = new List<string> { "message.received", "request.received" }
        };
        JsonElement nativeParameters = JsonSerializer.SerializeToElement(
            parameters,
            QqHostJsonContext.Default.QqSubscribeParameters);
        await _host.RequestAsync(
            "qq.message.subscribe",
            nativeParameters,
            cancellationToken);
        _nativeSubscriptionGeneration = _host.Generation;
    }

    private void HandleHostEvent(QqHostEvent @event)
    {
        if (!RememberEvent(@event))
        {
            return;
        }

        try
        {
            switch (@event.Event)
            {
                case "message.received":
                    _subscriptions.Publish(
                        QqDirectMessageMapper.NormalizeMessage(
                            @event.Payload,
                            _profile,
                            @event.Generation,
                            _profile.BindingId));
                    break;
                case "request.received":
                    _subscriptions.Publish(
                        QqDirectMessageMapper.NormalizeRequest(@event.Payload, _profile));
                    break;
                case "message.send_completion":
                case "qq.message.send_completion":
                case "media.download_complete":
                case "qq.media.download_complete":
                    PublishCallback(@event);
                    break;
            }
        }
        catch (QqDirectMappingException)
        {
            // Malformed native data is dropped at the binding boundary.
            // 中文：格式错误的原生数据会在 binding 边界处丢弃。
        }
    }

    private void PublishCallback(QqHostEvent @event)
    {
        (string Operation, string[] OriginatingOperations)? callback = @event.Event switch
        {
            "message.send_completion" or "qq.message.send_completion" =>
                ("qq.message.send_completion", new[] { "qq.message.send" }),
            "media.download_complete" or "qq.media.download_complete" =>
                ("qq.media.download_complete", new[] { "qq.media.download", "qq.file.download" }),
            _ => null
        };
        if (callback is null)
        {
            return;
        }

        string? requestId = @event.RequestId;
        if (string.IsNullOrWhiteSpace(requestId)
            && @event.Payload.ValueKind == JsonValueKind.Object
            && @event.Payload.TryGetProperty("request_id", out JsonElement payloadRequestId)
            && payloadRequestId.ValueKind == JsonValueKind.String)
        {
            requestId = payloadRequestId.GetString();
        }

        if (string.IsNullOrWhiteSpace(requestId))
        {
            return;
        }

        lock (_callbackGate)
        {
            if (!_callbackRequests.TryGetValue(requestId, out string? originating)
                || !callback.Value.OriginatingOperations.Contains(originating, StringComparer.Ordinal))
            {
                return;
            }
        }

        QqDirectNormalizedEvent normalized = QqDirectMessageMapper.NormalizeCallback(
            callback.Value.Operation,
            requestId,
            @event.EventId,
            @event.Payload);
        RemoveCallbackRequest(requestId);
        _subscriptions.Publish(normalized);
    }

    private bool RememberEvent(QqHostEvent @event)
    {
        string key = $"{@event.Generation}:{@event.EventId}";
        lock (_eventGate)
        {
            if (!_seenEventIds.Add(key))
            {
                return false;
            }

            if (_seenEventIds.Count > 2_048)
            {
                string? first = _seenEventIds.FirstOrDefault();
                if (first is not null)
                {
                    _seenEventIds.Remove(first);
                }
            }

            return true;
        }
    }

    private void RememberCallbackRequest(string requestId, string operation)
    {
        lock (_callbackGate)
        {
            _callbackRequests[requestId] = operation;
            if (_callbackRequests.Count > 2_048)
            {
                string? first = _callbackRequests.Keys.FirstOrDefault();
                if (first is not null)
                {
                    _callbackRequests.Remove(first);
                }
            }
        }
    }

    private void RemoveCallbackRequest(string? requestId)
    {
        if (requestId is null)
        {
            return;
        }

        lock (_callbackGate)
        {
            _callbackRequests.Remove(requestId);
        }
    }

    private static void ValidateCanonicalMessageEnvelope(DirectInvocationRequest request)
    {
        if (request.InterfaceVersion != QqDirectMessageMapper.InterfaceVersion
            || request.Method is not (
                QqDirectMessageMapper.SendMessageMethod
                or QqDirectMessageMapper.RespondRequestMethod))
        {
            throw new QqDirectMappingException(
                "METHOD_NOT_FOUND",
                "Only message.connector.v1/send_message and respond_request version 1 are registered.");
        }

        string expectedTypeUrl = request.Method == QqDirectMessageMapper.SendMessageMethod
            ? QqDirectMessageMapper.SendMessageRequestTypeUrl
            : QqDirectMessageMapper.RespondRequestTypeUrl;
        if (request.PayloadTypeUrl != expectedTypeUrl)
        {
            throw new QqDirectMappingException(
                "PAYLOAD_TYPE_MISMATCH",
                $"{request.Method} payload_type_url is not the canonical request type.");
        }

        if (request.Payload.Length > QqHostProtocol.MaxFrameBytes)
        {
            throw new QqDirectMappingException(
                "PAYLOAD_TOO_LARGE",
                "canonical message payload exceeds the 8 MiB limit.");
        }
    }

    private static string DecodeUtf8(ByteString payload, string field)
    {
        try
        {
            return new UTF8Encoding(false, true).GetString(payload.Span);
        }
        catch (DecoderFallbackException)
        {
            throw new QqDirectMappingException(
                "INVALID_REQUEST",
                $"{field} must be valid UTF-8 JSON.");
        }
    }

    private static DirectInvocationError SubscriptionFailure(
        QqDirectSubscriptionException exception) =>
        new()
        {
            Code = exception.DomainCode switch
            {
                "METHOD_NOT_FOUND" => DirectInvocationError.Types.Code.MethodNotFound,
                "CAPABILITY_UNAVAILABLE" => DirectInvocationError.Types.Code.Unavailable,
                _ => DirectInvocationError.Types.Code.InvalidRequest
            },
            Message = exception.Message,
            DomainCode = exception.DomainCode
        };

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

    private async Task EnsureHostStartedAsync(CancellationToken cancellationToken)
    {
        if (_host.State == "STOPPED" && _host.Generation > 0)
        {
            throw new QqHostException("CAPABILITY_UNAVAILABLE", "QQ Host is stopped");
        }

        if (_host.State == "FAILED")
        {
            await _host.RecoverAsync(cancellationToken);
        }
        else
        {
            await _host.StartAsync(cancellationToken);
        }

        if (_sessionGeneration != _host.Generation)
        {
            _sessionGeneration = _host.Generation;
            _sessionBootstrapStage = 0;
            _sessionState = "NATIVE_READY";
        }
    }

    private async Task EnsureSessionStartedAsync(CancellationToken cancellationToken)
    {
        await _sessionGate.WaitAsync(cancellationToken);
        try
        {
            string[] stages =
            {
                "qq.session.create",
                "qq.session.init",
                "qq.session.start_nt"
            };
            for (int index = 0; index < stages.Length; index++)
            {
                if (_sessionBootstrapStage > index)
                {
                    continue;
                }

                JsonElement result = await _host.RequestAsync(
                    stages[index],
                    CreateBootstrapParameters(),
                    cancellationToken);
                _sessionBootstrapStage = index + 1;
                UpdateSessionState(stages[index], result);
            }
        }
        finally
        {
            _sessionGate.Release();
        }
    }

    private JsonElement CreateBootstrapParameters()
    {
        return JsonSerializer.SerializeToElement(
            new Dictionary<string, string> { ["login_policy"] = _profile.LoginPolicy },
            QqHostJsonContext.Default.DictionaryStringString);
    }

    private void EnsureSessionReady()
    {
        if (_sessionState == "READY")
        {
            return;
        }

        string code = _sessionState == "LOGIN_REQUIRED"
            ? "LOGIN_REQUIRED"
            : "CAPABILITY_UNAVAILABLE";
        throw new QqHostException(code, $"QQ direct session is {_sessionState.ToLowerInvariant()}");
    }

    private void UpdateSessionState(
        string operation,
        JsonElement result)
    {
        _sessionBootstrapStage = operation switch
        {
            "qq.session.create" => Math.Max(_sessionBootstrapStage, 1),
            "qq.session.init" => Math.Max(_sessionBootstrapStage, 2),
            "qq.session.start_nt" => Math.Max(_sessionBootstrapStage, 3),
            _ => _sessionBootstrapStage
        };
        if (operation is "qq.session.create" or "qq.session.init")
        {
            _sessionState = "NATIVE_READY";
        }

        if (result.ValueKind != JsonValueKind.Object)
        {
            return;
        }

        string? accountId = null;
        if (result.TryGetProperty("account_id", out JsonElement account))
        {
            accountId = account.ValueKind == JsonValueKind.String
                ? account.GetString()
                : account.ValueKind == JsonValueKind.Number
                    ? account.GetRawText()
                    : null;
        }

        if (_profile.AccountId is not null
            && accountId is not null
            && accountId != _profile.AccountId)
        {
            _sessionState = "FAILED";
            throw new QqHostException("ACCOUNT_MISMATCH", "QQ Host account does not match binding");
        }

        string? state = result.TryGetProperty("state", out JsonElement stateElement)
            && stateElement.ValueKind == JsonValueKind.String
            ? stateElement.GetString()
            : null;
        bool ready = state is "ready" or "online" or "logged_in"
            || result.TryGetProperty("ready", out JsonElement readyElement)
                && readyElement.ValueKind == JsonValueKind.True;
        if (ready)
        {
            if (_profile.AccountId is not null && accountId is null)
            {
                _sessionState = "FAILED";
                throw new QqHostException(
                    "ACCOUNT_MISMATCH",
                    "QQ Host ready account is missing or mismatched");
            }

            _sessionState = "READY";
        }
        else if (state is "login_required" or "qr_required" or "offline")
        {
            _sessionState = "LOGIN_REQUIRED";
        }
        else if (state == "failed")
        {
            _sessionState = "FAILED";
        }

    }

    private static JsonElement ParseParameters(string operation, JsonElement root)
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

        JsonElement copy = parameters.Clone();
        QqHostOperationValidator.ValidateParameters(operation, copy);
        return copy;
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

    private static InvocationResult Failure(QqDirectMappingException exception)
    {
        DirectInvocationError.Types.Code code = exception.DomainCode switch
        {
            "METHOD_NOT_FOUND" => DirectInvocationError.Types.Code.MethodNotFound,
            "PAYLOAD_TOO_LARGE" or "PAYLOAD_TYPE_MISMATCH" or "INVALID_REQUEST" =>
                DirectInvocationError.Types.Code.InvalidRequest,
            _ => DirectInvocationError.Types.Code.ExecutionFailed
        };
        return InvocationResult.Failure(
            code,
            exception.Message,
            retryable: false,
            domainCode: exception.DomainCode);
    }
}
