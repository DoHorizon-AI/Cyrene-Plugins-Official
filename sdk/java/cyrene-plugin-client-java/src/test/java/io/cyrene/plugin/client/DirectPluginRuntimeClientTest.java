// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 DirectPluginRuntimeClientTest.java                              │
// │  Package: io.cyrene.plugin.client                                   │
// │  Role: End-to-end unit test with in-process gRPC test service.      │
// │                                                                     │
// │  模块职责：验证 T42(三类stub), T43(类型化调用), T44-T46(解析器SPI)    │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client;

import com.google.protobuf.ByteString;
import io.cyrene.plugin.client.channel.CyreneChannelManager;
import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.policy.CyreneInvocationPolicy;
import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.client.spi.PlatformProjectionBindingResolver;
import io.cyrene.plugin.client.spi.StaticBindingResolver;
import io.cyrene.plugin.client.typed.AgentClient;
import io.cyrene.plugin.client.typed.MemoryClient;
import io.cyrene.plugin.runtime.v1.DirectInvocationRequest;
import io.cyrene.plugin.runtime.v1.DirectInvocationResponse;
import io.cyrene.plugin.runtime.v1.DirectPayload;
import io.cyrene.plugin.runtime.v1.DirectPluginRuntimeGrpc;
import io.cyrene.plugin.runtime.v1.DirectStreamEnd;
import io.cyrene.plugin.runtime.v1.DirectStreamItem;
import io.cyrene.plugin.runtime.v1.HealthRequest;
import io.cyrene.plugin.runtime.v1.HealthResponse;
import io.cyrene.proto.agent.runtime.v1.AgentRunRequest;
import io.cyrene.proto.agent.runtime.v1.AgentRunResponse;
import io.cyrene.proto.agent.runtime.v1.AgentRunSuccess;
import io.cyrene.proto.agent.runtime.v1.AgentStreamEvent;
import io.cyrene.proto.agent.runtime.v1.ContentDeltaEvent;
import io.cyrene.proto.memory.provider.v1.MemoryItem;
import io.cyrene.proto.memory.provider.v1.StoreMemoryRequest;
import io.cyrene.proto.memory.provider.v1.StoreMemoryResponse;
import io.grpc.Server;
import io.grpc.netty.shaded.io.grpc.netty.NettyServerBuilder;
import io.grpc.stub.StreamObserver;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;

class DirectPluginRuntimeClientTest {

    private Server server;
    private int port;
    private StaticBindingResolver bindingResolver;
    private CyreneChannelManager channelManager;
    private DirectPluginRuntimeClient client;

