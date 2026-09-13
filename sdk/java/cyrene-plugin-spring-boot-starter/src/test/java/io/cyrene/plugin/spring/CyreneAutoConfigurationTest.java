// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CyreneAutoConfigurationTest.java                                │
// │  Package: io.cyrene.plugin.spring                                   │
// │  Role: Spring Boot auto-configuration test (T52).                   │
// │                                                                     │
// │  模块职责：验证 Spring Boot 3 自动装配、配置属性与 Bean 注入机制      │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.spring;

import io.cyrene.plugin.client.channel.CyreneChannelManager;
import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.client.spi.CyreneBindingResolver;
import io.cyrene.plugin.client.typed.AgentClient;
import io.cyrene.plugin.client.typed.MemoryClient;
import org.junit.jupiter.api.Test;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;

import static org.assertj.core.api.Assertions.assertThat;

class CyreneAutoConfigurationTest {

    private final ApplicationContextRunner runner = new ApplicationContextRunner()
        .withConfiguration(AutoConfigurations.of(CyreneAutoConfiguration.class));

    @Test
    void testAutoConfigurationRegistersAllBeans_T52() {
        runner.withPropertyValues(
            "cyrene.plugin.enabled=true",
            "cyrene.plugin.bindings.binding-agent.target-uri=direct://127.0.0.1:50051",
            "cyrene.plugin.bindings.binding-agent.generation=gen-100"
        ).run(context -> {
            assertThat(context).hasSingleBean(CyreneBindingResolver.class);
            assertThat(context).hasSingleBean(CyreneChannelManager.class);
            assertThat(context).hasSingleBean(DirectPluginRuntimeClient.class);
            assertThat(context).hasSingleBean(AgentClient.class);
            assertThat(context).hasSingleBean(MemoryClient.class);

            CyreneBindingResolver resolver = context.getBean(CyreneBindingResolver.class);
            var resolution = resolver.resolve("binding-agent");
            assertThat(resolution.getConnectionRef().getHost()).isEqualTo("127.0.0.1");
            assertThat(resolution.getGeneration().getValue()).isEqualTo("gen-100");
        });
    }

    @Test
    void testAutoConfigurationDisabledWhenPropertyFalse() {
        runner.withPropertyValues("cyrene.plugin.enabled=false")
            .run(context -> {
                assertThat(context).doesNotHaveBean(CyreneBindingResolver.class);
                assertThat(context).doesNotHaveBean(DirectPluginRuntimeClient.class);
            });
    }
}
