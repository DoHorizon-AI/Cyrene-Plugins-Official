// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CyreneBindingResolver.java                                      │
// │  Package: io.cyrene.plugin.client.spi                               │
// │  Role: SPI interface for stable binding resolution (T44).           │
// │                                                                     │
// │  模块职责：绑定解析器 SPI，输入稳定 binding_id，返回端点与代际       │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.spi;

/**
 * ════════════════════════════════════════════════════════════════════════
 * SPI for resolving stable plugin binding IDs into physical endpoints
 * and runtime generations.
 *
 * <p>定义 CyreneBindingResolver SPI，输入稳定 binding_id，返回当前
 * connection_ref 和 runtime generation (T44)。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
@FunctionalInterface
public interface CyreneBindingResolver {

    /**
     * Resolves a stable binding ID into its current physical endpoint and runtime generation.
     *
     * @param bindingId Stable logical identifier (e.g., "binding://cyrene-agent-service").
     * @return {@link BindingResolution} containing connection reference and generation.
     */
    BindingResolution resolve(String bindingId);
}
