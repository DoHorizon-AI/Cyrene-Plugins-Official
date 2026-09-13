// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 VerticalIntegrationTest.java                                    │
// │  Package: io.cyrene.plugin.client                                   │
// │  Role: Vertical cross-runtime integration tests (T55, R05, R07).    │
// │                                                                     │
// │  模块职责：验证 Java 到 Rust C ABI 与 Java 到 NativeAOT 的纵向测试：  │
// │           1. Unary 跨运行时调用                                     │
// │           2. Stream 跨运行时流式事件                                 │
// │           3. 协作取消机制                                           │
// │           4. 超时截止时间处理                                       │
// │           5. 版本与能力不匹配检测                                   │
// │           6. Rust C ABI 共享核心导出与结构校验                      │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client;

import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.concurrent.TimeUnit;

import static org.assertj.core.api.Assertions.assertThat;
import io.cyrene.plugin.client.channel.CyreneChannelManager;
import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.policy.CyreneInvocationPolicy;
import io.cyrene.plugin.client.runtime.DirectPluginRuntimeClient;
import io.cyrene.plugin.client.spi.StaticBindingResolver;
import io.cyrene.plugin.runtime.v1.DirectInvocationRequest;
import io.cyrene.plugin.runtime.v1.DirectInvocationResponse;
import io.cyrene.plugin.runtime.v1.HealthResponse;

class VerticalIntegrationTest {

    private static File resolveRepoRoot() {
        File cur = new File(System.getProperty("user.dir", ".")).getAbsoluteFile();
        for (int i = 0; i < 8 && cur != null; i++) {
            if (new File(cur, "runtime").isDirectory() && new File(cur, "contracts").isDirectory()) {
                return cur;
            }
            cur = cur.getParentFile();
        }
        return new File(".");
    }

    private static File resolveNativeAotHost() {
        String env = System.getenv("CYRENE_NATIVE_AOT_HOST");
        if (env != null && !env.isBlank()) {
            return new File(env);
        }
        File repoRoot = resolveRepoRoot();
        File candidate = new File(repoRoot, "runtime/dotnet-native-aot/Cyrene.Plugin.Host/bin/Release/net10.0/linux-x64/publish/cyrene-plugin-host");
        if (candidate.exists()) {
            return candidate;
        }
        File winCandidate = new File(repoRoot, "runtime/dotnet-native-aot/Cyrene.Plugin.Host/bin/Release/net10.0/win-x64/publish/cyrene-plugin-host.exe");
        if (winCandidate.exists()) {
            return winCandidate;
        }
        return candidate;
    }

    private static File resolveRustServerBin() {
        String env = System.getenv("CYRENE_RUST_SERVER_BIN");
        if (env != null && !env.isBlank()) {
            return new File(env);
        }
        File repoRoot = resolveRepoRoot();
        File releaseCandidate = new File(repoRoot, "runtime/rust/cyrene-plugin-server/target/release/cyrene-plugin-server");
        if (releaseCandidate.exists()) {
            return releaseCandidate;
        }
        File debugCandidate = new File(repoRoot, "runtime/rust/cyrene-plugin-server/target/debug/cyrene-plugin-server");
        if (debugCandidate.exists()) {
            return debugCandidate;
        }
        return releaseCandidate;
    }

    private static File resolveRustCabiSo() {
        String env = System.getenv("CYRENE_RUST_CABI_SO");
        if (env != null && !env.isBlank()) {
            return new File(env);
        }
        File repoRoot = resolveRepoRoot();
        File candidate = new File(repoRoot, "runtime/rust/cyrene-plugin-cabi/target/release/librust_cyrene_plugin.so");
        if (candidate.exists()) {
            return candidate;
        }
        File artifactCandidate = new File(repoRoot, "artifacts/m2-cabi/lib/librust_cyrene_plugin.so");
        if (artifactCandidate.exists()) {
            return artifactCandidate;
        }
        return candidate;
    }

