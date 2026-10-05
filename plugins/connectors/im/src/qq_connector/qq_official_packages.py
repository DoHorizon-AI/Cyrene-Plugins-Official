"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 qq_official_packages.py                                         │
│  Module: qq_connector.qq_official_packages                           │
│  Role: Manifest-locked Tencent QQ installer staging and rollback.   │
│                                                                     │
│  模块职责：按可信清单校验腾讯 QQ 原包并安全暂存、切换和回滚。          │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PACKAGE_LOCK_SCHEMA = "cyrene.qq.package-lock.v1"
SUPPORTED_PACKAGE_TARGETS = frozenset(
    {"linux-x86_64", "linux-aarch64", "windows-x86_64"}
)
MAX_LOCK_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_ALLOWED_PACKAGE_FORMATS = {
    "linux-x86_64": frozenset({"deb", "rpm", "appimage"}),
    "linux-aarch64": frozenset({"deb", "rpm", "appimage"}),
    "windows-x86_64": frozenset({"exe", "msi"}),
}


class QQPackageError(RuntimeError):
    """Typed failure from official QQ package locking or staging.

    腾讯 QQ 官方原包清单校验或暂存失败时的类型化错误。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class OfficialQQPackage:
    """One immutable package record from an operator-trusted lock.

    由操作员信任的 lock 文件提供的一条不可变 package 记录。
    """

    target: str
    client_version: str
    package_format: str
    filename: str
    download_url: str
    source_page: str
    sha256: str
    size_bytes: int | None


def load_package_lock(path: str | Path) -> tuple[OfficialQQPackage, ...]:
    """Read one owner-controlled manifest and validate exact package pins.

    Every URL, version and SHA-256 is supplied by the trusted manifest. This
    module accepts HTTPS only and rejects any source outside Tencent-owned QQ
    and GTIMG domains. It never discovers a latest version or invents a URL.

    读取一份由当前用户控制的 manifest，并校验准确的 package pin。所有下载 URL、
    版本和 SHA-256 都必须由可信 manifest 提供。这里只接受 HTTPS 和腾讯官方域名，
    不会自动发现最新版本，也不会构造下载地址。
    """

    lock_path = _trusted_lock_path(path)
    try:
        raw = lock_path.read_bytes()
    except OSError as exc:
        raise QQPackageError(
            "PACKAGE_LOCK_UNAVAILABLE", "QQ package lock cannot be read"
        ) from exc
    if len(raw) > MAX_LOCK_BYTES:
        raise QQPackageError(
            "INVALID_REQUEST", "QQ package lock exceeds the size limit"
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QQPackageError(
            "INVALID_REQUEST", "QQ package lock must be UTF-8 JSON"
        ) from exc
    if not isinstance(value, Mapping) or set(value).difference(
        {"schema", "publisher", "packages"}
    ):
        raise QQPackageError("INVALID_REQUEST", "QQ package lock has an invalid shape")
    if (
        value.get("schema") != PACKAGE_LOCK_SCHEMA
        or value.get("publisher") != "Tencent"
    ):
        raise QQPackageError(
            "UNSUPPORTED_VERSION", "QQ package lock schema or publisher is unsupported"
        )
    entries = value.get("packages")
    if not isinstance(entries, list) or len(entries) > len(SUPPORTED_PACKAGE_TARGETS):
        raise QQPackageError(
            "INVALID_REQUEST", "QQ package lock packages must be a bounded list"
        )
    packages: list[OfficialQQPackage] = []
    seen_targets: set[str] = set()
    for entry in entries:
        packages.append(_parse_package_entry(entry, seen_targets))
    return tuple(packages)


class QQOfficialPackageManager:
    """Stage original Tencent installers by digest and switch active versions.

    `stage` downloads only an exact locked artifact, verifies its digest, and
    atomically makes its immutable cache entry active. `rollback` switches the
    active pointer to the previous verified cache entry. It never extracts or
    launches an installer, runs a shell, or claims that a QQ Host is ready.

    按摘要暂存腾讯官方原始安装包，并切换当前版本。`stage` 只下载 lock 指定文件、
    校验摘要并原子更新缓存指针；`rollback` 将指针切回之前已校验的缓存项。它不会解包、
    启动 installer、执行 shell，也不会宣称 QQ Host 已就绪。
    """

    def __init__(
        self,
        lock_path: str | Path,
        store_root: str | Path,
        *,
        opener: Callable[[urllib.request.Request, int], Any] | None = None,
    ) -> None:
        self.lock_path = Path(lock_path)
        self.store_root = _secure_store_root(store_root)
        self._opener = opener or _open_official_url

    def stage(self, target: str) -> dict[str, Any]:
        """Fetch, verify, atomically stage and select one locked installer.

        下载、校验并原子暂存一份 lock 中的 installer，然后将它设为当前选择。
        """

        package = self._package_for_target(target)
        target_root = self.store_root / "packages" / target
        target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _ensure_private_directory(target_root)
        final_directory = target_root / package.client_version
        if final_directory.exists():
            if final_directory.is_symlink() or not final_directory.is_dir():
                raise QQPackageError(
                    "INVALID_REQUEST", "staged QQ package directory is unsafe"
                )
            existing = final_directory / package.filename
            if (
                existing.is_symlink()
                or not existing.is_file()
                or _file_sha256(existing) != package.sha256
            ):
                raise QQPackageError(
                    "PACKAGE_CONFLICT",
                    "a different QQ package already uses this version",
                )
            self._activate(package, final_directory)
            return self._stage_report(package, existing, reused=True)

        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=target_root))
        try:
            staged_file = staging / package.filename
            self._download(package, staged_file)
            _write_json_fsync(
                staging / "package.json",
                {
                    "schema": PACKAGE_LOCK_SCHEMA,
                    "target": package.target,
                    "client_version": package.client_version,
                    "package_format": package.package_format,
                    "filename": package.filename,
                    "sha256": package.sha256,
                    "size_bytes": staged_file.stat().st_size,
                },
            )
            os.replace(staging, final_directory)
            _fsync_directory(target_root)
            self._activate(package, final_directory)
            return self._stage_report(
                package, final_directory / package.filename, reused=False
            )
        except FileExistsError:
            raise QQPackageError(
                "PACKAGE_CONFLICT", "QQ package version was staged concurrently"
            ) from None
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def rollback(self, target: str) -> dict[str, Any]:
        """Atomically restore the previous verified cache selection.

        原子恢复上一个已校验的缓存版本选择。
        """

        _validate_target(target)
        with _store_lock(self.store_root):
            active_path = self.store_root / "active" / f"{target}.json"
            previous_path = self.store_root / "previous" / f"{target}.json"
            current = _read_pointer(active_path)
            previous = _read_pointer(previous_path)
            if previous is None:
                raise QQPackageError(
                    "ROLLBACK_UNAVAILABLE", "no previous QQ package is staged"
                )
            previous_file = _resolve_pointer_file(self.store_root, target, previous)
            if current is not None:
                _atomic_write_json(previous_path, current)
            _atomic_write_json(active_path, previous)
            return {
                "status": "STAGED_ROLLBACK",
                "target": target,
                "client_version": previous["client_version"],
                "sha256": previous["sha256"],
                "installer_path": str(previous_file),
                "runtime_ready": False,
            }

    def status(self, target: str) -> dict[str, Any]:
        """Report active and previous verified installers without exposing URLs.

        报告当前和上一个已校验 installer，不公开下载 URL。
        """

        _validate_target(target)
        active = _read_pointer(self.store_root / "active" / f"{target}.json")
        previous = _read_pointer(self.store_root / "previous" / f"{target}.json")
        return {
            "status": "STAGED" if active is not None else "NOT_CONFIGURED",
            "target": target,
            "active_version": active.get("client_version") if active else None,
            "active_sha256": active.get("sha256") if active else None,
            "previous_version": previous.get("client_version") if previous else None,
            "runtime_ready": False,
        }

    def _package_for_target(self, target: str) -> OfficialQQPackage:
        _validate_target(target)
        packages = load_package_lock(self.lock_path)
        package = next((item for item in packages if item.target == target), None)
        if package is None:
            raise QQPackageError(
                "NOT_CONFIGURED", f"no trusted QQ package lock entry for {target}"
            )
        return package

    def _download(self, package: OfficialQQPackage, destination: Path) -> None:
        request = urllib.request.Request(
            package.download_url,
            headers={"User-Agent": "CyreneQQPackageManager/1"},
            method="GET",
        )
        digest = hashlib.sha256()
        total = 0
        try:
            response = self._opener(request, DOWNLOAD_TIMEOUT_SECONDS)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise QQPackageError(
                "DOWNLOAD_FAILED", "Tencent QQ package download failed"
            ) from exc
        with response:
            final_url = getattr(response, "url", package.download_url)
            _validate_tencent_url(final_url, "download redirect")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise QQPackageError(
                        "DOWNLOAD_FAILED", "QQ package size header is invalid"
                    ) from exc
                if declared_size < 1 or declared_size > MAX_PACKAGE_BYTES:
                    raise QQPackageError(
                        "DOWNLOAD_FAILED", "QQ package exceeds the size limit"
                    )
                if (
                    package.size_bytes is not None
                    and declared_size != package.size_bytes
                ):
                    raise QQPackageError(
                        "CHECKSUM_MISMATCH", "QQ package size does not match the lock"
                    )
            with destination.open("xb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PACKAGE_BYTES:
                        raise QQPackageError(
                            "DOWNLOAD_FAILED", "QQ package exceeds the size limit"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if total == 0 or package.size_bytes is not None and total != package.size_bytes:
            raise QQPackageError(
                "CHECKSUM_MISMATCH", "QQ package size does not match the lock"
            )
        if digest.hexdigest() != package.sha256:
            raise QQPackageError(
                "CHECKSUM_MISMATCH", "QQ package SHA-256 does not match the lock"
            )

    def _activate(self, package: OfficialQQPackage, directory: Path) -> None:
        active_path = self.store_root / "active" / f"{package.target}.json"
        previous_path = self.store_root / "previous" / f"{package.target}.json"
        active_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _ensure_private_directory(active_path.parent)
        _ensure_private_directory(previous_path.parent)
        entry = {
            "target": package.target,
            "client_version": package.client_version,
            "package_format": package.package_format,
            "filename": package.filename,
            "sha256": package.sha256,
            "relative_path": str(directory.relative_to(self.store_root)),
        }
        with _store_lock(self.store_root):
            active = _read_pointer(active_path)
            if active is not None and active.get("sha256") != package.sha256:
                _atomic_write_json(previous_path, active)
            _atomic_write_json(active_path, entry)

    @staticmethod
    def _stage_report(
        package: OfficialQQPackage, artifact: Path, *, reused: bool
    ) -> dict[str, Any]:
        return {
            "status": "STAGED",
            "target": package.target,
            "client_version": package.client_version,
            "package_format": package.package_format,
            "sha256": package.sha256,
            "installer_path": str(artifact),
            "reused": reused,
            "runtime_ready": False,
        }


def target_for_current_platform() -> str:
    """Return one explicit supported target identifier for this host.

    返回当前主机唯一、明确支持的 target 标识。
    """

    if sys.platform.startswith("linux"):
        architecture = os.uname().machine.lower()
        target = (
            "linux-aarch64" if architecture in {"aarch64", "arm64"} else "linux-x86_64"
        )
    elif sys.platform == "win32":
        architecture = os.environ.get(
            "PROCESSOR_ARCHITEW6432",
            os.environ.get("PROCESSOR_ARCHITECTURE", ""),
        ).lower()
        target = "windows-x86_64" if architecture in {"amd64", "x86_64", "x64"} else ""
    else:
        target = ""
    _validate_target(target)
    return target


def _parse_package_entry(value: Any, seen_targets: set[str]) -> OfficialQQPackage:
    """Validate one closed package record and official Tencent source URL.

    校验一条字段封闭的 package 记录及腾讯官方源 URL。
    """

    fields = {
        "target",
        "client_version",
        "package_format",
        "filename",
        "download_url",
        "source_page",
        "sha256",
        "size_bytes",
    }
    if (
        not isinstance(value, Mapping)
        or set(value).difference(fields)
        or not fields.difference({"size_bytes"}).issubset(value)
    ):
        raise QQPackageError(
            "INVALID_REQUEST", "QQ package lock entry has an invalid shape"
        )
    target = value.get("target")
    _validate_target(target)
    if target in seen_targets:
        raise QQPackageError("INVALID_REQUEST", "QQ package lock has duplicate targets")
    seen_targets.add(target)
    client_version = _bounded_text(value.get("client_version"), "client_version", 128)
    if _SAFE_NAME_PATTERN.fullmatch(client_version) is None:
        raise QQPackageError(
            "INVALID_REQUEST", "QQ client version has an invalid format"
        )
    package_format = value.get("package_format")
    if package_format not in _ALLOWED_PACKAGE_FORMATS[target]:
        raise QQPackageError(
            "UNSUPPORTED_VERSION", "QQ package format is unsupported for target"
        )
    filename = _bounded_text(value.get("filename"), "filename", 128)
    if _SAFE_NAME_PATTERN.fullmatch(filename) is None:
        raise QQPackageError("INVALID_REQUEST", "QQ package filename is unsafe")
    download_url = _bounded_text(value.get("download_url"), "download_url", 2048)
    source_page = _bounded_text(value.get("source_page"), "source_page", 2048)
    _validate_tencent_url(download_url, "download_url")
    _validate_tencent_url(source_page, "source_page")
    checksum = value.get("sha256")
    if not isinstance(checksum, str) or _SHA256_PATTERN.fullmatch(checksum) is None:
        raise QQPackageError(
            "INVALID_REQUEST", "QQ package SHA-256 must be lowercase hex"
        )
    size_bytes = value.get("size_bytes")
    if "size_bytes" in value and (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 1 <= size_bytes <= MAX_PACKAGE_BYTES
    ):
        raise QQPackageError("INVALID_REQUEST", "QQ package size_bytes is invalid")
    return OfficialQQPackage(
        target=target,
        client_version=client_version,
        package_format=package_format,
        filename=filename,
        download_url=download_url,
        source_page=source_page,
        sha256=checksum,
        size_bytes=size_bytes,
    )


def _trusted_lock_path(value: str | Path) -> Path:
    """Require one existing lock owned and not writable by other POSIX users.

    要求 lock 文件存在，且 POSIX 上由当前用户所有、其他用户不可写。
    """

    path = Path(value)
    if not path.is_absolute():
        raise QQPackageError("INVALID_REQUEST", "QQ package lock path must be absolute")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise QQPackageError(
            "PACKAGE_LOCK_UNAVAILABLE", "QQ package lock does not exist"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise QQPackageError(
            "INVALID_REQUEST", "QQ package lock must be a regular non-symlink file"
        )
    if os.name == "posix" and (
        metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022
    ):
        raise QQPackageError(
            "UNTRUSTED_MANIFEST",
            "QQ package lock must be owner-controlled and not group/world writable",
        )
    return path


def _validate_tencent_url(value: str, field: str) -> None:
    """Accept HTTPS URLs only from Tencent's QQ or GTIMG domain families.

    仅接受来自腾讯 QQ 或 GTIMG 域名族的 HTTPS URL。
    """

    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise QQPackageError("INVALID_REQUEST", f"QQ {field} is invalid") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not _is_tencent_hostname(hostname)
    ):
        raise QQPackageError(
            "UNTRUSTED_SOURCE", f"QQ {field} must use an official Tencent HTTPS domain"
        )


def _is_tencent_hostname(hostname: str) -> bool:
    return (
        hostname == "qq.com"
        or hostname.endswith(".qq.com")
        or hostname == "gtimg.cn"
        or hostname.endswith(".gtimg.cn")
    )


def _validate_target(target: Any) -> None:
    if not isinstance(target, str) or target not in SUPPORTED_PACKAGE_TARGETS:
        raise QQPackageError("UNSUPPORTED_TARGET", "QQ package target is unsupported")


def _bounded_text(value: Any, field: str, max_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > max_bytes
    ):
        raise QQPackageError("INVALID_REQUEST", f"QQ package {field} is invalid")
    return value


def _secure_store_root(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise QQPackageError(
            "INVALID_REQUEST", "QQ package store path must be absolute"
        )
    if path.exists() and path.is_symlink():
        raise QQPackageError(
            "INVALID_REQUEST", "QQ package store must not be a symlink"
        )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ensure_private_directory(path)
    return path.resolve(strict=True)


def _ensure_private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise QQPackageError(
            "INVALID_REQUEST", "QQ package store contains an unsafe directory"
        )
    if os.name == "posix" and (
        metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077
    ):
        raise QQPackageError(
            "UNTRUSTED_STORE", "QQ package store directories must be owner-only"
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _write_json_fsync(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _read_pointer(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) > 16 * 1024:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if not all(
        isinstance(value.get(key), str)
        for key in ("target", "client_version", "filename", "sha256", "relative_path")
    ):
        return None
    return value


def _resolve_pointer_file(
    store_root: Path, target: str, pointer: Mapping[str, Any]
) -> Path:
    _validate_target(target)
    relative = pointer.get("relative_path")
    if not isinstance(relative, str):
        raise QQPackageError(
            "ROLLBACK_UNAVAILABLE", "previous QQ package pointer is invalid"
        )
    candidate = (store_root / relative).resolve(strict=True)
    try:
        candidate.relative_to(store_root.resolve(strict=True))
    except ValueError as exc:
        raise QQPackageError(
            "ROLLBACK_UNAVAILABLE", "previous QQ package path is unsafe"
        ) from exc
    filename = pointer.get("filename")
    if not isinstance(filename, str) or _SAFE_NAME_PATTERN.fullmatch(filename) is None:
        raise QQPackageError(
            "ROLLBACK_UNAVAILABLE", "previous QQ package filename is invalid"
        )
    artifact = candidate / filename
    if (
        artifact.is_symlink()
        or not artifact.is_file()
        or _file_sha256(artifact) != pointer.get("sha256")
    ):
        raise QQPackageError(
            "ROLLBACK_UNAVAILABLE", "previous QQ package failed digest validation"
        )
    return artifact


@contextmanager
def _store_lock(store_root: Path) -> Iterator[None]:
    """Serialize cache pointer writes across worker processes.

    跨 worker 进程串行化缓存指针写入。
    """

    lock_path = store_root / ".manager.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        elif os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        yield
    finally:
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_official_url(request: urllib.request.Request, timeout: int) -> Any:
    """Open one pinned Tencent URL and validate every redirect.

    打开一个已 pin 的腾讯 URL，并验证每次重定向。
    """

    class TencentRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: int, msg: str, headers: Any, new_url: str
        ) -> Any:
            _validate_tencent_url(new_url, "redirect")
            return super().redirect_request(req, fp, code, msg, headers, new_url)

    opener = urllib.request.build_opener(TencentRedirectHandler())
    return opener.open(request, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    """Run the local installer cache manager without invoking a shell.

    运行本地 installer 缓存管理器，不会调用 shell。
    """

    parser = argparse.ArgumentParser(prog="qq-connector-package")
    parser.add_argument("command", choices=("stage", "rollback", "status"))
    parser.add_argument("--lock", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--target", default=None)
    args = parser.parse_args(argv)
    try:
        target = args.target or target_for_current_platform()
        manager = QQOfficialPackageManager(args.lock, args.store)
        result = {
            "stage": lambda: manager.stage(target),
            "rollback": lambda: manager.rollback(target),
            "status": lambda: manager.status(target),
        }[args.command]()
    except QQPackageError as exc:
        print(
            json.dumps(
                {"status": exc.code, "message": exc.message}, separators=(",", ":")
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
