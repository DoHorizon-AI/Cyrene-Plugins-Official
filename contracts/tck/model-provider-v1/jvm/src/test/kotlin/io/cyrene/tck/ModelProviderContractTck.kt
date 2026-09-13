package io.cyrene.tck

import com.google.protobuf.Any
import io.cyrene.proto.model.provider.v1.ChatMessage
import io.cyrene.proto.model.provider.v1.EmbeddingBatch
import io.cyrene.proto.model.provider.v1.EmbeddingVector
import io.cyrene.proto.model.provider.v1.EmbeddingsRequest
import io.cyrene.proto.model.provider.v1.EmbeddingsResponse
import io.cyrene.proto.model.provider.v1.chatCompletionRequest
import io.cyrene.proto.model.provider.v1.chatMessage
import io.cyrene.proto.model.provider.v1.embeddingBatch
import io.cyrene.proto.model.provider.v1.embeddingVector
import io.cyrene.proto.model.provider.v1.embeddingsRequest
import io.cyrene.proto.model.provider.v1.embeddingsResponse
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class ModelProviderContractTck {
    @Test
    fun generatedJavaAndKotlinPayloadsRoundTripWithoutAPlatformEnvelope() {
        val embeddingRequest: EmbeddingsRequest = embeddingsRequest {
            inputs += listOf("alpha", "beta")
            model = "deterministic-model"
        }
        val packedRequest = Any.pack(embeddingRequest)
        assertTrue(packedRequest.`is`(EmbeddingsRequest::class.java))
        assertEquals(
            listOf("alpha", "beta"),
            packedRequest.unpack(EmbeddingsRequest::class.java).inputsList,
        )

        val batch: EmbeddingBatch = embeddingBatch {
            vectors += listOf<EmbeddingVector>(
                embeddingVector { values += listOf(1.0f, 2.0f) },
                embeddingVector { values += listOf(3.0f, 4.0f) },
            )
            dimensions = 2
            model = "deterministic-model"
        }
        val response: EmbeddingsResponse = embeddingsResponse { embeddings = batch }
        val packedResponse = Any.pack(response)
        assertTrue(packedResponse.`is`(EmbeddingsResponse::class.java))
        assertEquals(
            embeddingRequest.inputsCount,
            packedResponse.unpack(EmbeddingsResponse::class.java).embeddings.vectorsCount,
        )

        val chat = chatCompletionRequest {
            model = "deterministic-model"
            includeUsage = true
            messages += chatMessage {
                role = ChatMessage.Role.ROLE_USER
                content = "hello"
            }
        }
        val packedChat = Any.pack(chat)
        assertTrue(packedChat.`is`(chat.javaClass))
        assertTrue(packedChat.unpack(chat.javaClass).hasIncludeUsage())
    }
}
