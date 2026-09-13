// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CancellationToken.java                                          │
// │  Package: io.cyrene.plugin.client.context                           │
// │  Role: Thread-safe cancellation token for invocations (T51).        │
// │                                                                     │
// │  模块职责：线程安全的取消令牌，支持协作式主动取消                    │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.context;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Thread-safe cooperative cancellation token (T51).
 *
 * <p>用于向底层 gRPC 通道与上层调用链路传递主动取消信号。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class CancellationToken {

    private final AtomicBoolean cancelled = new AtomicBoolean(false);
    private final List<Runnable> callbacks = new CopyOnWriteArrayList<>();

    public boolean isCancelled() {
        return cancelled.get();
    }

    public void cancel() {
        if (cancelled.compareAndSet(false, true)) {
            for (Runnable callback : callbacks) {
                try {
                    callback.run();
                } catch (Throwable ignored) {
                }
            }
        }
    }

    public void onCancel(Runnable callback) {
        if (callback == null) return;
        if (cancelled.get()) {
            callback.run();
        } else {
            callbacks.add(callback);
        }
    }
}