    @BeforeEach
    void setUp() throws Exception {
        // Start real Netty test server on loopback port
        DirectPluginRuntimeGrpc.DirectPluginRuntimeImplBase serviceImpl =
            new DirectPluginRuntimeGrpc.DirectPluginRuntimeImplBase() {
                @Override
                public void invoke(DirectInvocationRequest request, StreamObserver<DirectInvocationResponse> resp) {
                    if ("agent.runtime.v1".equals(request.getCapability())) {
                        AgentRunResponse runResp = AgentRunResponse.newBuilder()
                            .setSuccess(AgentRunSuccess.newBuilder()
                                .setFinishReason("stop")
                                .setOutputText("Agent completed: " + request.getMethod())
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
                            .setItemId("mem-001")
                            .build();
                        resp.onNext(DirectInvocationResponse.newBuilder()
                            .setPayload(DirectPayload.newBuilder()
                                .setTypeUrl("type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryResponse")
                                .setValue(storeResp.toByteString())
                                .build())
                            .build());
                    } else {
                        // Echo payload
                        resp.onNext(DirectInvocationResponse.newBuilder()
                            .setPayload(DirectPayload.newBuilder()
                                .setTypeUrl(request.getPayloadTypeUrl())
                                .setValue(request.getPayload())
                                .build())
                            .build());
                    }
                    resp.onCompleted();
                }

                @Override
                public void invokeStream(DirectInvocationRequest request, StreamObserver<DirectStreamItem> resp) {
                    for (int i = 0; i < 3; i++) {
                        AgentStreamEvent event = AgentStreamEvent.newBuilder()
                            .setContentDelta(ContentDeltaEvent.newBuilder().setTextDelta("chunk-" + i).build())
                            .build();
                        resp.onNext(DirectStreamItem.newBuilder()
                            .setPayload(DirectPayload.newBuilder()
                                .setTypeUrl(AgentClient.TYPE_URL_EVENT)
                                .setValue(event.toByteString())
                                .build())
                            .build());
                    }
                    resp.onNext(DirectStreamItem.newBuilder()
                        .setEnd(DirectStreamEnd.getDefaultInstance())
                        .build());
                    resp.onCompleted();
                }

                @Override
                public void health(HealthRequest request, StreamObserver<HealthResponse> resp) {
                    resp.onNext(HealthResponse.newBuilder()
                        .setStatus(HealthResponse.Status.STATUS_SERVING)
                        .setPluginId("cyrene.test.plugin")
                        .setPluginVersion("1.0.0")
                        .addCapabilities("agent.runtime.v1")
                        .addCapabilities("memory.provider.v1")
                        .build());
                    resp.onCompleted();
                }
            };

        server = NettyServerBuilder.forAddress(new InetSocketAddress("127.0.0.1", 0))
            .addService(serviceImpl)
            .build()
            .start();
        port = server.getPort();

        bindingResolver = StaticBindingResolver.single("binding-test", "direct://127.0.0.1:" + port, "gen-001");
        channelManager = new CyreneChannelManager();
        client = new DirectPluginRuntimeClient(bindingResolver, channelManager, CyreneInvocationPolicy.defaultPolicy());
    }

    @AfterEach
    void tearDown() throws Exception {
        client.close();
        if (server != null) {
            server.shutdownNow();
            server.awaitTermination(1, TimeUnit.SECONDS);
        }
    }

    @Test
    void testBlockingUnaryInvoke_T42() {
        DirectInvocationRequest req = DirectInvocationRequest.newBuilder()
            .setCapability("test.echo")
            .setMethod("Echo")
            .setPayload(ByteString.copyFromUtf8("Hello Cyrene"))
            .build();

        DirectInvocationResponse resp = client.invoke("binding-test", req, InvocationContext.empty());
        assertThat(resp.hasPayload()).isTrue();
        assertThat(resp.getPayload().getValue().toStringUtf8()).isEqualTo("Hello Cyrene");
    }

    @Test
    void testFutureUnaryInvoke_T42() throws Exception {
        DirectInvocationRequest req = DirectInvocationRequest.newBuilder()
            .setCapability("test.echo")
            .setMethod("Echo")
            .setPayload(ByteString.copyFromUtf8("Future Call"))
            .build();

        CompletableFuture<DirectInvocationResponse> future = client.invokeFuture("binding-test", req, InvocationContext.empty());
        DirectInvocationResponse resp = future.get(2, TimeUnit.SECONDS);
        assertThat(resp.hasPayload()).isTrue();
        assertThat(resp.getPayload().getValue().toStringUtf8()).isEqualTo("Future Call");
    }

    @Test
    void testAsyncUnaryInvoke_T42() throws Exception {
        DirectInvocationRequest req = DirectInvocationRequest.newBuilder()
            .setCapability("test.echo")
            .setMethod("Echo")
            .setPayload(ByteString.copyFromUtf8("Async Call"))
            .build();

        CompletableFuture<String> resultFuture = new CompletableFuture<>();
        client.invokeAsync("binding-test", req, InvocationContext.empty(), new StreamObserver<>() {
            @Override
            public void onNext(DirectInvocationResponse value) {
                resultFuture.complete(value.getPayload().getValue().toStringUtf8());
            }

            @Override
            public void onError(Throwable t) {
                resultFuture.completeExceptionally(t);
            }

            @Override
            public void onCompleted() {}
        });

        String result = resultFuture.get(2, TimeUnit.SECONDS);
        assertThat(result).isEqualTo("Async Call");
    }

    @Test
    void testBlockingStreamInvoke_T42() {
        DirectInvocationRequest req = DirectInvocationRequest.newBuilder()
            .setCapability("test.stream")
            .setMethod("Stream")
            .build();

        Iterator<DirectStreamItem> iterator = client.invokeStream("binding-test", req, InvocationContext.empty());
        List<DirectStreamItem> items = new ArrayList<>();
        while (iterator.hasNext()) {
            items.add(iterator.next());
        }

        assertThat(items).hasSize(4); // 3 payloads + 1 end
        assertThat(items.get(0).getPayload().getTypeUrl()).isEqualTo(AgentClient.TYPE_URL_EVENT);
        assertThat(items.get(3).hasEnd()).isTrue();
    }

    @Test
    void testHealthCheck_T42_T50() {
        HealthResponse health = client.health("binding-test", HealthRequest.getDefaultInstance());
        assertThat(health.getStatus()).isEqualTo(HealthResponse.Status.STATUS_SERVING);
        assertThat(health.getPluginId()).isEqualTo("cyrene.test.plugin");
        assertThat(health.getCapabilitiesList()).contains("agent.runtime.v1", "memory.provider.v1");
    }

    @Test
    void testTypedAgentClient_T43() {
        AgentClient agentClient = new AgentClient(client);

        AgentRunRequest runReq = AgentRunRequest.newBuilder()
            .setPrompt("Solve task")
            .build();

        AgentRunResponse resp = agentClient.run("binding-test", runReq, InvocationContext.empty());
        assertThat(resp.hasSuccess()).isTrue();
        assertThat(resp.getSuccess().getOutputText()).isEqualTo("Agent completed: Run");

        // Streaming
        Iterator<AgentStreamEvent> stream = agentClient.runStream("binding-test", runReq, InvocationContext.empty());
        List<AgentStreamEvent> events = new ArrayList<>();
        while (stream.hasNext()) {
            events.add(stream.next());
        }
        assertThat(events).hasSize(3);
        assertThat(events.get(0).getContentDelta().getTextDelta()).isEqualTo("chunk-0");
        assertThat(events.get(1).getContentDelta().getTextDelta()).isEqualTo("chunk-1");
        assertThat(events.get(2).getContentDelta().getTextDelta()).isEqualTo("chunk-2");
    }

    @Test
    void testTypedMemoryClient_T43() {
        MemoryClient memoryClient = new MemoryClient(client);

        StoreMemoryRequest storeReq = StoreMemoryRequest.newBuilder()
            .setTenantId("tenant-001")
            .setItem(MemoryItem.newBuilder().setContent("Remember this").build())
            .build();

        StoreMemoryResponse resp = memoryClient.store("binding-test", storeReq, InvocationContext.empty());
        assertThat(resp.getItemId()).isEqualTo("mem-001");
    }

    @Test
    void testPlatformProjectionBindingResolver_T46() {
        PlatformProjectionBindingResolver resolver = new PlatformProjectionBindingResolver(bindingId ->
            new PlatformProjectionBindingResolver.ProjectionRecord(
                "direct://127.0.0.1:" + port,
                "gen-platform-99",
                null
            )
        );

        DirectPluginRuntimeClient platformClient = new DirectPluginRuntimeClient(
            resolver, channelManager, CyreneInvocationPolicy.defaultPolicy()
        );

        HealthResponse health = platformClient.health("binding-platform", HealthRequest.getDefaultInstance());
        assertThat(health.getStatus()).isEqualTo(HealthResponse.Status.STATUS_SERVING);
        assertThat(channelManager.hasActiveChannel("binding-platform", io.cyrene.plugin.client.spi.RuntimeGeneration.of("gen-platform-99"))).isTrue();
    }

    @Test
    void testR05_typeUrlAndInterfaceVersionUnification() {
        // R05: Verify all typed clients strictly use "1" and "type.cyrene.io/"
        assertThat(io.cyrene.plugin.client.typed.AgentClient.INTERFACE_VERSION).isEqualTo("1");
        assertThat(io.cyrene.plugin.client.typed.AgentClient.TYPE_URL_RUN_REQUEST)
            .isEqualTo("type.cyrene.io/cyrene.agent.runtime.v1.AgentRunRequest");
        assertThat(io.cyrene.plugin.client.typed.AgentClient.TYPE_URL_EVENT)
            .isEqualTo("type.cyrene.io/cyrene.agent.runtime.v1.AgentStreamEvent");

        assertThat(io.cyrene.plugin.client.typed.MemoryClient.INTERFACE_VERSION).isEqualTo("1");
        assertThat(io.cyrene.plugin.client.typed.MemoryClient.TYPE_URL_STORE_REQUEST)
            .isEqualTo("type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryRequest");
        assertThat(io.cyrene.plugin.client.typed.MemoryClient.TYPE_URL_GET_REQUEST)
            .isEqualTo("type.cyrene.io/cyrene.memory.provider.v1.GetMemoryRequest");

        assertThat(io.cyrene.plugin.client.typed.ModelClient.INTERFACE_VERSION).isEqualTo("1");
        assertThat(io.cyrene.plugin.client.typed.ModelClient.TYPE_URL_CHAT_COMPLETION_REQUEST)
            .isEqualTo("type.cyrene.io/cyrene.model.provider.v1.ChatCompletionRequest");
        assertThat(io.cyrene.plugin.client.typed.ModelClient.TYPE_URL_EMBEDDINGS_REQUEST)
            .isEqualTo("type.cyrene.io/cyrene.model.provider.v1.EmbeddingsRequest");

        assertThat(io.cyrene.plugin.client.typed.ConnectorClient.INTERFACE_VERSION).isEqualTo("1");
        assertThat(io.cyrene.plugin.client.typed.ConnectorClient.TYPE_URL_SEND_MESSAGE)
            .isEqualTo("type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest");

        assertThat(io.cyrene.plugin.client.typed.ToolClient.INTERFACE_VERSION).isEqualTo("1");
        assertThat(io.cyrene.plugin.client.typed.ToolClient.TYPE_URL_EXECUTE_TOOL)
            .isEqualTo("type.cyrene.io/cyrene.tool.provider.v1.ExecuteToolRequest");

        assertThat(io.cyrene.plugin.client.typed.ComputerClient.INTERFACE_VERSION).isEqualTo("1");
        assertThat(io.cyrene.plugin.client.typed.ComputerClient.TYPE_URL_COMMAND)
            .isEqualTo("type.cyrene.io/cyrene.computer.runtime.v1.CommandExecutionRequest");
    }
}
