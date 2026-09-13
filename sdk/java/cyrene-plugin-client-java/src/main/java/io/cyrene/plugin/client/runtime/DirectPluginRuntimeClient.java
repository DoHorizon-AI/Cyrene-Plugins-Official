// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 DirectPluginRuntimeClient.java                                  │
// │  Package: io.cyrene.plugin.client.runtime                           │
// │  Role: High-level client for DirectPluginRuntime (T41, T42).        │
// │                                                                     │
// │  模块职责：框架无关的插件调用核心客户端，支持阻塞、异步与 Future 模式 │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.runtime;

import com.google.common.util.concurrent.FutureCallback;
import com.google.common.util.concurrent.Futures;
import com.google.common.util.concurrent.ListenableFuture;
import com.google.common.util.concurrent.MoreExecutors;
import io.cyrene.plugin.client.channel.CyreneChannelManager;
import io.cyrene.plugin.client.context.InvocationContext;
import io.cyrene.plugin.client.policy.CyreneInvocationPolicy;
import io.cyrene.plugin.client.spi.BindingResolution;
import io.cyrene.plugin.client.spi.CyreneBindingResolver;
import io.cyrene.plugin.runtime.v1.DirectInvocationRequest;
import io.cyrene.plugin.runtime.v1.DirectInvocationResponse;
import io.cyrene.plugin.runtime.v1.DirectPluginRuntimeGrpc;
import io.cyrene.plugin.runtime.v1.DirectStreamItem;
import io.cyrene.plugin.runtime.v1.HealthRequest;
import io.cyrene.plugin.runtime.v1.HealthResponse;
import io.grpc.ManagedChannel;
import io.grpc.stub.StreamObserver;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Iterator;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Framework-neutral gRPC client for Cyrene DirectPluginRuntime (T41, T42).
 *
 * <p>核心契约：</p>
 * <ul>
 *   <li><b>T41</b>: 独立于 Spring 框架的纯粹客户端库。</li>
 *   <li><b>T42</b>: 提供 async、blocking 和 future 三种风格存根接口。</li>
 *   <li><b>T50</b>: 严禁隐式业务重试；健康检查具备明确幂等重试保护。</li>
 *   <li><b>T51</b>: 完整透传 Deadline、协作取消、Request ID 与 Trace Context。</li>
 * </ul>
 * ════════════════════════════════════════════════════════════════════════
 */
public class DirectPluginRuntimeClient implements AutoCloseable {

    private static final Logger log = LoggerFactory.getLogger(DirectPluginRuntimeClient.class);

    private final CyreneBindingResolver bindingResolver;
    private final CyreneChannelManager channelManager;
    private final CyreneInvocationPolicy invocationPolicy;

    public DirectPluginRuntimeClient(
        CyreneBindingResolver bindingResolver,
        CyreneChannelManager channelManager,
        CyreneInvocationPolicy invocationPolicy
    ) {
        this.bindingResolver = Objects.requireNonNull(bindingResolver, "bindingResolver must not be null");
        this.channelManager = Objects.requireNonNull(channelManager, "channelManager must not be null");
        this.invocationPolicy = invocationPolicy != null ? invocationPolicy : CyreneInvocationPolicy.defaultPolicy();
    }

    public CyreneBindingResolver getBindingResolver() {
        return bindingResolver;
    }

    public CyreneChannelManager getChannelManager() {
        return channelManager;
    }

    public CyreneInvocationPolicy getInvocationPolicy() {
        return invocationPolicy;
    }

    // ── 1. Blocking Invocations ──────────────────────────────────────────

    public DirectInvocationResponse invoke(
        String bindingId,
        DirectInvocationRequest request,
        InvocationContext context
    ) {
        invocationPolicy.assertNoHiddenBusinessRetry("Invoke");
        BindingResolution resolution = bindingResolver.resolve(bindingId);
        ManagedChannel channel = channelManager.getOrCreateChannel(resolution);

        DirectPluginRuntimeGrpc.DirectPluginRuntimeBlockingStub stub =
            DirectPluginRuntimeGrpc.newBlockingStub(channel);
        if (context != null) {
            stub = context.applyToStub(stub);
        }

        return stub.invoke(request);
    }

