"""Assemble the isolated Python IM parity reference package.

The formal package is the C# Native AOT artifact assembled by
``assemble_native_package.py``. This builder creates the Python implementation
reference used for cross-language parity and fake-Host TCK runs. It never
copies a QQ installation, native Host, account data, or credentials.

中文：组装隔离的 Python IM parity reference package。正式 package 是由 `assemble_native_package.py` 组装的 C# Native AOT 制品。此 builder 生成 Python 实现参考，用于跨语言 parity 和 fake-Host TCK。它绝不复制 QQ 安装、原生 Host、账户数据或凭据。
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

PACKAGE_VERSION = "0.1.0"
PYTHON_ENTRYPOINT = "qq_connector.plugin:ConnectorPlugin"
PACKAGE_RELATIVE_FILES = (
    "configuration.schema.json",
    "pyproject.toml",
    "requirements.lock",
)
SHARED_SCHEMA_FILES = (
    "contracts/json/message-connector-v1-inbound-request.schema.json",
    "contracts/json/message-connector-v1-request-response.schema.json",
)
LOCAL_SCHEMA_FILES = ("contracts/v1/schema.json",)


class PackageAssemblyError(ValueError):
    """Raised when a package candidate cannot be assembled safely.

        中文：无法安全组装 package 候选项时抛出的错误。
    """


def _read_json(path: Path) -> dict[str, Any]:
    """Read one package metadata document as a JSON object.

        中文：将一份 package 元数据文档作为 JSON 对象读取。
    """

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PackageAssemblyError(
            f"cannot read JSON metadata {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise PackageAssemblyError(f"JSON metadata is not an object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Write normalized package metadata with stable UTF-8 formatting.

        中文：使用稳定的 UTF-8 格式写入规范化 package 元数据。
    """

    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _rewrite_manifest_refs(manifest: dict[str, Any]) -> None:
    """Make shared schema references resolve from the package root.

        中文：使共享 schema 引用能够从 package 根目录解析。
    """

    for method in manifest.get("methods", []):
        if not isinstance(method, dict):
            continue
        for key in ("inputSchema", "outputSchema"):
            reference = method.get(key)
            if isinstance(reference, str):
                method[key] = reference.replace(
                    "../../../contracts/json/", "contracts/json/"
                )


def _rewrite_descriptor_refs(descriptor: dict[str, Any]) -> None:
    """Make descriptor references self-contained in an installed archive.

        中文：使 descriptor 引用在已安装 archive 中自包含。
    """

    package = descriptor.get("package")
    if isinstance(package, dict):
        package["manifest_ref"] = "plugin.manifest.json"
    configuration = descriptor.get("configuration")
    if isinstance(configuration, dict):
        configuration["schema_ref"] = "configuration.schema.json"

    runtime = descriptor.get("runtime")
    if isinstance(runtime, dict):
        runtime["kind"] = "subprocess-python"
        runtime["protocol"] = "cyrene.plugin.runtime.v1.DirectPluginRuntime"
        runtime["external"] = {
            "required": True,
            "kind": "official_qq_host_child",
            "artifact_ref": None,
        }
    implementation = descriptor.get("implementation")
    if isinstance(implementation, dict):
        implementation["entrypoint"] = PYTHON_ENTRYPOINT
    package = descriptor.get("package")
    if isinstance(package, dict):
        package["version"] = PACKAGE_VERSION
    for profile in descriptor.get("profiles", []):
        if not isinstance(profile, dict):
            continue
        profile_runtime = profile.get("runtime")
        if isinstance(profile_runtime, dict):
            profile_runtime["kind"] = "subprocess-python"
    lifecycle = descriptor.get("lifecycle")
    if isinstance(lifecycle, dict):
        lifecycle["rollback_identity"] = f"cyrene.connectors.im@{PACKAGE_VERSION}"
        lifecycle["cache_identity"] = f"cyrene.connectors.im@{PACKAGE_VERSION}"


def _rewrite_python_manifest(manifest: dict[str, Any]) -> None:
    """Project formal native metadata into a Python-only reference launcher.

        中文：将正式原生元数据投影为纯 Python reference 启动器。
    """

    manifest["version"] = PACKAGE_VERSION
    runtime = manifest.get("runtime")
    if isinstance(runtime, dict):
        runtime["language"] = "python"
        runtime["entrypoint"] = PYTHON_ENTRYPOINT
        runtime["launch"] = {
            "executable": "prepared-runtime",
            "args": [
                "-B",
                "src/cyrene_plugin_runtime/bootstrap.py",
                "--entrypoint",
                PYTHON_ENTRYPOINT,
            ],
        }
        runtime.pop("profiles", None)
    compatibility = manifest.get("compatibility")
    if isinstance(compatibility, dict):
        compatibility["requiresPython"] = ">=3.11"
        compatibility.pop("requiresDotnet", None)


