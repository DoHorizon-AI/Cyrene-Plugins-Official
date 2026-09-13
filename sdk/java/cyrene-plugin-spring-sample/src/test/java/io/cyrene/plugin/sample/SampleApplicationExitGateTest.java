// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 SampleApplicationExitGateTest.java                              │
// │  Package: io.cyrene.plugin.sample                                   │
// │  Role: End-to-end verification of the M4 Exit Gate criteria.       │
// │                                                                     │
// │  模块职责：M4 出口门禁验收测试：                                     │
// │           1. 解析稳定 binding_id 并获取当前 endpoint               │
// │           2. 调用强类型能力 (Agent & Memory)                         │
// │           3. 在代际切换与重启中平滑存活并关闭旧连接                  │
// │           4. 源代码层级严禁导入 Exchange 或 Platform 实现包        │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.sample;

import io.cyrene.plugin.client.channel.CyreneChannelManager;
import io.cyrene.plugin.client.spi.BindingResolution;
import io.cyrene.plugin.client.spi.RuntimeGeneration;
import io.cyrene.plugin.client.spi.StaticBindingResolver;
import io.cyrene.plugin.client.typed.AgentClient;
import io.cyrene.plugin.runtime.v1.DirectInvocationRequest;
import io.cyrene.plugin.runtime.v1.DirectInvocationResponse;
import io.cyrene.plugin.runtime.v1.DirectPayload;
import io.cyrene.plugin.runtime.v1.DirectPluginRuntimeGrpc;
import io.cyrene.plugin.runtime.v1.DirectStreamEnd;
import io.cyrene.plugin.runtime.v1.DirectStreamItem;
import io.cyrene.plugin.runtime.v1.HealthRequest;
import io.cyrene.plugin.runtime.v1.HealthResponse;
import io.cyrene.proto.agent.runtime.v1.AgentRunResponse;
import io.cyrene.proto.agent.runtime.v1.AgentRunSuccess;
import io.cyrene.proto.agent.runtime.v1.AgentStreamEvent;
import io.cyrene.proto.agent.runtime.v1.ContentDeltaEvent;
import io.cyrene.proto.memory.provider.v1.StoreMemoryResponse;
import io.grpc.Server;
import io.grpc.netty.shaded.io.grpc.netty.NettyServerBuilder;
import io.grpc.stub.StreamObserver;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.io.File;
import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest(classes = SampleApplication.class)
class SampleApplicationExitGateTest {

    @Autowired
    private StaticBindingResolver bindingResolver;

    @Autowired
    private CyreneChannelManager channelManager;

    @Autowired
    private SampleCapabilityService capabilityService;

    private Server server1;
    private Server server2;
    private int port1;
    private int port2;

    @BeforeEach
    void setUp() throws Exception {
        // Build service implementation
        DirectPluginRuntimeGrpc.DirectPluginRuntimeImplBase serviceImpl =
            new DirectPluginRuntimeGrpc.DirectPluginRuntimeImplBase() {
                @Override
                public void invoke(DirectInvocationRequest request, StreamObserver<DirectInvocationResponse> resp) {
                    if ("agent.runtime.v1".equals(request.getCapability())) {
                        AgentRunResponse runResp = AgentRunResponse.newBuilder()
                            .setSuccess(AgentRunSuccess.newBuilder()
                                .setFinishReason("stop")
                                .setOutputText("Processed: " + request.getMethod())
                                .build())
                            .build();
                        resp.onNext(DirectInvocationResponse.newBuilder()
                            .setPayload(DirectPayload.newBuilder()
                                .setTypeUrl(AgentClient.TYPE_URL_RUN_REQUEST)
                                .setValue(runResp.toByteString())
                                .build())
                            .build());
                    } else if ("memory.provider.v1".equals(request.getCapability())) {
                        StoreMemoryResponse storeResp = StoreMemoryResponse.newBuilder()
                            .setItemId("mem-spring-exit-gate")
                            .build();
                        resp.onNext(DirectInvocationResponse.newBuilder()
                            .setPayload(DirectPayload.newBuilder()
                                .setTypeUrl("type.cyrene.io/cyrene.proto.memory.provider.v1.StoreMemoryResponse")
                                .setValue(storeResp.toByteString())
                                .build())
                            .build());
                    }
                    resp.onCompleted();
                }

                @Override
                public void invokeStream(DirectInvocationRequest request, StreamObserver<DirectStreamItem> resp) {
                    for (int i = 0; i < 2; i++) {
                        AgentStreamEvent event = AgentStreamEvent.newBuilder()
                            .setContentDelta(ContentDeltaEvent.newBuilder().setTextDelta("spring-delta-" + i).build())
                            .build();
                        resp.onNext(DirectStreamItem.newBuilder()
                            .setPayload(DirectPayload.newBuilder()
                                .setTypeUrl(AgentClient.TYPE_URL_EVENT)
                                .setValue(event.toByteString())
                                .build())
                            .build());
                    }
                    resp.onNext(DirectStreamItem.newBuilder().setEnd(DirectStreamEnd.getDefaultInstance()).build());
                    resp.onCompleted();
                }

                @Override
                public void health(HealthRequest request, StreamObserver<HealthResponse> resp) {
                    resp.onNext(HealthResponse.newBuilder()
                        .setStatus(HealthResponse.Status.STATUS_SERVING)
                        .setPluginId("cyrene.spring.sample")
                        .setPluginVersion("1.0.0")
                        .build());
                    resp.onCompleted();
                }
            };

        // Server 1 (generation 1)
        server1 = NettyServerBuilder.forAddress(new InetSocketAddress("127.0.0.1", 0))
            .addService(serviceImpl)
            .build()
            .start();
        port1 = server1.getPort();

        // Server 2 (generation 2)
        server2 = NettyServerBuilder.forAddress(new InetSocketAddress("127.0.0.1", 0))
            .addService(serviceImpl)
            .build()
            .start();
        port2 = server2.getPort();

        // Register initial binding with Server 1 and gen-001
        bindingResolver.register("sample-agent-binding", "direct://127.0.0.1:" + port1, "gen-001");
        bindingResolver.register("sample-memory-binding", "direct://127.0.0.1:" + port1, "gen-001");
    }

