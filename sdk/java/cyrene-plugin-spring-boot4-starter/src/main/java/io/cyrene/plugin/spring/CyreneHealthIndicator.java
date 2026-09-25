// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CyreneHealthIndicator.java                                      │
// │  Package: io.cyrene.plugin.spring                                   │
// │  Role: Spring Boot Actuator health indicator (T52).                 │
// │                                                                     │
// │  模块职责：Spring Boot Actuator 健康检查指示器，定期探测绑定的插件端点│
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.spring;

import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.client.spi.CyreneBindingResolver;
import io.cyrene.plugin.runtime.v1.HealthRequest;
import io.cyrene.plugin.runtime.v1.HealthResponse;
import org.springframework.boot.health.contributor.Health;
import org.springframework.boot.health.contributor.HealthIndicator;

import java.util.Map;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Spring Actuator HealthIndicator for Cyrene Plugin bindings (T52).
 * ════════════════════════════════════════════════════════════════════════
 * <p>中文：针对 Cyrene Plugin binding 的 Spring Actuator HealthIndicator（T52）。</p>
 */
public class CyreneHealthIndicator implements HealthIndicator {

    private final DirectPluginRuntimeClient runtimeClient;
    private final CyrenePluginProperties properties;

    public CyreneHealthIndicator(DirectPluginRuntimeClient runtimeClient, CyrenePluginProperties properties) {
        this.runtimeClient = runtimeClient;
        this.properties = properties;
    }

    @Override
    public Health health() {
        Health.Builder builder = Health.up();
        Map<String, CyrenePluginProperties.BindingProperty> bindings = properties.getBindings();
        if (bindings == null || bindings.isEmpty()) {
            return builder.withDetail("bindings", "none configured").build();
        }

        for (String bindingId : bindings.keySet()) {
            try {
                HealthResponse resp = runtimeClient.health(bindingId, HealthRequest.getDefaultInstance());
                if (resp.getStatus() == HealthResponse.Status.STATUS_SERVING) {
                    builder.withDetail(bindingId, Map.of(
                        "status", "SERVING",
                        "pluginId", resp.getPluginId(),
                        "pluginVersion", resp.getPluginVersion(),
                        "capabilities", resp.getCapabilitiesList()
                    ));
                } else {
                    return Health.down()
                        .withDetail(bindingId, "Non-serving status: " + resp.getStatus())
                        .build();
                }
            } catch (Exception ex) {
                return Health.down(ex)
                    .withDetail(bindingId, "Health probe failed: " + ex.getMessage())
                    .build();
            }
        }

        return builder.build();
    }
}
