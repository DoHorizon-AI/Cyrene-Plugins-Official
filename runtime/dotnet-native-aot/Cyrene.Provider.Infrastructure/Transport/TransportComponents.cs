// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 TransportComponents.cs                                          │
// │  Namespace: Cyrene.Provider.Infrastructure.Transport                │
// │  Role: AOT-compatible HTTP transport and SSE reader (T82).          │
// └─────────────────────────────────────────────────────────────────────┘
// 中文：文件：TransportComponents.cs
// 中文：命名空间：Cyrene.Provider.Infrastructure.Transport
// 中文：职责：兼容 AOT 的 HTTP 传输与 SSE 读取器（T82）。

using System.Net.Http.Headers;
using System.Runtime.CompilerServices;
using Cyrene.Provider.Infrastructure.Diagnostics;
using Cyrene.Provider.Infrastructure.Security;

namespace Cyrene.Provider.Infrastructure.Transport;

public static class SseStreamReader
{
    public static async IAsyncEnumerable<string> ReadEventsAsync(
        Stream stream,
        [EnumeratorCancellation] CancellationToken cancellationToken = default
    )
    {
        using var reader = new StreamReader(stream);

        while (!cancellationToken.IsCancellationRequested)
        {
            var line = await reader.ReadLineAsync(cancellationToken).ConfigureAwait(false);
            if (line == null)
            {
                break;
            }

            var trimmed = line.Trim();
            if (trimmed.Length == 0 || trimmed.StartsWith(':'))
            {
                // Skip empty keep-alive or comments
// 中文：跳过空的 keep-alive 帧或注释帧。
                continue;
            }

            if (trimmed.StartsWith("data:", StringComparison.OrdinalIgnoreCase))
            {
                var payload = trimmed.Substring(5).Trim();
                if (payload == "[DONE]")
                {
                    yield break;
                }

                yield return payload;
            }
        }
    }
}

public sealed class HttpTransportClient
{
    private readonly HttpClient _httpClient;

    public HttpTransportClient(HttpClient? httpClient = null)
    {
        _httpClient = httpClient ?? new HttpClient();
    }

    public async Task<HttpResponseMessage> SendRequestAsync(
        HttpRequestMessage request,
        ProviderBindingConfiguration config,
        CancellationToken cancellationToken = default
    )
    {
        using var timeoutCts = new CancellationTokenSource(config.Timeout);
        using var linkedCts = CancellationTokenSource.CreateLinkedTokenSource(timeoutCts.Token, cancellationToken);

        try
        {
            var response = await _httpClient.SendAsync(
                request,
                HttpCompletionOption.ResponseHeadersRead,
                linkedCts.Token
            ).ConfigureAwait(false);

            if (!response.IsSuccessStatusCode)
            {
                var rawBody = await response.Content.ReadAsStringAsync(linkedCts.Token).ConfigureAwait(false);
                var retryAfter = response.Headers.RetryAfter?.Delta?.Seconds;
                var safeBody = RedactionSanitizer.Redact(rawBody);
                throw ErrorMapper.MapHttpError((int)response.StatusCode, safeBody, (int?)retryAfter);
            }

            return response;
        }
        catch (OperationCanceledException) when (timeoutCts.IsCancellationRequested && !cancellationToken.IsCancellationRequested)
        {
            throw new ProviderException(
                ProviderErrorCode.DeadlineExceeded,
                $"Request timed out after {config.Timeout.TotalSeconds} seconds."
            );
        }
    }
}
