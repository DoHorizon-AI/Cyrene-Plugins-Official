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
import subprocess
import sys
from pathlib import Path

PROTOCOL = "cyrene.package-dependency-preparer.v1"
PREPARER_ID = "cyrene.plugins.python-uv-v1"


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
    if not requirements or any(
        "==" not in requirement
        or any(marker in requirement for marker in ("<", ">", "*", "@", "!="))
        or "latest" in requirement.lower()
        for requirement in requirements
    ):
        raise ValueError("requirements.lock must contain exact immutable pins")
    return lock


def _run(command: list[str]) -> None:
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Python dependency preparation command failed with status {result.returncode}"
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
    if offline_wheelhouse is not None:
        install.extend(
            ["--no-index", "--find-links", str(offline_wheelhouse.resolve(strict=True))]
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
