// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 ConnectorClient.java                                            │
// │  Package: io.cyrene.plugin.client.typed                             │
// │  Role: Typed client for Message Connector (T43, R05).               │
// │                                                                     │
// │  模块职责：类型化 Connector 客户端，调用方无需手工拼 method 或 type URL │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.typed;

import com.google.protobuf.ByteString;
import com.google.protobuf.InvalidProtocolBufferException;
import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.runtime.v1.DirectInvocationRequest;
import io.cyrene.plugin.runtime.v1.DirectInvocationResponse;
import io.cyrene.plugin.runtime.v1.DirectStreamMode;
import io.cyrene.proto.message.connector.v1.DeliveryResult;
import io.cyrene.proto.message.connector.v1.SendMessageRequest;

import java.util.Objects;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Strongly-typed client for message.connector.v1 capability (T43, R05).
 * ════════════════════════════════════════════════════════════════════════
 * <p>中文：message.connector.v1 capability 的强类型客户端（T43、R05）。</p>
 */
public final class ConnectorClient {

    public static final String CAPABILITY = "message.connector.v1";
    public static final String INTERFACE_VERSION = "1";
    public static final String METHOD_SEND_MESSAGE = "SendMessage";
    public static final String TYPE_URL_SEND_MESSAGE = "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest";

    private final DirectPluginRuntimeClient runtimeClient;

    public ConnectorClient(DirectPluginRuntimeClient runtimeClient) {
        this.runtimeClient = Objects.requireNonNull(runtimeClient, "runtimeClient must not be null");
    }

    public DeliveryResult sendMessage(String bindingId, SendMessageRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_SEND_MESSAGE)
            .setPayloadTypeUrl(TYPE_URL_SEND_MESSAGE)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("SendMessage failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return DeliveryResult.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse DeliveryResult", e);
        }
    }

    public DirectInvocationResponse sendMessageBytes(String bindingId, ByteString requestBytes, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_SEND_MESSAGE)
            .setPayloadTypeUrl(TYPE_URL_SEND_MESSAGE)
            .setPayload(requestBytes)
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        return runtimeClient.invoke(bindingId, dir, context);
    }
}
