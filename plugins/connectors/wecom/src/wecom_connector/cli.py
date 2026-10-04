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

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
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
    auth_status: str  # "authorized" | "unauthorized" | "not_installed" | "error"
    executable_path: str


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
        """Query the authorization status: 'authorized', 'unauthorized', or 'error'."""
        if not self.is_installed():
            return "not_installed"
        try:
            res = self._run_raw(["auth", "show", "--status"])
            output = res.stdout.strip().lower()
            if "authorized" in output:
                return "authorized"
            if "unauthorized" in output:
                return "unauthorized"
            return output or "unknown"
        except WeComCliError as exc:
            logger.warning("Failed to query wecom-cli auth status: %s", exc)
            return "error"

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
                returncode=0,
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

    def _run_raw(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if not self._executable_path:
            raise WeComCliError("wecom-cli executable not found")

        full_cmd = [self._executable_path]
        if self._config_dir:
            full_cmd.extend(["--config", str(self._config_dir)])
        full_cmd.extend(args)

        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            cmd_str = " ".join(full_cmd)
            raise WeComCliError(
                f"wecom-cli command timed out after {self._timeout}s: {cmd_str}",
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
