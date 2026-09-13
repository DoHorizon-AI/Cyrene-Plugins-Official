using Cyrene.Model.Provider.V1;
using Google.Protobuf.WellKnownTypes;

var request = new EmbeddingsRequest
{
    Model = "deterministic-model",
    Inputs = { "alpha", "beta" },
};
var packedRequest = Any.Pack(request);
if (!packedRequest.Is(EmbeddingsRequest.Descriptor))
{
    throw new InvalidOperationException("Embedding request Any type is not canonical.");
}
var unpackedRequest = packedRequest.Unpack<EmbeddingsRequest>();
if (unpackedRequest.Inputs.Count != 2 || unpackedRequest.Model != "deterministic-model")
{
    throw new InvalidOperationException("Embedding request did not round-trip.");
}

var batch = new EmbeddingBatch
{
    Dimensions = 2,
    Model = "deterministic-model",
};
batch.Vectors.Add(new EmbeddingVector { Values = { 1.0f, 2.0f } });
batch.Vectors.Add(new EmbeddingVector { Values = { 3.0f, 4.0f } });
var response = new EmbeddingsResponse { Embeddings = batch };
var packedResponse = Any.Pack(response);
if (!packedResponse.Is(EmbeddingsResponse.Descriptor)
    || packedResponse.Unpack<EmbeddingsResponse>().Embeddings.Vectors.Count != request.Inputs.Count)
{
    throw new InvalidOperationException("Embedding response did not preserve batch cardinality.");
}

var chat = new ChatCompletionRequest
{
    Model = "deterministic-model",
    Stream = true,
    IncludeUsage = true,
    Messages =
    {
        new ChatMessage { Role = ChatMessage.Types.Role.User, Content = "hello" },
    },
};
var packedChat = Any.Pack(chat);
if (!packedChat.Is(ChatCompletionRequest.Descriptor)
    || !packedChat.Unpack<ChatCompletionRequest>().HasIncludeUsage)
{
    throw new InvalidOperationException("Structured chat optional presence did not round-trip.");
}


// Structured chat v2: index, identity, type, function facts, usage, and
// finish_reason must survive the projection byte-for-byte.
var toolChat = new ChatCompletionRequest
{
    Model = "deterministic-model",
    Stream = true,
    ParallelToolCalls = false,
    IncludeUsage = true,
    Messages =
    {
        new ChatMessage { Role = ChatMessage.Types.Role.User, Content = "weather?" },
    },
    Tools =
    {
        new ChatTool
        {
            Type = "function",
            Function = new ChatFunction
            {
                Name = "weather",
                Description = "look up weather",
                ParametersJson = "{\"type\":\"object\"}",
                Strict = true,
            },
        },
    },
    ToolChoice = new ChatToolChoice { Mode = "function", FunctionName = "weather" },
};
var unpackedToolChat = Any.Pack(toolChat).Unpack<ChatCompletionRequest>();
var toolFunction = unpackedToolChat.Tools[0].Function;
if (toolFunction is null
    || toolFunction.Name != "weather"
    || toolFunction.ParametersJson != "{\"type\":\"object\"}"
    || toolFunction.Strict != true
    || unpackedToolChat.ToolChoice.FunctionName != "weather"
    || unpackedToolChat.HasParallelToolCalls != true
    || unpackedToolChat.ParallelToolCalls != false
    || unpackedToolChat.IncludeUsage != true)
{
    throw new InvalidOperationException("Structured chat v2 request lost tool fields.");
}

var toolResponse = new ChatCompletionResponse
{
    Chunks =
    {
        new ChatCompletionChunk
        {
            Delta = string.Empty,
            FinishReason = "tool_calls",
            PromptTokens = 8,
            CompletionTokens = 4,
            TotalTokens = 12,
            Role = "assistant",
            ToolCalls =
            {
                new ChatToolCallDelta
                {
                    Index = 0,
                    Id = "call-1",
                    Type = "function",
                    FunctionName = "weather",
                    FunctionArguments = "{\"city\":",
                },
            },
        },
    },
};
var toolChunk = Any.Pack(toolResponse).Unpack<ChatCompletionResponse>().Chunks[0];
var toolDelta = toolChunk.ToolCalls[0];
if (toolDelta.Index != 0
    || toolDelta.Id != "call-1"
    || toolDelta.Type != "function"
    || toolDelta.FunctionName != "weather"
    || toolDelta.FunctionArguments != "{\"city\":"
    || toolChunk.FinishReason != "tool_calls"
    || toolChunk.PromptTokens != 8
    || toolChunk.CompletionTokens != 4
    || toolChunk.TotalTokens != 12)
{
    throw new InvalidOperationException("Structured chat v2 response lost tool or usage facts.");
}

Console.WriteLine("model.provider.v1 generated C# payload TCK: PASS");
