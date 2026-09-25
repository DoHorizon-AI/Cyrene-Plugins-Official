// ┌─────────────────────────────────────────────────────────────────────┐
// │  📄 ConnectionRef.java                                              │
// │  Package: io.cyrene.plugin.client.spi                               │
// │  Role: Connection reference and transport security validator (T49). │
// │                                                                     │
// │  模块职责：连接引用封装，严格校验 Loopback/本地 Socket 与 TLS 策略   │
// └─────────────────────────────────────────────────────────────────────┘

package io.cyrene.plugin.client.spi;

import java.net.URI;
import java.util.Objects;
import java.util.Set;

/**
 * ════════════════════════════════════════════════════════════════════════
 * Immutable connection reference pointing to a resolved plugin endpoint.
 *
 * <p>封装插件 endpoint URI。严禁对远端 endpoint 采用 insecure 明文传输；
 * 仅允许经过验证的 loopback (127.0.0.1, localhost, ::1) 或 local socket
 * (unix domain socket) 使用 insecure/plaintext 通道 (T49)。</p>
 * ════════════════════════════════════════════════════════════════════════
 */
public final class ConnectionRef {

    private static final Set<String> LOOPBACK_HOSTS = Set.of(
        "127.0.0.1",
        "localhost",
        "::1",
        "0:0:0:0:0:0:0:1"
    );

    private final URI targetUri;
    private final boolean loopbackOrLocal;

    public ConnectionRef(String rawUri) {
        Objects.requireNonNull(rawUri, "rawUri must not be null");
        try {
            this.targetUri = URI.create(rawUri.trim());
        } catch (Exception e) {
            throw new IllegalArgumentException("Invalid connection URI: " + rawUri, e);
        }
        this.loopbackOrLocal = computeLoopbackOrLocal(this.targetUri);
    }

    public static ConnectionRef of(String rawUri) {
        return new ConnectionRef(rawUri);
    }

    private static boolean computeLoopbackOrLocal(URI uri) {
        String scheme = uri.getScheme() != null ? uri.getScheme().toLowerCase() : "";
        if ("unix".equals(scheme) || "pipe".equals(scheme)) {
            return true;
        }
        String host = uri.getHost();
        if (host != null) {
            String lowerHost = host.toLowerCase();
            return LOOPBACK_HOSTS.contains(lowerHost);
        }
        return false;
    }

    public URI getTargetUri() {
        return targetUri;
    }

    public String getScheme() {
        return targetUri.getScheme() != null ? targetUri.getScheme() : "direct";
    }

    public String getHost() {
        return targetUri.getHost();
    }

    public int getPort() {
        return targetUri.getPort() > 0 ? targetUri.getPort() : 50051;
    }

    public String getPath() {
        return targetUri.getPath();
    }

    /**
     * Checks whether the target endpoint is verified loopback or local socket (T49).
     * <p>中文：检查目标 Endpoint 是否为已核验的 loopback 或本地套接字（T49）。</p>
     */
    public boolean isLoopbackOrLocal() {
        return loopbackOrLocal;
    }

    /**
     * Verifies transport security invariants (T49).
     *
     * @param useInsecure whether the caller requested insecure / plaintext transport.
     * @throws SecurityException if insecure transport is requested for a remote endpoint.
     * <p>中文：校验传输安全不变量（T49）。参数 useInsecure 表示调用方是否请求不安全／明文传输；如果远程 Endpoint 使用不安全传输，则抛出 SecurityException。</p>
     */
    public void validateTransportSecurity(boolean useInsecure) {
        if (useInsecure && !isLoopbackOrLocal()) {
            throw new SecurityException(
                "Security violation (T49): Insecure transport is prohibited for remote endpoint '"
                    + targetUri + "'. Remote endpoints must use TLS."
            );
        }
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        ConnectionRef that = (ConnectionRef) o;
        return Objects.equals(targetUri, that.targetUri);
    }

    @Override
    public int hashCode() {
        return Objects.hash(targetUri);
    }

    @Override
    public String toString() {
        return targetUri.toString();
    }
}
