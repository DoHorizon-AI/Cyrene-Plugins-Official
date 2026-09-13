// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CapabilityProfiles.cs                                           │
// │  Namespace: Cyrene.Provider.Infrastructure.Capability               │
// │  Role: Separated capability profiles for Provider Rebuild (T81).    │
// │                                                                     │
// │  模块职责：严格解耦 Model、Embedding、TTS、STT 与 Rerank capability   │
// │           profiles，杜绝大一统厨房水槽式上帝接口                          │
// └─────────────────────────────────────────────────────────────────────┘

namespace Cyrene.Provider.Infrastructure.Capability;

public record ChatMessage(string Role, string Content, string? Name = null);

public record ChatCompletionParameters(
    string Model,
    IReadOnlyList<ChatMessage> Messages,
    float? Temperature = null,
    int? MaxTokens = null,
    IReadOnlyList<string>? StopSequences = null
);

public record ChatCompletionResult(
    string Id,
    string Model,
    string Content,
    string? FinishReason,
    int? PromptTokens,
    int? CompletionTokens
);

public record ChatCompletionChunk(
    string Id,
    string Delta,
    string? FinishReason,
    int? PromptTokens = null,
    int? CompletionTokens = null,
    int? TotalTokens = null
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
