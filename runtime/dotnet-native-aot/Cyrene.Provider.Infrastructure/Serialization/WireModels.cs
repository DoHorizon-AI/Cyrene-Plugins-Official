// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 WireModels.cs                                                   │
// │  Namespace: Cyrene.Provider.Infrastructure.Serialization            │
// │  Role: Wire JSON models and Native AOT JsonSerializerContext.       │
// └─────────────────────────────────────────────────────────────────────┘

using System.Text.Json;
using System.Text.Json.Serialization;

namespace Cyrene.Provider.Infrastructure.Serialization;

// ── OpenAI Wire Models ────────────────────────────────────────────────

public sealed record OpenAiWireMessage(
    [property: JsonPropertyName("role")] string Role,
    [property: JsonPropertyName("content")] string Content,
    [property: JsonPropertyName("name")] string? Name = null,
    [property: JsonPropertyName("tool_call_id")] string? ToolCallId = null,
    [property: JsonPropertyName("tool_calls")] IReadOnlyList<OpenAiWireToolCall>? ToolCalls = null
);

public sealed record OpenAiStreamOptions(
    [property: JsonPropertyName("include_usage")] bool? IncludeUsage = null
);

public sealed record OpenAiChatRequest(
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("messages")] IReadOnlyList<OpenAiWireMessage> Messages,
    [property: JsonPropertyName("temperature")] float? Temperature = null,
    [property: JsonPropertyName("max_tokens")] int? MaxTokens = null,
    [property: JsonPropertyName("stream")] bool? Stream = null,
    [property: JsonPropertyName("tools")] IReadOnlyList<OpenAiWireTool>? Tools = null,
    [property: JsonPropertyName("tool_choice")] OpenAiWireToolChoice? ToolChoice = null,
    [property: JsonPropertyName("parallel_tool_calls")] bool? ParallelToolCalls = null,
    [property: JsonPropertyName("stream_options")] OpenAiStreamOptions? StreamOptions = null
);

public sealed record OpenAiChoiceDelta(
    [property: JsonPropertyName("content")] string? Content = null,
    [property: JsonPropertyName("tool_calls")] IReadOnlyList<OpenAiToolCallDelta>? ToolCalls = null
);

public sealed record OpenAiChoice(
    [property: JsonPropertyName("index")] int Index,
    [property: JsonPropertyName("message")] OpenAiWireMessage? Message = null,
    [property: JsonPropertyName("delta")] OpenAiChoiceDelta? Delta = null,
    [property: JsonPropertyName("finish_reason")] string? FinishReason = null
);

public sealed record OpenAiWireFunctionName(
    [property: JsonPropertyName("name")] string Name
);

public sealed record OpenAiWireFunctionDefinition(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("description")] string? Description = null,
    [property: JsonPropertyName("parameters")] JsonElement? Parameters = null,
    [property: JsonPropertyName("strict")] bool? Strict = null
);

public sealed record OpenAiWireTool(
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("function")] OpenAiWireFunctionDefinition Function
);

public sealed record OpenAiWireToolChoice(
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("function")] OpenAiWireFunctionName? Function = null
);

public sealed record OpenAiWireToolCallFunction(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("arguments")] string Arguments
);

public sealed record OpenAiWireToolCall(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("function")] OpenAiWireToolCallFunction Function
);

public sealed record OpenAiToolCallDeltaFunction(
    [property: JsonPropertyName("name")] string? Name = null,
    [property: JsonPropertyName("arguments")] string? Arguments = null
);

public sealed record OpenAiToolCallDelta(
    [property: JsonPropertyName("index")] int Index,
    [property: JsonPropertyName("id")] string? Id = null,
    [property: JsonPropertyName("type")] string? Type = null,
    [property: JsonPropertyName("function")] OpenAiToolCallDeltaFunction? Function = null
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

// One Anthropic content block. Text, tool_use, and tool_result shapes share the
// record; unused members stay null and are omitted on the wire.
public sealed record AnthropicContentBlock(
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("text")] string? Text = null,
    [property: JsonPropertyName("id")] string? Id = null,
    [property: JsonPropertyName("name")] string? Name = null,
    [property: JsonPropertyName("input")] JsonElement? Input = null,
    [property: JsonPropertyName("tool_use_id")] string? ToolUseId = null,
    [property: JsonPropertyName("content")] string? Content = null
);

public sealed record AnthropicWireMessage(
    [property: JsonPropertyName("role")] string Role,
    [property: JsonPropertyName("content")] IReadOnlyList<AnthropicContentBlock> Content
);

public sealed record AnthropicWireTool(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("description")] string? Description = null,
    [property: JsonPropertyName("input_schema")] JsonElement? InputSchema = null
);

public sealed record AnthropicWireToolChoice(
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("name")] string? Name = null
);

public sealed record AnthropicMessagesRequest(
    [property: JsonPropertyName("model")] string Model,
    [property: JsonPropertyName("messages")] IReadOnlyList<AnthropicWireMessage> Messages,
    [property: JsonPropertyName("max_tokens")] int MaxTokens,
    [property: JsonPropertyName("system")] string? System = null,
    [property: JsonPropertyName("temperature")] float? Temperature = null,
    [property: JsonPropertyName("stream")] bool? Stream = null,
    [property: JsonPropertyName("tools")] IReadOnlyList<AnthropicWireTool>? Tools = null,
    [property: JsonPropertyName("tool_choice")] AnthropicWireToolChoice? ToolChoice = null
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
    [property: JsonPropertyName("partial_json")] string? PartialJson = null,
    [property: JsonPropertyName("stop_reason")] string? StopReason = null
);

public sealed record AnthropicStreamMessage(
    [property: JsonPropertyName("id")] string? Id = null,
    [property: JsonPropertyName("model")] string? Model = null,
    [property: JsonPropertyName("usage")] AnthropicUsage? Usage = null
);

public sealed record AnthropicStreamEvent(
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("index")] int? Index = null,
    [property: JsonPropertyName("message")] AnthropicStreamMessage? Message = null,
    [property: JsonPropertyName("content_block")] AnthropicContentBlock? ContentBlock = null,
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
