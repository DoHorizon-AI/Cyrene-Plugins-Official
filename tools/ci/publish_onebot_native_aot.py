#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 publish_onebot_native_aot.py                                     │
│  Module: tools.ci                                                   │
│  Role: Publish and exercise one RID-specific OneBot Native AOT pack. │
│                                                                     │
│  模块职责：发布并纵向验收指定 RID 的 OneBot Native AOT 候选包              │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_RIDS = {"linux-x64", "linux-arm64"}
HOST_PROJECT = (
    "runtime/dotnet-native-aot/Cyrene.OneBot.V11.Host/"
    "Cyrene.OneBot.V11.Host.csproj"
)
HOST_EXECUTABLE = "cyrene-onebot-v11"
CAPABILITY = "message.connector.v1"
SEND_METHOD = "send_message"
SEND_REQUEST_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest"
)
DELIVERY_RESULT_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult"
)


def _parse_args() -> argparse.Namespace:
    """Parse one RID and its disposable output directory.

        中文：解析一个 RID 及其临时输出目录。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rid", choices=sorted(SUPPORTED_RIDS), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def _run(command: list[str], *, cwd: Path = REPOSITORY_ROOT) -> None:
    """Run one required command and retain its output in the CI log.

        中文：运行一条必需命令，并将其输出保留在 CI 日志中。"""

    print("+", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def _publish(rid: str, output_dir: Path) -> Path:
    """Publish the OneBot Host as a self-contained Native AOT executable.

        中文：将 OneBot Host 发布为独立部署的 Native AOT 可执行文件。"""

    _run(
        [
            "dotnet",
            "publish",
            HOST_PROJECT,
            "--configuration",
            "Release",
            "--runtime",
            rid,
            "--self-contained",
            "true",
            "-p:PublishAot=true",
            "--output",
            str(output_dir),
        ]
    )
    candidates = [output_dir / HOST_EXECUTABLE]
    if os.name == "nt":
        candidates.append(output_dir / f"{HOST_EXECUTABLE}.exe")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(
        f"published OneBot Native AOT executable not found in {output_dir}"
    )


class _OneBotHandler(BaseHTTPRequestHandler):
    """Record one local OneBot action and return a successful fake response.

        中文：记录一条本地 OneBot 动作，并返回成功的模拟响应。"""

    requests: ClassVar[list[dict[str, Any]]] = []
    request_lock = threading.Lock()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "malformed JSON")
            return
        with self.request_lock:
            self.requests.append({"path": self.path, "body": payload})
        response = json.dumps(
            {"status": "ok", "retcode": 0, "data": {"message_id": "native-1"}}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the fake transport quiet; failures are reported as evidence.

            中文：让模拟传输保持安静；失败会作为证据报告。"""

        del format, args


def _read_ready_announcement(process: subprocess.Popen[str]) -> dict[str, Any]:
    """Read the bounded JSON launch announcement from the child stdout.

        中文：从子进程标准输出中读取长度受限的 JSON 启动公告。"""

    if process.stdout is None:
        raise RuntimeError("OneBot Native AOT host stdout is not captured")
    stdout_fd = process.stdout.fileno()
    deadline = time.monotonic() + 15
    diagnostics: list[str] = []
    pending = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stdout_fd], [], [], 0.1)
        if not ready:
            if process.poll() is not None:
                break
            continue
        chunk = os.read(stdout_fd, 4096)
        if not chunk:
            if process.poll() is not None:
                break
            continue
        pending += chunk
        while b"\n" in pending:
            raw_line, pending = pending.split(b"\n", 1)
            text = raw_line.decode("utf-8", errors="replace").strip()
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                diagnostics.append(text)
                continue
            if isinstance(value, dict) and value.get("event") == "direct_plugin_ready":
                return value
            diagnostics.append(text)
    stderr = ""
    if process.poll() is None:
        process.terminate()
    try:
        _, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        _, stderr = process.communicate(timeout=5)
    raise RuntimeError(
        "OneBot Native AOT host did not announce readiness: "
        f"stdout={diagnostics!r} stderr={stderr!r} returncode={process.returncode}"
    )


def _canonical_send_request(message_wire: Any) -> Any:
    """Build one canonical group message for the direct-runtime smoke test.

        中文：为直连运行时冒烟检查构造一条规范群消息。"""

    return message_wire.SendMessageRequest(
        conversation=message_wire.ConversationScope(
            vendor="onebot.v11",
            account_id="10001",
            conversation_id="20001",
            kind=message_wire.CONVERSATION_KIND_GROUP,
        ),
        content=[
            message_wire.MessageContentPart(
                text=message_wire.TextContent(text="native-aot-ci")
            )
        ],
    )


