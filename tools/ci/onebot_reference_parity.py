#!/usr/bin/env python3
"""Compare redacted Python and C# OneBot real-run traces.

This verifier is intentionally independent of either implementation.  It is
safe to ship inside the external Python reference artifact because it reads
only JSON traces and never imports the repository's source tree.

The verifier fails closed: a missing profile, an incomplete scenario, a
non-passing required check, a different canonical payload, or a different
error/event result is a migration blocker.

中文：比较经过脱敏的 Python 与 C# OneBot 实际运行轨迹。

中文：此验证器刻意独立于任一实现。它只读取 JSON 轨迹，且从不导入仓库源代码树，因此可以安全地随外部 Python 参考产物一起发布。

中文：验证器采用失败即拒绝策略：缺少配置、场景不完整、必需检查未通过、规范载荷不同，或错误/事件结果不同，都会阻止迁移。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

TRACE_SCHEMA = "cyrene.onebot.real-trace.v1"
COMPARISON_SCHEMA = "cyrene.onebot.real-parity.v1"
PROFILE_NAMES = ("http_api", "forward_websocket", "reverse_websocket")
REQUIRED_CHECKS = (
    "startup_auth_health",
    "private_send",
    "group_send",
    "inbound_message",
    "request_event",
    "request_response",
    "delivery_retcode_error",
    "timeout",
    "cancellation",
    "disconnect",
    "reconnect",
    "close",
    "parallel_binding_isolation",
)
_VOLATILE_KEYS = frozenset(
    {
        "connection_ref",
        "runtime_id",
        "vendor_message_id",
        "vendor_request_id",
        "message_id",
        "timestamp",
        "time",
        "received_at",
    }
)


class ParityVerificationError(ValueError):
    """Raised when real-run evidence cannot authorize migration.

        中文：当实际运行证据不足以批准迁移时引发。"""


def _read_json(path: Path) -> dict[str, Any]:
    """Read one JSON object without accepting malformed evidence.

        中文：读取一个 JSON 对象，不接受格式错误的证据。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ParityVerificationError(f"cannot read trace {path}: {error}") from error
    if not isinstance(value, dict):
        raise ParityVerificationError(f"trace root must be an object: {path}")
    return value


def _required_text(value: Any, field: str) -> str:
    """Require bounded non-empty text for evidence metadata.

        中文：要求证据元数据为非空且长度受限的文本。"""

    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ParityVerificationError(f"{field} must be bounded non-empty text")
    return value.strip()


def _normalize(value: Any, *, key: str | None = None) -> Any:
    """Normalize deployment-generated identifiers while preserving semantics.

        中文：规范化部署生成的标识，同时保留语义。"""

    if key in _VOLATILE_KEYS:
        return "<normalized>"
    if isinstance(value, dict):
        return {
            child_key: _normalize(child_value, key=child_key)
            for child_key, child_value in sorted(value.items())
            if child_key not in {"raw_endpoint", "access_token", "token"}
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _validate_trace(trace: dict[str, Any], label: str) -> dict[str, Any]:
    """Validate one complete runtime trace and return its normalized copy.

        中文：验证一份完整的运行时轨迹并返回其规范化副本。"""

    if trace.get("schema") != TRACE_SCHEMA:
        raise ParityVerificationError(f"{label} has an unsupported trace schema")
    runtime = _required_text(trace.get("runtime"), f"{label}.runtime")
    if runtime not in {"python-reference", "csharp-native-aot"}:
        raise ParityVerificationError(f"{label}.runtime is not an approved runtime")
    _required_text(trace.get("source_revision"), f"{label}.source_revision")
    _required_text(trace.get("artifact_sha256"), f"{label}.artifact_sha256")
    _required_text(trace.get("scenario_version"), f"{label}.scenario_version")
    profiles = trace.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(PROFILE_NAMES):
        raise ParityVerificationError(
            f"{label}.profiles must contain exactly {', '.join(PROFILE_NAMES)}"
        )

    normalized_profiles: dict[str, Any] = {}
    for profile_name in PROFILE_NAMES:
        profile = profiles[profile_name]
        if not isinstance(profile, dict):
            raise ParityVerificationError(f"{label}.{profile_name} must be an object")
        checks = profile.get("checks")
        if not isinstance(checks, dict) or set(checks) != set(REQUIRED_CHECKS):
            raise ParityVerificationError(
                f"{label}.{profile_name}.checks is incomplete"
            )
        failed = [
            check
            for check in REQUIRED_CHECKS
            if checks[check] != "PASS"
        ]
        if failed:
            raise ParityVerificationError(
                f"{label}.{profile_name} has incomplete checks: {', '.join(failed)}"
            )
        for key in ("actions", "deliveries", "errors", "events", "isolation"):
            if key not in profile:
                raise ParityVerificationError(
                    f"{label}.{profile_name} is missing semantic trace field {key}"
                )
        normalized_profiles[profile_name] = _normalize(profile)

    return {
        "schema": TRACE_SCHEMA,
        "runtime": runtime,
        "source_revision": trace["source_revision"],
        "artifact_sha256": trace["artifact_sha256"],
        "scenario_version": trace["scenario_version"],
        "profiles": normalized_profiles,
    }


def compare_traces(
    python_trace: dict[str, Any],
    native_trace: dict[str, Any],
) -> dict[str, Any]:
    """Compare two complete traces and return redacted migration evidence.

        中文：比较两份完整轨迹并返回脱敏的迁移证据。"""

    python_normalized = _validate_trace(python_trace, "python")
    native_normalized = _validate_trace(native_trace, "csharp")
    if python_normalized["scenario_version"] != native_normalized["scenario_version"]:
        raise ParityVerificationError("Python and C# scenario versions differ")
    for profile_name in PROFILE_NAMES:
        python_profile = python_normalized["profiles"][profile_name]
        native_profile = native_normalized["profiles"][profile_name]
        if python_profile != native_profile:
            raise ParityVerificationError(
                f"normalized trace mismatch in profile {profile_name}"
            )
    return {
        "schema": COMPARISON_SCHEMA,
        "status": "PASS",
        "scenario_version": python_normalized["scenario_version"],
        "python": {
            "source_revision": python_normalized["source_revision"],
            "artifact_sha256": python_normalized["artifact_sha256"],
        },
        "csharp": {
            "source_revision": native_normalized["source_revision"],
            "artifact_sha256": native_normalized["artifact_sha256"],
        },
        "profiles": list(PROFILE_NAMES),
        "normalized_semantics": python_normalized["profiles"],
    }


def _parse_args() -> argparse.Namespace:
    """Parse two external trace paths and a redacted output path.

        中文：解析两个外部轨迹路径和一个脱敏输出路径。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-trace", type=Path, required=True)
    parser.add_argument("--native-trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """Compare the two traces and write PASS evidence only on exact parity.

        中文：比较两份轨迹，只有完全一致时才写入 PASS 证据。"""

    args = _parse_args()
    try:
        result = compare_traces(
            _read_json(args.python_trace), _read_json(args.native_trace)
        )
    except ParityVerificationError as error:
        print(f"ONEBOT_REAL_PARITY: BLOCKED: {error}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
