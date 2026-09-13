// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 StaticBindingResolver.java                                      │
// │  Package: io.cyrene.plugin.client.spi                               │
// │  Role: Deterministic test & fixed sidecar resolver (T45).           │
// │                                                                     │
// │  模块职责：静态绑定解析器，仅用于确定性测试与固定 sidecar 场景        │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.spi;

import java.util.Map;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;

/**
 * ════════════════════════════════════════════════════════════════════════
 * StaticBindingResolver for deterministic unit tests and fixed sidecars only (T45).
 *
 * <p>提供只用于确定性测试和固定 sidecar 的 StaticBindingResolver。严禁在生产
 * 动态拓扑环境中使用此解析器替代动态注册表。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class StaticBindingResolver implements CyreneBindingResolver {

    private final Map<String, BindingResolution> registry = new ConcurrentHashMap<>();

    public StaticBindingResolver() {}

    public StaticBindingResolver(Map<String, BindingResolution> initialBindings) {
        if (initialBindings != null) {
            this.registry.putAll(initialBindings);
        }
    }

    public static StaticBindingResolver single(String bindingId, String targetUri, String generation) {
        StaticBindingResolver resolver = new StaticBindingResolver();
        resolver.register(bindingId, targetUri, generation);
        return resolver;
    }

    public StaticBindingResolver register(String bindingId, String targetUri, String generation) {
        Objects.requireNonNull(bindingId, "bindingId must not be null");
        Objects.requireNonNull(targetUri, "targetUri must not be null");
        Objects.requireNonNull(generation, "generation must not be null");
        registry.put(bindingId, BindingResolution.of(bindingId, targetUri, generation));
        return this;
    }

    public StaticBindingResolver updateGeneration(String bindingId, String newGeneration) {
        BindingResolution current = registry.get(bindingId);
        if (current == null) {
            throw new IllegalArgumentException("Binding not found: " + bindingId);
        }
        registry.put(bindingId, new BindingResolution(
            bindingId,
            current.getConnectionRef(),
            RuntimeGeneration.of(newGeneration),
            current.getAttributes()
        ));
        return this;
    }

    @Override
    public BindingResolution resolve(String bindingId) {
        BindingResolution resolution = registry.get(bindingId);
        if (resolution == null) {
            throw new IllegalArgumentException("Unresolved binding ID: " + bindingId);
        }
        return resolution;
    }
}
