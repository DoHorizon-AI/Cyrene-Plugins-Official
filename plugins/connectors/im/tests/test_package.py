"""Tests for the isolated Python IM parity reference.

中文:隔离的 Python IM 对等实现参考测试。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import grpc
import pytest
from cyrene_plugin_runtime._generated import direct_plugin_runtime_pb2 as direct_wire
from cyrene_plugin_runtime._generated import (
    direct_plugin_runtime_pb2_grpc as direct_wire_grpc,
)

TOOLS_ROOT = Path(__file__).parents[1] / "tools"
_BUILDER_SPEC = importlib.util.spec_from_file_location(
    "im_package_builder", TOOLS_ROOT / "assemble_package.py"
)
if _BUILDER_SPEC is None or _BUILDER_SPEC.loader is None:
    raise ImportError("cannot load OneBot package builder")
_BUILDER = importlib.util.module_from_spec(_BUILDER_SPEC)
_BUILDER_SPEC.loader.exec_module(_BUILDER)
assemble_package = _BUILDER.assemble_package
build_package_archive = _BUILDER.build_package_archive


REPOSITORY_ROOT = Path(__file__).parents[4]


def _artifact_direct_config(runtime_root: Path, binding_id: str) -> dict[str, Any]:
    """Create one fixture binding whose worker imports only the package artifact.

        中文:创建一个夹具绑定,使其工作进程只导入软件包产物。"""

    runtime_root.mkdir(parents=True, exist_ok=True)
    fake_host = runtime_root / "fake_qq_host.py"
    shutil.copyfile(
        Path(__file__).parent / "fixtures/fake_qq_host.py",
        fake_host,
    )
    return {
        "runtime_profile": "qqnt-direct",
        "binding_id": binding_id,
        "host_executable": sys.executable,
        "host_args": [
            "-B",
            str(fake_host),
            f"--operation-log={runtime_root / 'operations.log'}",
        ],
        "data_dir": str(runtime_root / "qq-data"),
        "required_client_version": "qq-test-1",
        "required_host_abi": "fake-qqnt-linux-x86_64",
        "account_id": "10001",
        "platform": "linux-x86_64",
        "timeout_seconds": 2.0,
        "startup_timeout_seconds": 2.0,
        "shutdown_timeout_seconds": 2.0,
    }


def _start_artifact_runtime(
    package_root: Path, config: dict[str, Any], working_root: Path
) -> tuple[subprocess.Popen[str], str]:
    """Start the installed package outside the source checkout and read readiness.

        中文:在源代码检出目录之外启动已安装的软件包并读取就绪状态。"""

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(package_root / "src")
    environment["CYRENE_CAPABILITY_BINDING_ID"] = str(config["binding_id"])
    environment["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(
        config, ensure_ascii=False, separators=(",", ":")
    )
    command = [
        sys.executable,
        "-B",
        str(package_root / "src/cyrene_plugin_runtime/bootstrap.py"),
        "--entrypoint",
        "qq_connector.plugin:ConnectorPlugin",
        "--capability",
        "message.connector.v1",
        "--capability",
        "qq.client.v1",
        "--interface-version",
        "1",
        "--interface-version",
        "1",
        "--listen",
        "127.0.0.1:0",
    ]
    process = subprocess.Popen(
        command,
        cwd=working_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready_line = process.stdout.readline()
    if not ready_line:
        process.terminate()
        process.wait(timeout=2)
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise AssertionError(f"installed package runtime did not start: {stderr}")
    ready = json.loads(ready_line)
    connection_ref = ready.get("connection_ref")
    assert isinstance(connection_ref, str) and connection_ref.startswith("grpc://")
    return process, connection_ref


def _stop_artifact_runtime(process: subprocess.Popen[str]) -> tuple[int | None, str]:
    """Stop one artifact worker and collect bounded diagnostic output.

        中文:停止一个产物工作进程并收集有界诊断输出。"""

    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    stderr = process.stderr.read() if process.stderr is not None else ""
    return process.returncode, stderr


def _qq_extension_request(binding_id: str) -> direct_wire.DirectInvocationRequest:
    """Build one typed QQ extension request for the installed worker.

        中文:为已安装的工作进程构造一个有类型的 QQ 扩展请求。"""

    return direct_wire.DirectInvocationRequest(
        capability="qq.client.v1",
        interface_version="1",
        method="qq.group.list",
        payload_type_url="type.cyrene.io/qq.client.v1.Request",
        payload=json.dumps(
            {"params": {"account_id": "10001"}}, separators=(",", ":")
        ).encode("utf-8"),
        request_id=f"{binding_id}-extension",
    )


def _message_subscription_request(
    binding_id: str,
) -> direct_wire.DirectInvocationRequest:
    """Build one real direct-runtime subscription request for inbound messages.

        中文:为入站消息构造一个真实的直连运行时订阅请求。"""

    return direct_wire.DirectInvocationRequest(
        capability="message.connector.v1",
        interface_version="1",
        method="events",
        payload_type_url="type.cyrene.io/cyrene.message.connector.v1.Filter",
        payload=b"{}",
        request_id=f"{binding_id}-subscription",
        stream_mode=direct_wire.DIRECT_STREAM_MODE_SUBSCRIPTION,
    )


def test_assembled_package_contains_runtime_and_resolvable_schema_refs(
    tmp_path: Path,
) -> None:
    """The rollback payload must run without a source checkout.

        中文:回滚载荷必须能在没有源代码检出目录的情况下运行。"""

    package_root = assemble_package(REPOSITORY_ROOT, tmp_path / "package")
    assert (package_root / "src/cyrene_plugin_runtime/bootstrap.py").is_file()
    assert (package_root / "src/qq_connector/plugin.py").is_file()
    manifest = json.loads(
        (package_root / "plugin.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == "0.1.0"
    assert manifest["runtime"]["language"] == "python"
    assert manifest["runtime"]["entrypoint"] == (
        "qq_connector.plugin:ConnectorPlugin"
    )
    assert not list(package_root.rglob("__pycache__"))
    assert not list(package_root.rglob("*.pyc"))

    for method in manifest["methods"]:
        for key in ("inputSchema", "outputSchema"):
            reference = method.get(key)
            if not isinstance(reference, str) or not reference:
                continue
            relative = reference.split("#", 1)[0]
            assert (package_root / relative).is_file(), (
                method["name"],
                key,
                reference,
            )

    launch = manifest["runtime"]["launch"]
    bootstrap = next(
        argument
        for argument in launch["args"]
        if argument.endswith("src/cyrene_plugin_runtime/bootstrap.py")
    )
    assert (package_root / bootstrap).is_file()

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(package_root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cyrene_plugin_runtime.bootstrap; "
            "import qq_connector.plugin",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_package_archive_is_self_contained(tmp_path: Path) -> None:
    """The rollback ZIP must contain the Python runtime and metadata.

        中文:回滚 ZIP 必须包含 Python 运行时和元数据。"""

    archive_path = build_package_archive(REPOSITORY_ROOT, tmp_path / "onebot.zip")
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert "plugin.manifest.json" in names
        assert "package-descriptor.json" in names
        assert "src/cyrene_plugin_runtime/bootstrap.py" in names
        assert "src/qq_connector/plugin.py" in names
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
        assert (
            "contracts/json/message-connector-v1-inbound-request.schema.json"
            in names
        )

    second_path = build_package_archive(REPOSITORY_ROOT, tmp_path / "onebot-second.zip")
    assert hashlib.sha256(archive_path.read_bytes()).digest() == hashlib.sha256(
        second_path.read_bytes()
    ).digest()


def test_installed_artifact_runs_two_direct_bindings_without_source_checkout(
    tmp_path: Path,
) -> None:
    """Exercise direct gRPC Invoke/Subscribe from one installed package artifact.

    This is an artifact-level vertical gate. It proves package self-containment,
    direct runtime dispatch, fake Host isolation, and cleanup; it is not the
    Workspace P2.5 Product/AstrBot/pgvector harness or official QQ smoke.

        中文:从一个已安装的软件包产物执行直连 gRPC Invoke/Subscribe。

        中文:这是产物级的端到端门禁,用于证明软件包自包含、直连运行时分发、模拟 Host 隔离和资源清理;它不等同于 Workspace P2.5 Product/AstrBot/pgvector 测试框架,也不等同于官方 QQ 冒烟检查。
    """

    archive_path = build_package_archive(REPOSITORY_ROOT, tmp_path / "package.zip")
    package_root = tmp_path / "installed"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(package_root)
    runtime_root = tmp_path / "runtime"
    first_config = _artifact_direct_config(
        runtime_root / "qq-main", "qq-artifact-main"
    )
    second_config = _artifact_direct_config(
        runtime_root / "qq-secondary", "qq-artifact-secondary"
    )
    processes: list[subprocess.Popen[str]] = []
    channels: list[grpc.Channel] = []
    try:
        first_process, first_ref = _start_artifact_runtime(
            package_root, first_config, runtime_root / "qq-main"
        )
        processes.append(first_process)
        second_process, second_ref = _start_artifact_runtime(
            package_root, second_config, runtime_root / "qq-secondary"
        )
        processes.append(second_process)
        first_channel = grpc.insecure_channel(first_ref.removeprefix("grpc://"))
        channels.append(first_channel)
        second_channel = grpc.insecure_channel(second_ref.removeprefix("grpc://"))
        channels.append(second_channel)
        first_stub = direct_wire_grpc.DirectPluginRuntimeStub(first_channel)
        second_stub = direct_wire_grpc.DirectPluginRuntimeStub(second_channel)

        for stub in (first_stub, second_stub):
            health = stub.Health(direct_wire.HealthRequest(), timeout=2)
            assert health.status == direct_wire.HealthResponse.STATUS_SERVING
            assert health.plugin_id == "cyrene.connectors.im"
            assert list(health.capabilities) == [
                "message.connector.v1",
                "qq.client.v1",
            ]

        responses = (
            first_stub.Invoke(_qq_extension_request("qq-artifact-main"), timeout=4),
            second_stub.Invoke(
                _qq_extension_request("qq-artifact-secondary"), timeout=4
            ),
        )
        for response in responses:
            assert response.WhichOneof("result") == "payload"
            body = json.loads(response.payload.value.decode("utf-8"))
            assert body["operation"] == "qq.group.list"
            assert body["result"]["account_id"] == "10001"

        streams = (
            first_stub.InvokeStream(
                _message_subscription_request("qq-artifact-main"), timeout=4
            ),
            second_stub.InvokeStream(
                _message_subscription_request("qq-artifact-secondary"), timeout=4
            ),
        )
        for stream in streams:
            item = next(stream)
            assert item.WhichOneof("event") == "payload"
            assert item.payload.event_type == "inbound_message"
            assert item.payload.type_url.endswith("InboundMessagePayload")
            assert b"hello from qq" in item.payload.value
            stream.cancel()
    finally:
        for channel in channels:
            channel.close()
        stopped = [_stop_artifact_runtime(process) for process in processes]
        assert all(code == 0 for code, _ in stopped), stopped

    for binding_id, directory_name in (
        ("qq-artifact-main", "qq-main"),
        ("qq-artifact-secondary", "qq-secondary"),
    ):
        binding_root = runtime_root / directory_name
        data_dir = binding_root / "qq-data"
        owner_marker = json.loads(
            (data_dir / ".cyrene-binding-owner.json").read_text(encoding="utf-8")
        )
        assert owner_marker["binding_id"] == binding_id
        assert not (data_dir / ".cyrene-binding.lock").exists()
        assert (binding_root / "operations.log").read_text(
            encoding="utf-8"
        ).splitlines() == [
            "qq.session.create",
            "qq.session.init",
            "qq.session.start_nt",
            "qq.group.list",
            "qq.message.subscribe",
        ]


def test_package_builder_rejects_nonempty_output(tmp_path: Path) -> None:
    """A stale staging directory must never be silently overwritten.

        中文:不得静默覆盖过期的暂存目录。"""

    output = tmp_path / "package"
    output.mkdir()
    (output / "unexpected.txt").write_text("sentinel", encoding="utf-8")
    with pytest.raises(ValueError, match="must be empty"):
        assemble_package(REPOSITORY_ROOT, output)
