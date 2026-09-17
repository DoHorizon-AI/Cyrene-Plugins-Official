###############################################################################
# File: tools/ci/benchmark_onebot_native_aot.py
# Role: Collect descriptive performance evidence from an installed OneBot AOT package.
#
# 模块职责：从已安装的 OneBot AOT 包采集描述性性能证据。
###############################################################################
"""Collect repeatable, non-gating performance samples for a Native AOT package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import select
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAPABILITY = "message.connector.v1"
SEND_METHOD = "send_message"
SEND_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest"
DELIVERY_RESULT_TYPE_URL = "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult"
SAMPLES = 10


def _parse_args() -> argparse.Namespace:
    """Parse the installed binary and evidence destination."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--rid", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    return parser.parse_args()


class _OneBotHandler(BaseHTTPRequestHandler):
    """Return one deterministic successful OneBot response."""

    request_count: ClassVar[int] = 0
    request_lock = threading.Lock()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        with self.request_lock:
            type(self).request_count += 1
        response = json.dumps(
            {"status": "ok", "retcode": 0, "data": {"message_id": "benchmark-1"}}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the local benchmark peer quiet."""

        del format, args


def _read_ready(process: subprocess.Popen[str]) -> dict[str, Any]:
    """Read the bounded readiness announcement from the Native AOT process."""

    if process.stdout is None:
        raise RuntimeError("Native AOT stdout is not captured")
    descriptor = process.stdout.fileno()
    deadline = time.monotonic() + 15
    pending = b""
    diagnostics: list[str] = []
    while time.monotonic() < deadline:
        ready, _, _ = select.select([descriptor], [], [], 0.1)
        if not ready:
            if process.poll() is not None:
                break
            continue
        chunk = os.read(descriptor, 4096)
        if not chunk:
            if process.poll() is not None:
                break
            continue
        pending += chunk
        while b"\n" in pending:
            raw, pending = pending.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").strip()
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                diagnostics.append(line)
                continue
            if isinstance(value, dict) and value.get("event") == "direct_plugin_ready":
                return value
            diagnostics.append(line)
    stderr = ""
    if process.poll() is None:
        process.terminate()
    try:
        _, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        _, stderr = process.communicate(timeout=5)
    raise RuntimeError(
        "Native AOT host did not announce readiness: "
        f"stdout={diagnostics!r} stderr={stderr!r} returncode={process.returncode}"
    )


def _rss_bytes(pid: int) -> int | None:
    """Return one sampled Linux resident-set size."""

    status = Path(f"/proc/{pid}/status")
    if not status.is_file():
        return None
    for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def _canonical_request(message_wire: Any) -> Any:
    """Build one canonical local group message."""

    return message_wire.SendMessageRequest(
        conversation=message_wire.ConversationScope(
            vendor="onebot.v11",
            account_id="10001",
            conversation_id="20001",
            kind=message_wire.CONVERSATION_KIND_GROUP,
        ),
        content=[
            message_wire.MessageContentPart(
                text=message_wire.TextContent(text="native-performance")
            )
        ],
    )


def _percentile(values: list[float], percentile: float) -> float:
    """Return an inclusive percentile in milliseconds."""

    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(percentile) - 1]


def _sha256(path: Path) -> str:
    """Return a file's SHA-256 digest without loading it all into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect(binary: Path, package: Path, rid: str) -> dict[str, Any]:
    """Launch an installed package and collect descriptive latency samples."""

    if sys.platform != "linux":
        raise RuntimeError("Native AOT performance sampling currently requires Linux")
    if rid != "linux-x64":
        raise RuntimeError("descriptive performance sampling currently uses linux-x64")
    binary = binary.resolve(strict=True)
    package = package.resolve(strict=True)
    sys.path.insert(0, str(REPOSITORY_ROOT / "sdk/python/cyrene_plugin_runtime/src"))
    sys.path.insert(0, str(REPOSITORY_ROOT / "plugins/connectors/onebot-v11/src"))
    import grpc
    from cyrene_plugin_runtime._generated import (
        direct_plugin_runtime_pb2 as runtime_wire,
    )
    from cyrene_plugin_runtime._generated import (
        direct_plugin_runtime_pb2_grpc as runtime_wire_grpc,
    )
    from onebot_v11_connector._generated import message_connector_pb2 as message_wire

    _OneBotHandler.request_count = 0
    fake_server = ThreadingHTTPServer(("127.0.0.1", 0), _OneBotHandler)
    server_thread = threading.Thread(target=fake_server.serve_forever, daemon=True)
    server_thread.start()
    config = json.dumps(
        {
            "binding_id": f"benchmark-{rid}",
            "runtime_profile": "onebot-v11",
            "transport_profile": "http_api",
            "http_base_url": f"http://127.0.0.1:{fake_server.server_port}",
            "self_account_id": "10001",
            "timeout_seconds": 5,
        }
    )
    environment = os.environ.copy()
    environment["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = config
    environment["CYRENE_CAPABILITY_BINDING_ID"] = f"benchmark-{rid}"
    launch_started = time.perf_counter_ns()
    with tempfile.TemporaryDirectory(prefix="cyrene-onebot-benchmark-") as working:
        process = subprocess.Popen(
            [str(binary), "--listen", "127.0.0.1:0"],
            cwd=working,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return _collect_process_evidence(
            process,
            grpc_module=grpc,
            binary=binary,
            package=package,
            rid=rid,
            launch_started=launch_started,
            message_wire=message_wire,
            runtime_wire=runtime_wire,
            runtime_wire_grpc=runtime_wire_grpc,
            fake_server=fake_server,
            server_thread=server_thread,
        )


def _collect_process_evidence(
    process: subprocess.Popen[str],
    *,
    grpc_module: Any,
    binary: Path,
    package: Path,
    rid: str,
    launch_started: int,
    message_wire: Any,
    runtime_wire: Any,
    runtime_wire_grpc: Any,
    fake_server: ThreadingHTTPServer,
    server_thread: threading.Thread,
) -> dict[str, Any]:
    """Collect protocol samples and always close the child and fake peer."""

    channel = None
    try:
        announcement = _read_ready(process)
        ready_ms = (time.perf_counter_ns() - launch_started) / 1_000_000
        connection_ref = announcement.get("connection_ref")
        if not isinstance(connection_ref, str) or not connection_ref.startswith(
            "grpc://"
        ):
            raise RuntimeError(f"invalid readiness announcement: {announcement}")
        channel = grpc_module.insecure_channel(connection_ref.removeprefix("grpc://"))
        channel_started = time.perf_counter_ns()
        grpc_module.channel_ready_future(channel).result(timeout=10)
        channel_ready_ms = (time.perf_counter_ns() - channel_started) / 1_000_000
        client = runtime_wire_grpc.DirectPluginRuntimeStub(channel)
        health_started = time.perf_counter_ns()
        health = client.Health(runtime_wire.HealthRequest(), timeout=5)
        health_ms = (time.perf_counter_ns() - health_started) / 1_000_000
        if health.status != runtime_wire.HealthResponse.STATUS_SERVING:
            raise RuntimeError(f"package is not serving: {health}")

        canonical = _canonical_request(message_wire).SerializeToString()
        latencies: list[float] = []
        sampled_rss: list[int] = []
        for index in range(SAMPLES):
            started = time.perf_counter_ns()
            response = client.Invoke(
                runtime_wire.DirectInvocationRequest(
                    capability=CAPABILITY,
                    interface_version="1",
                    method=SEND_METHOD,
                    payload_type_url=SEND_REQUEST_TYPE_URL,
                    payload=canonical,
                    request_id=f"benchmark-{rid}-{index}",
                ),
                timeout=10,
            )
            latencies.append((time.perf_counter_ns() - started) / 1_000_000)
            rss = _rss_bytes(process.pid)
            if rss is not None:
                sampled_rss.append(rss)
            if response.WhichOneof("result") != "payload":
                raise RuntimeError(f"send_message failed: {response}")
            if response.payload.type_url != DELIVERY_RESULT_TYPE_URL:
                raise RuntimeError(
                    f"unexpected result type URL: {response.payload.type_url}"
                )
            delivery = message_wire.DeliveryResult.FromString(response.payload.value)
            if delivery.vendor_message_id != "benchmark-1":
                raise RuntimeError(f"unexpected delivery result: {delivery}")

        revision = os.environ.get("GITHUB_SHA")
        if not revision:
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPOSITORY_ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        return {
            "evidence_kind": "descriptive-native-aot-performance",
            "source_revision": revision,
            "timestamp_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "rid": rid,
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "package": {
                "path": str(package),
                "sha256": _sha256(package),
            },
            "binary": {
                "path": str(binary),
                "sha256": _sha256(binary),
                "size_bytes": binary.stat().st_size,
            },
            "samples": SAMPLES,
            "health": {"status": "SERVING", "latency_ms": health_ms},
            "startup": {
                "ready_announcement_ms": ready_ms,
                "grpc_channel_ready_ms": channel_ready_ms,
            },
            "send_message": {
                "min_ms": min(latencies),
                "median_ms": statistics.median(latencies),
                "p95_ms": _percentile(latencies, 95),
                "max_ms": max(latencies),
                "fake_onebot_requests": _OneBotHandler.request_count,
            },
            "sampled_peak_rss_bytes": max(sampled_rss) if sampled_rss else None,
            "host_abi": "not-applicable-generic-onebot",
            "redaction_status": "not-applicable-no-secret-captured",
            "comparison": "Run the Python 0.2.0 rollback artifact on the same host/configuration.",
        }
    finally:
        if channel is not None:
            channel.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        fake_server.shutdown()
        fake_server.server_close()
        server_thread.join(timeout=5)


def main() -> int:
    """Collect and write one benchmark record."""

    args = _parse_args()
    evidence = collect(args.binary, args.package, args.rid)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
