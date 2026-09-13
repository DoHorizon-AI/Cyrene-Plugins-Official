// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 BindingResolution.java                                          │
// │  Package: io.cyrene.plugin.client.spi                               │
// │  Role: Immutable result of SPI binding resolution (T44).           │
// │                                                                     │
// │  模块职责：绑定解析结果，包含稳定 binding_id、连接引用与代际        │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.spi;

import java.util.Collections;
import java.util.Map;
import java.util.Objects;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Result of resolving a stable plugin binding ID.
 *
 * <p>包含稳定 binding_id、当前 connection_ref、运行时代际 runtime generation
 * 以及元数据属性字典。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class BindingResolution {

    private final String bindingId;
    private final ConnectionRef connectionRef;
    private final RuntimeGeneration generation;
    private final long resolvedTimestampMillis;
    private final Map<String, String> attributes;

    public BindingResolution(
        String bindingId,
        ConnectionRef connectionRef,
        RuntimeGeneration generation,
        Map<String, String> attributes
    ) {
        this.bindingId = Objects.requireNonNull(bindingId, "bindingId must not be null");
        this.connectionRef = Objects.requireNonNull(connectionRef, "connectionRef must not be null");
        this.generation = Objects.requireNonNull(generation, "generation must not be null");
        this.resolvedTimestampMillis = System.currentTimeMillis();
        this.attributes = attributes != null ? Collections.unmodifiableMap(attributes) : Collections.emptyMap();
    }

    public static BindingResolution of(String bindingId, String targetUri, String generation) {
        return new BindingResolution(bindingId, ConnectionRef.of(targetUri), RuntimeGeneration.of(generation), null);
    }

    public String getBindingId() {
        return bindingId;
    }

    public ConnectionRef getConnectionRef() {
        return connectionRef;
    }

    public RuntimeGeneration getGeneration() {
        return generation;
    }

    public long getResolvedTimestampMillis() {
        return resolvedTimestampMillis;
    }

    public Map<String, String> getAttributes() {
        return attributes;
    }

    @Override
    public String toString() {
        return "BindingResolution{" +
            "bindingId='" + bindingId + '\'' +
            ", connectionRef=" + connectionRef +
            ", generation=" + generation +
            ", resolvedTimestampMillis=" + resolvedTimestampMillis +
            '}';
    }
}
