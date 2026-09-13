// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 RuntimeGeneration.java                                          │
// │  Package: io.cyrene.plugin.client.spi                               │
// │  Role: Value object for runtime generation tracking (T44, T47).     │
// │                                                                     │
// │  模块职责：运行时代际版本对象，用于按代际管理和失效 Channel          │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.spi;

import java.util.Objects;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Represents a runtime generation identifier for a plugin binding.
 *
 * <p>唯一标识插件实例的运行时代际。当插件经历激活、故障恢复、升级或回滚时，
 * 运行时代际变更，触发陈旧连接安全关闭与重建。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class RuntimeGeneration implements Comparable<RuntimeGeneration> {

    private final String value;

    public RuntimeGeneration(String value) {
        this.value = Objects.requireNonNull(value, "generation value must not be null").trim();
        if (this.value.isEmpty()) {
            throw new IllegalArgumentException("generation value must not be empty");
        }
    }

    public static RuntimeGeneration of(String value) {
        return new RuntimeGeneration(value);
    }

    public String getValue() {
        return value;
    }

    @Override
    public int compareTo(RuntimeGeneration other) {
        return this.value.compareTo(other.value);
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        RuntimeGeneration that = (RuntimeGeneration) o;
        return Objects.equals(value, that.value);
    }

    @Override
    public int hashCode() {
        return Objects.hash(value);
    }

    @Override
    public String toString() {
        return "RuntimeGeneration[" + value + "]";
    }
}
