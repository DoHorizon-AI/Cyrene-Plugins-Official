// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotWebSocketTransport.cs                                           │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: Forward-WebSocket action correlation and reconnect lifecycle.      │
// │                                                                         │
// │  模块职责：Forward WebSocket action 关联与重连生命周期                       │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Net.WebSockets;
using System.Text.Json;

namespace Cyrene.OneBot.V11.Core;

/// <summary>
/// AOT-safe OneBot v11 forward-WebSocket client.
/// <para>OneBot v11 forward-WebSocket 的 AOT 安全客户端。</para>
/// </summary>
public sealed class OneBotWebSocketTransport : IOneBotActionTransport, IDisposable
{
    public const int MaxMessageBytes = 16 * 1024 * 1024;

    private readonly OneBotProfile _profile;
    private readonly object _stateLock = new();
    private readonly object _pendingLock = new();
    private readonly SemaphoreSlim _sendLock = new(1, 1);
    private readonly CancellationTokenSource _shutdown = new();
    private readonly Dictionary<string, TaskCompletionSource<OneBotActionResponse>> _pending = new();
    private TaskCompletionSource<bool> _connected = NewSignal();
    private WebSocket? _socket;
    private Task? _runTask;
    private Action<JsonElement>? _eventHandler;
    private long _nextEcho;
    private int _reconnectCount;
    private bool _closed;

    public OneBotWebSocketTransport(OneBotProfile profile)
    {
        if (profile.TransportProfile == OneBotTransportProfile.ForwardWebSocket
            && profile.WebSocketUrl is null)
        {
            throw new OneBotConfigurationException(
                "TRANSPORT_PROFILE_MISMATCH",
                "OneBotWebSocketTransport requires a forward_websocket profile.");
        }

        if (profile.TransportProfile is not OneBotTransportProfile.ForwardWebSocket
            and not OneBotTransportProfile.ReverseWebSocket)
        {
            throw new OneBotConfigurationException(
                "TRANSPORT_PROFILE_MISMATCH",
                "OneBotWebSocketTransport requires a WebSocket profile.");
        }

        _profile = profile;
    }

    public bool Connected
    {
        get
        {
            lock (_stateLock)
            {
                return _socket?.State == WebSocketState.Open;
            }
        }
    }

    public int ReconnectCount
    {
        get
        {
            lock (_stateLock)
            {
                return _reconnectCount;
            }
        }
    }

    public void SetEventHandler(Action<JsonElement>? eventHandler)
    {
        lock (_stateLock)
        {
            _eventHandler = eventHandler;
        }
    }

    public void Start()
    {
        lock (_stateLock)
        {
            if (_closed || _runTask is { IsCompleted: false })
            {
                return;
            }

            if (_profile.TransportProfile == OneBotTransportProfile.ForwardWebSocket)
            {
                _runTask = RunAsync();
            }
        }
    }

    public void AttachSocket(WebSocket socket)
    {
        WebSocket? previous;
        lock (_stateLock)
        {
            if (_closed)
            {
                socket.Abort();
                socket.Dispose();
                return;
            }

            previous = _socket;
            _socket = socket;
            _connected.TrySetResult(true);
            _runTask = ReceiveAttachedAsync(socket);
        }

        previous?.Abort();
        previous?.Dispose();
    }

    public async Task<OneBotActionResponse> CallAsync(
        string action,
        OneBotActionRequest request,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(action)
            || action.Any(character => !char.IsLetterOrDigit(character) && character != '_'))
        {
            throw new OneBotTransportException("INVALID_ACTION", "OneBot action name is invalid.");
        }