    public Iterator<DirectStreamItem> invokeStream(
        String bindingId,
        DirectInvocationRequest request,
        InvocationContext context
    ) {
        invocationPolicy.assertNoHiddenBusinessRetry("InvokeStream");
        BindingResolution resolution = bindingResolver.resolve(bindingId);
        ManagedChannel channel = channelManager.getOrCreateChannel(resolution);

        DirectPluginRuntimeGrpc.DirectPluginRuntimeBlockingStub stub =
            DirectPluginRuntimeGrpc.newBlockingStub(channel);
        if (context != null) {
            stub = context.applyToStub(stub);
        }

        return stub.invokeStream(request);
    }

    // ── 2. Future Invocations ────────────────────────────────────────────

    public CompletableFuture<DirectInvocationResponse> invokeFuture(
        String bindingId,
        DirectInvocationRequest request,
        InvocationContext context
    ) {
        invocationPolicy.assertNoHiddenBusinessRetry("InvokeFuture");
        BindingResolution resolution = bindingResolver.resolve(bindingId);
        ManagedChannel channel = channelManager.getOrCreateChannel(resolution);

        DirectPluginRuntimeGrpc.DirectPluginRuntimeFutureStub stub =
            DirectPluginRuntimeGrpc.newFutureStub(channel);
        if (context != null) {
            stub = context.applyToStub(stub);
        }

        ListenableFuture<DirectInvocationResponse> lf = stub.invoke(request);
        CompletableFuture<DirectInvocationResponse> cf = new CompletableFuture<>();
        Futures.addCallback(lf, new FutureCallback<>() {
            @Override
            public void onSuccess(DirectInvocationResponse result) {
                cf.complete(result);
            }

            @Override
            public void onFailure(Throwable t) {
                cf.completeExceptionally(t);
            }
        }, MoreExecutors.directExecutor());

        return cf;
    }

    // ── 3. Async Invocations ─────────────────────────────────────────────

    public void invokeAsync(
        String bindingId,
        DirectInvocationRequest request,
        InvocationContext context,
        StreamObserver<DirectInvocationResponse> responseObserver
    ) {
        invocationPolicy.assertNoHiddenBusinessRetry("InvokeAsync");
        BindingResolution resolution = bindingResolver.resolve(bindingId);
        ManagedChannel channel = channelManager.getOrCreateChannel(resolution);

        DirectPluginRuntimeGrpc.DirectPluginRuntimeStub stub =
            DirectPluginRuntimeGrpc.newStub(channel);
        if (context != null) {
            stub = context.applyToStub(stub);
        }

        stub.invoke(request, responseObserver);
    }

    public void invokeStreamAsync(
        String bindingId,
        DirectInvocationRequest request,
        InvocationContext context,
        StreamObserver<DirectStreamItem> itemObserver
    ) {
        invocationPolicy.assertNoHiddenBusinessRetry("InvokeStreamAsync");
        BindingResolution resolution = bindingResolver.resolve(bindingId);
        ManagedChannel channel = channelManager.getOrCreateChannel(resolution);

        DirectPluginRuntimeGrpc.DirectPluginRuntimeStub stub =
            DirectPluginRuntimeGrpc.newStub(channel);
        if (context != null) {
            stub = context.applyToStub(stub);
        }

        stub.invokeStream(request, itemObserver);
    }

    // ── 4. Idempotent Health Check (T50) ─────────────────────────────────

    public HealthResponse health(String bindingId, HealthRequest request) {
        try {
            return invocationPolicy.executeIdempotentWithRetry("Health", () -> {
                BindingResolution resolution = bindingResolver.resolve(bindingId);
                ManagedChannel channel = channelManager.getOrCreateChannel(resolution);
                DirectPluginRuntimeGrpc.DirectPluginRuntimeBlockingStub stub =
                    DirectPluginRuntimeGrpc.newBlockingStub(channel);
                return stub.health(request != null ? request : HealthRequest.getDefaultInstance());
            });
        } catch (Exception e) {
            if (e instanceof RuntimeException re) throw re;
            throw new RuntimeException("Health check failed for binding: " + bindingId, e);
        }
    }

    @Override
    public void close() {
        channelManager.close();
    }
}
