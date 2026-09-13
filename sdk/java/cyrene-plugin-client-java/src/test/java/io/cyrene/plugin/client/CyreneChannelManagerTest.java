// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CyreneChannelManagerTest.java                                   │
// │  Package: io.cyrene.plugin.client                                   │
// │  Role: Unit tests for Channel caching, generation change, and TLS.  │
// │                                                                     │
// │  模块职责：验证 T47(按代际缓存), T48(代际切换关旧通道), T49(TLS门禁)  │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client;

import io.cyrene.plugin.client.channel.CyreneChannelManager;
import io.cyrene.plugin.client.spi.BindingResolution;
import io.cyrene.plugin.client.spi.ConnectionRef;
import io.cyrene.plugin.client.spi.RuntimeGeneration;
import io.grpc.ManagedChannel;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class CyreneChannelManagerTest {

    private CyreneChannelManager channelManager;

    @BeforeEach
    void setUp() {
        channelManager = new CyreneChannelManager();
    }

    @AfterEach
    void tearDown() {
        channelManager.close();
    }

    @Test
    void testChannelCachedByBindingAndGeneration_T47() {
        BindingResolution res1 = BindingResolution.of("binding-echo", "direct://127.0.0.1:50051", "gen-1");
        ManagedChannel ch1 = channelManager.getOrCreateChannel(res1);
        ManagedChannel ch2 = channelManager.getOrCreateChannel(res1);

        assertThat(ch1).isSameAs(ch2);
        assertThat(channelManager.getActiveChannelCount()).isEqualTo(1);
    }

    @Test
    void testGenerationChangeClosesStaleChannel_T48() {
        BindingResolution gen1 = BindingResolution.of("binding-echo", "direct://127.0.0.1:50051", "gen-1");
        ManagedChannel ch1 = channelManager.getOrCreateChannel(gen1);
        assertThat(ch1.isShutdown()).isFalse();

        // New generation arrives (e.g. upgrade or restart)
        BindingResolution gen2 = BindingResolution.of("binding-echo", "direct://127.0.0.1:50052", "gen-2");
        ManagedChannel ch2 = channelManager.getOrCreateChannel(gen2);

        assertThat(ch2).isNotSameAs(ch1);
        assertThat(ch1.isShutdown()).isTrue();
        assertThat(channelManager.hasActiveChannel("binding-echo", RuntimeGeneration.of("gen-1"))).isFalse();
        assertThat(channelManager.hasActiveChannel("binding-echo", RuntimeGeneration.of("gen-2"))).isTrue();
        assertThat(channelManager.getActiveChannelCount()).isEqualTo(1);
    }

    @Test
    void testPermitInsecureOnlyForLoopbackOrLocalSockets_T49() {
        // Loopback endpoints are allowed
        ConnectionRef loopback1 = ConnectionRef.of("direct://127.0.0.1:50051");
        assertThat(loopback1.isLoopbackOrLocal()).isTrue();
        loopback1.validateTransportSecurity(true); // Must not throw

        ConnectionRef loopback2 = ConnectionRef.of("direct://localhost:50051");
        assertThat(loopback2.isLoopbackOrLocal()).isTrue();
        loopback2.validateTransportSecurity(true); // Must not throw

        ConnectionRef unixSocket = ConnectionRef.of("unix:///tmp/cyrene.sock");
        assertThat(unixSocket.isLoopbackOrLocal()).isTrue();
        unixSocket.validateTransportSecurity(true); // Must not throw

        // Remote endpoint with insecure MUST throw SecurityException
        ConnectionRef remoteEndpoint = ConnectionRef.of("direct://192.168.1.100:50051");
        assertThat(remoteEndpoint.isLoopbackOrLocal()).isFalse();
        assertThatThrownBy(() -> remoteEndpoint.validateTransportSecurity(true))
            .isInstanceOf(SecurityException.class)
            .hasMessageContaining("T49");
    }
}
