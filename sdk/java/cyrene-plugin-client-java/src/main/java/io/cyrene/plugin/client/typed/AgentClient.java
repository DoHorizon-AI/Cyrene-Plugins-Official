// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 AgentClient.java                                                │
// │  Package: io.cyrene.plugin.client.typed                             │
// │  Role: Typed client for Agent Runtime (T43, R05).                   │
// │                                                                     │
// │  模块职责：类型化 Agent 客户端，调用方无需手工拼 method 或 type URL    │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.typed;

import com.google.protobuf.InvalidProtocolBufferException;
import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.runtime.v1.DirectInvocationRequest;
import io.cyrene.plugin.runtime.v1.DirectInvocationResponse;
import io.cyrene.plugin.runtime.v1.DirectStreamItem;
import io.cyrene.plugin.runtime.v1.DirectStreamMode;
import io.cyrene.proto.agent.runtime.v1.AgentRunRequest;
import io.cyrene.proto.agent.runtime.v1.AgentRunResponse;
import io.cyrene.proto.agent.runtime.v1.AgentStreamEvent;

import java.util.Iterator;
import java.util.NoSuchElementException;
import java.util.Objects;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Strongly-typed client for agent.runtime.v1 capability (T43, R05).
 *
 * <p>封装底层 DirectInvocationRequest 构造与序列化细节，调用方直接操作
 * {@link AgentRunRequest} 与 {@link AgentStreamEvent}。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class AgentClient {

    public static final String CAPABILITY = "agent.runtime.v1";
    public static final String INTERFACE_VERSION = "1";
    public static final String METHOD_RUN = "Run";
    public static final String METHOD_RUN_STREAM = "RunStream";
    public static final String TYPE_URL_RUN_REQUEST = "type.cyrene.io/cyrene.agent.runtime.v1.AgentRunRequest";
    public static final String TYPE_URL_EVENT = "type.cyrene.io/cyrene.agent.runtime.v1.AgentStreamEvent";

    private final DirectPluginRuntimeClient runtimeClient;

    public AgentClient(DirectPluginRuntimeClient runtimeClient) {
        this.runtimeClient = Objects.requireNonNull(runtimeClient, "runtimeClient must not be null");
    }

    public AgentRunResponse run(String bindingId, AgentRunRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_RUN)
            .setPayloadTypeUrl(TYPE_URL_RUN_REQUEST)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        DirectInvocationResponse resp = runtimeClient.invoke(bindingId, dir, context);
        if (resp.hasError()) {
            throw new RuntimeException("Agent Run failed: [" + resp.getError().getCode() + "] " + resp.getError().getMessage());
        }
        try {
            return AgentRunResponse.parseFrom(resp.getPayload().getValue());
        } catch (InvalidProtocolBufferException e) {
            throw new IllegalStateException("Failed to parse AgentRunResponse", e);
        }
    }

    public Iterator<AgentStreamEvent> runStream(String bindingId, AgentRunRequest request, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_RUN_STREAM)
            .setPayloadTypeUrl(TYPE_URL_RUN_REQUEST)
            .setPayload(request.toByteString())
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        Iterator<DirectStreamItem> rawStream = runtimeClient.invokeStream(bindingId, dir, context);

        return new Iterator<>() {
            private AgentStreamEvent nextEvent = null;

            @Override
            public boolean hasNext() {
                if (nextEvent != null) return true;
                while (rawStream.hasNext()) {
                    DirectStreamItem item = rawStream.next();
                    if (item.hasPayload()) {
                        try {
                            nextEvent = AgentStreamEvent.parseFrom(item.getPayload().getValue());
                            return true;
                        } catch (InvalidProtocolBufferException e) {
                            throw new IllegalStateException("Failed to parse AgentStreamEvent from stream payload", e);
                        }
                    } else if (item.hasEnd()) {
                        return false;
                    } else if (item.hasError()) {
                        throw new RuntimeException("Stream error: [" + item.getError().getCode() + "] " + item.getError().getMessage());
                    }
                }
                return false;
            }

            @Override
            public AgentStreamEvent next() {
                if (!hasNext()) throw new NoSuchElementException();
                AgentStreamEvent res = nextEvent;
                nextEvent = null;
                return res;
            }
        };
    }
}