        Start();
        using CancellationTokenSource timeout = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken);
        timeout.CancelAfter(TimeSpan.FromSeconds(_profile.TimeoutSeconds));
        try
        {
            await WaitUntilConnectedAsync(timeout.Token);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            throw new OneBotTransportException(
                "TIMEOUT",
                $"OneBot action '{action}' exceeded its configured connection timeout.");
        }

        string echo = $"{_profile.BindingId}:{Interlocked.Increment(ref _nextEcho)}";
        TaskCompletionSource<OneBotActionResponse> completion =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        lock (_pendingLock)
        {
            _pending[echo] = completion;
        }

        try
        {
            OneBotWebSocketActionRequest envelope = new()
            {
                Action = action,
                Params = request,
                Echo = echo
            };
            byte[] encoded = JsonSerializer.SerializeToUtf8Bytes(
                envelope,
                OneBotJsonContext.Default.OneBotWebSocketActionRequest);
            await _sendLock.WaitAsync(timeout.Token);
            try
            {
                WebSocket socket = CurrentSocket();
                await socket.SendAsync(
                    new ArraySegment<byte>(encoded),
                    WebSocketMessageType.Text,
                    true,
                    timeout.Token);
            }
            finally
            {
                _sendLock.Release();
            }

            OneBotActionResponse response = await completion.Task.WaitAsync(timeout.Token);
            if (!string.Equals(response.Status, "ok", StringComparison.OrdinalIgnoreCase)
                || response.Retcode != 0)
            {
                throw new OneBotTransportException(
                    "ACTION_REJECTED",
                    $"OneBot action '{action}' was rejected by the runtime.");
            }

            return response;
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            throw new OneBotTransportException(
                "TIMEOUT",
                $"OneBot action '{action}' exceeded its configured timeout.");
        }
        catch (WebSocketException)
        {
            throw new OneBotTransportException(
                "CAPABILITY_UNAVAILABLE",
                $"OneBot action '{action}' could not reach its runtime.");
        }
        finally
        {
            lock (_pendingLock)
            {
                _pending.Remove(echo);
            }
        }
    }

    public void Close()
    {
        lock (_stateLock)
        {
            if (_closed)
            {
                return;
            }

            _closed = true;
            _connected.TrySetCanceled();
            WebSocket? socket = _socket;
            _socket = null;
            socket?.Abort();
            socket?.Dispose();
        }

        _shutdown.Cancel();
        FailPending(new OneBotTransportException(
            "CAPABILITY_UNAVAILABLE",
            "OneBot WebSocket transport is closed."));
    }

    public void Dispose()
    {
        Close();
        _shutdown.Dispose();
        _sendLock.Dispose();
    }

    private async Task RunAsync()
    {
        double backoffSeconds = 0.05;
        while (!_shutdown.IsCancellationRequested)
        {
            ClientWebSocket? socket = null;
            try
            {
                socket = new ClientWebSocket();
                if (_profile.AccessToken.Length > 0)
                {
                    socket.Options.SetRequestHeader(
                        "Authorization",
                        $"Bearer {_profile.AccessToken}");
                }

                using CancellationTokenSource connectTimeout =
                    CancellationTokenSource.CreateLinkedTokenSource(_shutdown.Token);
                connectTimeout.CancelAfter(TimeSpan.FromSeconds(_profile.TimeoutSeconds));
                await socket.ConnectAsync(
                    new Uri(_profile.WebSocketUrl!),
                    connectTimeout.Token);
                SetConnected(socket);
                backoffSeconds = 0.05;
                await ReceiveLoopAsync(socket);
            }
            catch (OperationCanceledException) when (_shutdown.IsCancellationRequested)
            {
                break;
            }
            catch (OperationCanceledException)
            {
                FailPending(new OneBotTransportException(
                    "TIMEOUT",
                    "OneBot WebSocket connection attempt timed out."));
            }
            catch (WebSocketException)
            {
                FailPending(new OneBotTransportException(
                    "CAPABILITY_UNAVAILABLE",
                    "OneBot WebSocket runtime disconnected."));
            }
            catch (OneBotTransportException exception)
            {
                FailPending(exception);
            }
            catch (JsonException)
            {
                FailPending(new OneBotTransportException(
                    "PROTOCOL_MISMATCH",
                    "OneBot WebSocket runtime sent malformed JSON."));
            }
            finally
            {
                if (socket is not null)
                {
                    FailPending(new OneBotTransportException(
                        "CAPABILITY_UNAVAILABLE",
                        "OneBot WebSocket runtime disconnected."));
                }

                Disconnect(socket);
            }

            if (_shutdown.IsCancellationRequested)
            {
                break;
            }

            lock (_stateLock)
            {
                _reconnectCount++;
            }

            try
            {
                await Task.Delay(
                    TimeSpan.FromSeconds(backoffSeconds),
                    _shutdown.Token);
            }
            catch (OperationCanceledException) when (_shutdown.IsCancellationRequested)
            {
                break;
            }

            backoffSeconds = Math.Min(1.0, backoffSeconds * 2);
        }
    }

    private async Task ReceiveAttachedAsync(WebSocket socket)
    {
        try
        {
            await ReceiveLoopAsync(socket);
        }
        catch (OperationCanceledException) when (_shutdown.IsCancellationRequested)
        {
        }
        catch (WebSocketException)
        {
            FailPending(new OneBotTransportException(
                "CAPABILITY_UNAVAILABLE",
                "OneBot WebSocket runtime disconnected."));
        }
        catch (OneBotTransportException exception)
        {
            FailPending(exception);
        }
        catch (JsonException)
        {
            FailPending(new OneBotTransportException(
                "PROTOCOL_MISMATCH",
                "OneBot WebSocket runtime sent malformed JSON."));
        }
        finally
        {
            FailPending(new OneBotTransportException(
                "CAPABILITY_UNAVAILABLE",
                "OneBot WebSocket runtime disconnected."));
            Disconnect(socket);
        }
    }

    private async Task ReceiveLoopAsync(WebSocket socket)
    {
        byte[] buffer = new byte[16 * 1024];
        while (!_shutdown.IsCancellationRequested && socket.State == WebSocketState.Open)
        {
            using MemoryStream message = new();
            WebSocketReceiveResult receive;
            do
            {
                receive = await socket.ReceiveAsync(
                    new ArraySegment<byte>(buffer),
                    _shutdown.Token);
                if (receive.MessageType == WebSocketMessageType.Close)
                {
                    return;
                }

                if (receive.MessageType != WebSocketMessageType.Text)
                {
                    throw new OneBotTransportException(
                        "PROTOCOL_MISMATCH",
                        "OneBot WebSocket runtime sent a non-text frame.");
                }

                message.Write(buffer, 0, receive.Count);
                if (message.Length > MaxMessageBytes)
                {
                    throw new OneBotTransportException(
                        "RESPONSE_TOO_LARGE",
                        "OneBot WebSocket message exceeds the 16 MiB limit.");
                }
            }
            while (!receive.EndOfMessage);

            DispatchMessage(message.ToArray());
        }
    }

    private void DispatchMessage(byte[] encoded)
    {
        using JsonDocument document = JsonDocument.Parse(encoded);
        JsonElement root = document.RootElement;
        OneBotWebSocketFrame? frame = JsonSerializer.Deserialize(
            root,
            OneBotJsonContext.Default.OneBotWebSocketFrame);
        if (frame is null)
        {
            throw new OneBotTransportException(
                "PROTOCOL_MISMATCH",
                "OneBot WebSocket frame is empty.");
        }

        if (!string.IsNullOrEmpty(frame.Echo))
        {
            TaskCompletionSource<OneBotActionResponse>? completion = null;
            lock (_pendingLock)
            {
                if (_pending.Remove(
                        frame.Echo,
                        out TaskCompletionSource<OneBotActionResponse>? found))
                {
                    completion = found;
                }
            }

            completion?.TrySetResult(new OneBotActionResponse
            {
                Status = frame.Status,
                Retcode = frame.Retcode,
                Data = frame.Data,
                Echo = frame.Echo
            });
            return;
        }

        Action<JsonElement>? handler;
        lock (_stateLock)
        {
            handler = _eventHandler;
        }

        if (handler is not null)
        {
            try
            {
                handler(root.Clone());
            }
            catch (Exception)
            {
                // Event normalization is isolated from the transport reader.
            }
        }
    }

    private async Task WaitUntilConnectedAsync(CancellationToken cancellationToken)
    {
        while (true)
        {
            Task signal;
            lock (_stateLock)
            {
                signal = _connected.Task;
            }

            try
            {
                await signal.WaitAsync(cancellationToken);
                return;
            }
            catch (OneBotTransportException)
            {
                cancellationToken.ThrowIfCancellationRequested();
            }
        }
    }

    private WebSocket CurrentSocket()
    {
        lock (_stateLock)
        {
            if (_socket is null || _socket.State != WebSocketState.Open)
            {
                throw new OneBotTransportException(
                    "CAPABILITY_UNAVAILABLE",
                    "OneBot WebSocket runtime is not connected.");
            }

            return _socket;
        }
    }

    private void SetConnected(ClientWebSocket socket)
    {
        lock (_stateLock)
        {
            _socket = socket;
            _connected.TrySetResult(true);
        }
    }

    private void Disconnect(WebSocket? socket)
    {
        lock (_stateLock)
        {
            if (ReferenceEquals(_socket, socket))
            {
                _socket = null;
                _connected.TrySetException(new OneBotTransportException(
                    "CAPABILITY_UNAVAILABLE",
                    "OneBot WebSocket runtime disconnected."));
                _connected = NewSignal();
            }
        }

        socket?.Abort();
        socket?.Dispose();
    }

    private void FailPending(OneBotTransportException exception)
    {
        TaskCompletionSource<OneBotActionResponse>[] completions;
        lock (_pendingLock)
        {
            completions = _pending.Values.ToArray();
            _pending.Clear();
        }

        foreach (TaskCompletionSource<OneBotActionResponse> completion in completions)
        {
            completion.TrySetException(exception);
        }
    }

    private static TaskCompletionSource<bool> NewSignal() => new(
        TaskCreationOptions.RunContinuationsAsynchronously);
}
