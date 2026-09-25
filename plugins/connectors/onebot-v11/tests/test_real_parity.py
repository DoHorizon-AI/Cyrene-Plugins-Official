"""Tests for the fail-closed real OneBot parity verifier.

中文：对真实 OneBot 对等性验证器进行失败即拒绝测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

VERIFIER_PATH = Path(__file__).parents[4] / "tools/ci/onebot_reference_parity.py"
SPEC = importlib.util.spec_from_file_location("onebot_reference_parity", VERIFIER_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError("cannot load OneBot parity verifier")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def _trace(runtime: str) -> dict:
    """Build one complete semantic trace with deployment IDs.

        中文：构建一份包含部署标识的完整语义轨迹。"""

    profile = {
        "checks": {check: "PASS" for check in VERIFIER.REQUIRED_CHECKS},
        "actions": [
            {
                "action": "send_group_msg",
                "params": {"group_id": "20001", "message": [{"type": "text"}]},
            }
        ],
        "deliveries": [
            {
                "status": "ACCEPTED",
                "vendor_message_id": "runtime-specific-id",
            }
        ],
        "errors": [{"category": "TIMEOUT", "retryable": True}],
        "events": [
            {"event_type": "inbound_message", "timestamp": "runtime-specific-time"}
        ],
        "isolation": {"binding_id": "runtime-specific-binding"},
    }
    return {
        "schema": VERIFIER.TRACE_SCHEMA,
        "runtime": runtime,
        "source_revision": "a" * 40,
        "artifact_sha256": "b" * 64,
        "scenario_version": "onebot-real-v1",
        "profiles": {name: profile.copy() for name in VERIFIER.PROFILE_NAMES},
    }


def test_real_trace_comparison_normalizes_deployment_ids() -> None:
    """Runtime-generated IDs do not create false mismatches.

        中文：运行时生成的标识不会造成误报差异。"""

    result = VERIFIER.compare_traces(
        _trace("python-reference"), _trace("csharp-native-aot")
    )
    assert result["status"] == "PASS"
    assert result["profiles"] == list(VERIFIER.PROFILE_NAMES)


def test_real_trace_comparison_rejects_incomplete_profile() -> None:
    """A skipped required scenario blocks migration.

        中文：跳过必需场景会阻止迁移。"""

    python_trace = _trace("python-reference")
    python_trace["profiles"]["http_api"]["checks"]["request_event"] = "NOT_RUN"
    with pytest.raises(VERIFIER.ParityVerificationError, match="incomplete checks"):
        VERIFIER.compare_traces(python_trace, _trace("csharp-native-aot"))


def test_real_trace_comparison_rejects_semantic_difference() -> None:
    """Different action parameters cannot be hidden by normalization.

        中文：不同的动作参数不能通过规范化隐藏。"""

    native_trace = _trace("csharp-native-aot")
    native_trace["profiles"]["forward_websocket"]["actions"][0]["params"][
        "group_id"
    ] = "20002"
    with pytest.raises(VERIFIER.ParityVerificationError, match="trace mismatch"):
        VERIFIER.compare_traces(_trace("python-reference"), native_trace)
