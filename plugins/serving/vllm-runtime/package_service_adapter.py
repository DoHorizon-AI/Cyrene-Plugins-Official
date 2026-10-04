"""Start the package's HTTP import service through the host lifecycle envelope.

The emitted ``direct_plugin_ready`` event is used only as the existing process
supervisor's startup handshake. The connection is HTTP and does not implement
the SDK's DirectPluginRuntime gRPC protocol.

中文：通过 Host 生命周期握手启动 package 自有 HTTP 导入服务。该握手只表示进程
就绪，不表示 SDK 的 DirectPluginRuntime gRPC 协议已实现。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from vllm_runtime import RuntimeHandler, ServingRuntime, _bounded_home, _token

PACKAGE_ID = "cyrene.serving.vllm-runtime"
CAPABILITY_ID = "execution.engine.v1"
INTERFACE_VERSION = "1"
HTTP_PROTOCOL = "cyrene.serving.runtime.http.v1"
CONNECTION_REF_SCHEME = "cyrene-http-v1"
HEALTH_TIMEOUT_SECONDS = 4.0
HEALTH_POLL_SECONDS = 0.05


class AdapterError(ValueError):
    """Raised when host activation or package metadata is invalid.

    中文：Host 激活参数或 package 元数据无效时抛出。
    """


def _read_manifest(path: Path) -> dict[str, object]:
    """Read the manifest installed beside this package's runtime code."""

    if path.is_symlink() or not path.is_file():
        raise AdapterError("SERVING_PACKAGE_MANIFEST_UNAVAILABLE")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AdapterError("SERVING_PACKAGE_MANIFEST_INVALID") from error
    if not isinstance(value, dict):
        raise AdapterError("SERVING_PACKAGE_MANIFEST_INVALID")
    return value


def package_metadata(manifest: Mapping[str, object]) -> dict[str, str]:
    """Validate the installed HTTP service identity and return its public metadata."""

    runtime = manifest.get("runtime")
    methods = manifest.get("methods")
    if (
        manifest.get("id") != PACKAGE_ID
        or manifest.get("version") != "0.1.0"
        or manifest.get("capabilities") != [CAPABILITY_ID]
        or not isinstance(runtime, dict)
        or runtime.get("language") != "python"
        or runtime.get("protocol") != HTTP_PROTOCOL
        or runtime.get("transport") != "http"
        or runtime.get("connectionRefScheme") != CONNECTION_REF_SCHEME
        or runtime.get("entrypoint") != "package_service_adapter:main"
        or runtime.get("launch")
        != {"executable": "prepared-runtime", "args": ["-B", "package_service_adapter.py"]}
        or not isinstance(methods, list)
        or not methods
        or any(
            not isinstance(method, dict)
            or method.get("interfaceVersion") != INTERFACE_VERSION
            or method.get("executionMode") != "service"
            for method in methods
        )
    ):
        raise AdapterError("SERVING_PACKAGE_CONTRACT_MISMATCH")
    return {
        "package_id": PACKAGE_ID,
        "package_version": "0.1.0",
        "capability": CAPABILITY_ID,
        "interface_version": INTERFACE_VERSION,
        "transport": "http",
    }


def encode_connection_ref(host: str, port: int) -> str:
    """Encode the one supported opaque HTTP handle for a bound loopback socket."""

    if host != "127.0.0.1" or not 1 <= port <= 65535:
        raise AdapterError("SERVING_CONNECTION_REF_INVALID")
    return f"{CONNECTION_REF_SCHEME}://{host}:{port}"


def decode_connection_ref(value: str) -> tuple[str, int]:
    """Decode only the canonical loopback HTTP handle emitted by this adapter."""

    if not isinstance(value, str) or len(value) > 128:
        raise AdapterError("SERVING_CONNECTION_REF_INVALID")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise AdapterError("SERVING_CONNECTION_REF_INVALID") from error
    if (
        parsed.scheme != CONNECTION_REF_SCHEME
        or parsed.netloc != f"127.0.0.1:{port}"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1 <= port <= 65535
    ):
        raise AdapterError("SERVING_CONNECTION_REF_INVALID")
    return "127.0.0.1", port


