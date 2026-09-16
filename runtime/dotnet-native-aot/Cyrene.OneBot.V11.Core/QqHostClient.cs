// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqHostClient.cs                                                       │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: One-binding QQ Host subprocess lifecycle and correlation.          │
// │                                                                         │
// │  模块职责：单 binding QQ Host 子进程启停、握手、请求关联与故障隔离              │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json;

namespace Cyrene.OneBot.V11.Core;

/// <summary>Immutable launch inputs for one binding-local QQ Host process.</summary>
public sealed record QqHostLaunchConfiguration(
    string BindingId,
    string HostExecutable,
    IReadOnlyList<string> HostArguments,
    string DataDirectory,
    string RequiredClientVersion,
    string RequiredHostAbi,
    string Platform = "linux-x86_64",
    double TimeoutSeconds = 10,
    double StartupTimeoutSeconds = 30,
    double ShutdownTimeoutSeconds = 2,
    int MaxRestartAttempts = 2,
    double RestartWindowSeconds = 60,
    double RestartBackoffSeconds = 0.25,
    double RestartBackoffMaxSeconds = 5,
    double CrashCircuitCooldownSeconds = 60,
    string? InstallationManifest = null);

/// <summary>Negotiated QQ Host compatibility facts safe for diagnostics.</summary>
public sealed record QqHostCompatibility(
    string Protocol,
    string ProtocolVersion,
    string BindingId,
    int Generation,
    string Platform,
    string ClientVersion,
    string HostAbi);

/// <summary>Bounded restart and crash-circuit state safe for diagnostics.</summary>
public sealed record QqHostSupervision(
    string State,
    string? FailureCode,
    int RestartAttemptsInWindow,
    int MaxRestartAttempts,
    bool CircuitOpen);

/// <summary>Structured error from the QQ Host process boundary.</summary>
public sealed class QqHostException : Exception
{
    public QqHostException(string code, string message)
        : base(message)
    {
        Code = code;
    }

    public string Code { get; }
}

/// <summary>Supervises one authorized QQ Host child over inherited stdio.</summary>
public sealed class QqHostClient : IAsyncDisposable
{
    private sealed class PendingRequest
    {
        public TaskCompletionSource<QqHostResponse> Completion { get; } =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private readonly QqHostLaunchConfiguration _configuration;
    private readonly SemaphoreSlim _lifecycleGate = new(1, 1);
    private readonly SemaphoreSlim _transitionGate = new(1, 1);
    private readonly SemaphoreSlim _writeGate = new(1, 1);
    private readonly object _stateGate = new();
    private readonly Dictionary<string, PendingRequest> _pending = new(StringComparer.Ordinal);
    private CancellationTokenSource? _lifetimeCancellation;
    private Process? _process;
    private Stream? _input;
    private Stream? _output;
    private Task? _readerTask;
    private Task? _stderrTask;
    private Task? _listenerTask;
    private long _requestCounter;
    private int _generation;
    private string _state = "CREATED";
    private string? _failureCode;
    private QqHostCompatibility? _compatibility;
    private readonly List<DateTimeOffset> _restartHistory = new();
    private readonly List<string> _diagnostics = new();
    private DateTimeOffset _circuitOpenUntil;
    private bool _disposed;

    public QqHostClient(
        QqHostLaunchConfiguration configuration,
        Action<QqHostEvent>? eventHandler = null)
    {
        _configuration = ValidateConfiguration(configuration);
        EventHandler = eventHandler;
    }

    /// <summary>Receives only validated, current-generation Host events.</summary>
    public Action<QqHostEvent>? EventHandler { get; set; }

    public string BindingId => _configuration.BindingId;

    public int Generation
    {
        get
        {
            lock (_stateGate)
            {
                return _generation;
            }
        }
    }

    public string State
    {
        get
        {
            lock (_stateGate)
            {
                return _state;
            }
        }
    }

    public QqHostCompatibility? Compatibility
    {
        get
        {
            lock (_stateGate)
            {
                return _compatibility;
            }
        }
    }

    public string? FailureCode
    {
        get
        {
            lock (_stateGate)
            {
                return _failureCode;
            }
        }
    }

    public bool CrashCircuitOpen
    {
        get
        {
            lock (_stateGate)
            {
                return DateTimeOffset.UtcNow < _circuitOpenUntil;
            }
        }
    }

