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

    public QqDirectInvocationDispatcher(QqDirectProfile profile, QqHostClient host)
    {
        _profile = profile;
        _host = host;
    }

    public void Dispose() => _sessionGate.Dispose();

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
