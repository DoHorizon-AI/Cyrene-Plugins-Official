// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 ToolClient.java                                                 │
// │  Package: io.cyrene.plugin.client.typed                             │
// │  Role: Typed client for Tool Provider (T43, R05).                   │
// │                                                                     │
// │  模块职责：类型化 Tool 客户端，调用方无需手工拼 method 或 type URL     │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.typed;

import com.google.protobuf.ByteString;
import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.runtime.v1.DirectInvocationRequest;
import io.cyrene.plugin.runtime.v1.DirectInvocationResponse;
import io.cyrene.plugin.runtime.v1.DirectStreamMode;

import java.util.Objects;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Strongly-typed client for tool.provider.v1 capability (T43, R05).
 * ════════════════════════════════════════════════════════════════════════
 * <p>中文：tool.provider.v1 capability 的强类型客户端（T43、R05）。</p>
 */
public final class ToolClient {

    public static final String CAPABILITY = "tool.provider.v1";
    public static final String INTERFACE_VERSION = "1";
    public static final String METHOD_EXECUTE_TOOL = "ExecuteTool";
    public static final String TYPE_URL_EXECUTE_TOOL = "type.cyrene.io/cyrene.tool.provider.v1.ExecuteToolRequest";

    private final DirectPluginRuntimeClient runtimeClient;

    public ToolClient(DirectPluginRuntimeClient runtimeClient) {
        this.runtimeClient = Objects.requireNonNull(runtimeClient, "runtimeClient must not be null");
    }

    public DirectInvocationResponse executeTool(String bindingId, ByteString requestBytes, InvocationContext context) {
        DirectInvocationRequest dir = DirectInvocationRequest.newBuilder()
            .setCapability(CAPABILITY)
            .setInterfaceVersion(INTERFACE_VERSION)
            .setMethod(METHOD_EXECUTE_TOOL)
            .setPayloadTypeUrl(TYPE_URL_EXECUTE_TOOL)
            .setPayload(requestBytes)
            .setRequestId(context != null ? context.getRequestId() : "")
            .setStreamMode(DirectStreamMode.DIRECT_STREAM_MODE_INVOCATION)
            .build();

        return runtimeClient.invoke(bindingId, dir, context);
    }
}