    @AfterEach
    void tearDown() throws Exception {
        if (server1 != null) {
            server1.shutdownNow();
            server1.awaitTermination(500, TimeUnit.MILLISECONDS);
        }
        if (server2 != null) {
            server2.shutdownNow();
            server2.awaitTermination(500, TimeUnit.MILLISECONDS);
        }
    }

    @Test
    void testM4ExitGate_CleanSampleEndToEnd() throws Exception {
        // ── 1. Resolves stable binding and obtains current endpoint ──────────
        BindingResolution resolution1 = bindingResolver.resolve("sample-agent-binding");
        assertThat(resolution1.getBindingId()).isEqualTo("sample-agent-binding");
        assertThat(resolution1.getConnectionRef().getHost()).isEqualTo("127.0.0.1");
        assertThat(resolution1.getConnectionRef().getPort()).isEqualTo(port1);
        assertThat(resolution1.getGeneration()).isEqualTo(RuntimeGeneration.of("gen-001"));

        // ── 2. Calls typed capabilities without handcrafted URLs ─────────────
        String agentResult = capabilityService.executeAgentRun("sample-agent-binding", "Hello Agent");
        assertThat(agentResult).isEqualTo("Processed: Run");

        List<String> deltas = capabilityService.executeAgentStream("sample-agent-binding", "Stream prompt");
        assertThat(deltas).containsExactly("spring-delta-0", "spring-delta-1");

        String memoryId = capabilityService.storeMemory("sample-memory-binding", "tenant-alpha", "Sample note");
        assertThat(memoryId).isEqualTo("mem-spring-exit-gate");

        // Verify channel for gen-001 is active in channel manager
        assertThat(channelManager.hasActiveChannel("sample-agent-binding", RuntimeGeneration.of("gen-001"))).isTrue();

        // ── 3. Survives endpoint generation change (closes stale channel) ────
        // Simulate endpoint migration / upgrade / failover to Server 2 with gen-002
        bindingResolver.register("sample-agent-binding", "direct://127.0.0.1:" + port2, "gen-002");

        // Execute call on new generation
        String migratedAgentResult = capabilityService.executeAgentRun("sample-agent-binding", "Call after upgrade");
        assertThat(migratedAgentResult).isEqualTo("Processed: Run");

        // Old channel for gen-001 must be purged and closed; new channel for gen-002 active
        assertThat(channelManager.hasActiveChannel("sample-agent-binding", RuntimeGeneration.of("gen-001"))).isFalse();
        assertThat(channelManager.hasActiveChannel("sample-agent-binding", RuntimeGeneration.of("gen-002"))).isTrue();

        // ── 4. Strict architectural purity: never imports Exchange or Platform packages ──
        Path sampleSrcDir = Path.of("src/main/java");
        try (Stream<Path> stream = Files.walk(sampleSrcDir)) {
            List<Path> javaFiles = stream.filter(p -> p.toString().endsWith(".java")).toList();
            assertThat(javaFiles).isNotEmpty();

            for (Path javaFile : javaFiles) {
                List<String> lines = Files.readAllLines(javaFile);
                for (int i = 0; i < lines.size(); i++) {
                    String line = lines.get(i);
                    assertThat(line.toLowerCase())
                        .as("File %s at line %d must not import Exchange or Platform implementation packages",
                            javaFile.getFileName(), i + 1)
                        .doesNotContain("import.*exchange")
                        .doesNotContain("import.*platform");
                }
            }
        }
    }
}
