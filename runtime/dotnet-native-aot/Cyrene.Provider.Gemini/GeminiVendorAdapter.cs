// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 GeminiVendorAdapter.cs                                          │
// │  Namespace: Cyrene.Provider.Gemini                                  │
// │  Role: Strict Gemini native API wire translation (model.provider.v1).│
// │                                                                     │
// │  模块职责：仅负责厂商 Wire Translation（Gemini REST/SSE <-> Cyrene），│
// │            零产品策略、零全局路由、通过绑定配置显式注入凭证。          │
// └─────────────────────────────────────────────────────────────────────┘

using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using Cyrene.Provider.Infrastructure.Capability;
using Cyrene.Provider.Infrastructure.Diagnostics;
using Cyrene.Provider.Infrastructure.Security;
using Cyrene.Provider.Infrastructure.Serialization;
using Cyrene.Provider.Infrastructure.Transport;

namespace Cyrene.Provider.Gemini;

public sealed class GeminiVendorAdapter : IModelCapability, IEmbeddingCapability
{
    private const string ApiKeyHeader = "x-goog-api-key";

    private readonly ProviderBindingConfiguration _config;
    private readonly HttpTransportClient _transport;

    public GeminiVendorAdapter(
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
        var wireReq = BuildRequest(parameters);
        var json = JsonSerializer.Serialize(
            wireReq,
            ProviderJsonSerializerContext.Default.GeminiGenerateContentRequest
        );
        using var httpRequest = NewRequest($"{ModelPath(parameters.Model)}:generateContent", json);

        using var response = await _transport.SendRequestAsync(httpRequest, _config, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);

        var parsed = JsonSerializer.Deserialize(
            body,
            ProviderJsonSerializerContext.Default.GeminiGenerateContentResponse
        );
        if (parsed?.Candidates is not { Count: > 0 })
        {
            throw new ProviderException(
                ProviderErrorCode.MalformedResponse,
                "Gemini response contained no candidates."
            );
        }

        var candidate = parsed.Candidates[0];
        var toolCalls = ExtractToolCalls(candidate);
        return new ChatCompletionResult(
            // Gemini native responses carry no message id; the field stays empty.
            Id: string.Empty,
            Model: parameters.Model,
            Content: ExtractText(candidate),
            FinishReason: candidate.FinishReason,
            PromptTokens: parsed.UsageMetadata?.PromptTokenCount,
            CompletionTokens: parsed.UsageMetadata?.CandidatesTokenCount,
            ToolCalls: toolCalls.Count > 0 ? toolCalls : null,
            TotalTokens: parsed.UsageMetadata?.TotalTokenCount
        );
    }

    public async IAsyncEnumerable<ChatCompletionChunk> StreamChatAsync(
        ChatCompletionParameters parameters,
        [EnumeratorCancellation] CancellationToken cancellationToken = default
    )
    {
        var wireReq = BuildRequest(parameters);
        var json = JsonSerializer.Serialize(
            wireReq,
            ProviderJsonSerializerContext.Default.GeminiGenerateContentRequest
        );
        using var httpRequest = NewRequest(
            $"{ModelPath(parameters.Model)}:streamGenerateContent?alt=sse",
            json
        );

        using var response = await _transport.SendRequestAsync(httpRequest, _config, cancellationToken).ConfigureAwait(false);
        var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);

