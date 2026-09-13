// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CyrenePluginProperties.java                                     │
// │  Package: io.cyrene.plugin.spring                                   │
// │  Role: Strongly-typed Spring configuration properties (T52).        │
// │                                                                     │
// │  模块职责：Spring Boot 类型化配置属性，声明端点绑定与幂等重试策略    │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.spring;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.util.HashMap;
import java.util.Map;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Configuration properties for Cyrene Plugin integration (T52).
 * ════════════════════════════════════════════════════════════════════════
 */
@ConfigurationProperties(prefix = "cyrene.plugin")
public class CyrenePluginProperties {

    private boolean enabled = true;
    private Map<String, BindingProperty> bindings = new HashMap<>();
    private RetryProperty retry = new RetryProperty();

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public Map<String, BindingProperty> getBindings() {
        return bindings;
    }

    public void setBindings(Map<String, BindingProperty> bindings) {
        this.bindings = bindings;
    }

    public RetryProperty getRetry() {
        return retry;
    }

    public void setRetry(RetryProperty retry) {
        this.retry = retry;
    }

    public static class BindingProperty {
        private String targetUri;
        private String generation = "gen-001";
        private Map<String, String> attributes = new HashMap<>();

        public String getTargetUri() {
            return targetUri;
        }

        public void setTargetUri(String targetUri) {
            this.targetUri = targetUri;
        }

        public String getGeneration() {
            return generation;
        }

        public void setGeneration(String generation) {
            this.generation = generation;
        }

        public Map<String, String> getAttributes() {
            return attributes;
        }

        public void setAttributes(Map<String, String> attributes) {
            this.attributes = attributes;
        }
    }

    public static class RetryProperty {
        private int maxAttempts = 3;
        private long backoffMillis = 50;

        public int getMaxAttempts() {
            return maxAttempts;
        }

        public void setMaxAttempts(int maxAttempts) {
            this.maxAttempts = maxAttempts;
        }

        public long getBackoffMillis() {
            return backoffMillis;
        }

        public void setBackoffMillis(long backoffMillis) {
            this.backoffMillis = backoffMillis;
        }
    }
}
