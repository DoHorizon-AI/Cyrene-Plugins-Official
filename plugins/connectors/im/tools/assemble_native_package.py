#!/usr/bin/env python3
"""Assemble the Linux x86_64 Native AOT IM connector package.

中文：组装 Linux x86_64 Native AOT IM connector package。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

SUPPORTED_RIDS = {"linux-x64": "x86_64"}
PACKAGE_VERSION = "0.1.0"
PLUGIN_ID = "cyrene.connectors.im"
EXECUTABLE = "bin/cyrene-im"
PROTOCOL = "cyrene.plugin.runtime.v1.DirectPluginRuntime"
PACKAGE_FILES = (
    "README.md",
    "QQ_API_PLAN.md",
    "QQ_SIDE_INTERFACES.md",
    "QQNT_DIRECT_API_MATRIX.md",
    "QQNT_DIRECT_PROTOCOL.md",
    "QQNT_DIRECT_REAL_SMOKE.md",
    "NATIVE_AOT_OPERATIONS.md",
    "configuration.schema.json",
    "contracts/v1/schema.json",
    "contracts/json/message-connector-v1-inbound-request.schema.json",
    "contracts/json/message-connector-v1-request-response.schema.json",
)


class NativePackageAssemblyError(ValueError):
    """Raised when an IM Native AOT package cannot be assembled safely.

        中文：无法安全组装 IM Native AOT package 时抛出的错误。
    """


def _read_json(path: Path) -> dict[str, Any]:
    """Read one JSON object used as package metadata.

        中文：将用于 package 元数据的一份 JSON 对象读取出来。
    """

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise NativePackageAssemblyError(
            f"cannot read JSON metadata {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise NativePackageAssemblyError(f"JSON metadata is not an object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Write stable UTF-8 JSON metadata.

        中文：使用稳定的 UTF-8 格式写入 JSON 元数据。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rewrite_manifest(manifest: dict[str, Any], rid: str) -> dict[str, Any]:
    """Project the source manifest into one RID-specific native package.

        中文：将 source manifest 投影为一个特定 RID 的原生 package。
    """

    projected = json.loads(json.dumps(manifest))
    projected["version"] = PACKAGE_VERSION
    runtime = projected["runtime"]
    runtime["language"] = "csharp"
    runtime.pop("entrypoint", None)
    runtime["protocol"] = PROTOCOL
    runtime["launch"] = {"executable": EXECUTABLE}
    for method in projected.get("methods", []):
        if not isinstance(method, dict):
            continue
        for key in ("inputSchema", "outputSchema"):
            reference = method.get(key)
            if isinstance(reference, str):
                method[key] = reference.replace(
                    "../../../contracts/json/", "contracts/json/"
                )
    compatibility = projected["compatibility"]
    compatibility.pop("requiresPython", None)
    compatibility.pop("requiresDotnet", None)
    compatibility["os"] = ["linux"]
    compatibility["architectures"] = [SUPPORTED_RIDS[rid]]
    return projected


def _rewrite_descriptor(descriptor: dict[str, Any], rid: str) -> dict[str, Any]:
    """Project the source descriptor into one RID-specific native package.

        中文：将 source descriptor 投影为一个特定 RID 的原生 package。
    """

    projected = json.loads(json.dumps(descriptor))
    package = projected["package"]
    package["manifest_ref"] = "plugin.manifest.json"
    package["version"] = PACKAGE_VERSION
    implementation = projected["implementation"]
    implementation["entrypoint"] = EXECUTABLE
    dependencies = projected["dependencies"]
    dependencies["lock"] = {"status": "NOT_REQUIRED"}
    configuration = projected["configuration"]
    configuration["schema_ref"] = "configuration.schema.json"
    runtime = projected["runtime"]
    runtime["kind"] = "native-executable"
    runtime["protocol"] = PROTOCOL
    runtime["entrypoint"] = EXECUTABLE
    runtime["external"] = {
        "required": True,
        "kind": "official_qq_host_child",
        "artifact_ref": EXECUTABLE,
    }
    for profile in projected.get("profiles", []):
        if not isinstance(profile, dict):
            continue
        profile_runtime = profile.get("runtime")
        if not isinstance(profile_runtime, dict):
            continue
        profile_runtime["kind"] = "configured-native-child"
        profile_runtime.pop("entrypoint", None)
    projected["compatibility"] = {
        "platformVersion": ">=0.1.0",
        "capability_interface": [
            "message.connector.v1@1",
            "qq.client.v1@1",
        ],
        "os": ["linux"],
        "architectures": [SUPPORTED_RIDS[rid]],
    }
    lifecycle = projected["lifecycle"]
    lifecycle["rollback_identity"] = f"{PLUGIN_ID}@0.1.0-python-reference"
    lifecycle["cache_identity"] = f"{PLUGIN_ID}@{PACKAGE_VERSION}"
    return projected


def _copy_required(source: Path, destination: Path) -> None:
    """Copy one regular package input and reject symlink indirection.

        中文：复制一个普通 package 输入文件，并拒绝符号链接转向。
    """

    if source.is_symlink() or not source.is_file():
        raise NativePackageAssemblyError(
            f"required package file is missing or unsafe: {source}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def assemble_native_package(
    repository_root: Path,
    binary: Path,
    output_root: Path,
    rid: str,
) -> Path:
    """Assemble one unpacked IM Native AOT package.

        中文：组装一个未打包的 IM Native AOT package。
    """

    if rid not in SUPPORTED_RIDS:
        raise NativePackageAssemblyError(
            f"unsupported Native AOT RID {rid!r}; expected {sorted(SUPPORTED_RIDS)}"
        )
    repository_root = repository_root.resolve(strict=True)
    connector_root = repository_root / "plugins/connectors/im"
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise NativePackageAssemblyError(
            f"Native AOT binary is not executable: {binary}"
        )
    binary = binary.resolve(strict=True)
    output_root = output_root.resolve()
    if output_root.exists() and any(output_root.iterdir()):
        raise NativePackageAssemblyError(f"package output must be empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    for relative in PACKAGE_FILES:
        source = (
            repository_root / relative
            if relative.startswith("contracts/json/")
            else connector_root / relative
        )
        _copy_required(source, output_root / relative)
    _copy_required(
        connector_root / "plugin.manifest.json",
        output_root / "plugin.manifest.json",
    )
    _copy_required(
        connector_root / "package-descriptor.json",
        output_root / "package-descriptor.json",
    )
    executable = output_root / EXECUTABLE
    _copy_required(binary, executable)
    executable.chmod(executable.stat().st_mode | 0o111)

    _write_json(
        output_root / "plugin.manifest.json",
        _rewrite_manifest(_read_json(output_root / "plugin.manifest.json"), rid),
    )
    _write_json(
        output_root / "package-descriptor.json",
        _rewrite_descriptor(_read_json(output_root / "package-descriptor.json"), rid),
    )
    _write_json(
        output_root / "native-artifact.json",
        {
            "artifact_kind": "native-executable",
            "binary": EXECUTABLE,
            "byte_size": executable.stat().st_size,
            "plugin_id": PLUGIN_ID,
            "rid": rid,
            "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "version": PACKAGE_VERSION,
        },
    )
    return output_root


def build_native_package_archive(
    repository_root: Path,
    binary: Path,
    output_path: Path,
    rid: str,
) -> Path:
    """Build a deterministic ZIP archive for one IM Native AOT package.

        中文：为一个 IM Native AOT package 生成确定性 ZIP archive。
    """

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cyrene-im-native-package-") as temporary:
        package_root = assemble_native_package(
            repository_root, binary, Path(temporary) / "package", rid
        )
        with zipfile.ZipFile(
            output_path, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in sorted(package_root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(package_root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o100755 if relative == EXECUTABLE else 0o100644
                info.external_attr = mode << 16
                archive.writestr(info, path.read_bytes())
    return output_path


def _parse_args() -> argparse.Namespace:
    """Parse one candidate assembly request.

        中文：解析一项候选制品组装请求。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--rid", choices=sorted(SUPPORTED_RIDS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--directory", action="store_true")
    return parser.parse_args()


def main() -> int:
    """Assemble one IM Native AOT candidate without publishing it.

        中文：组装一个 IM Native AOT 候选制品，但不发布它。
    """

    args = _parse_args()
    if args.directory:
        assemble_native_package(
            args.repository_root, args.binary, args.output, args.rid
        )
    else:
        build_native_package_archive(
            args.repository_root, args.binary, args.output, args.rid
        )
    print(f"IM_NATIVE_PACKAGE_ASSEMBLY: PASS output={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