def _exercise_package(binary: Path, rid: str) -> dict[str, Any]:
    """Call Health and send_message through the packaged process endpoint.

        中文：通过打包后的进程端点调用 Health 和 send_message。"""

    sys.path.insert(
        0, str(REPOSITORY_ROOT / "sdk/python/cyrene_plugin_runtime/src")
    )
    sys.path.insert(
        0, str(REPOSITORY_ROOT / "plugins/connectors/onebot-v11/src")
    )
    import grpc
    from cyrene_plugin_runtime._generated import (
        direct_plugin_runtime_pb2 as runtime_wire,
    )
    from cyrene_plugin_runtime._generated import (
        direct_plugin_runtime_pb2_grpc as runtime_wire_grpc,
    )
    from onebot_v11_connector._generated import message_connector_pb2 as message_wire

    _OneBotHandler.requests = []
    fake_server = ThreadingHTTPServer(("127.0.0.1", 0), _OneBotHandler)
    fake_thread = threading.Thread(target=fake_server.serve_forever, daemon=True)
    fake_thread.start()
    onebot_config = json.dumps(
        {
            "binding_id": f"ci-{rid}",
            "runtime_profile": "onebot-v11",
            "transport_profile": "http_api",
            "http_base_url": f"http://127.0.0.1:{fake_server.server_port}",
            "self_account_id": "10001",
            "timeout_seconds": 5,
        }
    )
    environment = os.environ.copy()
    environment["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = onebot_config
    environment["CYRENE_CAPABILITY_BINDING_ID"] = f"ci-{rid}"
    process = subprocess.Popen(
        [str(binary), "--listen", "127.0.0.1:0"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    channel = None
    try:
        announcement = _read_ready_announcement(process)
        connection_ref = announcement.get("connection_ref")
        if not isinstance(connection_ref, str) or not connection_ref.startswith("grpc://"):
            raise RuntimeError(f"invalid OneBot readiness announcement: {announcement}")
        target = connection_ref.removeprefix("grpc://")
        channel = grpc.insecure_channel(target)
        grpc.channel_ready_future(channel).result(timeout=10)
        client = runtime_wire_grpc.DirectPluginRuntimeStub(channel)
        health = client.Health(runtime_wire.HealthRequest(), timeout=5)
        if health.status != runtime_wire.HealthResponse.STATUS_SERVING:
            raise RuntimeError(f"packaged OneBot host is not serving: {health}")
        if health.plugin_id != "cyrene.connectors.onebot-v11":
            raise RuntimeError(f"unexpected plugin id from Health: {health.plugin_id}")

        canonical = _canonical_send_request(message_wire)
        response = client.Invoke(
            runtime_wire.DirectInvocationRequest(
                capability=CAPABILITY,
                interface_version="1",
                method=SEND_METHOD,
                payload_type_url=SEND_REQUEST_TYPE_URL,
                payload=canonical.SerializeToString(),
                request_id=f"ci-{rid}-send-1",
            ),
            timeout=10,
        )
        if response.WhichOneof("result") != "payload":
            raise RuntimeError(f"packaged OneBot send_message failed: {response}")
        if response.payload.type_url != DELIVERY_RESULT_TYPE_URL:
            raise RuntimeError(
                f"unexpected send_message result type URL: {response.payload.type_url}"
            )
        delivery = message_wire.DeliveryResult.FromString(response.payload.value)
        if delivery.vendor_message_id != "native-1":
            raise RuntimeError(f"unexpected delivery result: {delivery}")
        with _OneBotHandler.request_lock:
            requests = list(_OneBotHandler.requests)
        if len(requests) != 1:
            raise RuntimeError(f"expected one fake OneBot request, got {requests}")
        request = requests[0]
        if request["path"] != "/send_group_msg":
            raise RuntimeError(f"unexpected OneBot action path: {request}")
        body = request["body"]
        if body.get("group_id") != 20001 or body.get("message") != [
            {"type": "text", "data": {"text": "native-aot-ci"}}
        ]:
            raise RuntimeError(f"unexpected OneBot action body: {body}")
        return {
            "rid": rid,
            "connection_ref": connection_ref,
            "health": {
                "status": "SERVING",
                "plugin_id": health.plugin_id,
                "plugin_version": health.plugin_version,
                "capabilities": list(health.capabilities),
            },
            "invoke": {
                "method": SEND_METHOD,
                "result_type_url": response.payload.type_url,
                "vendor_message_id": delivery.vendor_message_id,
                "onebot_path": request["path"],
            },
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
        fake_thread.join(timeout=5)


def publish_and_smoke(rid: str, output_dir: Path) -> dict[str, Any]:
    """Publish, package, unpack, and exercise one Linux OneBot candidate.

        中文：发布、打包、解包并执行一个 Linux OneBot 候选构建。"""

    if rid not in SUPPORTED_RIDS:
        raise ValueError(f"unsupported OneBot Native AOT RID: {rid}")
    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="cyrene-onebot-publish-") as temporary:
        published = _publish(rid, Path(temporary))
        package_archive = output_dir / f"cyrene-onebot-v11-native-{rid}.zip"
        package_builder = (
            REPOSITORY_ROOT
            / "plugins/connectors/onebot-v11/tools/assemble_native_package.py"
        )
        _run(
            [
                sys.executable,
                str(package_builder),
                "--repository-root",
                str(REPOSITORY_ROOT),
                "--binary",
                str(published),
                "--rid",
                rid,
                "--output",
                str(package_archive),
            ]
        )
    unpacked = output_dir / "unpacked"
    unpacked.mkdir()
    with zipfile.ZipFile(package_archive) as archive:
        archive.extractall(unpacked)
    packaged_binary = unpacked / "bin/cyrene-onebot-v11"
    if packaged_binary.is_file():
        packaged_binary.chmod(packaged_binary.stat().st_mode | 0o111)
    if not packaged_binary.is_file() or not os.access(packaged_binary, os.X_OK):
        raise FileNotFoundError(f"packaged executable is not runnable: {packaged_binary}")
    evidence = _exercise_package(packaged_binary, rid)
    evidence["package"] = str(package_archive.relative_to(output_dir))
    evidence["binary"] = str(packaged_binary.relative_to(output_dir))
    evidence_path = output_dir / "native-onebot-aot-evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence


def main() -> int:
    """Publish and exercise one RID-specific OneBot Native AOT package.

        中文：发布并执行一个针对指定 RID 的 OneBot Native AOT 包。"""

    args = _parse_args()
    evidence = publish_and_smoke(args.rid, args.output_dir)
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
