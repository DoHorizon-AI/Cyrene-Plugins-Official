"""
Tests for plugin runtime W3C trace correlation, secret redaction, structured logging,
and canonical PLUGIN.<DOMAIN>.<REASON> error mappings.

中文：测试 Plugin 运行时的 W3C 轨迹关联、机密信息脱敏、结构化日志，以及规范的 PLUGIN.<DOMAIN>.<REASON> 错误映射。
"""

from __future__ import annotations

import io
import json
import sys

from cyrene_plugin_runtime.errors import (
    PLUGIN_RUNTIME_ERROR_MAPPINGS,
    map_plugin_error,
)
from cyrene_plugin_runtime.logging import (
    emit_diagnostic_error,
    format_cyrene_log,
    is_sensitive_key,
    parse_w3c_traceparent,
    redact_attributes,
    sanitize_correlation_id,
    sanitize_request_id,
)
from cyrene_plugin_runtime.server import _wire_error


def test_parse_w3c_traceparent() -> None:
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    span_id = "00f067aa0ba902b7"
    raw = f"00-{trace_id}-{span_id}-01"
    parsed = parse_w3c_traceparent(raw)
    assert parsed == (trace_id, span_id)

    assert parse_w3c_traceparent(None) is None
    assert parse_w3c_traceparent("invalid") is None
    assert parse_w3c_traceparent(f"00-{'0'*32}-{'1'*16}-01") is None


def test_sanitize_correlation_id() -> None:
    assert sanitize_correlation_id(None) is None
    assert sanitize_correlation_id("") is None
    assert sanitize_correlation_id("req-123_abc.XYZ") == "req-123_abc.XYZ"
    assert sanitize_correlation_id("bad<chars>!@#") == "badchars"

    long_id = "x" * 200
    sanitized = sanitize_request_id(long_id)
    assert sanitized is not None
    assert len(sanitized) == 128


def test_secret_redaction_and_token_preservation() -> None:
    assert is_sensitive_key("authorization")
    assert is_sensitive_key("api_key")
    assert is_sensitive_key("secret")

    # Tokens and counts must be preserved
    # 中文：必须保留令牌和计数。
    assert not is_sensitive_key("tokens")
    assert not is_sensitive_key("prompt_tokens")
    assert not is_sensitive_key("token_count")

    data = {
        "auth": "secret_val",
        "tokens": 42,
        "nested": {"token_count": 10, "password": "pass"},
    }
    redacted = redact_attributes(data)
    assert redacted["auth"] == "[REDACTED]"
    assert redacted["tokens"] == 42
    assert redacted["nested"]["token_count"] == 10
    assert redacted["nested"]["password"] == "[REDACTED]"


def test_format_cyrene_log_structure() -> None:
    line = format_cyrene_log(
        "INFO",
        "plugin.invoked",
        "Plugin capability called",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        span_id="00f067aa0ba902b7",
        attributes={"capability": "message-connector-v1"},
    )
    record = json.loads(line)
    assert record["schema_version"] == 1
    assert record["level"] == "INFO"
    assert record["event.name"] == "plugin.invoked"
    assert record["service.name"] == "cyrene-plugin-runtime"
    assert record["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert record["span_id"] == "00f067aa0ba902b7"
    assert record["attributes"]["capability"] == "message-connector-v1"


def test_emit_diagnostic_error_strictly_to_stderr() -> None:
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = stdout_buf = io.StringIO()
    sys.stderr = stderr_buf = io.StringIO()
    try:
        emit_diagnostic_error(
            "plugin.error",
            "PLUGIN.RUNTIME.EXECUTION_FAILED",
            "Plugin execution error occurred",
            attributes={"error_detail": "test"},
        )
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    # stdout MUST be completely empty (preserving machine protocols)
    # 中文：stdout 必须完全为空（以保留机器协议）。
    assert stdout_buf.getvalue() == ""

    # stderr MUST contain the structured NDJSON record
    # 中文：stderr 必须包含结构化 NDJSON 记录。
    stderr_output = stderr_buf.getvalue()
    assert stderr_output.endswith("\n")
    record = json.loads(stderr_output.strip())
    assert record["level"] == "ERROR"
    assert record["event.name"] == "plugin.error"
    assert record["attributes"]["error.code"] == "PLUGIN.RUNTIME.EXECUTION_FAILED"
    assert record["attributes"]["error_detail"] == "test"


def test_plugin_error_mappings() -> None:
    for raw_code, expected in PLUGIN_RUNTIME_ERROR_MAPPINGS.items():
        mapped = map_plugin_error(raw_code)
        assert mapped["code"] == expected["code"]
        assert mapped["cause_kind"] == expected["cause_kind"]
        assert mapped["recovery_action"] == expected["recovery_action"]

    # Custom plugin domain mappings
    # 中文：自定义 Plugin 域映射。
    m1 = map_plugin_error("ONEBOT.CONNECTION_LOST")
    assert m1["code"] == "PLUGIN.ONEBOT.CONNECTION_LOST"
    assert m1["recovery_action"] == "query_state_first"

    m2 = map_plugin_error("PLUGIN.MCP.TOOL_CALL_FAILED")
    assert m2["code"] == "PLUGIN.MCP.TOOL_CALL_FAILED"


def test_server_wire_error_emits_to_stderr() -> None:
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = stdout_buf = io.StringIO()
    sys.stderr = stderr_buf = io.StringIO()
    try:
        err = _wire_error(
            "INVALID_INPUT: payload malformed",
            request_id="req-999",
            capability="tool-provider-v1",
            method="execute",
        )
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    # stdout empty
    # 中文：stdout 为空。
    assert stdout_buf.getvalue() == ""

    # stderr has diagnostic error
    # 中文：stderr 包含诊断错误。
    record = json.loads(stderr_buf.getvalue().strip())
    assert record["attributes"]["error.code"] == "PLUGIN.RUNTIME.INVALID_INPUT"
    assert record["attributes"]["request_id"] == "req-999"
    assert record["attributes"]["plugin.capability"] == "tool-provider-v1"
    assert record["attributes"]["plugin.method"] == "execute"
    assert err.domain_code == "INVALID_INPUT"
    assert err.message == "payload malformed"
