// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqDirectInvocationDispatcher.cs                                      │
// │  Namespace: Cyrene.Im.Core                                                │
// │  Role: Explicit qq.client.v1 dispatch over the QQ Host bridge.            │
// │                                                                         │
// │  模块职责：通过固定 operation 注册表将 qq.client.v1 调用送入 QQ Host         │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Globalization;
using System.Text;
using System.Text.Json;
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
    public const string LoginStateEventType = "qq_login_state";
    public const string LoginStateEventTypeUrl = "type.cyrene.io/qq.client.v1.LoginStateEvent";
    private const int MaxQrPayloadBytes = 4 * 1024;
    private const int MaxQrTtlSeconds = 10 * 60;
    private static readonly HashSet<string> OutboundOperations = new(StringComparer.Ordinal)
    {
        "qq.message.send",
        "qq.message.forward",
        "qq.message.forward_comment",
        "qq.message.multi_forward",
        "qq.file.forward"
    };

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
    private readonly object _loginGate = new();
    private readonly Dictionary<string, DateTimeOffset> _loginAttempts = new(StringComparer.Ordinal);
    private readonly Dictionary<string, QqLoginPollResult> _terminalLoginStates = new(StringComparer.Ordinal);
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

    /// <summary>Probe live QQ login API readiness without starting a configured Host.</summary>
    /// <remarks>中文：不启动已配置 Host，直接探测 QQ 登录 API 的实时就绪状态。</remarks>
    public async ValueTask<QqDirectHealthReport> HealthAsync(
        CancellationToken cancellationToken)
    {
        QqHostCompatibility? compatibility = _host.Compatibility;
        QqDirectHealthReport report = new()
        {
            Status = "NOT_RUN",
            HostState = _host.State,
            ApiReady = false,
            DedicatedAccountConfirmed = _profile.AccountId is not null
                && _profile.DedicatedAccountConfirmed,
            Generation = _host.Generation,
            ClientVersion = compatibility?.ClientVersion,
            HostAbi = compatibility?.HostAbi,
            FailureCode = _host.FailureCode
        };

        if (_host.State == "FAILED")
        {
            report.Status = _host.FailureCode == "NO_INSTALLATION" ? "NOT_RUN" : "FAILED";
            return report;
        }

        if (_host.State != "NATIVE_READY"
            || _sessionGeneration != _host.Generation
            || _sessionBootstrapStage < 3)
        {
            report.Status = "NOT_RUN";
            return report;
        }

        if (_sessionState != "READY")
        {
            report.Status = _sessionState;
            return report;
        }

        if (!report.DedicatedAccountConfirmed)
        {
            report.Status = "ACCOUNT_UNCONFIRMED";
            return report;
        }

        try
        {
            JsonElement result = await _host.RequestAsync(
                "qq.login.self_status",
                CreateAccountParameters(_profile.AccountId!),
                cancellationToken);
            QqHostOperationValidator.ValidateResult("qq.login.self_status", result);
            string? accountId = result.ValueKind == JsonValueKind.Object
                && result.TryGetProperty("account_id", out JsonElement account)
                ? JsonIdentifier(account)
                : null;
            if (accountId != _profile.AccountId)
            {
                report.Status = "ACCOUNT_MISMATCH";
                report.FailureCode = "ACCOUNT_MISMATCH";
                _sessionState = "FAILED";
                return report;
            }

            string? state = result.ValueKind == JsonValueKind.Object
                && result.TryGetProperty("state", out JsonElement stateValue)
                && stateValue.ValueKind == JsonValueKind.String
                ? stateValue.GetString()
                : null;
            bool ready = state is "ready" or "online" or "logged_in"
                || result.ValueKind == JsonValueKind.Object
                    && result.TryGetProperty("ready", out JsonElement readyValue)
                    && readyValue.ValueKind == JsonValueKind.True;
            report.Status = ready ? "HEALTHY" : "LOGIN_REQUIRED";
            report.ApiReady = ready;
            if (!ready)
            {
                _sessionState = "LOGIN_REQUIRED";
            }

            return report;
        }
        catch (QqHostException exception)
        {
            report.Status = "UNHEALTHY";
            report.FailureCode = exception.Code;
            return report;
        }
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
            parameters = PrepareOperationParameters(request.Method, operation!, parameters);
            if (request.Method == "qq.login.poll"
                && parameters.TryGetProperty("login_id", out JsonElement cachedLoginId)
                && JsonIdentifier(cachedLoginId) is string cachedId)
            {
                lock (_loginGate)
                {
                    if (_terminalLoginStates.TryGetValue(
                            cachedId,
                            out QqLoginPollResult? cachedResult))
                    {
                        QqClientResponse cachedResponse = new()
                        {
                            Operation = request.Method,
                            Status = "accepted",
                            Result = JsonSerializer.SerializeToElement(
                                cachedResult,
                                QqHostJsonContext.Default.QqLoginPollResult),
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
                                cachedResponse,
                                QqHostJsonContext.Default.QqClientResponse));
                    }
                }
            }

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
            if (request.Method == "qq.login.qr")
            {
                QqLoginQrResult qr = NormalizeQrResult(result);
                lock (_loginGate)
                {
                    _loginAttempts[qr.LoginId] = DateTimeOffset.Parse(
                        qr.ExpiresAtUtc,
                        CultureInfo.InvariantCulture,
                        DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal);
                    _terminalLoginStates.Remove(qr.LoginId);
                }

                _sessionState = "QR_PENDING";
                result = JsonSerializer.SerializeToElement(
                    qr,
                    QqHostJsonContext.Default.QqLoginQrResult);
            }
            else if (request.Method == "qq.login.poll")
            {
                result = NormalizeLoginPollResult(result);
            }
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
                if (subscription.Filter.LoginStateOnly)
                {
                    EnsureDedicatedAccount();
                }
                else
                {
                    EnsureSessionReady();
                    await EnsureNativeSubscriptionAsync(cancellationToken);
                }
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
        EnsureDedicatedAccount();
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
        EnsureDedicatedAccount();
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
                case "login.qr":
                    PublishLoginQrEvent(@event.Payload);
                    break;
                case "login.state":
                    PublishLoginStateEvent(@event.Payload);
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
            lock (_loginGate)
            {
                _loginAttempts.Clear();
                _terminalLoginStates.Clear();
            }
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

    private JsonElement PrepareOperationParameters(
        string operation,
        QqHostOperation operationSpec,
        JsonElement parameters)
    {
        if (OutboundOperations.Contains(operation))
        {
            EnsureDedicatedAccount();
        }

        if (operationSpec.Mapping != "login")
        {
            return parameters;
        }

        EnsureDedicatedAccount();
        if (operation == "qq.login.qr" && _profile.LoginPolicy != "qr")
        {
            throw new QqHostException("INVALID_REQUEST", "login_policy must be qr");
        }

        if (operation == "qq.login.qr")
        {
            lock (_loginGate)
            {
                foreach (string expired in _loginAttempts
                             .Where(attempt => attempt.Value <= DateTimeOffset.UtcNow)
                             .Select(attempt => attempt.Key)
                             .ToArray())
                {
                    _loginAttempts.Remove(expired);
                }

                if (_loginAttempts.Count >= 4)
                {
                    throw new QqHostException(
                        "LOGIN_IN_PROGRESS",
                        "too many active QQ QR login attempts");
                }
            }
        }

        if (operation == "qq.login.poll")
        {
            if (parameters.TryGetProperty("qr_code", out _))
            {
                throw new QqHostException(
                    "INVALID_REQUEST",
                    "QR contents must not be repeated in poll requests");
            }

            string loginId = RequiredJsonIdentifier(parameters, "login_id");
            lock (_loginGate)
            {
                if (!_terminalLoginStates.ContainsKey(loginId))
                {
                    if (!_loginAttempts.TryGetValue(loginId, out DateTimeOffset expiry))
                    {
                        throw new QqHostException(
                            "INVALID_REQUEST",
                            "QQ login attempt is unknown");
                    }

                    if (expiry <= DateTimeOffset.UtcNow)
                    {
                        _loginAttempts.Remove(loginId);
                        throw new QqHostException("QR_EXPIRED", "QQ login QR has expired");
                    }
                }
            }
        }

        return BindConfiguredAccount(parameters);
    }

    private JsonElement BindConfiguredAccount(JsonElement parameters)
    {
        EnsureDedicatedAccount();
        string accountId = _profile.AccountId!;
        if (parameters.TryGetProperty("account_id", out JsonElement configured))
        {
            if (JsonIdentifier(configured) != accountId)
            {
                throw new QqHostException(
                    "ACCOUNT_MISMATCH",
                    "QQ login account does not match binding");
            }

            return parameters.Clone();
        }

        using MemoryStream buffer = new();
        using (Utf8JsonWriter writer = new(buffer))
        {
            writer.WriteStartObject();
            foreach (JsonProperty property in parameters.EnumerateObject())
            {
                property.WriteTo(writer);
            }

            writer.WriteString("account_id", accountId);
            writer.WriteEndObject();
        }

        using JsonDocument document = JsonDocument.Parse(buffer.ToArray());
        return document.RootElement.Clone();
    }

    private void EnsureDedicatedAccount()
    {
        if (string.IsNullOrWhiteSpace(_profile.AccountId)
            || !_profile.DedicatedAccountConfirmed)
        {
            throw new QqHostException(
                "ACCOUNT_UNCONFIRMED",
                "QQ login and outbound actions require a confirmed dedicated account");
        }
    }

    private static QqLoginQrResult NormalizeQrResult(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object
            || value.EnumerateObject().Any(property => property.Name is not (
                "login_id" or "qr_payload" or "expires_at_utc" or "state")))
        {
            throw new QqHostException("PROTOCOL_MISMATCH", "QQ QR result has an invalid shape");
        }

        string loginId = RequiredJsonIdentifier(value, "login_id");
        string qrPayload = RequiredJsonString(value, "qr_payload");
        string expiryText = RequiredJsonString(value, "expires_at_utc");
        string state = RequiredJsonString(value, "state");
        if (state != "pending")
        {
            throw new QqHostException("PROTOCOL_MISMATCH", "QQ QR state must be pending");
        }

        if (Encoding.UTF8.GetByteCount(qrPayload) > MaxQrPayloadBytes
            || qrPayload.Any(char.IsControl))
        {
            throw new QqHostException("PROTOCOL_MISMATCH", "QQ QR payload is invalid or too large");
        }

        if (!DateTimeOffset.TryParse(
                expiryText,
                CultureInfo.InvariantCulture,
                DateTimeStyles.RoundtripKind,
                out DateTimeOffset expiry)
            || expiry.Offset != TimeSpan.Zero)
        {
            throw new QqHostException("PROTOCOL_MISMATCH", "QQ QR expiry must be UTC");
        }

        TimeSpan ttl = expiry - DateTimeOffset.UtcNow;
        if (ttl <= TimeSpan.Zero)
        {
            throw new QqHostException("QR_EXPIRED", "QQ login QR has expired");
        }

        if (ttl > TimeSpan.FromSeconds(MaxQrTtlSeconds))
        {
            throw new QqHostException("PROTOCOL_MISMATCH", "QQ QR expiry exceeds ten minutes");
        }

        return new QqLoginQrResult
        {
            LoginId = loginId,
            QrPayload = qrPayload,
            ExpiresAtUtc = expiry.UtcDateTime.ToString(
                "yyyy-MM-dd'T'HH:mm:ss.fff'Z'",
                CultureInfo.InvariantCulture),
            State = "pending"
        };
    }

    private JsonElement NormalizeLoginPollResult(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object
            || value.EnumerateObject().Any(property => property.Name is not (
                "login_id" or "state" or "account_id")))
        {
            throw new QqHostException("PROTOCOL_MISMATCH", "QQ QR poll result has an invalid shape");
        }

        string loginId = RequiredJsonIdentifier(value, "login_id");
        string state = RequiredJsonString(value, "state");
        if (state is not ("scanned" or "authorized" or "expired" or "failed"))
        {
            throw new QqHostException("PROTOCOL_MISMATCH", "QQ QR poll state is invalid");
        }

        string? accountId = value.TryGetProperty("account_id", out JsonElement account)
            ? JsonIdentifier(account)
            : null;
        lock (_loginGate)
        {
            if (!_loginAttempts.TryGetValue(loginId, out DateTimeOffset expiry))
            {
                if (_terminalLoginStates.TryGetValue(loginId, out QqLoginPollResult? cached)
                    && cached.State == state
                    && cached.AccountId == accountId)
                {
                    return JsonSerializer.SerializeToElement(
                        cached,
                        QqHostJsonContext.Default.QqLoginPollResult);
                }

                throw new QqHostException(
                    "PROTOCOL_MISMATCH",
                    "QQ QR poll returned an unknown login_id");
            }

            if (expiry <= DateTimeOffset.UtcNow)
            {
                _loginAttempts.Remove(loginId);
                throw new QqHostException("QR_EXPIRED", "QQ login QR has expired");
            }
        }

        if (state == "authorized")
        {
            if (accountId != _profile.AccountId)
            {
                _sessionState = "FAILED";
                throw new QqHostException(
                    "ACCOUNT_MISMATCH",
                    "QQ QR login account does not match binding");
            }

            _sessionState = "READY";
        }
        else if (accountId is not null)
        {
            throw new QqHostException(
                "PROTOCOL_MISMATCH",
                "QQ account is exposed before QR authorization");
        }
        else if (state == "scanned")
        {
            _sessionState = "QR_PENDING";
        }
        else if (state == "expired")
        {
            _sessionState = "LOGIN_REQUIRED";
        }
        else
        {
            _sessionState = "FAILED";
        }

        QqLoginPollResult normalized = new()
        {
            LoginId = loginId,
            State = state,
            AccountId = accountId
        };
        if (state is "authorized" or "expired" or "failed")
        {
            lock (_loginGate)
            {
                _loginAttempts.Remove(loginId);
                _terminalLoginStates[loginId] = normalized;
                while (_terminalLoginStates.Count > 32)
                {
                    _terminalLoginStates.Remove(_terminalLoginStates.Keys.First());
                }
            }

            PublishLoginEvent(new QqLoginStateEventPayload
            {
                Event = "login.state",
                LoginId = loginId,
                State = normalized.State,
                AccountId = normalized.AccountId,
                FailureCode = normalized.FailureCode
            });
        }

        return JsonSerializer.SerializeToElement(
            normalized,
            QqHostJsonContext.Default.QqLoginPollResult);
    }

    private void PublishLoginQrEvent(JsonElement payload)
    {
        try
        {
            EnsureDedicatedAccount();
            QqLoginQrResult qr = NormalizeQrResult(payload);
            lock (_loginGate)
            {
                if (_terminalLoginStates.ContainsKey(qr.LoginId))
                {
                    return;
                }

                _loginAttempts[qr.LoginId] = DateTimeOffset.Parse(
                    qr.ExpiresAtUtc,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal);
            }

            _sessionState = "QR_PENDING";
            QqLoginStateEventPayload normalized = new()
            {
                Event = "login.qr",
                LoginId = qr.LoginId,
                QrPayload = qr.QrPayload,
                ExpiresAtUtc = qr.ExpiresAtUtc,
                State = "pending"
            };
            PublishLoginEvent(normalized);
        }
        catch (QqHostException)
        {
            // Malformed QR contents are discarded without diagnostic output.
        }
    }

    private void PublishLoginStateEvent(JsonElement payload)
    {
        try
        {
            EnsureDedicatedAccount();
            if (payload.ValueKind != JsonValueKind.Object
                || payload.EnumerateObject().Any(property => property.Name is not (
                    "login_id" or "state" or "account_id")))
            {
                return;
            }

            string loginId = RequiredJsonIdentifier(payload, "login_id");
            string state = RequiredJsonString(payload, "state");
            lock (_loginGate)
            {
                if (!_loginAttempts.TryGetValue(loginId, out DateTimeOffset expiry)
                    || expiry <= DateTimeOffset.UtcNow)
                {
                    _loginAttempts.Remove(loginId);
                    return;
                }
            }

            if (state is not ("scanned" or "authorized" or "expired" or "failed"))
            {
                return;
            }

            string? accountId = payload.TryGetProperty("account_id", out JsonElement account)
                ? JsonIdentifier(account)
                : null;
            QqLoginStateEventPayload normalized = new()
            {
                Event = "login.state",
                LoginId = loginId,
                State = state
            };
            if (state == "authorized")
            {
                if (accountId != _profile.AccountId)
                {
                    normalized.State = "failed";
                    normalized.FailureCode = "ACCOUNT_MISMATCH";
                    _sessionState = "FAILED";
                    CacheTerminalLoginState(loginId, new QqLoginPollResult
                    {
                        LoginId = loginId,
                        State = "failed",
                        FailureCode = "ACCOUNT_MISMATCH"
                    });
                }
                else
                {
                    normalized.AccountId = accountId;
                    _sessionState = "READY";
                    CacheTerminalLoginState(loginId, new QqLoginPollResult
                    {
                        LoginId = loginId,
                        State = "authorized",
                        AccountId = accountId
                    });
                }
            }
            else if (accountId is not null)
            {
                return;
            }
            else if (state == "scanned")
            {
                _sessionState = "QR_PENDING";
            }
            else
            {
                _sessionState = state == "expired" ? "LOGIN_REQUIRED" : "FAILED";
                CacheTerminalLoginState(loginId, new QqLoginPollResult
                {
                    LoginId = loginId,
                    State = state
                });
            }

            PublishLoginEvent(normalized);
        }
        catch (QqHostException)
        {
            // Malformed login state is discarded without diagnostic output.
        }
    }

    private void CacheTerminalLoginState(string loginId, QqLoginPollResult state)
    {
        lock (_loginGate)
        {
            _loginAttempts.Remove(loginId);
            _terminalLoginStates[loginId] = state;
            while (_terminalLoginStates.Count > 32)
            {
                _terminalLoginStates.Remove(_terminalLoginStates.Keys.First());
            }
        }
    }

    private void PublishLoginEvent(QqLoginStateEventPayload payload)
    {
        _subscriptions.Publish(new QqDirectNormalizedEvent(
            LoginStateEventType,
            LoginStateEventTypeUrl,
            JsonSerializer.SerializeToUtf8Bytes(
                payload,
                QqHostJsonContext.Default.QqLoginStateEventPayload),
            null,
            null,
            null));
    }

    private static string RequiredJsonIdentifier(JsonElement value, string propertyName)
    {
        if (!value.TryGetProperty(propertyName, out JsonElement property))
        {
            throw new QqHostException("PROTOCOL_MISMATCH", $"QQ {propertyName} is missing");
        }

        string? identifier = JsonIdentifier(property);
        if (string.IsNullOrWhiteSpace(identifier) || identifier.Length > 256)
        {
            throw new QqHostException("PROTOCOL_MISMATCH", $"QQ {propertyName} is invalid");
        }

        return identifier;
    }

    private static string RequiredJsonString(JsonElement value, string propertyName)
    {
        if (!value.TryGetProperty(propertyName, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || string.IsNullOrWhiteSpace(property.GetString()))
        {
            throw new QqHostException("PROTOCOL_MISMATCH", $"QQ {propertyName} is invalid");
        }

        return property.GetString()!;
    }

    private static string? JsonIdentifier(JsonElement value) => value.ValueKind switch
    {
        JsonValueKind.String => value.GetString(),
        JsonValueKind.Number => value.GetRawText(),
        _ => null
    };

    private static JsonElement CreateAccountParameters(string accountId) =>
        JsonSerializer.SerializeToElement(
            new Dictionary<string, string> { ["account_id"] = accountId },
            QqHostJsonContext.Default.DictionaryStringString);

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
