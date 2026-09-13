// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 OpenAiVendorAdapter.cs                                          │
// │  Namespace: Cyrene.Provider.OpenAi                                  │
// │  Role: Strict OpenAI wire translation adapter (T83, T85).           │
// │                                                                     │
// │  模块职责：仅负责厂商 Wire Translation（OpenAI REST/SSE <-> Cyrene）， │
// │           零产品策略、零全局路由、通过绑定配置显式注入凭证 (T84, T87)     │
// └─────────────────────────────────────────────────────────────────────┘

using System.Net.Http.Headers;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using Cyrene.Provider.Infrastructure.Capability;
using Cyrene.Provider.Infrastructure.Diagnostics;
using Cyrene.Provider.Infrastructure.Security;
using Cyrene.Provider.Infrastructure.Serialization;
using Cyrene.Provider.Infrastructure.Transport;

namespace Cyrene.Provider.OpenAi;

public sealed class OpenAiVendorAdapter : IModelCapability, IEmbeddingCapability
{
    private readonly ProviderBindingConfiguration _config;
    private readonly HttpTransportClient _transport;

    public OpenAiVendorAdapter(
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
        var wireMessages = parameters.Messages.Select(m => new OpenAiWireMessage(m.Role, m.Content, m.Name)).ToList();
        var wireReq = new OpenAiChatRequest(
            Model: parameters.Model,
            Messages: wireMessages,
            Temperature: parameters.Temperature,
            MaxTokens: parameters.MaxTokens,
            Stream: false
        );

        var json = JsonSerializer.Serialize(wireReq, ProviderJsonSerializerContext.Default.OpenAiChatRequest);
        using var httpRequest = new HttpRequestMessage(HttpMethod.Post, new Uri(_config.EndpointUri, "/v1/chat/completions"))
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };
        httpRequest.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _config.ApiKey);
        if (!string.IsNullOrEmpty(_config.OrganizationId))
        {
            httpRequest.Headers.Add("OpenAI-Organization", _config.OrganizationId);
        }

        using var response = await _transport.SendRequestAsync(httpRequest, _config, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);

        var parsed = JsonSerializer.Deserialize(body, ProviderJsonSerializerContext.Default.OpenAiChatResponse);
        if (parsed == null || parsed.Choices == null || parsed.Choices.Count == 0)
        {
            throw new ProviderException(ProviderErrorCode.MalformedResponse, "OpenAI response contained no choices.");
        }

        var firstChoice = parsed.Choices[0];
        return new ChatCompletionResult(
            Id: parsed.Id,
            Model: parsed.Model,
            Content: firstChoice.Message?.Content ?? string.Empty,
            FinishReason: firstChoice.FinishReason,
            PromptTokens: parsed.Usage?.PromptTokens,
            CompletionTokens: parsed.Usage?.CompletionTokens
        );
    }

    public async IAsyncEnumerable<ChatCompletionChunk> StreamChatAsync(
        ChatCompletionParameters parameters,
        [EnumeratorCancellation] CancellationToken cancellationToken = default
    )
    {
        var wireMessages = parameters.Messages.Select(m => new OpenAiWireMessage(m.Role, m.Content, m.Name)).ToList();
        var wireReq = new OpenAiChatRequest(
            Model: parameters.Model,
            Messages: wireMessages,
            Temperature: parameters.Temperature,
            MaxTokens: parameters.MaxTokens,
            Stream: true
        );

        var json = JsonSerializer.Serialize(wireReq, ProviderJsonSerializerContext.Default.OpenAiChatRequest);
        using var httpRequest = new HttpRequestMessage(HttpMethod.Post, new Uri(_config.EndpointUri, "/v1/chat/completions"))
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };
        httpRequest.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _config.ApiKey);

        using var response = await _transport.SendRequestAsync(httpRequest, _config, cancellationToken).ConfigureAwait(false);
        var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);

        await foreach (var sseEvent in SseStreamReader.ReadEventsAsync(stream, cancellationToken).ConfigureAwait(false))
        {
            OpenAiChatResponse? chunk = null;
            try
            {
                chunk = JsonSerializer.Deserialize(sseEvent, ProviderJsonSerializerContext.Default.OpenAiChatResponse);
            }
            catch
            {
                // Ignore malformed chunk lines or comments
                continue;
            }

            if (chunk?.Choices != null && chunk.Choices.Count > 0)
            {
                var choice = chunk.Choices[0];
                yield return new ChatCompletionChunk(
                    Id: chunk.Id,
                    Delta: choice.Delta?.Content ?? string.Empty,
                    FinishReason: choice.FinishReason
                );
            }
        }
    }

    public async Task<EmbeddingResult> GenerateEmbeddingsAsync(
        EmbeddingParameters parameters,
        CancellationToken cancellationToken = default
    )
    {
        var wireReq = new OpenAiEmbeddingRequest(
            Model: parameters.Model,
            Input: parameters.Inputs,
            Dimensions: parameters.Dimensions
        );

        var json = JsonSerializer.Serialize(wireReq, ProviderJsonSerializerContext.Default.OpenAiEmbeddingRequest);
        using var httpRequest = new HttpRequestMessage(HttpMethod.Post, new Uri(_config.EndpointUri, "/v1/embeddings"))
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json")
        };
        httpRequest.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _config.ApiKey);

        using var response = await _transport.SendRequestAsync(httpRequest, _config, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);

        var parsed = JsonSerializer.Deserialize(body, ProviderJsonSerializerContext.Default.OpenAiEmbeddingResponse);
        if (parsed == null || parsed.Data == null)
        {
            throw new ProviderException(ProviderErrorCode.MalformedResponse, "OpenAI embedding response invalid.");
        }

        var vectors = parsed.Data.Select(d => new EmbeddingVector(d.Index, d.Embedding)).ToList();
        return new EmbeddingResult(
            Model: parsed.Model,
            Embeddings: vectors,
            TotalTokens: parsed.Usage?.TotalTokens
        );
    }
}