        await foreach (var sseEvent in SseStreamReader.ReadEventsAsync(stream, cancellationToken).ConfigureAwait(false))
        {
            GeminiGenerateContentResponse? parsed;
            try
            {
                parsed = JsonSerializer.Deserialize(
                    sseEvent,
                    ProviderJsonSerializerContext.Default.GeminiGenerateContentResponse
                );
            }
            catch (JsonException)
            {
                // Ignore malformed keep-alive or vendor comment frames.
                continue;
            }

            if (parsed?.Candidates is not { Count: > 0 })
            {
                // Usage-only frames close the stream without candidate content.
                if (parsed?.UsageMetadata is { } usage)
                {
                    yield return new ChatCompletionChunk(
                        Id: string.Empty,
                        Delta: string.Empty,
                        FinishReason: null,
                        PromptTokens: usage.PromptTokenCount,
                        CompletionTokens: usage.CandidatesTokenCount,
                        TotalTokens: usage.TotalTokenCount
                    );
                }
                continue;
            }

            var candidate = parsed.Candidates[0];
            var toolCallDeltas = ExtractToolCallDeltas(candidate);
            yield return new ChatCompletionChunk(
                Id: string.Empty,
                Delta: ExtractText(candidate),
                FinishReason: candidate.FinishReason,
                PromptTokens: parsed.UsageMetadata?.PromptTokenCount,
                CompletionTokens: parsed.UsageMetadata?.CandidatesTokenCount,
                TotalTokens: parsed.UsageMetadata?.TotalTokenCount,
                ToolCalls: toolCallDeltas.Count > 0 ? toolCallDeltas : null
            );
        }
    }

    public async Task<EmbeddingResult> GenerateEmbeddingsAsync(
        EmbeddingParameters parameters,
        CancellationToken cancellationToken = default
    )
    {
        var modelPath = $"models/{NormalizedModel(parameters.Model)}";
        var requests = parameters.Inputs
            .Select(input => new GeminiEmbeddingRequestItem(
                Model: modelPath,
                Content: new GeminiContent(Parts: new[] { new GeminiPart(Text: input) }),
                OutputDimensionality: parameters.Dimensions
            ))
            .ToList();
        var json = JsonSerializer.Serialize(
            new GeminiBatchEmbedRequest(requests),
            ProviderJsonSerializerContext.Default.GeminiBatchEmbedRequest
        );
        using var httpRequest = NewRequest(
            $"{ModelPath(parameters.Model)}:batchEmbedContents",
            json
        );

        using var response = await _transport.SendRequestAsync(httpRequest, _config, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        var parsed = JsonSerializer.Deserialize(
            body,
            ProviderJsonSerializerContext.Default.GeminiBatchEmbedResponse
        );
        if (parsed?.Embeddings is not { Count: > 0 })
        {
            throw new ProviderException(
                ProviderErrorCode.MalformedResponse,
                "Gemini embedding response contained no embeddings."
            );
        }

        var vectors = parsed.Embeddings
            .Select((embedding, index) => new EmbeddingVector(index, embedding.Values))
            .ToList();
        return new EmbeddingResult(
            Model: parameters.Model,
            Embeddings: vectors,
            TotalTokens: null
        );
    }

    // ── request construction ───────────────────────────────────────────

    private static GeminiGenerateContentRequest BuildRequest(ChatCompletionParameters parameters)
    {
        var systemParts = parameters.Messages
            .Where(message => message.Role == "system")
            .Select(message => message.Content)
            .Where(content => !string.IsNullOrEmpty(content))
            .ToList();

        var toolTargetsById = new Dictionary<string, (string Name, int Order)>();
        var callOrder = 0;
        foreach (var message in parameters.Messages)
        {
            if (message.ToolCalls is not { } calls)
            {
                continue;
            }
            foreach (var call in calls)
            {
                toolTargetsById[call.Id] = (call.Function.Name, callOrder);
                callOrder++;
            }
        }

        var contents = new List<GeminiContent>();
        var remaining = parameters.Messages.Where(item => item.Role != "system").ToList();
        for (var index = 0; index < remaining.Count; index++)
        {
            var message = remaining[index];

            if (message.Role == "tool")
            {
                // Gemini correlates tool responses by function name and, when a
                // name repeats within one turn, by position. Collect the run of
                // tool responses and emit it in the order of the assistant tool
                // calls so parallel calls stay unambiguous even when the runtime
                // reports its tool messages out of order.
                var run = new List<(int Order, GeminiContent Content)>();
                while (index < remaining.Count && remaining[index].Role == "tool")
                {
                    run.Add(ResolveToolResponse(remaining[index], toolTargetsById));
                    index++;
                }
                index--;
                run.Sort((left, right) => left.Order.CompareTo(right.Order));
                contents.AddRange(run.Select(item => item.Content));
                continue;
            }

            var parts = new List<GeminiPart>();
            if (!string.IsNullOrEmpty(message.Content))
            {
                parts.Add(new GeminiPart(Text: message.Content));
            }
            if (message.ToolCalls is { } toolCalls)
            {
                foreach (var call in toolCalls)
                {
                    parts.Add(new GeminiPart(FunctionCall: new GeminiFunctionCall(
                        Name: call.Function.Name,
                        Args: ParseObject(call.Function.Arguments)
                    )));
                }
            }
            if (parts.Count == 0)
            {
                parts.Add(new GeminiPart(Text: string.Empty));
            }
            contents.Add(new GeminiContent(
                Role: message.Role == "assistant" ? "model" : "user",
                Parts: parts
            ));
        }

        var hasGenerationConfig = parameters.Temperature is not null ||
            parameters.MaxTokens is not null ||
            parameters.StopSequences is { Count: > 0 };
        return new GeminiGenerateContentRequest(
            Contents: contents,
            SystemInstruction: systemParts.Count > 0
                ? new GeminiContent(Parts: new[] { new GeminiPart(Text: string.Join("\n", systemParts)) })
                : null,
            GenerationConfig: hasGenerationConfig
                ? new GeminiGenerationConfig(
                    Temperature: parameters.Temperature,
                    MaxOutputTokens: parameters.MaxTokens,
                    StopSequences: parameters.StopSequences is { Count: > 0 } ? parameters.StopSequences : null
                )
                : null,
            Tools: ToTools(parameters.Tools),
            ToolConfig: ToToolConfig(parameters.ToolChoice)
        );
    }

    private static GeminiTool[]? ToTools(IReadOnlyList<ChatTool>? tools)
    {
        if (tools is not { Count: > 0 })
        {
            return null;
        }
        var declarations = tools
            .Select(tool => new GeminiFunctionDeclaration(
                Name: tool.Function.Name,
                Description: tool.Function.Description,
                Parameters: ParseJsonOrNull(tool.Function.ParametersJson)
            ))
            .ToList();
        return new[] { new GeminiTool(declarations) };
    }

    private static GeminiToolConfig? ToToolConfig(ChatToolChoice? choice)
    {
        if (choice is null)
        {
            return null;
        }
        return choice.Mode switch
        {
            "auto" => new GeminiToolConfig(new GeminiFunctionCallingConfig("AUTO")),
            "required" => new GeminiToolConfig(new GeminiFunctionCallingConfig("ANY")),
            "none" => new GeminiToolConfig(new GeminiFunctionCallingConfig("NONE")),
            "function" when !string.IsNullOrEmpty(choice.FunctionName) => new GeminiToolConfig(
                new GeminiFunctionCallingConfig("ANY", new[] { choice.FunctionName })
            ),
            _ => throw new ProviderException(
                ProviderErrorCode.WireProtocolViolation,
                $"Unsupported tool_choice mode {choice.Mode} for the Gemini native API."
            ),
        };
    }

    // ── response extraction ────────────────────────────────────────────

    private static string ExtractText(GeminiCandidate candidate)
    {
        if (candidate.Content?.Parts is not { Count: > 0 } parts)
        {
            return string.Empty;
        }
        var builder = new StringBuilder();
        foreach (var part in parts)
        {
            if (!string.IsNullOrEmpty(part.Text))
            {
                builder.Append(part.Text);
            }
        }
        return builder.ToString();
    }

    private static List<ChatToolCall> ExtractToolCalls(GeminiCandidate candidate)
    {
        var calls = new List<ChatToolCall>();
        if (candidate.Content?.Parts is not { Count: > 0 } parts)
        {
            return calls;
        }
        foreach (var part in parts)
        {
            if (part.FunctionCall is not { } call)
            {
                continue;
            }
            calls.Add(new ChatToolCall(
                // Gemini carries no call id; a deterministic local id keeps
                // tool_call history correlatable inside the capability model.
                Id: $"gemini-call-{calls.Count}",
                Type: "function",
                Function: new ChatToolCallFunction(
                    Name: call.Name,
                    Arguments: call.Args?.GetRawText() ?? "{}"
                )
            ));
        }
        return calls;
    }

    private static List<ChatToolCallDelta> ExtractToolCallDeltas(GeminiCandidate candidate)
    {
        var deltas = new List<ChatToolCallDelta>();
        if (candidate.Content?.Parts is not { Count: > 0 } parts)
        {
            return deltas;
        }
        foreach (var part in parts)
        {
            if (part.FunctionCall is not { } call)
            {
                continue;
            }
            deltas.Add(new ChatToolCallDelta(
                Index: deltas.Count,
                Id: $"gemini-call-{deltas.Count}",
                Type: "function",
                FunctionName: call.Name,
                FunctionArguments: call.Args?.GetRawText() ?? "{}"
            ));
        }
        return deltas;
    }

    /// <summary>
    /// Resolves one tool message into its Gemini functionResponse. Gemini
    /// carries no call ids, so the response is correlated through the function
    /// name of the assistant tool call that the tool_call_id belongs to; the
    /// call order keeps parallel calls with the same name positional.
    /// </summary>
    private static (int Order, GeminiContent Content) ResolveToolResponse(
        ChatMessage message,
        Dictionary<string, (string Name, int Order)> toolTargetsById
    )
    {
        if (message.ToolCallId is null ||
            !toolTargetsById.TryGetValue(message.ToolCallId, out var target))
        {
            throw new ProviderException(
                ProviderErrorCode.WireProtocolViolation,
                "Gemini requires a resolvable tool name for tool responses; "
                    + "the tool_call_id has no matching assistant tool call."
            );
        }
        return (
            target.Order,
            new GeminiContent(
                Role: "user",
                Parts: new[]
                {
                    new GeminiPart(FunctionResponse: new GeminiFunctionResponse(
                        Name: target.Name,
                        Response: ParseObjectOrWrap(message.Content)
                    )),
                }
            )
        );
    }

    // ── helpers ────────────────────────────────────────────────────────

    private HttpRequestMessage NewRequest(string relativePath, string json)
    {
        var request = new HttpRequestMessage(
            HttpMethod.Post,
            new Uri(_config.EndpointUri, relativePath)
        )
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json"),
        };
        request.Headers.Add(ApiKeyHeader, _config.ApiKey);
        return request;
    }

    private static string NormalizedModel(string model) =>
        model.StartsWith("models/", StringComparison.Ordinal) ? model["models/".Length..] : model;

    private static string ModelPath(string model) => $"v1beta/models/{NormalizedModel(model)}";

    private static JsonElement ParseObject(string json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return JsonDocument.Parse("{}").RootElement.Clone();
        }
        JsonElement element;
        try
        {
            using var document = JsonDocument.Parse(json);
            element = document.RootElement.Clone();
        }
        catch (JsonException error)
        {
            throw new ProviderException(
                ProviderErrorCode.WireProtocolViolation,
                $"Tool call arguments are not valid JSON: {error.Message}"
            );
        }
        if (element.ValueKind != JsonValueKind.Object)
        {
            throw new ProviderException(
                ProviderErrorCode.WireProtocolViolation,
                "Gemini function call arguments must be a JSON object."
            );
        }
        return element;
    }

    private static JsonElement ParseObjectOrWrap(string content)
    {
        if (!string.IsNullOrWhiteSpace(content))
        {
            try
            {
                using var document = JsonDocument.Parse(content);
                if (document.RootElement.ValueKind == JsonValueKind.Object)
                {
                    return document.RootElement.Clone();
                }
            }
            catch (JsonException)
            {
                // Fall through to the deterministic wrapper below.
            }
        }
        var encoded = JsonEncodedText.Encode(content ?? string.Empty);
        using var wrapper = JsonDocument.Parse($"{{\"output\":\"{encoded}\"}}");
        return wrapper.RootElement.Clone();
    }

    private static JsonElement? ParseJsonOrNull(string? json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return null;
        }
        try
        {
            using var document = JsonDocument.Parse(json);
            return document.RootElement.Clone();
        }
        catch (JsonException error)
        {
            throw new ProviderException(
                ProviderErrorCode.WireProtocolViolation,
                $"Tool parameters are not valid JSON Schema: {error.Message}"
            );
        }
    }
}
