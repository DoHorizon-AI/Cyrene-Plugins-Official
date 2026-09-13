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

Console.WriteLine("model.provider.v1 generated C# payload TCK: PASS");
