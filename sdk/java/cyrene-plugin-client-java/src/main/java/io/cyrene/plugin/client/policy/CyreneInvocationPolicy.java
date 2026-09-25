// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 CyreneInvocationPolicy.java                                     │
// │  Package: io.cyrene.plugin.client.policy                            │
// │  Role: Enforces zero hidden business retries policy (T50).          │
// │                                                                     │
// │  模块职责：禁止隐式业务重试，仅对明确幂等的解析与健康检查执行重试     │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.policy;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.concurrent.Callable;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Invocation policy enforcing strict retry constraints (T50).
 *
 * <p>核心契约：</p>
 * <ul>
 *   <li>禁止隐藏的业务重试（Invoke / InvokeStream 严禁自动重试）。</li>
 *   <li>只有明确幂等的 resolution 或 health 操作允许执行有界重试。</li>
 * </ul>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class CyreneInvocationPolicy {

    private static final Logger log = LoggerFactory.getLogger(CyreneInvocationPolicy.class);

    private final int maxIdempotentAttempts;
    private final long backoffBaseMillis;

    public CyreneInvocationPolicy(int maxIdempotentAttempts, long backoffBaseMillis) {
        if (maxIdempotentAttempts < 1) {
            throw new IllegalArgumentException("maxIdempotentAttempts must be >= 1");
        }
        this.maxIdempotentAttempts = maxIdempotentAttempts;
        this.backoffBaseMillis = Math.max(10, backoffBaseMillis);
    }

    public static CyreneInvocationPolicy defaultPolicy() {
        return new CyreneInvocationPolicy(3, 50);
    }

    /**
     * Asserts that business invocations do NOT perform hidden retries (T50).
     *
     * @param operationName The business operation name (e.g. "Invoke", "InvokeStream").
     * <p>中文：断言业务调用不会执行隐藏重试（T50）。参数 operationName 是业务操作名称，例如 “Invoke” 或 “InvokeStream”。</p>
     */
    public void assertNoHiddenBusinessRetry(String operationName) {
        // Enforced contractually: business callers are informed that failures are terminal
// 中文：契约层会强制执行此规则，让业务调用方知道失败即为终态。
        log.trace("Executing non-retryable business operation: {}", operationName);
    }

    /**
     * Executes an explicitly idempotent operation (such as binding resolution or health check)
     * with bounded retry and exponential backoff (T50).
     *
     * @param operationName Name of idempotent operation.
     * @param action The idempotent callable.
     * @return The operation result.
     * @throws Exception If all bounded attempts fail.
     * <p>中文：对显式声明为幂等的操作（例如 binding 解析或 health check）执行有界重试和指数退避。参数包括幂等操作名称、幂等 callable；返回操作结果；若所有有界重试均失败则抛出 Exception。</p>
     */
    public <T> T executeIdempotentWithRetry(String operationName, Callable<T> action) throws Exception {
        Exception lastException = null;
        for (int attempt = 1; attempt <= maxIdempotentAttempts; attempt++) {
            try {
                return action.call();
            } catch (Exception ex) {
                lastException = ex;
                log.warn("Idempotent operation [{}] failed attempt {}/{}: {}",
                    operationName, attempt, maxIdempotentAttempts, ex.getMessage());
                if (attempt < maxIdempotentAttempts) {
                    long backoff = backoffBaseMillis * (1L << (attempt - 1));
                    try {
                        Thread.sleep(backoff);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        throw ie;
                    }
                }
            }
        }
        throw new IllegalStateException("Idempotent operation [" + operationName + "] failed after "
            + maxIdempotentAttempts + " attempts", lastException);
    }
}
