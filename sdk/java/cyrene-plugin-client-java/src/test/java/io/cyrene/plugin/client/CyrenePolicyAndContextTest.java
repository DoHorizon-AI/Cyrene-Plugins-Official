// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CyrenePolicyAndContextTest.java                                 │
// │  Package: io.cyrene.plugin.client                                   │
// │  Role: Unit tests for invocation retry policy and context limits.   │
// │                                                                     │
// │  模块职责：验证 T50(无隐式重试与幂等重试), T51(透传与有界元数据)     │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client;

import io.cyrene.plugin.client.context.CancellationToken;
import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.policy.CyreneInvocationPolicy;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class CyrenePolicyAndContextTest {

    @Test
    void testIdempotentRetrySucceedsWithinLimit_T50() throws Exception {
        CyreneInvocationPolicy policy = new CyreneInvocationPolicy(3, 10);
        AtomicInteger attempts = new AtomicInteger(0);

        String result = policy.executeIdempotentWithRetry("HealthCheck", () -> {
            if (attempts.incrementAndGet() < 3) {
                throw new RuntimeException("Transient network blip");
            }
            return "SUCCESS";
        });

        assertThat(result).isEqualTo("SUCCESS");
        assertThat(attempts.get()).isEqualTo(3);
    }

    @Test
    void testIdempotentRetryFailsWhenExceedingLimit_T50() {
        CyreneInvocationPolicy policy = new CyreneInvocationPolicy(2, 5);
        AtomicInteger attempts = new AtomicInteger(0);

        assertThatThrownBy(() -> policy.executeIdempotentWithRetry("Resolve", () -> {
            attempts.incrementAndGet();
            throw new RuntimeException("Persistent failure");
        })).isInstanceOf(IllegalStateException.class)
           .hasMessageContaining("failed after 2 attempts");

        assertThat(attempts.get()).isEqualTo(2);
    }

    @Test
    void testInvocationContextPropertiesAndCancellation_T51() {
        CancellationToken token = new CancellationToken();
        AtomicBoolean cancelledFlag = new AtomicBoolean(false);
        token.onCancel(() -> cancelledFlag.set(true));

        InvocationContext ctx = InvocationContext.newBuilder()
            .requestId("req-12345")
            .traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
            .deadline(Duration.ofMillis(2500))
            .cancellationToken(token)
            .addMetadata("x-test-header", "value1")
            .build();

        assertThat(ctx.getRequestId()).isEqualTo("req-12345");
        assertThat(ctx.getTraceparent()).contains("4bf92f3577b34da6a3ce929d0e0e4736");
        assertThat(ctx.getDeadline()).isEqualTo(Duration.ofMillis(2500));
        assertThat(ctx.getBoundedMetadata()).containsEntry("x-test-header", "value1");

        assertThat(token.isCancelled()).isFalse();
        token.cancel();
        assertThat(token.isCancelled()).isTrue();
        assertThat(cancelledFlag.get()).isTrue();
    }

    @Test
    void testBoundedMetadataRejectsOversizedPayload_T51() {
        String largeValue = "A".repeat(8193);

        assertThatThrownBy(() -> InvocationContext.newBuilder()
            .addMetadata("x-large", largeValue)
            .build()
        ).isInstanceOf(IllegalArgumentException.class)
         .hasMessageContaining("Bounded metadata limit exceeded");
    }
}