    public int RestartAttemptsInWindow
    {
        get
        {
            lock (_stateGate)
            {
                PruneRestartHistory(DateTimeOffset.UtcNow);
                return _restartHistory.Count;
            }
        }
    }

    /// <summary>Returns bounded restart state without process arguments or secrets.</summary>
    public QqHostSupervision Supervision
    {
        get
        {
            lock (_stateGate)
            {
                PruneRestartHistory(DateTimeOffset.UtcNow);
                return new QqHostSupervision(
                    _state,
                    _failureCode,
                    _restartHistory.Count,
                    _configuration.MaxRestartAttempts,
                    DateTimeOffset.UtcNow < _circuitOpenUntil);
            }
        }
    }

    /// <summary>Returns bounded stderr and supervision diagnostics after redaction.</summary>
    public IReadOnlyList<string> Diagnostics
    {
        get
        {
            lock (_stateGate)
            {
                return _diagnostics.ToArray();
            }
        }
    }

    public async Task StartAsync(CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        await _lifecycleGate.WaitAsync(cancellationToken);
        try
        {
            lock (_stateGate)
            {
                if (_process is not null && !_process.HasExited)
                {
                    return;
                }

                _generation++;
                _state = "STARTING";
                _failureCode = null;
                _compatibility = null;
            }

            QqHostInstallation installation = QqHostInstallationResolver.Resolve(_configuration);
            Directory.CreateDirectory(installation.DataDirectory);
            ValidateDataDirectory(installation.DataDirectory);
            Process process = StartProcess(installation);
            lock (_stateGate)
            {
                _process = process;
                _input = process.StandardInput.BaseStream;
                _output = process.StandardOutput.BaseStream;
                _lifetimeCancellation = new CancellationTokenSource();
                CancellationToken lifetimeToken = _lifetimeCancellation.Token;
                _readerTask = Task.Run(
                    () => ReadLoopAsync(process, lifetimeToken),
                    CancellationToken.None);
                _stderrTask = Task.Run(
                    () => DrainStderrAsync(process, lifetimeToken),
                    CancellationToken.None);
                _listenerTask = Task.Run(
                    () => WatchForTcpListenersAsync(process, lifetimeToken),
                    CancellationToken.None);
            }

            JsonElement helloParams = JsonSerializer.SerializeToElement(
                new QqHostHelloParams
                {
                    Protocol = QqHostProtocol.Protocol,
                    ProtocolVersion = QqHostProtocol.Version,
                    Platform = _configuration.Platform,
                    RequiredClientVersion = _configuration.RequiredClientVersion,
                    RequiredHostAbi = _configuration.RequiredHostAbi
                },
                QqHostJsonContext.Default.QqHostHelloParams);
            JsonElement report = await RequestCoreAsync(
                "hello",
                helloParams,
                _configuration.StartupTimeoutSeconds,
                allowHello: true,
                cancellationToken);
            QqHostCompatibility compatibility = ValidateHello(report);
            AssertNoTcpListener(process);
            lock (_stateGate)
            {
                if (_state != "FAILED" && !process.HasExited)
                {
                    _compatibility = compatibility;
                    _state = "NATIVE_READY";
                }
                else if (_state != "FAILED")
                {
                    _state = "FAILED";
                    _failureCode = "PROCESS_EXITED";
                }
            }
        }
        catch
        {
            await CloseCoreAsync(CancellationToken.None);
            throw;
        }
        finally
        {
            _lifecycleGate.Release();
        }
    }

    public async Task<JsonElement> RequestAsync(
        string operation,
        JsonElement parameters,
        CancellationToken cancellationToken,
        Action<string>? requestIdHandler = null)
    {
        ThrowIfDisposed();
        if (!QqHostOperationRegistry.TryGet(operation, out QqHostOperation? spec))
        {
            throw new QqHostException("UNKNOWN_OPERATION", $"unsupported QQ operation {operation}");
        }

        if (!spec.Requestable)
        {
            throw new QqHostException(
                "UNSUPPORTED_OPERATION",
                $"QQ operation {operation} is callback-only");
        }

        if (parameters.ValueKind != JsonValueKind.Object)
        {
            throw new QqHostException("INVALID_REQUEST", "QQ operation params must be an object");
        }

        return await RequestCoreAsync(
            operation,
            parameters,
            _configuration.TimeoutSeconds,
            allowHello: false,
            cancellationToken,
            requestIdHandler);
    }