def _required_environment(environ: Mapping[str, str]) -> tuple[Path, Path, Path, str]:
    """Load deployment configuration only from the owner's activation environment."""

    names = (
        "CYRENE_VLLM_RUNTIME_HOME",
        "CYRENE_ARTIFACT_ROOT",
        "CYRENE_SERVING_CREDENTIAL_FILE",
        "CYRENE_SERVING_CONTROL_URL",
    )
    values = [environ.get(name, "") for name in names]
    if any(not value or "\n" in value or "\r" in value for value in values):
        raise AdapterError("SERVING_OWNER_CONFIGURATION_REQUIRED")
    runtime_home, artifact_root, credential_file = (Path(value) for value in values[:3])
    if any(not path.is_absolute() for path in (runtime_home, artifact_root, credential_file)):
        raise AdapterError("SERVING_OWNER_PATHS_MUST_BE_ABSOLUTE")
    if credential_file.is_symlink():
        raise AdapterError("SERVING_CREDENTIAL_INVALID")
    control_url = values[3]
    try:
        parsed = urlsplit(control_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise AdapterError("SERVING_CONTROL_URL_INVALID") from error
    loopback = host == "localhost"
    if host:
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
    if (
        parsed.scheme not in {"https", "http"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and not loopback)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise AdapterError("SERVING_CONTROL_URL_INVALID")
    return runtime_home, artifact_root, credential_file, control_url


def _parse_listen(value: str) -> tuple[str, int]:
    """Require the host supervisor's dynamic IPv4 loopback bind request."""

    host, separator, raw_port = value.rpartition(":")
    if not separator or host != "127.0.0.1" or not raw_port.isdecimal():
        raise AdapterError("SERVING_LISTEN_ADDRESS_INVALID")
    port = int(raw_port)
    if port != 0:
        raise AdapterError("SERVING_LISTEN_ADDRESS_INVALID")
    return host, port


def _probe_ready(server: ThreadingHTTPServer) -> None:
    """Wait for an actual HTTP health response before declaring activation ready."""

    host, port = server.server_address[:2]
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    request = urllib.request.Request(f"http://{host}:{port}/readyz", method="GET")
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=0.25) as response:
                body = json.loads(response.read(1024))
                if response.status == 200 and body == {"status": "ok"}:
                    return
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(HEALTH_POLL_SECONDS)
    raise AdapterError("SERVING_HTTP_HEALTH_CHECK_FAILED")


def start_service(
    arguments: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    manifest_path: Path | None = None,
) -> tuple[ThreadingHTTPServer, threading.Thread, dict[str, str]]:
    """Start the real model import HTTP service and return its verified endpoint."""

    parser = argparse.ArgumentParser(description="Cyrene vLLM package HTTP lifecycle adapter")
    parser.add_argument("--capability", required=True)
    parser.add_argument("--interface-version", required=True)
    parser.add_argument("--listen", required=True)
    options = parser.parse_args(arguments)
    if options.capability != CAPABILITY_ID or options.interface_version != INTERFACE_VERSION:
        raise AdapterError("SERVING_HOST_CONTRACT_MISMATCH")
    host, requested_port = _parse_listen(options.listen)
    source = os.environ if environ is None else environ
    runtime_home, artifact_root, credential_file, control_url = _required_environment(source)
    metadata = package_metadata(
        _read_manifest(manifest_path or Path(__file__).resolve().parent / "plugin.manifest.json")
    )
    home = _bounded_home(runtime_home)
    if stat.S_IMODE(credential_file.stat().st_mode) != 0o600:
        raise AdapterError("SERVING_CREDENTIAL_INVALID")
    token = _token(credential_file)
    if not artifact_root.exists() or not artifact_root.is_dir():
        raise AdapterError("SERVING_ARTIFACT_ROOT_UNAVAILABLE")
    RuntimeHandler.runtime = ServingRuntime(
        home=home,
        artifact_root=artifact_root.resolve(),
        command=["vllm", "serve"],
        control_url=control_url,
    )
    RuntimeHandler.token = token
    RuntimeHandler.package_metadata = metadata
    server = ThreadingHTTPServer((host, requested_port), RuntimeHandler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, name="vllm-package-http", daemon=True)
    worker.start()
    try:
        _probe_ready(server)
        actual_host, actual_port = server.server_address[:2]
        ready = {
            "event": "direct_plugin_ready",
            "capability": CAPABILITY_ID,
            "interfaceVersion": INTERFACE_VERSION,
            "connection_ref": encode_connection_ref(actual_host, actual_port),
        }
        return server, worker, ready
    except Exception:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1.0)
        raise


def main() -> int:
    """Run until the PackageRuntime supervisor terminates this process."""

    try:
        server, worker, ready = start_service()
    except (AdapterError, OSError, ValueError) as error:
        print(f"vllm-package-service-adapter: {error}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(ready, separators=(",", ":"), sort_keys=True), flush=True)
    try:
        worker.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
