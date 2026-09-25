// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CapabilityProfiles.cs                                           │
// │  Namespace: Cyrene.Provider.Infrastructure.Capability               │
// │  Role: Separated capability profiles for Provider Rebuild (T81).    │
// │                                                                     │
// │  模块职责：严格解耦 Model、Embedding、TTS、STT 与 Rerank capability   │
// │           profiles，杜绝大一统厨房水槽式上帝接口                          │
// └─────────────────────────────────────────────────────────────────────┘

namespace Cyrene.Provider.Infrastructure.Capability;

public record ChatMessage(
    string Role,
    string Content,
    string? Name = null,
    string? ToolCallId = null,
    IReadOnlyList<ChatToolCall>? ToolCalls = null
);

public record ChatCompletionParameters(
    string Model,
    IReadOnlyList<ChatMessage> Messages,
    float? Temperature = null,
    int? MaxTokens = null,
    IReadOnlyList<string>? StopSequences = null,
    IReadOnlyList<ChatTool>? Tools = null,
    ChatToolChoice? ToolChoice = null,
    bool? ParallelToolCalls = null,
    bool? IncludeUsage = null
);

public record ChatCompletionResult(
    string Id,
    string Model,
    string Content,
    string? FinishReason,
    int? PromptTokens,
    int? CompletionTokens,
    IReadOnlyList<ChatToolCall>? ToolCalls = null,
    int? TotalTokens = null
);

public record ChatCompletionChunk(
    string Id,
    string Delta,
    string? FinishReason,
    int? PromptTokens = null,
    int? CompletionTokens = null,
    int? TotalTokens = null,
    IReadOnlyList<ChatToolCallDelta>? ToolCalls = null
);

// Structured chat (interface version 2) capability models. They mirror the
// optional model.provider.v1 fields carried by the chat_completion_v2 method.
// 中文：结构化聊天（接口版本 2）capability 模型。它们对应 `chat_completion_v2` 方法携带的可选 `model.provider.v1` 字段。

public record ChatFunctionDefinition(
    string Name,
    string? Description,
    string ParametersJson,
    bool? Strict = null
);

public record ChatTool(string Type, ChatFunctionDefinition Function);

// A named function uses Mode "function" and sets FunctionName.
// 中文：命名函数会使用 Mode `function` 并设置 FunctionName。
public record ChatToolChoice(string Mode, string? FunctionName = null);

public record ChatToolCallFunction(string Name, string Arguments);

public record ChatToolCall(string Id, string Type, ChatToolCallFunction Function);

public record ChatToolCallDelta(
    int Index,
    string? Id = null,
    string? Type = null,
    string? FunctionName = null,
    string? FunctionArguments = null
);

public interface IModelCapability
{
    string CapabilityName => "model.v1";
    Task<ChatCompletionResult> CompleteChatAsync(ChatCompletionParameters parameters, CancellationToken cancellationToken = default);
    IAsyncEnumerable<ChatCompletionChunk> StreamChatAsync(ChatCompletionParameters parameters, CancellationToken cancellationToken = default);
}

public record EmbeddingParameters(string Model, IReadOnlyList<string> Inputs, int? Dimensions = null);

public record EmbeddingVector(int Index, IReadOnlyList<float> Values);

public record EmbeddingResult(string Model, IReadOnlyList<EmbeddingVector> Embeddings, int? TotalTokens);

public interface IEmbeddingCapability
{
    string CapabilityName => "embedding.v1";
    Task<EmbeddingResult> GenerateEmbeddingsAsync(EmbeddingParameters parameters, CancellationToken cancellationToken = default);
}

// The speech and rerank profiles below are internal-only seeds: they have no
// canonical contract, no runtime dispatch, and must not be published or
// advertised as capabilities (W6-3). Resurrect them only with a real consumer
// and a canonical contract.
// 中文：下方的 speech 和 rerank profile 只是内部种子数据：它们没有规范契约、没有运行时分派，且不得作为 capability 发布或对外公布（W6-3）。只有在出现真实消费者且建立规范契约后，才可以恢复这些 profile。
public record TtsParameters(string Model, string Text, string Voice, string OutputFormat = "mp3", float? Speed = null);

public record TtsResult(byte[] AudioData, string MimeType, int DurationMs);

public interface ITtsCapability
{
    string CapabilityName => "tts.v1";
    Task<TtsResult> SynthesizeSpeechAsync(TtsParameters parameters, CancellationToken cancellationToken = default);
}

public record SttParameters(string Model, byte[] AudioData, string AudioFormat, string? Language = null);

public record SttResult(string Text, string? DetectedLanguage, float? Confidence);

public interface ISttCapability
{
    string CapabilityName => "stt.v1";
    Task<SttResult> TranscribeSpeechAsync(SttParameters parameters, CancellationToken cancellationToken = default);
}

public record RerankDocument(string Id, string Text);

public record RerankParameters(string Model, string Query, IReadOnlyList<RerankDocument> Documents, int? TopN = null);

public record RerankRankedDocument(string Id, int Index, float RelevanceScore);

public record RerankResult(string Model, IReadOnlyList<RerankRankedDocument> Results);

public interface IRerankCapability
{
    string CapabilityName => "rerank.v1";
    Task<RerankResult> RerankDocumentsAsync(RerankParameters parameters, CancellationToken cancellationToken = default);
}
