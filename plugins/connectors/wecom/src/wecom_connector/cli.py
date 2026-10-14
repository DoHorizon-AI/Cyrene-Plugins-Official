"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 cli.py                                                          │
│  Package: wecom_connector                                           │
│  Role: Python wrapper for official @wecom/cli.                      │
│                                                                     │
│  模块职责：企业微信官方 CLI (@wecom/cli) 的 Python 结构化适配器。        │
│  · 运行环境探针：探测 wecom-cli / wecom 可执行文件路径与版本 (>=1.2.1)     │
│  · 授权管理：查询 wecom-cli auth show --status 与非交互初始化              │
│  · 服务能力发现：通过 --schema 动态读取 contact, calendar, todo 等服务结构 │
│  · 安全防护：遵循 wecomcli-shared 规范，对内部机器 ID 进行流转保护与隔离   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_EXECUTABLE_CANDIDATES = [
    "wecom-cli",
    "wecom",
    Path.home() / ".npm-global/bin/wecom-cli",
    Path.home() / ".npm-global/bin/wecom",
]

MIN_SUPPORTED_CLI_VERSION = "1.2.1"


class WeComCliError(RuntimeError):
    """Exception raised when a wecom-cli execution fails."""

    def __init__(
        self,
        message: str,
        returncode: int = 1,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class WeComCliStatus:
    available: bool
    version: str
    # "authorized" | "unauthorized" | "not_installed" | "unknown" | "error"
    auth_status: str
    executable_path: str


class WeComCliAuthorizationProcess:
    """Supervise the official QR onboarding process without capturing stderr."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process
        self.authorization_events: queue.Queue[tuple[str, str | None]] = queue.Queue(
            maxsize=512
        )
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="wecom-auth-output",
            daemon=True,
        )
        self._reader.start()

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                if len(line) > 4096:
                    line = line[:4096]
                try:
                    self.authorization_events.put_nowait(("line", line))
                except queue.Full:
                    continue
        finally:
            with suppress(queue.Full):
                self.authorization_events.put_nowait(("eof", None))


class WeComCliClient:
    """Wrapper and command dispatcher for @wecom/cli."""

    def __init__(
        self,
        executable_path: str | Path | None = None,
        config_dir: str | Path | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._executable_path = self._resolve_executable(executable_path)
        self._config_dir = Path(config_dir) if config_dir else None
        self._timeout = timeout_seconds

    def _resolve_executable(self, custom_path: str | Path | None) -> str | None:
        if custom_path:
            p = str(custom_path)
            if shutil.which(p) or os.path.isfile(p):
                return p

        for candidate in DEFAULT_EXECUTABLE_CANDIDATES:
            cand_str = str(candidate)
            resolved = shutil.which(cand_str)
            if resolved:
                return resolved
            if os.path.isfile(cand_str) and os.access(cand_str, os.X_OK):
                return cand_str

        return None

    def is_installed(self) -> bool:
        return self._executable_path is not None

    def get_version(self) -> str:
        """Query and return the CLI version string."""
        if not self.is_installed():
            raise WeComCliError("wecom-cli executable not found in PATH")
        res = self._run_raw(["--version"])
        match = re.search(r"wecom-cli\s+([0-9]+\.[0-9]+\.[0-9]+)", res.stdout)
        if match:
            return match.group(1)
        # fallback to raw trimmed output
        return res.stdout.strip()

    def get_auth_status(self) -> str:
        """Query exact CLI status; unexpected output never authorizes a profile."""
        if not self.is_installed():
            return "not_installed"
        try:
            res = self._run_raw(["auth", "show", "--status"])
            output = res.stdout.strip().lower()
            if output in {"authorized", "unauthorized"}:
                return output
            return "unknown"
        except WeComCliError as exc:
            logger.warning(
                "Failed to query wecom-cli auth status (exit code %d)",
                exc.returncode,
            )
            return "error"

    def get_profile_identity_fingerprint(self, binding_id: str) -> str:
        """Return a one-way fingerprint of the native CLI profile's Bot ID.

        The CLI's identity remains inside its configured profile. Only a hash
        scoped to this connector binding leaves this method.
        """
        result = self._run_raw(["auth", "show"])
        status_lines = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().lower().startswith("status:")
        ]
        identity_lines = [
            line.strip()[len("Bot ID:") :].strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("Bot ID:")
        ]
        if (
            len(status_lines) != 1
            or status_lines[0].lower() != "status: authorized"
            or len(identity_lines) != 1
        ):
            raise WeComCliError("wecom-cli profile identity is unavailable")
        identity = identity_lines[0]
        if (
            not identity
            or len(identity.encode("utf-8")) > 512
            or any(ord(character) < 32 for character in identity)
        ):
            raise WeComCliError("wecom-cli profile identity is invalid")
        material = f"{binding_id}\0{identity}".encode()
        return hashlib.sha256(material).hexdigest()

    def get_status(self) -> WeComCliStatus:
        """Return combined installation, version, and auth status."""
        installed = self.is_installed()
        if not installed:
            return WeComCliStatus(
                available=False,
                version="",
                auth_status="not_installed",
                executable_path="",
            )
        try:
            ver = self.get_version()
        except Exception:
            ver = "unknown"
        auth = self.get_auth_status()
        return WeComCliStatus(
            available=True,
            version=ver,
            auth_status=auth,
            executable_path=self._executable_path or "",
        )

    def get_service_schema(self, service: str) -> dict[str, Any]:
        """Fetch the JSON schema description for a service (e.g. calendar, todo)."""
        res = self._run_raw([service, "--schema"])
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise WeComCliError(
                f"Failed to parse JSON schema for service {service}: {exc}",
                stdout=res.stdout,
            ) from exc

    def execute(
        self,
        service: str,
        subcommand: str | None = None,
        args: list[str] | Mapping[str, Any] | None = None,
        *,
        json_output: bool = True,
    ) -> dict[str, Any] | str:
        """Execute a wecom-cli command and return parsed output."""
        if not self.is_installed():
            raise WeComCliError("wecom-cli executable not found")

        cmd_args = [service]
        if subcommand:
            cmd_args.append(subcommand)

        if isinstance(args, list):
            cmd_args.extend(args)
        elif isinstance(args, Mapping):
            for k, v in args.items():
                flag = f"--{k.replace('_', '-')}"
                if isinstance(v, bool):
                    if v:
                        cmd_args.append(flag)
                elif v is not None:
                    cmd_args.extend([flag, str(v)])

        res = self._run_raw(cmd_args)
        raw_out = res.stdout.strip()

        if json_output:
            try:
                return json.loads(raw_out)
            except json.JSONDecodeError:
                # Return wrapped raw output if command does not emit valid JSON
                return {"output": raw_out, "exit_code": 0}

        return raw_out

    def start_auth_init(self) -> WeComCliAuthorizationProcess:
        """Start only the official noninteractive QR flow for this profile.

        No caller controls the executable, arguments, working directory, or URL.
        The official CLI receives the binding-owned profile through its fixed
        environment variable. Its stderr is discarded because it can contain
        raw vendor diagnostics.
        """
        if not self.is_installed():
            raise WeComCliError("wecom-cli executable not found")
        command = [
            self._executable_path or "wecom-cli",
            "auth",
            "init",
            "--noninteractive",
            "--no-browser",
        ]
        env = self._child_environment()
        kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            raise WeComCliError("Failed to start official WeCom authorization") from exc
        return WeComCliAuthorizationProcess(process)

    def _run_raw(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if not self._executable_path:
            raise WeComCliError("wecom-cli executable not found")

        full_cmd = [self._executable_path, *args]
        env = self._child_environment()

        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise WeComCliError(
                f"wecom-cli command timed out after {self._timeout}s",
                returncode=124,
            ) from exc
        except OSError as exc:
            raise WeComCliError(
                f"Failed to spawn wecom-cli: {exc}", returncode=1
            ) from exc

        if result.returncode != 0:
            err_details = result.stderr or result.stdout
            raise WeComCliError(
                f"wecom-cli failed (exit code {result.returncode}): {err_details}",
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        return result

    def _child_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._config_dir:
            # The official CLI selects profiles via the child environment.
            # Ambient tokens/headers must not replace this binding's identity.
            # 中文:官方 CLI 通过子进程环境选择配置;宿主令牌和请求头不可覆盖绑定身份。
            env["WECOM_CLI_CONFIG_DIR"] = str(self._config_dir)
            for key in tuple(env):
                if (
                    key == "WECOM_CLI_ACCESS_TOKEN"
                    or key == "WECOM_CLI_ADDITIONAL_HEADERS"
                    or key.startswith("WECOM_CLI_ADDITIONAL_HEADERS_")
                ):
                    env.pop(key)
        return env
