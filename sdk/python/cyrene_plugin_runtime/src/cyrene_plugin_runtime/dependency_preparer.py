"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 dependency_preparer.py                                          │
│  Module: cyrene_plugin_runtime                                      │
│  Role: Prepare a lock-addressed Python runtime for Plugin packages. │
│                                                                     │
│  模块职责：为 Plugin 包准备按依赖锁寻址的 Python 运行时。                │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path

PROTOCOL = "cyrene.package-dependency-preparer.v1"
PREPARER_ID = "cyrene.plugins.python-uv-v1"
WHEELHOUSE_TARGET = "linux-ubuntu-24.04-x86_64-python-3.12"
_LOCK_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)$")


def _sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _runtime_executable(runtime_root: Path) -> Path:
    relative = (
        Path("Scripts/python.exe") if sys.platform == "win32" else Path("bin/python")
    )
    return runtime_root / relative


def _validate_lock(package_root: Path, expected_digest: str) -> Path:
    lock = package_root / "requirements.lock"
    payload = lock.read_bytes()
    if _sha256_bytes(payload) != expected_digest:
        raise ValueError("requirements.lock does not match the authorized digest")
    requirements = [
        line.strip()
        for line in payload.decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    matches = [_LOCK_PIN.fullmatch(requirement) for requirement in requirements]
    if not requirements or any(match is None for match in matches):
        raise ValueError("requirements.lock must contain exact immutable pins")
    names = [_canonicalize_name(match.group(1)) for match in matches if match]
    if len(names) != len(set(names)):
        raise ValueError("requirements.lock repeats a distribution")
    return lock


def _canonicalize_name(name: str) -> str:
    """Apply the PyPA distribution-name normalization without extra dependencies."""

    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return f"sha256:{digest.hexdigest()}", size


def _validate_wheelhouse(
    wheelhouse: Path, *, lock: Path, expected_digest: str
) -> Path:
    """Verify the lock-bound wheel list, file hashes, and wheel metadata."""

    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ValueError("offline wheelhouse must be a regular directory")
    manifest_path = wheelhouse / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("offline wheelhouse manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        lock_bytes = lock.read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"offline wheelhouse manifest is invalid: {error}") from error
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {"schemaVersion", "target", "requirementsLockSha256", "wheels"}
        or manifest.get("schemaVersion") != 1
        or manifest.get("target") != WHEELHOUSE_TARGET
        or manifest.get("requirementsLockSha256") != expected_digest
    ):
        raise ValueError("offline wheelhouse is not bound to this target and lock")

    expected: dict[str, str] = {}
    for line in lock_bytes.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_PIN.fullmatch(line)
        if match is None:
            raise ValueError("requirements.lock must contain exact immutable pins")
        expected[_canonicalize_name(match.group(1))] = match.group(2)

    wheels = manifest.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        raise ValueError("offline wheelhouse manifest has no wheel records")
    listed: set[str] = set()
    found: dict[str, str] = {}
    for record in wheels:
        if not isinstance(record, dict) or set(record) != {
            "filename",
            "name",
            "version",
            "sha256",
            "size",
        }:
            raise ValueError("offline wheelhouse contains an invalid wheel record")
        filename = record.get("filename")
        name = record.get("name")
        version = record.get("version")
        digest = record.get("sha256")
        size = record.get("size")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or not filename.endswith(".whl")
            or not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
        ):
            raise ValueError("offline wheelhouse contains an unsafe wheel record")
        canonical_name = _canonicalize_name(name)
        if canonical_name in found or expected.get(canonical_name) != version:
            raise ValueError("offline wheelhouse does not match locked distributions")
        wheel_parts = filename[:-4].split("-")
        if (
            len(wheel_parts) != 5
            or _canonicalize_name(wheel_parts[0]) != canonical_name
            or wheel_parts[1] != version
        ):
            raise ValueError("offline wheel filename does not match its locked identity")
        path = wheelhouse / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"offline wheel is missing or unsafe: {filename}")
        actual_digest, actual_size = _sha256_file(path)
        if actual_digest != digest or actual_size != size:
            raise ValueError(f"offline wheel hash or size mismatch: {filename}")
        try:
            with zipfile.ZipFile(path) as archive:
                metadata_names = [
                    item
                    for item in archive.namelist()
                    if item.endswith(".dist-info/METADATA")
                ]
                if len(metadata_names) != 1:
                    raise ValueError("wheel must contain exactly one METADATA file")
                metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        except (OSError, UnicodeError, zipfile.BadZipFile) as error:
            raise ValueError(f"offline wheel is invalid: {filename}") from error
        metadata_name = metadata.get("Name")
        metadata_version = metadata.get("Version")
        if (
            not isinstance(metadata_name, str)
            or not isinstance(metadata_version, str)
            or _canonicalize_name(metadata_name) != canonical_name
            or metadata_version != version
        ):
            raise ValueError(f"offline wheel metadata identity mismatch: {filename}")
        found[canonical_name] = filename
        listed.add(filename)

    if set(found) != set(expected):
        missing = sorted(set(expected).difference(found))
        raise ValueError(f"offline wheelhouse is incomplete for locked packages: {missing}")
    actual_entries = {
        item.name
        for item in wheelhouse.iterdir()
        if item.name != "manifest.json"
    }
    if actual_entries != listed or any(
        item.is_symlink() or not item.is_file() for item in wheelhouse.iterdir()
    ):
        raise ValueError("offline wheelhouse contains unlisted or unsafe files")
    if not lock_bytes:
        raise ValueError("requirements.lock is empty")
    return wheelhouse


