"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 errors.py                                                       │
│  Module: cyrene_plugin_runtime.errors                               │
│  Role: Plugin error taxonomy and canonical error code mapping.       │
│                                                                     │
│  模块职责：插件运行时错误分类与 PLUGIN.<DOMAIN>.<REASON> 规范错误码映射。  │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

PLUGIN_RUNTIME_ERROR_MAPPINGS: dict[str, dict[str, str]] = {
    "INVALID_INPUT": {
        "code": "PLUGIN.RUNTIME.INVALID_INPUT",
        "cause_kind": "validation",
        "recovery_action": "fix_configuration",
    },
    "UNSUPPORTED_INPUT": {
        "code": "PLUGIN.RUNTIME.UNSUPPORTED_INPUT",
        "cause_kind": "validation",
        "recovery_action": "fix_configuration",
    },
    "INVALID_REQUEST": {
        "code": "PLUGIN.RUNTIME.INVALID_REQUEST",
        "cause_kind": "validation",
        "recovery_action": "fix_configuration",
    },
    "UNKNOWN_OPERATION": {
        "code": "PLUGIN.RUNTIME.UNKNOWN_OPERATION",
        "cause_kind": "not_found",
        "recovery_action": "fix_configuration",
    },
    "METHOD_NOT_FOUND": {
        "code": "PLUGIN.RUNTIME.METHOD_NOT_FOUND",
        "cause_kind": "not_found",
        "recovery_action": "fix_configuration",
    },
    "METHOD_NOT_SUPPORTED": {
        "code": "PLUGIN.RUNTIME.METHOD_NOT_SUPPORTED",
        "cause_kind": "not_found",
        "recovery_action": "fix_configuration",
    },
    "CANCELLED": {
        "code": "PLUGIN.RUNTIME.CANCELLED",
        "cause_kind": "cancellation",
        "recovery_action": "user_action_required",
    },
    "TIMEOUT": {
        "code": "PLUGIN.RUNTIME.TIMEOUT",
        "cause_kind": "timeout",
        "recovery_action": "safely_retry",
    },
    "DEADLINE_EXCEEDED": {
        "code": "PLUGIN.RUNTIME.DEADLINE_EXCEEDED",
        "cause_kind": "timeout",
        "recovery_action": "safely_retry",
    },
    "CAPABILITY_UNAVAILABLE": {
        "code": "PLUGIN.RUNTIME.CAPABILITY_UNAVAILABLE",
        "cause_kind": "infrastructure",
        "recovery_action": "query_state_first",
    },
    "UNAVAILABLE": {
        "code": "PLUGIN.RUNTIME.UNAVAILABLE",
        "cause_kind": "infrastructure",
        "recovery_action": "query_state_first",
    },
    "EXECUTION_FAILED": {
        "code": "PLUGIN.RUNTIME.EXECUTION_FAILED",
        "cause_kind": "execution",
        "recovery_action": "fix_configuration",
    },
}


def map_plugin_error(code_or_name: str, *, default_family: str = "RUNTIME") -> dict[str, str]:
    """Map a plugin error code or raw string to canonical PLUGIN.<FAMILY>.<REASON>.

        中文:将 Plugin 错误码或原始字符串映射为规范的 PLUGIN.<FAMILY>.<REASON>。"""
    raw = code_or_name.strip()
    if raw in PLUGIN_RUNTIME_ERROR_MAPPINGS:
        return PLUGIN_RUNTIME_ERROR_MAPPINGS[raw]

    normalized = raw.upper().replace(" ", "_").replace("-", "_")
    if normalized.startswith("PLUGIN."):
        canonical = normalized
    else:
        parts = normalized.split(".", 1)
        if len(parts) == 2:
            canonical = f"PLUGIN.{parts[0]}.{parts[1]}"
        else:
            canonical = f"PLUGIN.{default_family}.{normalized}"

    return {
        "code": canonical,
        "cause_kind": "unknown",
        "recovery_action": "query_state_first",
    }
