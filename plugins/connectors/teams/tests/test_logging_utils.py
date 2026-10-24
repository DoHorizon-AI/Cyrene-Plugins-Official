"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_logging_utils.py                                            │
│  Module: tests.test_logging_utils                                    │
│  Role: Verify URL and bearer material are redacted from SDK logs.     │
│                                                                      │
│  模块职责：验证 SDK 日志中的 URL 与 bearer 凭据会被脱敏               │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging

from teams_connector.logging_utils import SensitiveDataLogFilter


def test_sensitive_log_filter_redacts_url_and_bearer_value():
    """Never expose service URLs or bearer credentials in log text.

    中文：日志文本不得暴露 service URL 或 bearer 凭据。
    """

    record = logging.LogRecord(
        name="microsoft_teams.apps.http.http_server",
        level=logging.ERROR,
        pathname="test.py",
        lineno=1,
        msg="JWT failed for Bearer private-token at https://internal.example.invalid/callback",
        args=(),
        exc_info=None,
    )

    assert SensitiveDataLogFilter().filter(record) is True
    message = record.getMessage()
    assert "private-token" not in message
    assert "internal.example.invalid" not in message
    assert "Bearer [REDACTED]" in message
    assert "[URL]" in message