def _resolve_offline_wheelhouse(
    package_root: Path,
    *,
    lock: Path,
    lock_digest: str,
    explicit: Path | None,
) -> Path | None:
    """Validate packaged wheels while retaining the legacy external override."""

    packaged = package_root / "wheelhouse"
    if explicit is None:
        if not packaged.exists() and not packaged.is_symlink():
            return None
        return _validate_wheelhouse(packaged, lock=lock, expected_digest=lock_digest)
    if packaged.exists() and explicit.resolve(strict=True) == packaged.resolve(strict=True):
        return _validate_wheelhouse(explicit, lock=lock, expected_digest=lock_digest)
    resolved = explicit.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("explicit offline wheelhouse must be a directory")
    return resolved


def _run(command: list[str]) -> None:
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        if len(detail) > 2000:
            detail = detail[-2000:]
        raise RuntimeError(
            "Python dependency preparation command failed with status "
            f"{result.returncode}" + (f": {detail}" if detail else "")
        )


def _python_identity(executable: Path) -> bytes:
    result = subprocess.run(
        [
            str(executable),
            "-I",
            "-c",
            (
                "import platform,sys; print(sys.implementation.name); "
                "print(sys.version); print(platform.platform())"
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 4096:
        raise RuntimeError("prepared Python runtime identity is unavailable")
    return result.stdout


def prepare(
    *,
    package_root: Path,
    runtime_root: Path,
    lock_digest: str,
    python: str,
    uv: str,
    offline_wheelhouse: Path | None,
) -> dict[str, object]:
    """Create one lock-addressed venv and return Platform-neutral evidence.

    创建按依赖锁寻址的虚拟环境，并返回与语言无关的 Platform 证据。

    Args:
        package_root: Verified package root containing ``requirements.lock``.
        runtime_root: Staging directory owned by Platform for adapter output.
        lock_digest: Authorized lowercase SHA-256 dependency-lock digest.
        python: Python interpreter used to create the virtual environment.
        uv: Exact ``uv`` executable selected by the deployment.
        offline_wheelhouse: Optional verified local wheel source.

    Returns:
        A ``cyrene.package-dependency-preparer.v1`` evidence document.
    """

    package_root = package_root.resolve(strict=True)
    runtime_root.mkdir(parents=True, exist_ok=True)
    lock = _validate_lock(package_root, lock_digest)
    wheelhouse = _resolve_offline_wheelhouse(
        package_root,
        lock=lock,
        lock_digest=lock_digest,
        explicit=offline_wheelhouse,
    )
    runtime_python = _runtime_executable(runtime_root)

    _run([uv, "venv", "--allow-existing", "--python", python, str(runtime_root)])
    install = [
        uv,
        "pip",
        "install",
        "--python",
        str(runtime_python),
        "--no-deps",
        "--requirement",
        str(lock),
    ]
    if wheelhouse is not None:
        install.extend(
            ["--no-index", "--find-links", str(wheelhouse.resolve(strict=True))]
        )
    _run(install)
    if not runtime_python.is_file():
        raise RuntimeError("prepared Python runtime executable is missing")

    relative_runtime = runtime_python.relative_to(runtime_root).as_posix()
    runtime_identity = b"\0".join(
        (
            PREPARER_ID.encode("utf-8"),
            lock_digest.encode("ascii"),
            relative_runtime.encode("utf-8"),
            _python_identity(runtime_python),
        )
    )
    return {
        "protocol": PROTOCOL,
        "preparer": PREPARER_ID,
        "runtime_digest": _sha256_bytes(runtime_identity),
        "runtime_executable": relative_runtime,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--lock-digest", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--offline-wheelhouse", type=Path)
    options = parser.parse_args()
    try:
        evidence = prepare(
            package_root=options.package_root,
            runtime_root=options.runtime_root,
            lock_digest=options.lock_digest,
            python=options.python,
            uv=options.uv,
            offline_wheelhouse=options.offline_wheelhouse,
        )
    except (OSError, UnicodeError, ValueError, RuntimeError) as error:
        print(f"cyrene-plugin-python-preparer: {error}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