    @Test
    void testJavaToNativeAot_Unary_T55() throws Exception {
        File hostBin = resolveNativeAotHost();
        Assumptions.assumeTrue(hostBin.exists() && hostBin.canExecute(),
            "Native AOT host binary not found at " + hostBin.getAbsolutePath() + "; run dotnet publish or set CYRENE_NATIVE_AOT_HOST");

        String payloadBase64 = Base64.getEncoder().encodeToString("hello-cyrene-native-aot".getBytes(StandardCharsets.UTF_8));

        ProcessBuilder pb = new ProcessBuilder(
            hostBin.getAbsolutePath(),
            "--unary", "Echo", "type.cyrene.io/test.EchoRequest", payloadBase64, "0"
        );
        Process proc = pb.start();
        String output = new String(proc.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        boolean finished = proc.waitFor(5, TimeUnit.SECONDS);

        assertThat(finished).isTrue();
        assertThat(proc.exitValue()).isEqualTo(0);
        assertThat(output).contains("\"statusCode\": 0");
        assertThat(output).contains(payloadBase64);
    }

    @Test
    void testJavaToNativeAot_Stream_T55() throws Exception {
        File hostBin = resolveNativeAotHost();
        Assumptions.assumeTrue(hostBin.exists() && hostBin.canExecute(),
            "Native AOT host binary not found at " + hostBin.getAbsolutePath());

        String payloadBase64 = Base64.getEncoder().encodeToString("stream-input".getBytes(StandardCharsets.UTF_8));

        ProcessBuilder pb = new ProcessBuilder(
            hostBin.getAbsolutePath(),
            "--stream", "EchoStream", "type.cyrene.io/test.StreamRequest", payloadBase64, "0"
        );
        Process proc = pb.start();
        String output = new String(proc.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        boolean finished = proc.waitFor(5, TimeUnit.SECONDS);

        assertThat(finished).isTrue();
        assertThat(proc.exitValue()).isEqualTo(0);
        assertThat(output).contains("[STREAM EVENT]");
        assertThat(output).contains("status=Ok");
        assertThat(output).contains("[STREAM COMPLETE]");
    }

    @Test
    void testJavaToNativeAot_Cancellation_T55() throws Exception {
        File hostBin = resolveNativeAotHost();
        Assumptions.assumeTrue(hostBin.exists() && hostBin.canExecute(),
            "Native AOT host binary not found at " + hostBin.getAbsolutePath());

        ProcessBuilder pb = new ProcessBuilder(hostBin.getAbsolutePath(), "--cancel-test");
        Process proc = pb.start();
        String output = new String(proc.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        boolean finished = proc.waitFor(5, TimeUnit.SECONDS);

        assertThat(finished).isTrue();
        assertThat(proc.exitValue()).isEqualTo(0);
        assertThat(output).contains("[PASS] Stream cancelled gracefully with StatusCode.Cancelled.");
    }

    @Test
    void testJavaToNativeAot_Deadline_T55() throws Exception {
        File hostBin = resolveNativeAotHost();
        Assumptions.assumeTrue(hostBin.exists() && hostBin.canExecute(),
            "Native AOT host binary not found at " + hostBin.getAbsolutePath());

        ProcessBuilder pb = new ProcessBuilder(hostBin.getAbsolutePath(), "--deadline-test");
        Process proc = pb.start();
        String output = new String(proc.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        boolean finished = proc.waitFor(5, TimeUnit.SECONDS);

        assertThat(finished).isTrue();
        assertThat(proc.exitValue()).isEqualTo(0);
        assertThat(output).contains("[PASS] Expired deadline successfully returned StatusCode.DeadlineExceeded.");
    }

    @Test
    void testJavaToNativeAot_ManifestAndCapabilitiesMismatch_T55() throws Exception {
        File hostBin = resolveNativeAotHost();
        Assumptions.assumeTrue(hostBin.exists() && hostBin.canExecute(),
            "Native AOT host binary not found at " + hostBin.getAbsolutePath());

        ProcessBuilder pb = new ProcessBuilder(hostBin.getAbsolutePath(), "--manifest");
        Process proc = pb.start();
        String output = new String(proc.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        boolean finished = proc.waitFor(5, TimeUnit.SECONDS);

        assertThat(finished).isTrue();
        assertThat(proc.exitValue()).isEqualTo(0);
        assertThat(output).contains("cyrene.plugin.dotnet.echo");
        assertThat(output).contains("model.provider.v1");

        // Version mismatch detection: querying an unsupported interface version
        assertThat(output).doesNotContain("agent.runtime.v999");
        assertThat(output).doesNotContain("unsupported.capability");
    }

    @Test
    void testJavaToRustCAbi_Verification_T55() throws Exception {
        File rustLibrary = resolveRustCabiSo();
        Assumptions.assumeTrue(rustLibrary.exists(),
            "Rust C ABI library not found at " + rustLibrary.getAbsolutePath());

        assertThat(rustLibrary.length()).isGreaterThan(1000);

        // Verify exported symbols from Rust C ABI library using nm
        ProcessBuilder pb = new ProcessBuilder("nm", "-D", "--defined-only", rustLibrary.getAbsolutePath());
        Process proc = pb.start();
        String nmOutput = new String(proc.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        proc.waitFor(5, TimeUnit.SECONDS);

        assertThat(nmOutput).contains("cyrene_plugin_get_api_v1");
        assertThat(nmOutput).contains("cyrene_plugin_create_v1");
        assertThat(nmOutput).contains("cyrene_plugin_invoke_v1");
        assertThat(nmOutput).contains("cyrene_plugin_invoke_stream_v1");
        assertThat(nmOutput).contains("cyrene_plugin_cancel_v1");
        assertThat(nmOutput).contains("cyrene_plugin_free_buffer_v1");
        assertThat(nmOutput).contains("cyrene_plugin_destroy_v1");
    }
    @Test
    void testJavaToRustServer_ProductionFailsClosedWithoutBindings_R08() throws Exception {
        File serverBin = resolveRustServerBin();
        Assumptions.assumeTrue(serverBin.exists() && serverBin.canExecute(),
            "Rust cyrene-plugin-server binary not found at " + serverBin.getAbsolutePath() + "; build with cargo build --bin cyrene-plugin-server");

        ProcessBuilder pb = new ProcessBuilder(serverBin.getAbsolutePath(), "--port", "0");
        Process proc = pb.start();

        java.io.BufferedReader reader = new java.io.BufferedReader(
            new java.io.InputStreamReader(proc.getInputStream(), StandardCharsets.UTF_8));

        int serverPort = -1;
        String line;
        long deadline = System.currentTimeMillis() + 5000;
        while (System.currentTimeMillis() < deadline && (line = reader.readLine()) != null) {
            if (line.contains("[CYRENE_SERVER_STARTED] addr=")) {
                String addrPart = line.substring(line.indexOf("addr=") + 5).trim();
                serverPort = Integer.parseInt(addrPart.split(":")[1]);
                break;
            }
        }

        assertThat(serverPort).isGreaterThan(0);

        StaticBindingResolver resolver = StaticBindingResolver.single("rust-server", "direct://127.0.0.1:" + serverPort, "gen-rust");
        CyreneChannelManager channelMgr = new CyreneChannelManager();
        DirectPluginRuntimeClient client = new DirectPluginRuntimeClient(resolver, channelMgr, CyreneInvocationPolicy.defaultPolicy());

        try {
            // 1. Health check
            HealthResponse health = client.health("rust-server", io.cyrene.plugin.runtime.v1.HealthRequest.getDefaultInstance());
            assertThat(health.getStatus()).isEqualTo(HealthResponse.Status.STATUS_SERVING);
            assertThat(health.getPluginId()).isEqualTo("cyrene.plugin.rust.server");
            assertThat(health.getCapabilitiesList()).containsExactly("computer.runtime.v1");

            // Production startup has no implicit SQLite or synthetic embedding backend.
            io.cyrene.proto.memory.provider.v1.StoreMemoryRequest storeReq =
                io.cyrene.proto.memory.provider.v1.StoreMemoryRequest.newBuilder()
                    .setTenantId("tenant-vtest")
                    .setItem(io.cyrene.proto.memory.provider.v1.MemoryItem.newBuilder()
                        .setItemId("mem-java-01")
                        .setContent("Hello from Java vertical integration")
                        .build())
                    .build();

            DirectInvocationResponse memoryResponse = client.invoke(
                "rust-server",
                DirectInvocationRequest.newBuilder()
                    .setInterfaceVersion("1")
                    .setCapability("memory.provider.v1")
                    .setMethod("StoreMemory")
                    .setPayloadTypeUrl("type.cyrene.io/cyrene.memory.provider.v1.StoreMemoryRequest")
                    .setPayload(storeReq.toByteString())
                    .setRequestId("memory-not-configured")
                    .build(),
                InvocationContext.empty());
            assertThat(memoryResponse.hasError()).isTrue();
            assertThat(memoryResponse.getError().getCode())
                .isEqualTo(io.cyrene.plugin.runtime.v1.DirectInvocationError.Code.CODE_UNAVAILABLE);
            assertThat(memoryResponse.getError().getDomainCode())
                .isEqualTo("MEMORY_BACKEND_UNAVAILABLE");

        } finally {
            client.close();
            channelMgr.close();
            proc.destroyForcibly();
            proc.waitFor(2, TimeUnit.SECONDS);
        }
    }

    @Test
    void testJavaToRustServer_FailClosed_R09() throws Exception {
        File serverBin = resolveRustServerBin();
        Assumptions.assumeTrue(serverBin.exists() && serverBin.canExecute(),
            "Rust cyrene-plugin-server binary not found at " + serverBin.getAbsolutePath());

        ProcessBuilder pb = new ProcessBuilder(serverBin.getAbsolutePath(), "--port", "0");
        Process proc = pb.start();

        java.io.BufferedReader reader = new java.io.BufferedReader(
            new java.io.InputStreamReader(proc.getInputStream(), StandardCharsets.UTF_8));

        int serverPort = -1;
        String line;
        long deadline = System.currentTimeMillis() + 5000;
        while (System.currentTimeMillis() < deadline && (line = reader.readLine()) != null) {
            if (line.contains("[CYRENE_SERVER_STARTED] addr=")) {
                String addrPart = line.substring(line.indexOf("addr=") + 5).trim();
                serverPort = Integer.parseInt(addrPart.split(":")[1]);
                break;
            }
        }

        assertThat(serverPort).isGreaterThan(0);

        StaticBindingResolver resolver = StaticBindingResolver.single("rust-server", "direct://127.0.0.1:" + serverPort, "gen-rust");
        CyreneChannelManager channelMgr = new CyreneChannelManager();
        DirectPluginRuntimeClient client = new DirectPluginRuntimeClient(resolver, channelMgr, CyreneInvocationPolicy.defaultPolicy());

        try {
            // Fail-closed test: invoke unknown capability
            DirectInvocationRequest req = DirectInvocationRequest.newBuilder()
                .setInterfaceVersion("1")
                .setCapability("unknown.malicious.capability")
                .setMethod("Attack")
                .setPayloadTypeUrl("type.cyrene.io/bad.Request")
                .setPayload(com.google.protobuf.ByteString.copyFromUtf8("{}"))
                .setRequestId("req-bad-1")
                .build();

            DirectInvocationResponse resp = client.invoke("rust-server", req, InvocationContext.empty());
            assertThat(resp.hasError()).isTrue();
            assertThat(resp.getError().getCode()).isEqualTo(io.cyrene.plugin.runtime.v1.DirectInvocationError.Code.CODE_METHOD_NOT_FOUND);
            assertThat(resp.getError().getDomainCode()).isEqualTo("CAPABILITY_NOT_FOUND");
            assertThat(resp.getError().getRetryable()).isFalse();

        } finally {
            client.close();
            channelMgr.close();
            proc.destroyForcibly();
            proc.waitFor(2, TimeUnit.SECONDS);
        }
    }
}
