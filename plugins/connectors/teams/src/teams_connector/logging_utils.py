"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 logging_utils.py                                                 │
│  Module: teams_connector.logging_utils                              │
│  Role: Redact URLs and bearer credentials from SDK log records.       │
│                                                                      │
│  模块职责：从 SDK 日志记录中脱敏 URL 与 bearer 凭据                    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import re


class SensitiveDataLogFilter(logging.Filter):
    """Redact URL and bearer token values before logging handlers run.

    中文：在日志交给 handler 前隐藏 URL 与 bearer token。
    """

    _bearer = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
    _url = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        """Sanitize a record while preserving its non-sensitive status text.

        中文：隐藏敏感值，同时保留非敏感状态文本。
        """

        try:
            message = record.getMessage()
        except Exception:
            return True
        message = self._bearer.sub("Bearer [REDACTED]", message)
        record.msg = self._url.sub("[URL]", message)
        record.args = ()
        return True


def install_sensitive_log_filters() -> None:
    """Attach redaction to existing Teams SDK and HTTP auth loggers.

    中文：为现有 Teams SDK 与 HTTP 鉴权 logger 添加脱敏过滤器。
    """

    log_filter = SensitiveDataLogFilter()
    names = {
        "httpx",
        "requests",
        "urllib3",
        "microsoft_teams",
    }
    names.update(
        name
        for name in logging.Logger.manager.loggerDict
        if name.startswith("microsoft_teams.")
    )
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(item, SensitiveDataLogFilter) for item in logger.filters):
            logger.addFilter(log_filter)
        # The SDK emits activity objects at DEBUG. Message content and member
        # identities belong in the protected Navigator event, not application logs.
        if name.startswith("microsoft_teams") and logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)
