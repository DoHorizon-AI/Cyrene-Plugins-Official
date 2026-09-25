// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 PlatformProjectionBindingResolver.java                          │
// │  Package: io.cyrene.plugin.client.spi                               │
// │  Role: Platform resolver projection adapter (T46).                  │
// │                                                                     │
// │  模块职责：消费 Platform 暴露的轻量投影，不复制 Platform 业务模型     │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.spi;

import java.util.Collections;
import java.util.Map;
import java.util.Objects;
import java.util.function.Function;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Platform adapter consuming the existing resolver projection without
 * copying Platform business models (T46).
 *
 * <p>提供 Product 侧 Platform adapter，消费现有 resolver projection，不复制
 * Platform 业务模型。只提取 connection_ref 与 runtime_generation。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class PlatformProjectionBindingResolver implements CyreneBindingResolver {

    /**
     * Minimal projection record exposed by platform registry/discovery service.
     * <p>中文：由 Platform registry／discovery 服务暴露的最小投影记录。</p>
     */
    public record ProjectionRecord(
        String targetUri,
        String runtimeGeneration,
        Map<String, String> attributes
    ) {
        public ProjectionRecord {
            Objects.requireNonNull(targetUri, "targetUri must not be null");
            Objects.requireNonNull(runtimeGeneration, "runtimeGeneration must not be null");
            if (attributes == null) {
                attributes = Collections.emptyMap();
            }
        }
    }

    private final Function<String, ProjectionRecord> projectionProvider;

    public PlatformProjectionBindingResolver(Function<String, ProjectionRecord> projectionProvider) {
        this.projectionProvider = Objects.requireNonNull(projectionProvider, "projectionProvider must not be null");
    }

    @Override
    public BindingResolution resolve(String bindingId) {
        Objects.requireNonNull(bindingId, "bindingId must not be null");
        ProjectionRecord projection = projectionProvider.apply(bindingId);
        if (projection == null) {
            throw new IllegalStateException("Platform projection returned null for binding: " + bindingId);
        }

        return new BindingResolution(
            bindingId,
            ConnectionRef.of(projection.targetUri()),
            RuntimeGeneration.of(projection.runtimeGeneration()),
            projection.attributes()
        );
    }
}
