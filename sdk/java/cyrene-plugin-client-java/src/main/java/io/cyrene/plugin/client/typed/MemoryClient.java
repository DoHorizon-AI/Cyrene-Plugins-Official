// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 MemoryClient.java                                               │
// │  Package: io.cyrene.plugin.client.typed                             │
// │  Role: Typed client for Memory Provider (T43, R05).                 │
// │                                                                     │
// │  模块职责：类型化 Memory 客户端，调用方无需手工拼 method 或 type URL   │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.typed;

import com.google.protobuf.InvalidProtocolBufferException;
import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.runtime.v1.DirectInvocationRequest;
import io.cyrene.plugin.runtime.v1.DirectInvocationResponse;
import io.cyrene.plugin.runtime.v1.DirectStreamMode;
import io.cyrene.proto.memory.provider.v1.DeleteMemoryRequest;
import io.cyrene.proto.memory.provider.v1.DeleteMemoryResponse;
import io.cyrene.proto.memory.provider.v1.GetMemoryRequest;
import io.cyrene.proto.memory.provider.v1.GetMemoryResponse;
import io.cyrene.proto.memory.provider.v1.RecallMemoryRequest;
import io.cyrene.proto.memory.provider.v1.RecallMemoryResponse;
import io.cyrene.proto.memory.provider.v1.StoreMemoryRequest;
import io.cyrene.proto.memory.provider.v1.StoreMemoryResponse;

import java.util.Objects;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Strongly-typed client for memory.provider.v1 capability (T43, R05).
 *
 * <p>封装 Memory 存储、获取、检索与删除操作，自动打包解包 Protobuf 载荷。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class MemoryClient {

    public static final String CAPABILITY = "memory.provider.v1";
    public static final String INTERFACE_VERSION = "1";

    public static final String METHOD_STORE = "StoreMemory";
    public static final String METHOD_GET = "GetMemory";
    public static final String METHOD_RECALL = "RecallMemory";
    public static final String METHOD_DELETE = "DeleteMemory";

    public static final String TYPE_URL_STORE_REQUEST = "type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryRequest";
    public static final String TYPE_URL_GET_REQUEST = "type.cyrene.io/cyrene.memory.provider.v1.GetMemoryRequest";
    public static final String TYPE_URL_RECALL_REQUEST = "type.cyrene.io/cyrene.memory.provider.v1.RecallMemoryRequest";
    public static final String TYPE_URL_DELETE_REQUEST = "type.cyrene.io/cyrene.memory.provider.v1.DeleteMemoryRequest";

    private final DirectPluginRuntimeClient runtimeClient;

    public MemoryClient(DirectPluginRuntimeClient runtimeClient) {
        this.runtimeClient = Objects.requireNonNull(runtimeClient, "runtimeClient must not be null");
    }

    public StoreMemoryResponse store(String bindingId, StoreMemoryRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_STORE)
            .setPayloadTypeUrl(TYPE_URL_STORE_REQUEST)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("StoreMemory failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return StoreMemoryResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse StoreMemoryResponse", e);
        }
    }

    public GetMemoryResponse get(String bindingId, GetMemoryRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_GET)
            .setPayloadTypeUrl(TYPE_URL_GET_REQUEST)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("GetMemory failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return GetMemoryResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse GetMemoryResponse", e);
        }
    }

    public RecallMemoryResponse recall(String bindingId, RecallMemoryRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_RECALL)
            .setPayloadTypeUrl(TYPE_URL_RECALL_REQUEST)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("RecallMemory failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return RecallMemoryResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse RecallMemoryResponse", e);
        }
    }

    public DeleteMemoryResponse delete(String bindingId, DeleteMemoryRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_DELETE)
            .setPayloadTypeUrl(TYPE_URL_DELETE_REQUEST)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("DeleteMemory failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return DeleteMemoryResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse DeleteMemoryResponse", e);
        }
    }
}
