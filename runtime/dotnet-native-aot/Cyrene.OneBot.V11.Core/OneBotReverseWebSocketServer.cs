// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotReverseWebSocketServer.cs                                       │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: Binding-local reverse-WebSocket handshake and listener lifecycle.  │
// │                                                                         │
// │  模块职责：binding 独立的 reverse WebSocket 握手与监听器生命周期               │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;

namespace Cyrene.OneBot.V11.Core;

/// <summary>
/// Accepts one or more reverse-WebSocket peers for one configured binding.
/// <para>为单个配置 binding 接受一个或多个 reverse-WebSocket 对端。</para>
/// </summary>
public sealed class OneBotReverseWebSocketServer : IDisposable
{
    private const int MaxHandshakeBytes = 64 * 1024;

    private readonly OneBotProfile _profile;
    private readonly OneBotWebSocketTransport _transport;
    private readonly CancellationTokenSource _shutdown = new();
    private readonly object _stateLock = new();
    private TcpListener? _listener;
    private TcpClient? _currentClient;
    private Task? _acceptTask;
    private bool _closed;

    public OneBotReverseWebSocketServer(
        OneBotProfile profile,
        OneBotWebSocketTransport transport)
    {
        if (profile.TransportProfile != OneBotTransportProfile.ReverseWebSocket)
        {
            throw new OneBotConfigurationException(
                "TRANSPORT_PROFILE_MISMATCH",
                "Reverse WebSocket server requires a reverse_websocket profile.");
        }

        _profile = profile;
        _transport = transport;
    }

    public int ListenPort
    {
        get
        {
            lock (_stateLock)
            {
                return (_listener?.LocalEndpoint as IPEndPoint)?.Port ?? 0;
            }
        }
    }

    public void Start()
    {
        lock (_stateLock)
        {
            if (_closed || _listener is not null)
            {
                return;
            }

            IPAddress address = ResolveAddress(_profile.ReverseListenHost);
            _listener = new TcpListener(address, _profile.ReverseListenPort);
            _listener.Start();
            _acceptTask = AcceptLoopAsync(_listener);
        }
    }

    public void Close()
    {
        TcpListener? listener;
        TcpClient? client;
        lock (_stateLock)
        {
            if (_closed)
            {
                return;
            }

            _closed = true;
            listener = _listener;
            client = _currentClient;
            _listener = null;
            _currentClient = null;
        }

        _shutdown.Cancel();
        listener?.Stop();
        client?.Close();
        _transport.Close();
    }

    public void Dispose()
    {
        Close();
        _shutdown.Dispose();
    }

    private async Task AcceptLoopAsync(TcpListener listener)
    {
        while (!_shutdown.IsCancellationRequested)
        {
            TcpClient? client = null;
            try
            {
                client = await listener.AcceptTcpClientAsync(_shutdown.Token);
                await AcceptClientAsync(client, _shutdown.Token);
            }
            catch (OperationCanceledException) when (_shutdown.IsCancellationRequested)
            {
                break;
            }
            catch (SocketException) when (_shutdown.IsCancellationRequested)
            {
                break;
            }
            finally
            {
                if (client is not null && !IsCurrentClient(client))
                {
                    client.Close();
                }
            }
        }
    }