    public async Task RestartAsync(CancellationToken cancellationToken)
    {
        await _transitionGate.WaitAsync(cancellationToken);
        try
        {
            await CloseAsync(cancellationToken);
            await StartAsync(cancellationToken);
        }
        finally
        {
            _transitionGate.Release();
        }
    }

    public async Task RecoverAsync(CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        await _transitionGate.WaitAsync(cancellationToken);
        try
        {
            DateTimeOffset now = DateTimeOffset.UtcNow;
            double delay;
            lock (_stateGate)
            {
                if (_state != "FAILED"
                    || _failureCode is not ("PROCESS_EXITED" or "STDIO_CLOSED"))
                {
                    throw new QqHostException(
                        "CAPABILITY_UNAVAILABLE",
                        "QQ Host failure is not eligible for automatic recovery");
                }

                PruneRestartHistory(now);
                if (now < _circuitOpenUntil)
                {
                    throw new QqHostException("CAPABILITY_UNAVAILABLE", "QQ Host crash circuit is open");
                }

                if (_restartHistory.Count >= _configuration.MaxRestartAttempts)
                {
                    _circuitOpenUntil = now.AddSeconds(_configuration.CrashCircuitCooldownSeconds);
                    throw new QqHostException(
                        "CAPABILITY_UNAVAILABLE",
                        "QQ Host crash restart budget exhausted");
                }

                int attempt = _restartHistory.Count;
                _restartHistory.Add(now);
                delay = Math.Min(
                    _configuration.RestartBackoffSeconds * Math.Pow(2, attempt),
                    _configuration.RestartBackoffMaxSeconds);
            }

            if (delay > 0)
            {
                await Task.Delay(TimeSpan.FromSeconds(delay), cancellationToken);
            }

            await CloseAsync(cancellationToken);
            await StartAsync(cancellationToken);
        }
        finally
        {
            _transitionGate.Release();
        }
    }

