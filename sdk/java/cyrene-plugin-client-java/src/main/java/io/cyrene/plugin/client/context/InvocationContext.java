// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 InvocationContext.java                                          │
// │  Package: io.cyrene.plugin.client.context                           │
// │  Role: Carries deadlines, cancellation, trace, and metadata (T51).  │
// │                                                                     │
// │  模块职责：传递 deadline、取消、request ID、trace context 与有界 metadata │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.context;

import io.grpc.CallOptions;
import io.grpc.Channel;
import io.grpc.ClientCall;
import io.grpc.ClientInterceptor;
import io.grpc.Deadline;
import io.grpc.ForwardingClientCall;
import io.grpc.Metadata;
import io.grpc.MethodDescriptor;
import io.grpc.stub.AbstractStub;

import java.time.Duration;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Invocation context propagating deadlines, cancellation, request IDs,
 * trace context, and bounded metadata (T51).
 *
 * <p>传递 deadline、取消、request ID、trace context 和有界 metadata。
 * 强制限制元数据总体字节尺寸（最大 8KB），防止传输膨胀。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class InvocationContext {

    public static final int MAX_METADATA_BYTES = 8192; // 8 KB bounded metadata | 中文：元数据大小上限为 8 KB

    private static final Metadata.Key<String> KEY_REQUEST_ID =
        Metadata.Key.of("x-request-id", Metadata.ASCII_STRING_MARSHALLER);
    private static final Metadata.Key<String> KEY_TRACEPARENT =
        Metadata.Key.of("traceparent", Metadata.ASCII_STRING_MARSHALLER);

    private final String requestId;
    private final String traceparent;
    private final Duration deadline;
    private final CancellationToken cancellationToken;
    private final Map<String, String> boundedMetadata;

    private InvocationContext(Builder builder) {
        this.requestId = builder.requestId != null ? builder.requestId : "req-" + UUID.randomUUID();
        this.traceparent = builder.traceparent;
        this.deadline = builder.deadline;
        this.cancellationToken = builder.cancellationToken != null ? builder.cancellationToken : new CancellationToken();
        this.boundedMetadata = Collections.unmodifiableMap(new HashMap<>(builder.metadata));
    }

    public static Builder newBuilder() {
        return new Builder();
    }

    public static InvocationContext empty() {
        return newBuilder().build();
    }

    public String getRequestId() {
        return requestId;
    }

    public String getTraceparent() {
        return traceparent;
    }

    public Duration getDeadline() {
        return deadline;
    }

    public CancellationToken getCancellationToken() {
        return cancellationToken;
    }

    public Map<String, String> getBoundedMetadata() {
        return boundedMetadata;
    }

    /**
     * Applies this context to a gRPC stub (deadline, interceptors for metadata propagation).
     * <p>中文：将 deadline 和用于传递 metadata 的 interceptor 等上下文应用到 gRPC stub。</p>
     */
    public <T extends AbstractStub<T>> T applyToStub(T stub) {
        T configured = stub;
        if (deadline != null && !deadline.isNegative() && !deadline.isZero()) {
            configured = configured.withDeadline(Deadline.after(deadline.toMillis(), TimeUnit.MILLISECONDS));
        }

        configured = configured.withInterceptors(new ClientInterceptor() {
            @Override
            public <ReqT, RespT> ClientCall<ReqT, RespT> interceptCall(
                MethodDescriptor<ReqT, RespT> method,
                CallOptions callOptions,
                Channel next
            ) {
                ClientCall<ReqT, RespT> call = next.newCall(method, callOptions);

                // Register cooperative cancellation
// 中文：注册协作式取消。
                cancellationToken.onCancel(() -> {
                    try {
                        call.cancel("Invocation cancelled by client context", null);
                    } catch (Throwable ignored) {
                    }
                });

                return new ForwardingClientCall.SimpleForwardingClientCall<>(call) {
                    @Override
                    public void start(Listener<RespT> responseListener, Metadata headers) {
                        headers.put(KEY_REQUEST_ID, requestId);
                        if (traceparent != null && !traceparent.isEmpty()) {
                            headers.put(KEY_TRACEPARENT, traceparent);
                        }
                        for (Map.Entry<String, String> entry : boundedMetadata.entrySet()) {
                            Metadata.Key<String> key = Metadata.Key.of(entry.getKey(), Metadata.ASCII_STRING_MARSHALLER);
                            headers.put(key, entry.getValue());
                        }
                        super.start(responseListener, headers);
                    }
                };
            }
        });

        return configured;
    }

    public static final class Builder {
        private String requestId;
        private String traceparent;
        private Duration deadline;
        private CancellationToken cancellationToken;
        private final Map<String, String> metadata = new HashMap<>();

        public Builder requestId(String requestId) {
            this.requestId = requestId;
            return this;
        }

        public Builder traceparent(String traceparent) {
            this.traceparent = traceparent;
            return this;
        }

        public Builder deadline(Duration deadline) {
            this.deadline = deadline;
            return this;
        }

        public Builder cancellationToken(CancellationToken token) {
            this.cancellationToken = token;
            return this;
        }

        public Builder addMetadata(String key, String value) {
            Objects.requireNonNull(key, "metadata key must not be null");
            Objects.requireNonNull(value, "metadata value must not be null");
            metadata.put(key, value);
            return this;
        }

        public InvocationContext build() {
            // Check bounded metadata size (T51)
// 中文：检查 metadata 大小是否处于有界范围内（T51）。
            int totalBytes = 0;
            for (Map.Entry<String, String> entry : metadata.entrySet()) {
                totalBytes += entry.getKey().getBytes().length + entry.getValue().getBytes().length;
            }
            if (totalBytes > MAX_METADATA_BYTES) {
                throw new IllegalArgumentException(
                    "Bounded metadata limit exceeded: " + totalBytes + " bytes (max: " + MAX_METADATA_BYTES + ")"
                );
            }
            return new InvocationContext(this);
        }
    }
}
