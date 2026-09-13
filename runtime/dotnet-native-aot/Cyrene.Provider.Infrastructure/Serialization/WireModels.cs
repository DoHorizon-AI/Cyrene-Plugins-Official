// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 WireModels.cs                                                   │
// │  Namespace: Cyrene.Provider.Infrastructure.Serialization            │
// │  Role: Wire JSON models and Native AOT JsonSerializerContext.       │
// └─────────────────────────────────────────────────────────────────────┘

using System.Text.Json.Serialization;

namespace Cyrene.Provider.Infrastructure.Serialization;

// ── OpenAI Wire Models ────────────────────────────────────────────────

public sealed record OpenAiWireMessage(
    [property: JsonPropertyName("role")] string Role,
    [property: JsonPropertyName("content")] string Content,
    [property: JsonPropertyName("name")] string? Name = null
);

public sealed record OpenAiChatRequest(
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("messages")] IReadOnlyList<OpenAiWireMessage> Messages,
    [property: JsonPropertyName("temperature")] float? Temperature = null,
    [property: JsonPropertyName("max_tokens")] int? MaxTokens = null,
    [property: JsonPropertyName("stream")] bool? Stream = null
);

public sealed record OpenAiChoiceDelta(
    [property: JsonPropertyName("content")] string? Content = null
);

public sealed record OpenAiChoice(
    [property: JsonPropertyName("index")] int Index,
    [property: JsonPropertyName("message")] OpenAiWireMessage? Message = null,
    [property: JsonPropertyName("delta")] OpenAiChoiceDelta? Delta = null,
    [property: JsonPropertyName("finish_reason")] string? FinishReason = null
);

public sealed record OpenAiUsage(
    [property: JsonPropertyName("prompt_tokens")] int PromptTokens,
    [property: JsonPropertyName("completion_tokens")] int CompletionTokens,
    [property: JsonPropertyName("total_tokens")] int TotalTokens
);

public sealed record OpenAiChatResponse(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("choices")] IReadOnlyList<OpenAiChoice>? Choices = null,
    [property: JsonPropertyName("usage")] OpenAiUsage? Usage = null
);

public sealed record OpenAiEmbeddingRequest(
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("input")] IReadOnlyList<string> Input,
    [property: JsonPropertyName("dimensions")] int? Dimensions = null
);

public sealed record OpenAiEmbeddingData(
    [property: JsonPropertyName("index")] int Index,
    [property: JsonPropertyName("embedding")] IReadOnlyList<float> Embedding
);

public sealed record OpenAiEmbeddingResponse(
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("data")] IReadOnlyList<OpenAiEmbeddingData> Data,
    [property: JsonPropertyName("usage")] OpenAiUsage? Usage = null
);

// ── Anthropic Wire Models ──────────────────────────────────────────────

public sealed record AnthropicContentBlock(
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("text")] string? Text = null
);

public sealed record AnthropicWireMessage(
    [property: JsonPropertyName("role")] string Role,
    [property: JsonPropertyName("content")] string Content
);

public sealed record AnthropicMessagesRequest(
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("messages")] IReadOnlyList<AnthropicWireMessage> Messages,
    [property: JsonPropertyName("max_tokens")] int MaxTokens,
    [property: JsonPropertyName("temperature")] float? Temperature = null,
    [property: JsonPropertyName("stream")] bool? Stream = null
);

public sealed record AnthropicUsage(
    [property: JsonPropertyName("input_tokens")] int InputTokens,
    [property: JsonPropertyName("output_tokens")] int OutputTokens
);

public sealed record AnthropicMessagesResponse(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("role")] string? Role = null,
    [property: JsonPropertyName("content")] IReadOnlyList<AnthropicContentBlock>? Content = null,
    [property: JsonPropertyName("stop_reason")] string? StopReason = null,
    [property: JsonPropertyName("usage")] AnthropicUsage? Usage = null
);

// Anthropic streaming event models (SSE data frames carry the event type inline).

public sealed record AnthropicStreamError(
    [property: JsonPropertyName("type")] string? Type = null,
    [property: JsonPropertyName("message")] string? Message = null
);

public sealed record AnthropicStreamDelta(
    [property: JsonPropertyName("type")] string? Type = null,
    [property: JsonPropertyName("text")] string? Text = null,
    [property: JsonPropertyName("stop_reason")] string? StopReason = null
);

public sealed record AnthropicStreamMessage(
    [property: JsonPropertyName("id")] string? Id = null,
    [property: JsonPropertyName("model")] string? Model = null,
    [property: JsonPropertyName("usage")] AnthropicUsage? Usage = null
);

public sealed record AnthropicStreamEvent(
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("message")] AnthropicStreamMessage? Message = null,
    [property: JsonPropertyName("delta")] AnthropicStreamDelta? Delta = null,
    [property: JsonPropertyName("usage")] AnthropicUsage? Usage = null,
    [property: JsonPropertyName("error")] AnthropicStreamError? Error = null
);

[JsonSerializable(typeof(OpenAiChatRequest))]
[JsonSerializable(typeof(OpenAiChatResponse))]
[JsonSerializable(typeof(OpenAiEmbeddingRequest))]
[JsonSerializable(typeof(OpenAiEmbeddingResponse))]
[JsonSerializable(typeof(AnthropicMessagesRequest))]
[JsonSerializable(typeof(AnthropicMessagesResponse))]
[JsonSerializable(typeof(AnthropicStreamEvent))]
[JsonSourceGenerationOptions(PropertyNamingPolicy = JsonKnownNamingPolicy.Unspecified, DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull)]
public partial class ProviderJsonSerializerContext : JsonSerializerContext
{
}