def _copy_required_file(source: Path, destination: Path) -> None:
    """Copy one required file and fail instead of creating a partial package.

        中文：复制一个必需文件；若无法复制则失败，不生成不完整 package。
    """

    if not source.is_file():
        raise PackageAssemblyError(f"required package file is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def assemble_package(repository_root: Path, output_root: Path) -> Path:
    """Assemble one unpacked package root and return its resolved path.

    Args:
        repository_root: Plugins repository containing the connector and SDK.
        output_root: Empty directory that will receive the package payload.

    Returns:
        The resolved package root.

    Raises:
        PackageAssemblyError: If inputs are missing or output is not empty.

        中文：组装一个未打包的 package 根目录，并返回解析后的路径。

参数 `repository_root` 是包含 connector 和 SDK 的 Plugins 仓库；`output_root` 是接收 package 负载的空目录。返回解析后的 package 根目录。如果输入缺失或输出目录非空，则抛出 `PackageAssemblyError`。
    """

    repository_root = repository_root.resolve(strict=True)
    connector_root = repository_root / "plugins/connectors/im"
    runtime_root = (
        repository_root
        / "sdk/python/cyrene_plugin_runtime/src/cyrene_plugin_runtime"
    )
    output_root = output_root.resolve()
    if output_root.exists() and any(output_root.iterdir()):
        raise PackageAssemblyError(f"package output must be empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    for relative in PACKAGE_RELATIVE_FILES:
        _copy_required_file(connector_root / relative, output_root / relative)
    _copy_required_file(connector_root / "README.md", output_root / "README.md")
    for relative in ("plugin.manifest.json", "package-descriptor.json"):
        _copy_required_file(connector_root / relative, output_root / relative)
    for relative in LOCAL_SCHEMA_FILES:
        _copy_required_file(connector_root / relative, output_root / relative)
    for relative in SHARED_SCHEMA_FILES:
        _copy_required_file(repository_root / relative, output_root / relative)

    source_package = connector_root / "src/qq_connector"
    if not source_package.is_dir():
        raise PackageAssemblyError(
            f"connector reference package is missing: {source_package}"
        )
    shutil.copytree(
        source_package,
        output_root / "src/qq_connector",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
    )
    if not runtime_root.is_dir():
        raise PackageAssemblyError(f"SDK runtime package is missing: {runtime_root}")
    shutil.copytree(
        runtime_root,
        output_root / "src/cyrene_plugin_runtime",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
    )

    manifest_path = output_root / "plugin.manifest.json"
    manifest = _read_json(manifest_path)
    _rewrite_manifest_refs(manifest)
    _rewrite_python_manifest(manifest)
    _write_json(manifest_path, manifest)
    descriptor_path = output_root / "package-descriptor.json"
    descriptor = _read_json(descriptor_path)
    _rewrite_descriptor_refs(descriptor)
    _write_json(descriptor_path, descriptor)
    return output_root


def build_package_archive(repository_root: Path, output_path: Path) -> Path:
    """Build a deterministic ZIP archive from an assembled package candidate.

        中文：根据已组装的 package 候选项生成确定性 ZIP archive。
    """

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cyrene-onebot-package-") as temporary:
        package_root = assemble_package(repository_root, Path(temporary) / "package")
        with zipfile.ZipFile(
            output_path, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in sorted(package_root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(package_root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
    return output_path


def _parse_args() -> argparse.Namespace:
    """Parse the package candidate command-line arguments.

        中文：解析 package 候选项的命令行参数。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--directory",
        action="store_true",
        help="write an unpacked package directory instead of a ZIP archive",
    )
    return parser.parse_args()


def main() -> int:
    """Build one candidate package without publishing or mutating repository files.

        中文：构建一个候选 package，但不发布制品，也不修改仓库文件。
    """

    args = _parse_args()
    if args.directory:
        assemble_package(args.repository_root, args.output)
    else:
        build_package_archive(args.repository_root, args.output)
    print(f"PACKAGE_ASSEMBLY: PASS output={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