    public async Task CloseAsync(CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        await _lifecycleGate.WaitAsync(cancellationToken);
        try
        {
            await CloseCoreAsync(cancellationToken);
        }
        finally
        {
            _lifecycleGate.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        await _lifecycleGate.WaitAsync();
        try
        {
            await CloseCoreAsync(CancellationToken.None);
        }
        finally
        {
            _lifecycleGate.Release();
            _lifecycleGate.Dispose();
            _transitionGate.Dispose();
            _writeGate.Dispose();
        }
    }

    private async Task<JsonElement> RequestCoreAsync(
        string operation,
        JsonElement parameters,
        double timeoutSeconds,
        bool allowHello,
        CancellationToken cancellationToken,
        Action<string>? requestIdHandler = null)
    {
        Process process;
        int generation;
        Stream input;
        lock (_stateGate)
        {
            process = _process
                ?? throw new QqHostException("CAPABILITY_UNAVAILABLE", "QQ Host is not running");
            generation = _generation;
            input = _input
                ?? throw new QqHostException("CAPABILITY_UNAVAILABLE", "QQ Host stdin is unavailable");
            if (_state is "FAILED" or "DRAINING" or "STOPPED")
            {
                throw new QqHostException("CAPABILITY_UNAVAILABLE", "QQ Host is unavailable");
            }
        }

        if (!allowHello && operation == "hello")
        {
            throw new QqHostException("INVALID_REQUEST", "hello is reserved for Host negotiation");
        }

        string requestId;
        PendingRequest pending = new();
        lock (_stateGate)
        {
            requestId = $"{BindingId}:{generation}:{Interlocked.Increment(ref _requestCounter)}";
            _pending.Add(requestId, pending);
        }
        requestIdHandler?.Invoke(requestId);

        QqHostRequest message = new()
        {
            RequestId = requestId,
            BindingId = BindingId,
            Generation = generation,
            Operation = operation,
            Params = parameters.Clone()
        };
        try
        {
            await SendAsync(input, message, cancellationToken);
            QqHostResponse response = await pending.Completion.Task.WaitAsync(
                TimeSpan.FromSeconds(timeoutSeconds),
                cancellationToken);
            if (!response.Ok)
            {
                throw new QqHostException(
                    response.Error?.Code ?? "EXECUTION_FAILED",
                    response.Error?.Message ?? "QQ Host rejected the operation");
            }

            return response.Result.Clone();
        }
        catch (TimeoutException)
        {
            RemovePending(requestId);
            await SendCancelBestEffortAsync(input, requestId, generation);
            throw new QqHostException(
                "TIMEOUT",
                $"QQ Host operation {operation} timed out");
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            RemovePending(requestId);
            await SendCancelBestEffortAsync(input, requestId, generation);
            throw new QqHostException("CANCELLED", "QQ Host operation was cancelled");
        }
        catch (QqHostProtocolException exception)
        {
            RemovePending(requestId);
            throw new QqHostException("PROTOCOL_MISMATCH", exception.Message);
        }
        catch (IOException)
        {
            RemovePending(requestId);
            MarkFailed("STDIO_CLOSED", "QQ Host stdio write failed", process);
            throw new QqHostException("CAPABILITY_UNAVAILABLE", "QQ Host stdio write failed");
        }
    }

    private async Task SendAsync(
        Stream input,
        QqHostRequest message,
        CancellationToken cancellationToken)
    {
        await _writeGate.WaitAsync(cancellationToken);
        try
        {
            await QqHostProtocol.WriteAsync(input, message, cancellationToken);
        }
        finally
        {
            _writeGate.Release();
        }
    }

    private async Task SendCancelBestEffortAsync(
        Stream input,
        string requestId,
        int generation)
    {
        try
        {
            await _writeGate.WaitAsync();
            try
            {
                QqHostRequest cancel = new()
                {
                    Type = "cancel",
                    RequestId = requestId,
                    BindingId = BindingId,
                    Generation = generation,
                    Operation = string.Empty,
                    Params = JsonSerializer.SerializeToElement(
                        new Dictionary<string, string>(),
                        QqHostJsonContext.Default.DictionaryStringString)
                };
                await QqHostProtocol.WriteAsync(input, cancel, CancellationToken.None);
            }
            finally
            {
                _writeGate.Release();
            }
        }
        catch (Exception)
        {
            // Cancellation is best effort once the caller has already received a terminal error.
        }
    }

    private async Task ReadLoopAsync(Process process, CancellationToken cancellationToken)
    {
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                byte[]? frame = await QqHostProtocol.ReadFrameAsync(
                    process.StandardOutput.BaseStream,
                    cancellationToken);
                if (frame is null)
                {
                    MarkFailed("STDIO_CLOSED", "QQ Host closed stdio", process);
                    return;
                }

                using JsonDocument document = JsonDocument.Parse(frame);
                JsonElement root = document.RootElement;
                string? type = root.TryGetProperty("type", out JsonElement typeElement)
                    ? typeElement.GetString()
                    : null;
                if (type == "event")
                {
                    HandleEvent(JsonSerializer.Deserialize(
                        frame,
                        QqHostJsonContext.Default.QqHostEvent));
                }
                else if (type is "response" or "hello_ack")
                {
                    HandleResponse(JsonSerializer.Deserialize(
                        frame,
                        QqHostJsonContext.Default.QqHostResponse));
                }
                else
                {
                    throw new QqHostProtocolException("QQ Host sent an unknown message type");
                }
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (QqHostProtocolException exception)
        {
            FailPending(new QqHostException("PROTOCOL_MISMATCH", exception.Message));
            MarkFailed("PROTOCOL_MISMATCH", exception.Message, process);
        }
        catch (JsonException)
        {
            FailPending(new QqHostException("PROTOCOL_MISMATCH", "QQ Host JSON is invalid"));
            MarkFailed("PROTOCOL_MISMATCH", "QQ Host JSON is invalid", process);
        }
        catch (IOException)
        {
            FailPending(new QqHostException("CAPABILITY_UNAVAILABLE", "QQ Host stdio closed"));
            MarkFailed("STDIO_CLOSED", "QQ Host stdio closed", process);
        }
    }

    private async Task DrainStderrAsync(Process process, CancellationToken cancellationToken)
    {
        try
        {
            while (!cancellationToken.IsCancellationRequested
                && await process.StandardError.ReadLineAsync(cancellationToken) is string line)
            {
                string trimmed = line.Trim();
                if (trimmed.Length > 0)
                {
                    RecordDiagnostic(trimmed);
                }
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (IOException)
        {
        }
    }

    private async Task WatchForTcpListenersAsync(
        Process process,
        CancellationToken cancellationToken)
    {
        try
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                await Task.Delay(TimeSpan.FromMilliseconds(250), cancellationToken);
                try
                {
                    AssertNoTcpListener(process);
                }
                catch (QqHostException exception)
                {
                    MarkFailed(
                        exception.Code,
                        exception.Message,
                        process,
                        new QqHostException(exception.Code, exception.Message));
                    RecordDiagnostic(exception.Message);
                    TryKill(process);
                    return;
                }
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
    }

    private void HandleResponse(QqHostResponse? response)
    {
        if (response is null
            || response.RequestId is null
            || response.BindingId != BindingId
            || response.Generation != Generation)
        {
            throw new QqHostProtocolException(
                "QQ Host response crossed binding or generation");
        }

        PendingRequest? pending;
        lock (_stateGate)
        {
            _pending.Remove(response.RequestId, out pending);
        }

        pending?.Completion.TrySetResult(response);
    }

    private void HandleEvent(QqHostEvent? @event)
    {
        if (@event is null
            || string.IsNullOrWhiteSpace(@event.EventId)
            || string.IsNullOrWhiteSpace(@event.Event)
            || @event.BindingId != BindingId
            || @event.Generation != Generation)
        {
            throw new QqHostProtocolException(
                "QQ Host event crossed binding or generation");
        }

        try
        {
            EventHandler?.Invoke(
                new QqHostEvent
                {
                    Type = @event.Type,
                    Event = @event.Event,
                    EventId = @event.EventId,
                    RequestId = @event.RequestId,
                    BindingId = @event.BindingId,
                    Generation = @event.Generation,
                    Payload = @event.Payload.Clone()
                });
        }
        catch (Exception)
        {
            // A malformed vendor event must not terminate the protocol reader.
        }
    }

    private async Task CloseCoreAsync(CancellationToken cancellationToken)
    {
        Process? process;
        Stream? input;
        int generation;
        int processId;
        IReadOnlyList<ProcessIdentity> processTree;
        lock (_stateGate)
        {
            process = _process;
            input = _input;
            generation = _generation;
            if (process is null)
            {
                _state = "STOPPED";
                return;
            }

            _state = "DRAINING";
            processId = process.Id;
            processTree = CaptureProcessTree(processId);
        }

        if (!process.HasExited && input is not null)
        {
            QqHostShutdown shutdown = new()
            {
                BindingId = BindingId,
                Generation = generation
            };
            try
            {
                await _writeGate.WaitAsync(cancellationToken);
                try
                {
                    await QqHostProtocol.WriteAsync(input, shutdown, cancellationToken);
                }
                finally
                {
                    _writeGate.Release();
                }
            }
            catch (Exception)
            {
            }

            try
            {
                await process.WaitForExitAsync(cancellationToken).WaitAsync(
                    TimeSpan.FromSeconds(_configuration.ShutdownTimeoutSeconds),
                    cancellationToken);
            }
            catch (Exception)
            {
                TryKill(process);
            }
        }

        FailPending(new QqHostException("CAPABILITY_UNAVAILABLE", "QQ Host stopped"));
        CancellationTokenSource? lifetimeCancellation;
        lock (_stateGate)
        {
            lifetimeCancellation = _lifetimeCancellation;
            _lifetimeCancellation = null;
        }

        lifetimeCancellation?.Cancel();
        TryKill(process);
        process.Dispose();
        await WaitForBackgroundTaskAsync(_readerTask);
        await WaitForBackgroundTaskAsync(_stderrTask);
        await WaitForBackgroundTaskAsync(_listenerTask);
        KillCapturedDescendants(processTree, processId);
        lifetimeCancellation?.Dispose();
        lock (_stateGate)
        {
            _process = null;
            _input = null;
            _output = null;
            _readerTask = null;
            _stderrTask = null;
            _listenerTask = null;
            _state = "STOPPED";
            _failureCode = null;
        }
    }

    private Process StartProcess(QqHostInstallation installation)
    {
        ProcessStartInfo startInfo = new()
        {
            FileName = installation.HostExecutable,
            WorkingDirectory = installation.DataDirectory,
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true
        };
        foreach (string argument in _configuration.HostArguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        int generation = Generation;
        startInfo.Environment["CYRENE_QQ_BINDING_ID"] = BindingId;
        startInfo.Environment["CYRENE_QQ_BINDING_GENERATION"] =
            generation.ToString(CultureInfo.InvariantCulture);
        startInfo.Environment["CYRENE_QQ_BINDING_DATA_DIR"] = installation.DataDirectory;
        Process process = new() { StartInfo = startInfo, EnableRaisingEvents = true };
        try
        {
            if (!process.Start())
            {
                throw new QqHostException("CAPABILITY_UNAVAILABLE", "QQ Host could not be started");
            }
        }
        catch (QqHostException)
        {
            process.Dispose();
            throw;
        }
        catch (Exception exception) when (exception is InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            process.Dispose();
            throw new QqHostException("CAPABILITY_UNAVAILABLE", "QQ Host could not be started");
        }

        return process;
    }

    private QqHostCompatibility ValidateHello(JsonElement report)
    {
        if (report.ValueKind != JsonValueKind.Object)
        {
            throw new QqHostException("PROTOCOL_MISMATCH", "QQ Host hello report is not an object");
        }

        string protocol = RequiredProperty(report, "protocol");
        string protocolVersion = RequiredProperty(report, "protocol_version");
        string bindingId = RequiredProperty(report, "binding_id");
        int generation = RequiredIntProperty(report, "generation");
        string platform = RequiredProperty(report, "platform");
        string clientVersion = RequiredProperty(report, "client_version");
        string hostAbi = RequiredProperty(report, "abi");
        if (protocol != QqHostProtocol.Protocol
            || protocolVersion != QqHostProtocol.Version
            || bindingId != BindingId
            || generation != Generation
            || platform != _configuration.Platform)
        {
            throw new QqHostException("PROTOCOL_MISMATCH", "QQ Host hello fields mismatched");
        }

        if (clientVersion != _configuration.RequiredClientVersion
            || hostAbi != _configuration.RequiredHostAbi)
        {
            throw new QqHostException("UNSUPPORTED_VERSION", "QQ Host version or ABI is not allow-listed");
        }

        return new QqHostCompatibility(
            protocol,
            protocolVersion,
            bindingId,
            generation,
            platform,
            clientVersion,
            hostAbi);
    }

    private void FailPending(QqHostException exception)
    {
        PendingRequest[] pending;
        lock (_stateGate)
        {
            pending = _pending.Values.ToArray();
            _pending.Clear();
        }

        foreach (PendingRequest item in pending)
        {
            item.Completion.TrySetException(exception);
        }
    }

    private void RemovePending(string requestId)
    {
        lock (_stateGate)
        {
            _pending.Remove(requestId);
        }
    }

    private void MarkFailed(
        string code,
        string message,
        Process process,
        QqHostException? pendingException = null)
    {
        lock (_stateGate)
        {
            if (!ReferenceEquals(_process, process) || _state is "DRAINING" or "STOPPED")
            {
                return;
            }

            _state = "FAILED";
            _failureCode = code == "STDIO_CLOSED" && process.HasExited
                ? "PROCESS_EXITED"
                : code;
        }

        FailPending(pendingException ?? new QqHostException("CAPABILITY_UNAVAILABLE", message));
    }

    private void RecordDiagnostic(string value)
    {
        string redacted = RedactDiagnostic(value);
        lock (_stateGate)
        {
            _diagnostics.Add(redacted.Length > 512 ? redacted[..512] : redacted);
            if (_diagnostics.Count > 64)
            {
                _diagnostics.RemoveRange(0, _diagnostics.Count - 64);
            }
        }
    }

    private void PruneRestartHistory(DateTimeOffset now)
    {
        _restartHistory.RemoveAll(
            timestamp => timestamp < now.AddSeconds(-_configuration.RestartWindowSeconds));
    }

    private static QqHostLaunchConfiguration ValidateConfiguration(
        QqHostLaunchConfiguration configuration)
    {
        if (string.IsNullOrWhiteSpace(configuration.BindingId)
            || string.IsNullOrWhiteSpace(configuration.HostExecutable)
            || string.IsNullOrWhiteSpace(configuration.DataDirectory)
            || string.IsNullOrWhiteSpace(configuration.RequiredClientVersion)
            || string.IsNullOrWhiteSpace(configuration.RequiredHostAbi))
        {
            throw new QqHostException("INVALID_REQUEST", "QQ Host launch fields are required");
        }

        if (!Path.IsPathRooted(configuration.HostExecutable)
            || !Path.IsPathRooted(configuration.DataDirectory))
        {
            throw new QqHostException("INVALID_REQUEST", "QQ Host paths must be absolute");
        }

        if (configuration.Platform != "linux-x86_64")
        {
            throw new QqHostException(
                "UNSUPPORTED_VERSION",
                "the first qqnt-direct target is linux-x86_64");
        }

        if (OperatingSystem.IsWindows() || OperatingSystem.IsMacOS())
        {
            throw new QqHostException(
                "UNSUPPORTED_VERSION",
                "qqnt-direct requires a Linux x86_64 host");
        }

        return configuration;
    }

    private static void ValidateDataDirectory(string path)
    {
        FileAttributes attributes = File.GetAttributes(path);
        if ((attributes & FileAttributes.Directory) == 0
            || (attributes & FileAttributes.ReparsePoint) != 0)
        {
            throw new QqHostException("INVALID_REQUEST", "QQ Host data directory must be a real directory");
        }
    }

    private static string RequiredProperty(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || string.IsNullOrWhiteSpace(property.GetString()))
        {
            throw new QqHostException("PROTOCOL_MISMATCH", $"QQ Host hello field {name} is missing");
        }

        return property.GetString()!;
    }

    private static int RequiredIntProperty(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out JsonElement property)
            || property.ValueKind != JsonValueKind.Number
            || !property.TryGetInt32(out int result))
        {
            throw new QqHostException("PROTOCOL_MISMATCH", $"QQ Host hello field {name} is invalid");
        }

        return result;
    }

    private sealed record ProcessIdentity(int Pid, string StartTime);

    private static void AssertNoTcpListener(Process process)
    {
        if (!OperatingSystem.IsLinux() || process.HasExited)
        {
            return;
        }

        HashSet<string> listeningInodes = ReadListeningTcpInodes();
        if (listeningInodes.Count == 0)
        {
            return;
        }

        foreach (int pid in ReadProcessTree(process.Id))
        {
            string fdRoot = $"/proc/{pid}/fd";
            IEnumerable<string> entries;
            try
            {
                entries = Directory.EnumerateFileSystemEntries(fdRoot);
            }
            catch (IOException)
            {
                continue;
            }
            catch (UnauthorizedAccessException)
            {
                continue;
            }

            foreach (string entry in entries)
            {
                string? target;
                try
                {
                    target = new FileInfo(entry).LinkTarget;
                }
                catch (IOException)
                {
                    continue;
                }
                catch (UnauthorizedAccessException)
                {
                    continue;
                }

                if (target is null
                    || !target.StartsWith("socket:[", StringComparison.Ordinal)
                    || !target.EndsWith(']'))
                {
                    continue;
                }

                string inode = target[8..^1];
                if (listeningInodes.Contains(inode))
                {
                    throw new QqHostException(
                        "PROTOCOL_MISMATCH",
                        "QQ Host process tree opened a TCP listener");
                }
            }
        }
    }

    private static HashSet<string> ReadListeningTcpInodes()
    {
        HashSet<string> inodes = new(StringComparer.Ordinal);
        foreach (string table in new[] { "/proc/net/tcp", "/proc/net/tcp6" })
        {
            string[] lines;
            try
            {
                lines = File.ReadAllLines(table);
            }
            catch (IOException)
            {
                continue;
            }
            catch (UnauthorizedAccessException)
            {
                continue;
            }

            foreach (string line in lines.Skip(1))
            {
                string[] fields = line.Split(
                    (char[]?)null,
                    StringSplitOptions.RemoveEmptyEntries);
                if (fields.Length > 9 && fields[3].Equals("0A", StringComparison.OrdinalIgnoreCase))
                {
                    inodes.Add(fields[9]);
                }
            }
        }

        return inodes;
    }

    private static int[] ReadProcessTree(int rootPid)
    {
        Dictionary<int, int> parents = new();
        IEnumerable<string> entries;
        try
        {
            entries = Directory.EnumerateDirectories("/proc");
        }
        catch (IOException)
        {
            return new[] { rootPid };
        }
        catch (UnauthorizedAccessException)
        {
            return new[] { rootPid };
        }

        foreach (string entry in entries)
        {
            string name = Path.GetFileName(entry);
            if (!int.TryParse(name, out int pid))
            {
                continue;
            }

            if (!TryReadProcessStat(pid, out int parentPid, out _))
            {
                continue;
            }

            parents[pid] = parentPid;
        }

        HashSet<int> result = new() { rootPid };
        bool changed;
        do
        {
            changed = false;
            foreach ((int pid, int parentPid) in parents)
            {
                if (result.Contains(parentPid) && result.Add(pid))
                {
                    changed = true;
                }
            }
        }
        while (changed);

        return result.ToArray();
    }

    private static IReadOnlyList<ProcessIdentity> CaptureProcessTree(int rootPid)
    {
        if (!OperatingSystem.IsLinux())
        {
            return Array.Empty<ProcessIdentity>();
        }

        List<ProcessIdentity> identities = new();
        foreach (int pid in ReadProcessTree(rootPid))
        {
            if (TryReadProcessStat(pid, out _, out string startTime))
            {
                identities.Add(new ProcessIdentity(pid, startTime));
            }
        }

        return identities;
    }

    private static bool TryReadProcessStat(
        int pid,
        out int parentPid,
        out string startTime)
    {
        parentPid = 0;
        startTime = string.Empty;
        try
        {
            string stat = File.ReadAllText($"/proc/{pid}/stat");
            int closingName = stat.LastIndexOf(')');
            if (closingName < 0)
            {
                return false;
            }

            string[] fields = stat[(closingName + 2)..].Split(
                (char[]?)null,
                StringSplitOptions.RemoveEmptyEntries);
            if (fields.Length <= 19 || !int.TryParse(fields[1], out parentPid))
            {
                return false;
            }

            startTime = fields[19];
            return true;
        }
        catch (IOException)
        {
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            return false;
        }
    }

    private static void KillCapturedDescendants(
        IReadOnlyList<ProcessIdentity> processTree,
        int rootPid)
    {
        if (!OperatingSystem.IsLinux())
        {
            return;
        }

        foreach (ProcessIdentity identity in processTree)
        {
            if (identity.Pid == rootPid
                || !TryReadProcessStat(identity.Pid, out _, out string currentStartTime)
                || currentStartTime != identity.StartTime)
            {
                continue;
            }

            try
            {
                using Process descendant = Process.GetProcessById(identity.Pid);
                if (!descendant.HasExited)
                {
                    descendant.Kill();
                }
            }
            catch (ArgumentException)
            {
            }
            catch (InvalidOperationException)
            {
            }
            catch (System.ComponentModel.Win32Exception)
            {
            }
        }
    }

    private static void TryKill(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (InvalidOperationException)
        {
        }
        catch (System.ComponentModel.Win32Exception)
        {
        }
    }

    private static async Task WaitForBackgroundTaskAsync(Task? task)
    {
        if (task is null || task.IsCompleted)
        {
            return;
        }

        try
        {
            await task.WaitAsync(TimeSpan.FromSeconds(2));
        }
        catch (Exception) when (task.IsCompleted || task.IsCanceled || task.IsFaulted)
        {
        }
        catch (TimeoutException)
        {
        }
    }

    private static string RedactDiagnostic(string value)
    {
        const string redacted = "<redacted>";
        string[] markers = ["password", "token", "secret", "ticket", "cookie"];
        StringBuilder builder = new(value.Length);
        int cursor = 0;
        while (cursor < value.Length)
        {
            int markerStart = -1;
            string? marker = null;
            foreach (string candidate in markers)
            {
                int candidateStart = value.IndexOf(
                    candidate,
                    cursor,
                    StringComparison.OrdinalIgnoreCase);
                if (candidateStart >= 0 && (markerStart < 0 || candidateStart < markerStart))
                {
                    markerStart = candidateStart;
                    marker = candidate;
                }
            }

            if (markerStart < 0 || marker is null)
            {
                builder.Append(value, cursor, value.Length - cursor);
                break;
            }

            int separator = markerStart + marker.Length;
            while (separator < value.Length && char.IsWhiteSpace(value[separator]))
            {
                separator++;
            }

            if (separator >= value.Length || (value[separator] != ':' && value[separator] != '='))
            {
                builder.Append(value, cursor, markerStart + marker.Length - cursor);
                cursor = markerStart + marker.Length;
                continue;
            }

            int secretStart = separator + 1;
            while (secretStart < value.Length && char.IsWhiteSpace(value[secretStart]))
            {
                secretStart++;
            }
            int secretEnd = secretStart;
            while (secretEnd < value.Length && !char.IsWhiteSpace(value[secretEnd]))
            {
                secretEnd++;
            }

            builder.Append(value, cursor, secretStart - cursor);
            builder.Append(redacted);
            cursor = secretEnd;
        }

        return builder.ToString();
    }

    private void ThrowIfDisposed()
    {
        ObjectDisposedException.ThrowIf(_disposed, nameof(QqHostClient));
    }
}
