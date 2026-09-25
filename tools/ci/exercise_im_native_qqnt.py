#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 exercise_im_native_qqnt.py                                  │
│  Module: tools.ci                                                   │
│  Role: Exercise packaged qqnt-direct Native AOT acceptance.          │
│                                                                     │
│  模块职责：在源代码目录之外验收 Native AOT 的 qqnt-direct 进程边界       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAPABILITY = "message.connector.v1"
QQ_CAPABILITY = "qq.client.v1"
REQUEST_TYPE_URL = "type.cyrene.io/qq.client.v1.Request"
RESPONSE_TYPE_URL = "type.cyrene.io/qq.client.v1.Response"
FILTER_TYPE_URL = "type.cyrene.io/cyrene.message.connector.v1.Filter"
MESSAGE_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.InboundMessagePayload"
)
SEND_REQUEST_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest"
)
DELIVERY_RESULT_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult"
)


def _parse_args() -> argparse.Namespace:
    """Parse the packaged binary and evidence destination.

        中文:解析已打包的二进制文件和证据输出位置。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--rid", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    return parser.parse_args()


def _read_ready(process: subprocess.Popen[str]) -> dict[str, Any]:
    """Read the bounded Native AOT readiness announcement.

        中文:读取长度受限的 Native AOT 就绪公告。"""

    if process.stdout is None:
        raise RuntimeError("Native AOT stdout is not captured")
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
        "Native AOT qqnt-direct host did not announce readiness: "
        f"stdout={diagnostics!r} stderr={stderr!r} returncode={process.returncode}"
    )


def _qq_request(
    runtime_wire: Any,
    *,
    method: str,
    payload: dict[str, Any],
    request_id: str,
) -> Any:
    """Build one fixed qq.client.v1 request envelope.

        中文:构造一个固定的 qq.client.v1 请求封装。"""

    return runtime_wire.DirectInvocationRequest(
        capability=QQ_CAPABILITY,
        interface_version="1",
        method=method,
        payload_type_url=REQUEST_TYPE_URL,
        payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        request_id=request_id,
    )


def _subscription_request(runtime_wire: Any, request_id: str) -> Any:
    """Build one canonical message event subscription request.

        中文:构造一个规范消息事件订阅请求。"""

    return runtime_wire.DirectInvocationRequest(
        capability=CAPABILITY,
        interface_version="1",
        method="events",
        payload_type_url=FILTER_TYPE_URL,
        payload=b"{}",
        request_id=request_id,
        stream_mode=runtime_wire.DIRECT_STREAM_MODE_SUBSCRIPTION,
    )


def _canonical_send_request(message_wire: Any) -> Any:
    """Build one canonical QQ group message.

        中文:构造一条规范 QQ 群消息。"""

    return message_wire.SendMessageRequest(
        conversation=message_wire.ConversationScope(
            vendor="qq",
            account_id="10001",
            conversation_id="20001",
            kind=message_wire.CONVERSATION_KIND_GROUP,
        ),
        content=[
            message_wire.MessageContentPart(
                text=message_wire.TextContent(text="native-qq")
            )
        ],
    )


def _copy_fake_host(runtime_root: Path) -> Path:
    """Copy the fixture Host into disposable runtime state.

        中文:将夹具 Host 复制到临时运行时状态目录。"""

    fake_host = runtime_root / "fake_qq_host.py"
    shutil.copyfile(
        REPOSITORY_ROOT / "plugins/connectors/im/tests/fixtures/fake_qq_host.py",
        fake_host,
    )
    return fake_host


def _exercise(binary: Path, rid: str) -> dict[str, Any]:
    """Exercise one packaged qqnt-direct process and its external Host.

        中文:运行一个已打包的 qqnt-direct 进程及其外部 Host。"""

    sys.path.insert(0, str(REPOSITORY_ROOT / "sdk/python/cyrene_plugin_runtime/src"))
    sys.path.insert(0, str(REPOSITORY_ROOT / "plugins/connectors/im/src"))
    import grpc
    from cyrene_plugin_runtime._generated import (
        direct_plugin_runtime_pb2 as runtime_wire,
    )
    from cyrene_plugin_runtime._generated import (
        direct_plugin_runtime_pb2_grpc as runtime_wire_grpc,
    )
    from qq_connector._generated import message_connector_pb2 as message_wire

    binding_id = f"ci-{rid}-qqnt"
    with tempfile.TemporaryDirectory(prefix="cyrene-onebot-qqnt-") as temporary:
        runtime_root = Path(temporary)
        process_root = runtime_root / "process"
        process_root.mkdir()
        fake_host = _copy_fake_host(runtime_root)
        data_dir = runtime_root / "qq-data"
        operation_log = runtime_root / "operations.log"
        config = {
            "runtime_profile": "qqnt-direct",
            "binding_id": binding_id,
            "host_executable": sys.executable,
            "host_args": [
                "-B",
                str(fake_host),
                f"--operation-log={operation_log}",
            ],
            "data_dir": str(data_dir),
            "required_client_version": "qq-test-1",
            "required_host_abi": "fake-qqnt-linux-x86_64",
            "account_id": "10001",
            "platform": "linux-x86_64",
            "timeout_seconds": 5,
            "startup_timeout_seconds": 5,
            "shutdown_timeout_seconds": 2,
        }
        environment = os.environ.copy()
        environment["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
        environment["CYRENE_CAPABILITY_BINDING_ID"] = binding_id
        process = subprocess.Popen(
            [str(binary), "--listen", "127.0.0.1:0"],
            cwd=process_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        channel = None
        stream = None
        reader: threading.Thread | None = None
        try:
            announcement = _read_ready(process)
            connection_ref = announcement.get("connection_ref")
            if not isinstance(connection_ref, str) or not connection_ref.startswith(
                "grpc://"
            ):
                raise RuntimeError(
                    f"invalid qqnt-direct readiness announcement: {announcement}"
                )
            channel = grpc.insecure_channel(connection_ref.removeprefix("grpc://"))
            grpc.channel_ready_future(channel).result(timeout=10)
            client = runtime_wire_grpc.DirectPluginRuntimeStub(channel)
            health = client.Health(runtime_wire.HealthRequest(), timeout=5)
            if health.status != runtime_wire.HealthResponse.STATUS_SERVING:
                raise RuntimeError(f"packaged qqnt-direct host is not serving: {health}")
            if list(health.capabilities) != [CAPABILITY, QQ_CAPABILITY]:
                raise RuntimeError(f"unexpected qqnt-direct capabilities: {health}")

            extension = client.Invoke(
                _qq_request(
                    runtime_wire,
                    method="qq.group.list",
                    payload={"params": {"account_id": "10001"}},
                    request_id=f"{binding_id}-group-list",
                ),
                timeout=10,
            )
            if extension.WhichOneof("result") != "payload":
                raise RuntimeError(f"qq.client.v1 group list failed: {extension}")
            if extension.payload.type_url != RESPONSE_TYPE_URL:
                raise RuntimeError(f"unexpected QQ response type URL: {extension}")
            extension_body = json.loads(extension.payload.value.decode("utf-8"))
            if extension_body.get("operation") != "qq.group.list":
                raise RuntimeError(f"unexpected QQ operation response: {extension_body}")
            if extension_body.get("result", {}).get("account_id") != "10001":
                raise RuntimeError(f"unexpected QQ group list result: {extension_body}")

            stream = client.InvokeStream(
                _subscription_request(runtime_wire, f"{binding_id}-subscription"),
                timeout=10,
            )
            events: queue.Queue[tuple[str, Any]] = queue.Queue()

            def consume() -> None:
                try:
                    events.put(("item", next(stream)))
                except (
                    grpc.RpcError,
                    OSError,
                    RuntimeError,
                    StopIteration,
                ) as error:
                    events.put(("error", error))

            reader = threading.Thread(target=consume, daemon=True)
            reader.start()
            event_kind, event_item = events.get(timeout=10)
            if event_kind == "error":
                raise RuntimeError(f"qqnt-direct subscription failed: {event_item}")
            if event_item.WhichOneof("event") != "payload":
                raise RuntimeError(
                    f"qqnt-direct returned a non-payload event: {event_item}"
                )
            if event_item.payload.type_url != MESSAGE_TYPE_URL:
                raise RuntimeError(f"unexpected QQ event type URL: {event_item}")
            inbound = message_wire.InboundMessagePayload.FromString(
                event_item.payload.value
            )
            if inbound.message_id != "native-message-1":
                raise RuntimeError(f"unexpected normalized QQ event: {inbound}")
            if (
                inbound.conversation.vendor != "qq"
                or inbound.conversation.conversation_id != "20001"
            ):
                raise RuntimeError(f"unexpected QQ event conversation: {inbound}")
            if inbound.sender_id != "uid-20002":
                raise RuntimeError(f"unexpected QQ event sender: {inbound}")

            canonical = _canonical_send_request(message_wire)
            response = client.Invoke(
                runtime_wire.DirectInvocationRequest(
                    capability=CAPABILITY,
                    interface_version="1",
                    method="send_message",
                    payload_type_url=SEND_REQUEST_TYPE_URL,
                    payload=canonical.SerializeToString(),
                    request_id=f"{binding_id}-send",
                ),
                timeout=10,
            )
            if response.WhichOneof("result") != "payload":
                raise RuntimeError(f"qqnt-direct send_message failed: {response}")
            if response.payload.type_url != DELIVERY_RESULT_TYPE_URL:
                raise RuntimeError(f"unexpected QQ delivery type URL: {response}")
            delivery = message_wire.DeliveryResult.FromString(response.payload.value)
            if not delivery.vendor_message_id.startswith(f"{binding_id}-1-message-1"):
                raise RuntimeError(f"unexpected QQ delivery result: {delivery}")
            if delivery.vendor_extension.vendor != "qq":
                raise RuntimeError(f"unexpected QQ delivery vendor: {delivery}")

            stream.cancel()
            operation_lines = operation_log.read_text(encoding="utf-8").splitlines()
            expected_operations = [
                "qq.session.create",
                "qq.session.init",
                "qq.session.start_nt",
                "qq.group.list",
                "qq.message.subscribe",
                "qq.message.send",
            ]
            if operation_lines != expected_operations:
                raise RuntimeError(
                    "unexpected QQ Host operation order: "
                    f"expected={expected_operations!r} actual={operation_lines!r}"
                )
            return {
                "rid": rid,
                "connection_ref": connection_ref,
                "health": {
                    "status": "SERVING",
                    "plugin_id": health.plugin_id,
                    "plugin_version": health.plugin_version,
                    "capabilities": list(health.capabilities),
                },
                "qq_extension": {
                    "method": "qq.group.list",
                    "result_type_url": extension.payload.type_url,
                },
                "event": {
                    "type_url": event_item.payload.type_url,
                    "message_id": inbound.message_id,
                    "conversation_id": inbound.conversation.conversation_id,
                },
                "invoke": {
                    "method": "send_message",
                    "result_type_url": response.payload.type_url,
                    "vendor": delivery.vendor_extension.vendor,
                    "vendor_message_id": delivery.vendor_message_id,
                },
                "host_operations": operation_lines,
            }
        finally:
            if stream is not None:
                stream.cancel()
            if reader is not None:
                reader.join(timeout=2)
            if channel is not None:
                channel.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.returncode != 0:
                stderr = process.stderr.read() if process.stderr is not None else ""
                raise RuntimeError(
                    f"packaged qqnt-direct process exited with {process.returncode}: {stderr}"
                )
            if not operation_log.is_file():
                raise RuntimeError("fake QQ Host did not receive any operations")
            if (data_dir / ".cyrene-binding-lock").exists():
                raise RuntimeError("qqnt-direct binding lock was not released")


def main() -> int:
    """Run the packaged qqnt-direct acceptance and write bounded evidence.

        中文:运行已打包 qqnt-direct 的验收流程并写入有界证据。"""

    args = _parse_args()
    binary = args.binary.resolve(strict=True)
    evidence = _exercise(binary, args.rid)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
