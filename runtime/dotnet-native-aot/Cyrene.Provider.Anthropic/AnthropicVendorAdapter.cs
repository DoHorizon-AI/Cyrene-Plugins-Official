// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 AnthropicVendorAdapter.cs                                       │
// │  Namespace: Cyrene.Provider.Anthropic                               │
// │  Role: Strict Anthropic Messages API wire translation (T83, T85).  │
// └─────────────────────────────────────────────────────────────────────┘

using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using Cyrene.Provider.Infrastructure.Capability;
using Cyrene.Provider.Infrastructure.Diagnostics;
using Cyrene.Provider.Infrastructure.Security;
using Cyrene.Provider.Infrastructure.Serialization;
using Cyrene.Provider.Infrastructure.Transport;

namespace Cyrene.Provider.Anthropic;

public sealed class AnthropicVendorAdapter : IModelCapability
{
    private readonly ProviderBindingConfiguration _config;
    private readonly HttpTransportClient _transport;

    public AnthropicVendorAdapter(
        ProviderBindingConfiguration config,
        HttpTransportClient? transport = null
    )
    {
        _config = config ?? throw new ArgumentNullException(nameof(config));
        _transport = transport ?? new HttpTransportClient();
    }

    public async Task<ChatCompletionResult> CompleteChatAsync(
        ChatCompletionParameters parameters,
        CancellationToken cancellationToken = default
    )
    {
        var wireMessages = parameters.Messages
            .Select(m => new AnthropicWireMessage(m.Role, m.Content))
            .ToList();

        var wireReq = new AnthropicMessagesRequest(
            Model: parameters.Model,
            Messages: wireMessages,
            MaxTokens: parameters.MaxTokens ?? 1024,
            Temperature: parameters.Temperature,
            Stream: false
        );

        var json = JsonSerializer.Serialize(wireReq, ProviderJsonSerializerContext.Default.AnthropicMessagesRequest);
        using var httpRequest = new HttpRequestMessage(HttpMethod.Post, new Uri(_config.EndpointUri, "/v1/messages"))
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };
        httpRequest.Headers.Add("x-api-key", _config.ApiKey);
        httpRequest.Headers.Add("anthropic-version", "2023-06-01");

        using var response = await _transport.SendRequestAsync(httpRequest, _config, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);

        var parsed = JsonSerializer.Deserialize(body, ProviderJsonSerializerContext.Default.AnthropicMessagesResponse);
        if (parsed == null || parsed.Content == null || parsed.Content.Count == 0)
        {
            throw new ProviderException(ProviderErrorCode.MalformedResponse, "Anthropic response contained no content blocks.");
        }

        var fullText = string.Join("\n", parsed.Content.Where(c => c.Type == "text" && c.Text != null).Select(c => c.Text));
        return new ChatCompletionResult(
            Id: parsed.Id,
            Model: parsed.Model,
            Content: fullText,
            FinishReason: parsed.StopReason,
            PromptTokens: parsed.Usage?.InputTokens,
            CompletionTokens: parsed.Usage?.OutputTokens
        );
    }

    public async IAsyncEnumerable<ChatCompletionChunk> StreamChatAsync(
        ChatCompletionParameters parameters,
        [EnumeratorCancellation] CancellationToken cancellationToken = default
    )
    {
        var wireMessages = parameters.Messages
            .Select(m => new AnthropicWireMessage(m.Role, m.Content))
            .ToList();

        var wireReq = new AnthropicMessagesRequest(
            Model: parameters.Model,
            Messages: wireMessages,
            MaxTokens: parameters.MaxTokens ?? 1024,
            Temperature: parameters.Temperature,
            Stream: true
        );

        var json = JsonSerializer.Serialize(wireReq, ProviderJsonSerializerContext.Default.AnthropicMessagesRequest);
        using var httpRequest = new HttpRequestMessage(HttpMethod.Post, new Uri(_config.EndpointUri, "/v1/messages"))
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };
        httpRequest.Headers.Add("x-api-key", _config.ApiKey);
        httpRequest.Headers.Add("anthropic-version", "2023-06-01");

        using var response = await _transport.SendRequestAsync(httpRequest, _config, cancellationToken).ConfigureAwait(false);
        var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);

        var messageId = string.Empty;
        int? promptTokens = null;

        await foreach (var sseEvent in SseStreamReader.ReadEventsAsync(stream, cancellationToken).ConfigureAwait(false))
        {
            AnthropicStreamEvent? parsed;
            try
            {
                parsed = JsonSerializer.Deserialize(sseEvent, ProviderJsonSerializerContext.Default.AnthropicStreamEvent);
            }
            catch (JsonException)
            {
                // Ignore malformed or vendor comment frames, matching the OpenAI stream path.
                continue;
            }

            switch (parsed?.Type)
            {
                case "message_start":
                    messageId = parsed.Message?.Id ?? messageId;
                    promptTokens = parsed.Message?.Usage?.InputTokens ?? promptTokens;
                    break;

                case "content_block_delta" when parsed.Delta?.Text is { Length: > 0 } text:
                    yield return new ChatCompletionChunk(messageId, text, null);
                    break;

                case "message_delta":
                    yield return new ChatCompletionChunk(
                        Id: messageId,
                        Delta: string.Empty,
                        FinishReason: parsed.Delta?.StopReason,
                        PromptTokens: promptTokens,
                        CompletionTokens: parsed.Usage?.OutputTokens
                    );
                    break;

                case "error":
                    throw MapStreamError(parsed.Error);

                default:
                    // message_stop, content_block_start/stop, and ping carry no delta.
                    break;
            }
        }
    }

    private static ProviderException MapStreamError(AnthropicStreamError? error)
    {
        var errorCode = error?.Type == "overloaded_error"
            ? ProviderErrorCode.ServiceUnavailable
            : ProviderErrorCode.WireProtocolViolation;
        return new ProviderException(
            errorCode,
            error?.Message ?? "Anthropic stream reported an error event."
        );
    }
}
