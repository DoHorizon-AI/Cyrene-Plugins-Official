// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CyreneChannelManager.java                                       │
// │  Package: io.cyrene.plugin.client.channel                           │
// │  Role: Manages gRPC channels keyed by binding + generation (T47-49).│
// │                                                                     │
// │  模块职责：按 binding_id + 代际缓存 Channel，自动关闭陈旧 Channel，   │
// │           且对远端端点强制执行 TLS 安全防护                          │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.channel;

import io.cyrene.plugin.client.spi.BindingResolution;
import io.cyrene.plugin.client.spi.ConnectionRef;
import io.cyrene.plugin.client.spi.RuntimeGeneration;
import io.grpc.ManagedChannel;
import io.grpc.netty.shaded.io.grpc.netty.NettyChannelBuilder;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Map;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Manages gRPC channels for Cyrene Plugin invocations.
 *
 * <p>核心契约：</p>
 * <ul>
 *   <li><b>T47</b>: 按 {@code binding_id + runtime generation} 缓存 channel，不持久化 endpoint 字符串。</li>
 *   <li><b>T48</b>: activation、recovery、upgrade 或 rollback 改变 runtime generation 时关闭旧 channel。</li>
 *   <li><b>T49</b>: 只允许经过验证的 loopback 或本地 socket 使用 insecure endpoint；远端 endpoint 必须使用 TLS。</li>
 * </ul>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class CyreneChannelManager implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(CyreneChannelManager.class);

    public record ChannelKey(String bindingId, RuntimeGeneration generation) {
        public ChannelKey {
            Objects.requireNonNull(bindingId, "bindingId must not be null");
            Objects.requireNonNull(generation, "generation must not be null");
        }
    }

    private final Map<ChannelKey, ManagedChannel> channelCache = new ConcurrentHashMap<>();
    private final Map<String, RuntimeGeneration> latestGenerations = new ConcurrentHashMap<>();
    private volatile boolean closed = false;

    /**
     * Obtains or creates a {@link ManagedChannel} for the given binding resolution.
     *
     * @param resolution The resolved binding metadata.
     * @return An active gRPC ManagedChannel.
     * <p>中文：根据给定的 binding resolution 获取或创建 ManagedChannel。参数 resolution 是已解析的 binding 元数据；返回值是可用的 gRPC ManagedChannel。</p>
     */
    public ManagedChannel getOrCreateChannel(BindingResolution resolution) {
        if (closed) {
            throw new IllegalStateException("CyreneChannelManager is closed");
        }

        String bindingId = resolution.getBindingId();
        RuntimeGeneration newGeneration = resolution.getGeneration();
        ChannelKey key = new ChannelKey(bindingId, newGeneration);

        // ── T48: Detect generation change and close stale channels ────────────
// 中文：T48：检测代次变化并关闭过期 channel。
        RuntimeGeneration previousGeneration = latestGenerations.put(bindingId, newGeneration);
        if (previousGeneration != null && !previousGeneration.equals(newGeneration)) {
            log.info("Runtime generation change detected for [{}]: {} -> {}. Closing stale channel.",
                bindingId, previousGeneration, newGeneration);
            closeStaleChannel(new ChannelKey(bindingId, previousGeneration));
        }

        // ── T47: Cache by binding_id + runtime generation ─────────────────────
// 中文：T47：按 binding_id 和 Runtime 代次缓存。
        return channelCache.computeIfAbsent(key, k -> buildChannel(resolution));
    }

    private ManagedChannel buildChannel(BindingResolution resolution) {
        ConnectionRef conn = resolution.getConnectionRef();
        String host = conn.getHost() != null ? conn.getHost() : "127.0.0.1";
        int port = conn.getPort();

        log.debug("Building ManagedChannel for [{}] target={}:{}", resolution.getBindingId(), host, port);

        NettyChannelBuilder builder = NettyChannelBuilder.forAddress(host, port);

        // ── T49: Validate loopback vs TLS enforcement ─────────────────────────
// 中文：T49：校验 loopback 与 TLS 强制策略。
        if (conn.isLoopbackOrLocal()) {
            builder.usePlaintext();
            log.debug("Verified loopback or local socket for [{}], plaintext allowed (T49).", resolution.getBindingId());
        } else {
            // Remote endpoints MUST use TLS
// 中文：远程 Endpoint 必须使用 TLS。
            conn.validateTransportSecurity(false); // throws SecurityException if invalid | 中文：校验失败时抛出 SecurityException
            builder.useTransportSecurity();
            log.debug("Remote endpoint for [{}], TLS transport security enforced (T49).", resolution.getBindingId());
        }

        return builder.build();
    }

    private void closeStaleChannel(ChannelKey staleKey) {
        ManagedChannel channel = channelCache.remove(staleKey);
        if (channel != null && !channel.isShutdown()) {
            try {
                log.info("Gracefully shutting down stale channel for [{}] gen [{}]",
                    staleKey.bindingId(), staleKey.generation());
                channel.shutdown();
                if (!channel.awaitTermination(200, TimeUnit.MILLISECONDS)) {
                    channel.shutdownNow();
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                channel.shutdownNow();
            }
        }
    }

    public int getActiveChannelCount() {
        return channelCache.size();
    }

    public boolean hasActiveChannel(String bindingId, RuntimeGeneration generation) {
        return channelCache.containsKey(new ChannelKey(bindingId, generation));
    }

    @Override
    public void close() {
        if (closed) {
            return;
        }
        closed = true;
        log.info("Shutting down all active Cyrene channels (count={})", channelCache.size());
        for (Map.Entry<ChannelKey, ManagedChannel> entry : channelCache.entrySet()) {
            ManagedChannel ch = entry.getValue();
            if (!ch.isShutdown()) {
                ch.shutdownNow();
            }
        }
        channelCache.clear();
        latestGenerations.clear();
    }
}
