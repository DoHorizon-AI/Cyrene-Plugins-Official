// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CyreneAutoConfiguration.java                                    │
// │  Package: io.cyrene.plugin.spring                                   │
// │  Role: Spring Boot auto-configuration for Cyrene SDK (T52).         │
// │                                                                     │
// │  模块职责：Spring Boot 自动装配，注册解析器、通道管理器与类型化客户端  │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.spring;

import io.cyrene.plugin.client.channel.CyreneChannelManager;
import io.cyrene.plugin.client.policy.CyreneInvocationPolicy;
import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.client.spi.CyreneBindingResolver;
import io.cyrene.plugin.client.spi.StaticBindingResolver;
import io.cyrene.plugin.client.typed.AgentClient;
import io.cyrene.plugin.client.typed.ConnectorClient;
import io.cyrene.plugin.client.typed.MemoryClient;
import io.cyrene.plugin.client.typed.ModelClient;
import io.cyrene.plugin.client.typed.ToolClient;
import org.springframework.boot.health.contributor.HealthIndicator;
import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.boot.autoconfigure.condition.ConditionalOnClass;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.Map;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Spring Boot auto-configuration for Cyrene Plugin Java SDK (T52).
 * ════════════════════════════════════════════════════════════════════════
 */
@AutoConfiguration
@ConditionalOnProperty(prefix = "cyrene.plugin", name = "enabled", havingValue = "true", matchIfMissing = true)
@EnableConfigurationProperties(CyrenePluginProperties.class)
public class CyreneAutoConfiguration {

    @Bean
    @ConditionalOnMissingBean
    public CyreneBindingResolver cyreneBindingResolver(CyrenePluginProperties properties) {
        StaticBindingResolver resolver = new StaticBindingResolver();
        Map<String, CyrenePluginProperties.BindingProperty> bindings = properties.getBindings();
        if (bindings != null) {
            bindings.forEach((bindingId, prop) -> {
                if (prop.getTargetUri() != null && !prop.getTargetUri().isEmpty()) {
                    resolver.register(bindingId, prop.getTargetUri(), prop.getGeneration());
                }
            });
        }
        return resolver;
    }

    @Bean(destroyMethod = "close")
    @ConditionalOnMissingBean
    public CyreneChannelManager cyreneChannelManager() {
        return new CyreneChannelManager();
    }

    @Bean
    @ConditionalOnMissingBean
    public CyreneInvocationPolicy cyreneInvocationPolicy(CyrenePluginProperties properties) {
        return new CyreneInvocationPolicy(
            properties.getRetry().getMaxAttempts(),
            properties.getRetry().getBackoffMillis()
        );
    }

    @Bean(destroyMethod = "close")
    @ConditionalOnMissingBean
    public DirectPluginRuntimeClient directPluginRuntimeClient(
        CyreneBindingResolver bindingResolver,
        CyreneChannelManager channelManager,
        CyreneInvocationPolicy invocationPolicy
    ) {
        return new DirectPluginRuntimeClient(bindingResolver, channelManager, invocationPolicy);
    }

    @Bean
    @ConditionalOnMissingBean
    public AgentClient cyreneAgentClient(DirectPluginRuntimeClient runtimeClient) {
        return new AgentClient(runtimeClient);
    }

    @Bean
    @ConditionalOnMissingBean
    public MemoryClient cyreneMemoryClient(DirectPluginRuntimeClient runtimeClient) {
        return new MemoryClient(runtimeClient);
    }

    @Bean
    @ConditionalOnMissingBean
    public ModelClient cyreneModelClient(DirectPluginRuntimeClient runtimeClient) {
        return new ModelClient(runtimeClient);
    }

    @Bean
    @ConditionalOnMissingBean
    public ToolClient cyreneToolClient(DirectPluginRuntimeClient runtimeClient) {
        return new ToolClient(runtimeClient);
    }

    @Bean
    @ConditionalOnMissingBean
    public ConnectorClient cyreneConnectorClient(DirectPluginRuntimeClient runtimeClient) {
        return new ConnectorClient(runtimeClient);
    }

    // ── Actuator Health Indicator (T52) via nested static configuration ──

    @Configuration(proxyBeanMethods = false)
    @ConditionalOnClass(HealthIndicator.class)
    public static class CyreneHealthIndicatorConfiguration {

        @Bean
        @ConditionalOnMissingBean(name = "cyrenePluginHealthIndicator")
        public HealthIndicator cyrenePluginHealthIndicator(
            DirectPluginRuntimeClient runtimeClient,
            CyrenePluginProperties properties
        ) {
            return new CyreneHealthIndicator(runtimeClient, properties);
        }
    }
}
