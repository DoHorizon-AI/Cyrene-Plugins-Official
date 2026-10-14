"""
┌──────────────────────────────────────────────────────────────────────┐
│  __init__.py                                                         │
│  Module: email_notification_connector                                │
│  Role: Export the independent SMTP email notification entrypoint.    │
│                                                                      │
│  模块职责：导出独立 SMTP 邮件通知运行时入口。                            │
└──────────────────────────────────────────────────────────────────────┘
"""

from .plugin import EmailNotificationProvider

__all__ = ["EmailNotificationProvider"]