    private async Task AcceptClientAsync(TcpClient client, CancellationToken cancellationToken)
    {
        NetworkStream stream = client.GetStream();
        string headers = await ReadHeadersAsync(stream, cancellationToken);
        Dictionary<string, string> values = ParseHeaders(headers);
        if (!IsWebSocketUpgrade(headers, values))
        {
            await WriteResponseAsync(stream, "HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
            return;
        }

        if (_profile.AccessToken.Length > 0
            && (!values.TryGetValue("authorization", out string? authorization)
                || !string.Equals(
                    authorization,
                    $"Bearer {_profile.AccessToken}",
                    StringComparison.Ordinal)))
        {
            await WriteResponseAsync(
                stream,
                "HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n");
            return;
        }

        if (!values.TryGetValue("sec-websocket-key", out string? key)
            || string.IsNullOrWhiteSpace(key))
        {
            await WriteResponseAsync(stream, "HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
            return;
        }

        string accept = ComputeWebSocketAccept(key);
        await WriteResponseAsync(
            stream,
            "HTTP/1.1 101 Switching Protocols\r\n"
            + "Upgrade: websocket\r\n"
            + "Connection: Upgrade\r\n"
            + $"Sec-WebSocket-Accept: {accept}\r\n\r\n");

        WebSocket socket = WebSocket.CreateFromStream(
            stream,
            isServer: true,
            subProtocol: null,
            keepAliveInterval: TimeSpan.FromSeconds(20));
        lock (_stateLock)
        {
            TcpClient? previous = _currentClient;
            _currentClient = client;
            previous?.Close();
        }

        _transport.AttachSocket(socket);
    }

    private static bool IsWebSocketUpgrade(
        string headers,
        Dictionary<string, string> values)
    {
        bool requestLineIsGet = headers.StartsWith("GET ", StringComparison.Ordinal);
        bool upgrade = values.TryGetValue("upgrade", out string? upgradeValue)
            && string.Equals(upgradeValue, "websocket", StringComparison.OrdinalIgnoreCase);
        bool connection = values.TryGetValue("connection", out string? connectionValue)
            && connectionValue.Contains("upgrade", StringComparison.OrdinalIgnoreCase);
        return requestLineIsGet && upgrade && connection;
    }

    private static async Task<string> ReadHeadersAsync(
        NetworkStream stream,
        CancellationToken cancellationToken)
    {
        using MemoryStream output = new();
        byte[] buffer = new byte[1];
        while (output.Length < MaxHandshakeBytes)
        {
            int read = await stream.ReadAsync(buffer, cancellationToken);
            if (read == 0)
            {
                throw new IOException("Reverse WebSocket peer closed during handshake.");
            }

            output.WriteByte(buffer[0]);
            if (output.Length >= 4)
            {
                byte[] bytes = output.GetBuffer();
                int length = checked((int)output.Length);
                if (bytes[length - 4] == '\r'
                    && bytes[length - 3] == '\n'
                    && bytes[length - 2] == '\r'
                    && bytes[length - 1] == '\n')
                {
                    return Encoding.ASCII.GetString(bytes, 0, length);
                }
            }
        }

        throw new OneBotTransportException(
            "PROTOCOL_MISMATCH",
            "Reverse WebSocket handshake is too large.");
    }

    private static Dictionary<string, string> ParseHeaders(string headers)
    {
        Dictionary<string, string> values = new(StringComparer.OrdinalIgnoreCase);
        string[] lines = headers.Split("\r\n", StringSplitOptions.RemoveEmptyEntries);
        foreach (string line in lines.Skip(1))
        {
            int separator = line.IndexOf(':');
            if (separator > 0)
            {
                values[line[..separator].Trim().ToLowerInvariant()] =
                    line[(separator + 1)..].Trim();
            }
        }

        return values;
    }

    private static async Task WriteResponseAsync(NetworkStream stream, string response)
    {
        await stream.WriteAsync(Encoding.ASCII.GetBytes(response));
    }

    private static IPAddress ResolveAddress(string host)
    {
        if (string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase))
        {
            return IPAddress.Loopback;
        }

        if (IPAddress.TryParse(host, out IPAddress? parsed))
        {
            return parsed;
        }

        IPAddress? resolved = Dns.GetHostAddresses(host)
            .FirstOrDefault(address => address.AddressFamily == AddressFamily.InterNetwork);
        return resolved ?? throw new OneBotConfigurationException(
            "INVALID_REVERSE_LISTENER",
            "reverse_listen_host could not be resolved.");
    }

    private static string ComputeWebSocketAccept(string key)
    {
#pragma warning disable CA5350 // RFC 6455 requires SHA-1 for Sec-WebSocket-Accept.
        return Convert.ToBase64String(
            System.Security.Cryptography.SHA1.HashData(
                Encoding.ASCII.GetBytes(
                    key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")));
#pragma warning restore CA5350
    }

    private bool IsCurrentClient(TcpClient client)
    {
        lock (_stateLock)
        {
            return ReferenceEquals(_currentClient, client);
        }
    }
}
