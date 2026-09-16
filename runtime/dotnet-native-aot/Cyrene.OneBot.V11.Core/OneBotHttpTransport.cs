// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 OneBotHttpTransport.cs                                                │
// │  Namespace: Cyrene.OneBot.V11.Core                                        │
// │  Role: Bounded HTTP action transport for a configured OneBot profile.     │
// │                                                                         │
// │  模块职责：为单个 OneBot profile 提供有界 HTTP action 传输                    │
// └─────────────────────────────────────────────────────────────────────────┘

using System.Buffers;
using System.Net.Http.Headers;
using System.Text.Json;

namespace Cyrene.OneBot.V11.Core;

/// <summary>Binding-local transport for OneBot action calls.</summary>
public interface IOneBotActionTransport
{
    Task<OneBotActionResponse> CallAsync(
        string action,
        OneBotActionRequest request,
        CancellationToken cancellationToken);
}

/// <summary>Safe transport failure classification for the direct runtime.</summary>
public sealed class OneBotTransportException : Exception
{
    public OneBotTransportException(string domainCode, string message)
        : base(message)
    {
        DomainCode = domainCode;
    }

    public string DomainCode { get; }
}

/// <summary>
/// AOT-safe HTTP action transport with response-size and timeout bounds.
/// <para>带响应大小与超时边界的 AOT 安全 HTTP action 传输。</para>
/// </summary>
public sealed class OneBotHttpTransport : IOneBotActionTransport, IDisposable
{
    public const int MaxResponseBytes = 8 * 1024 * 1024;

    private readonly OneBotProfile _profile;
    private readonly HttpClient _httpClient;
    private readonly bool _ownsHttpClient;

    public OneBotHttpTransport(
        OneBotProfile profile,
        HttpClient? httpClient = null)
    {
        if (!profile.IsHttpApi || profile.HttpBaseUrl is null)
        {
            throw new OneBotConfigurationException(
                "TRANSPORT_PROFILE_MISMATCH",
                "OneBotHttpTransport requires an http_api profile with http_base_url.");
        }

        _profile = profile;
        _httpClient = httpClient ?? new HttpClient
        {
            Timeout = Timeout.InfiniteTimeSpan
        };
        _ownsHttpClient = httpClient is null;
    }

    public async Task<OneBotActionResponse> CallAsync(
        string action,
        OneBotActionRequest request,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(action)
            || action.Any(character => !char.IsLetterOrDigit(character) && character != '_'))
        {
            throw new OneBotTransportException(
                "INVALID_ACTION",
                "OneBot action name is invalid.");
        }

        Uri endpoint = new($"{_profile.HttpBaseUrl!.TrimEnd('/')}/{action}");
        byte[] encoded = JsonSerializer.SerializeToUtf8Bytes(
            request,
            OneBotJsonContext.Default.OneBotActionRequest);
        using HttpRequestMessage message = new(HttpMethod.Post, endpoint)
        {
            Content = new ByteArrayContent(encoded)
        };
        message.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
        message.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        if (_profile.AccessToken.Length > 0)
        {
            message.Headers.Authorization = new AuthenticationHeaderValue(
                "Bearer",
                _profile.AccessToken);
        }

        using CancellationTokenSource timeout = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken);
        timeout.CancelAfter(TimeSpan.FromSeconds(_profile.TimeoutSeconds));

        HttpResponseMessage response;
        try
        {
            response = await _httpClient.SendAsync(
                message,
                HttpCompletionOption.ResponseHeadersRead,
                timeout.Token);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            throw new OneBotTransportException(
                "TIMEOUT",
                $"OneBot action '{action}' exceeded its configured timeout.");
        }
        catch (HttpRequestException)
        {
            throw new OneBotTransportException(
                "CAPABILITY_UNAVAILABLE",
                $"OneBot action '{action}' could not reach its runtime.");
        }

        using (response)
        {
            if (response.Content.Headers.ContentLength > MaxResponseBytes)
            {
                throw new OneBotTransportException(
                    "RESPONSE_TOO_LARGE",
                    "OneBot action response exceeds the 8 MiB limit.");
            }

            byte[] body = await ReadBoundedAsync(
                response,
                timeout.Token,
                cancellationToken);
            if (!response.IsSuccessStatusCode)
            {
                throw new OneBotTransportException(
                    "HTTP_STATUS",
                    $"OneBot action '{action}' returned HTTP {(int)response.StatusCode}.");
            }

            OneBotActionResponse? decoded;
            try
            {
                decoded = JsonSerializer.Deserialize(
                    body,
                    OneBotJsonContext.Default.OneBotActionResponse);
            }
            catch (JsonException)
            {
                throw new OneBotTransportException(
                    "PROTOCOL_MISMATCH",
                    $"OneBot action '{action}' returned malformed JSON.");
            }

            if (decoded is null)
            {
                throw new OneBotTransportException(
                    "PROTOCOL_MISMATCH",
                    $"OneBot action '{action}' returned an empty response.");
            }

            if (!string.Equals(decoded.Status, "ok", StringComparison.OrdinalIgnoreCase)
                || decoded.Retcode != 0)
            {
                throw new OneBotTransportException(
                    "ACTION_REJECTED",
                    $"OneBot action '{action}' was rejected by the runtime.");
            }

            return decoded;
        }
    }

    public void Dispose()
    {
        if (_ownsHttpClient)
        {
            _httpClient.Dispose();
        }
    }

    private static async Task<byte[]> ReadBoundedAsync(
        HttpResponseMessage response,
        CancellationToken cancellationToken,
        CancellationToken callerCancellationToken)
    {
        try
        {
            await using Stream stream = await response.Content.ReadAsStreamAsync(cancellationToken);
            using MemoryStream output = new();
            byte[] buffer = ArrayPool<byte>.Shared.Rent(16 * 1024);
            try
            {
                int total = 0;
                while (true)
                {
                    int allowed = Math.Min(buffer.Length, MaxResponseBytes + 1 - total);
                    int read = await stream.ReadAsync(
                        buffer.AsMemory(0, allowed),
                        cancellationToken);
                    if (read == 0)
                    {
                        return output.ToArray();
                    }

                    total += read;
                    if (total > MaxResponseBytes)
                    {
                        throw new OneBotTransportException(
                            "RESPONSE_TOO_LARGE",
                            "OneBot action response exceeds the 8 MiB limit.");
                    }

                    output.Write(buffer, 0, read);
                }
            }
            finally
            {
                ArrayPool<byte>.Shared.Return(buffer);
            }
        }
        catch (OperationCanceledException) when (!callerCancellationToken.IsCancellationRequested)
        {
            throw new OneBotTransportException(
                "TIMEOUT",
                "OneBot action response exceeded its configured timeout.");
        }
    }
}

/// <summary>Explicit registry that maps profile names to transport implementations.</summary>
public static class OneBotTransportFactory
{
    public static IOneBotActionTransport Create(
        OneBotProfile profile,
        HttpClient? httpClient = null) => profile.TransportProfile switch
        {
            OneBotTransportProfile.HttpApi => new OneBotHttpTransport(profile, httpClient),
            OneBotTransportProfile.ForwardWebSocket or OneBotTransportProfile.ReverseWebSocket =>
                throw new OneBotConfigurationException(
                    "TRANSPORT_NOT_IMPLEMENTED",
                    "WebSocket transport is reserved for the next OneBot runtime slice."),
            _ => throw new OneBotConfigurationException(
                "INVALID_TRANSPORT_PROFILE",
                "OneBot transport profile is not registered.")
        };
}
