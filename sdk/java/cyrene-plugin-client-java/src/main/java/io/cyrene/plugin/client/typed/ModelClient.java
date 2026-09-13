// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 ModelClient.java                                                │
// │  Package: io.cyrene.plugin.client.typed                             │
// │  Role: Typed client for Model Provider (T43, R05).                  │
// │                                                                     │
// │  模块职责：类型化 Model 客户端，调用方无需手工拼 method 或 type URL    │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.typed;

import com.google.protobuf.ByteString;
import com.google.protobuf.InvalidProtocolBufferException;
import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.runtime.v1.DirectInvocationRequest;
import io.cyrene.plugin.runtime.v1.DirectInvocationResponse;
import io.cyrene.plugin.runtime.v1.DirectStreamMode;
import io.cyrene.proto.model.provider.v1.ChatCompletionRequest;
import io.cyrene.proto.model.provider.v1.ChatCompletionResponse;
import io.cyrene.proto.model.provider.v1.EmbeddingsRequest;
import io.cyrene.proto.model.provider.v1.EmbeddingsResponse;

import java.util.Objects;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Strongly-typed client for model.provider.v1 capability (T43, R05).
 *
 * <p>封装 Model 对话生成与嵌入向量计算，自动处理 Protobuf 序列化与反序列化。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class ModelClient {

    public static final String CAPABILITY = "model.provider.v1";
    public static final String INTERFACE_VERSION = "1";

    public static final String METHOD_CHAT_COMPLETION = "chat_completion";
    public static final String METHOD_EMBEDDINGS = "embeddings";

    public static final String TYPE_URL_CHAT_COMPLETION_REQUEST = "type.cyrene.io/cyrene.model.provider.v1.ChatCompletionRequest";
    public static final String TYPE_URL_EMBEDDINGS_REQUEST = "type.cyrene.io/cyrene.model.provider.v1.EmbeddingsRequest";

    private final DirectPluginRuntimeClient runtimeClient;

    public ModelClient(DirectPluginRuntimeClient runtimeClient) {
        this.runtimeClient = Objects.requireNonNull(runtimeClient, "runtimeClient must not be null");
    }

    public ChatCompletionResponse chatCompletion(String bindingId, ChatCompletionRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_CHAT_COMPLETION)
            .setPayloadTypeUrl(TYPE_URL_CHAT_COMPLETION_REQUEST)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("chat_completion failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return ChatCompletionResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse ChatCompletionResponse", e);
        }
    }

    public EmbeddingsResponse embeddings(String bindingId, EmbeddingsRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_EMBEDDINGS)
            .setPayloadTypeUrl(TYPE_URL_EMBEDDINGS_REQUEST)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("embeddings failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return EmbeddingsResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse EmbeddingsResponse", e);
        }
    }

    public DirectInvocationResponse chatCompletionBytes(String bindingId, ByteString requestBytes, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_CHAT_COMPLETION)
            .setPayloadTypeUrl(TYPE_URL_CHAT_COMPLETION_REQUEST)
            .setPayload(requestBytes)
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        return runtimeClient.invoke(bindingId, dir, context);
    }
}
