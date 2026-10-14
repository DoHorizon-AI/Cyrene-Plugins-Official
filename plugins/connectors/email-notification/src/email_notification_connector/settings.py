"""
┌──────────────────────────────────────────────────────────────────────┐
│  settings.py                                                         │
│  Module: email_notification_connector.settings                       │
│  Role: Validate binding-owned SMTP, identity, and secret references.  │
│                                                                      │
│  模块职责：校验绑定持有的 SMTP、身份范围与密钥环境变量引用。             │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

_ENV_REF = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_HEX_KEY = re.compile(r"^[0-9A-Fa-f]{64}$")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_LOCAL_PART = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

_CONFIG_FIELDS = frozenset(
    {
        "smtp_host",
        "smtp_port",
        "tls_mode",
        "allow_loopback_test_transport",
        "timeout_seconds",
        "sender",
        "recipients",
        "state_db_path",
        "smtp_username_env_ref",
        "smtp_password_env_ref",
        "context_hmac_key_env_ref",
        "caller_workload",
        "audience",
        "organization_id",
        "workspace_id",
        "expected_issuer",
        "expected_subject",
    }
)
_REQUIRED_FIELDS = frozenset(
    {
        "smtp_host",
        "smtp_port",
        "sender",
        "recipients",
        "state_db_path",
        "context_hmac_key_env_ref",
        "caller_workload",
        "audience",
        "organization_id",
        "workspace_id",
    }
)


class ConfigurationError(ValueError):
    """Raised when a binding cannot be used without weakening its boundary."""


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Validated SMTP and signed-caller settings for one immutable binding."""

    binding_id: str = field(repr=False)
    smtp_host: str = field(repr=False)
    smtp_port: int
    tls_mode: str
    timeout_seconds: float
    sender: str = field(repr=False)
    recipients: Mapping[str, str] = field(repr=False)
    state_db_path: Path = field(repr=False)
    caller_workload: str = field(repr=False)
    audience: str = field(repr=False)
    organization_id: str = field(repr=False)
    workspace_id: str = field(repr=False)
    expected_issuer: str | None = field(repr=False)
    expected_subject: str | None = field(repr=False)
    context_hmac_key: bytes = field(repr=False)
    smtp_username: str | None = field(default=None, repr=False)
    smtp_password: str | None = field(default=None, repr=False)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        binding_id: str,
        environ: Mapping[str, str] | None = None,
    ) -> ProviderSettings:
        """Validate one activation object and resolve named secrets.

        Args:
            value: JSON object owned by the configured binding.
            binding_id: Binding identity supplied by the Plugin runtime.
            environ: Secret environment mapping; defaults to the process environment.
        Returns:
            Immutable settings for the one configured SMTP destination.
        Raises:
            ConfigurationError: If an endpoint, trust scope, or secret is invalid.
        """

        if not isinstance(value, Mapping):
            raise ConfigurationError("binding configuration must be an object")
        if any(not isinstance(key, str) for key in value):
            raise ConfigurationError("binding configuration fields must be strings")
        unknown = set(value).difference(_CONFIG_FIELDS)
        missing = _REQUIRED_FIELDS.difference(value)
        if unknown or missing:
            raise ConfigurationError("binding configuration fields are invalid")
        if not _bounded_text(binding_id, 200):
            raise ConfigurationError("binding identity is invalid")

        env = os.environ if environ is None else environ
        host = _host(value["smtp_host"])
        port = value["smtp_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigurationError("SMTP port is invalid")

        tls_mode = value.get("tls_mode", "implicit")
        if not isinstance(tls_mode, str) or tls_mode not in {
            "implicit",
            "starttls",
            "loopback_test_plain",
        }:
            raise ConfigurationError("SMTP TLS mode is invalid")
        allow_plain_test = value.get("allow_loopback_test_transport", False)
        if not isinstance(allow_plain_test, bool):
            raise ConfigurationError("loopback test transport setting is invalid")
        if tls_mode == "loopback_test_plain" and (
            not allow_plain_test or not _is_literal_loopback_host(host)
        ):
            raise ConfigurationError("plain SMTP is restricted to explicit loopback tests")
        if allow_plain_test and tls_mode != "loopback_test_plain":
            raise ConfigurationError("loopback test transport flag does not match TLS mode")

        timeout = value.get("timeout_seconds", 15)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ConfigurationError("SMTP timeout is invalid")

        sender = _mailbox(value["sender"])
        raw_recipients = value["recipients"]
        if not isinstance(raw_recipients, Mapping) or not raw_recipients:
            raise ConfigurationError("recipient allowlist must be a non-empty object")
        recipients: dict[str, str] = {}
        for alias, mailbox in raw_recipients.items():
            if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
                raise ConfigurationError("recipient alias is invalid")
            recipients[alias] = _mailbox(mailbox)

        state_db_path_value = value["state_db_path"]
        if (
            not isinstance(state_db_path_value, str)
            or not state_db_path_value
            or "\x00" in state_db_path_value
        ):
            raise ConfigurationError("private state path is invalid")
        state_db_path = Path(state_db_path_value)
        if not state_db_path.is_absolute():
            raise ConfigurationError("private state path must be absolute")

        context_key_ref = _environment_ref(value["context_hmac_key_env_ref"])
        encoded_key = env.get(context_key_ref)
        if not isinstance(encoded_key, str) or _HEX_KEY.fullmatch(encoded_key) is None:
            raise ConfigurationError("signed caller key is unavailable")
        try:
            context_key = bytes.fromhex(encoded_key)
        except ValueError as error:
            raise ConfigurationError("signed caller key is invalid") from error

        username_ref = value.get("smtp_username_env_ref")
        password_ref = value.get("smtp_password_env_ref")
        if (username_ref is None) != (password_ref is None):
            raise ConfigurationError("SMTP credential references must be configured as a pair")
        username: str | None = None
        password: str | None = None
        if username_ref is not None and password_ref is not None:
            username = env.get(_environment_ref(username_ref))
            password = env.get(_environment_ref(password_ref))
            if not username or not password:
                raise ConfigurationError("SMTP credentials are unavailable")

        return cls(
            binding_id=binding_id,
            smtp_host=host,
            smtp_port=port,
            tls_mode=str(tls_mode),
            timeout_seconds=float(timeout),
            sender=sender,
            recipients=MappingProxyType(recipients),
            state_db_path=state_db_path,
            caller_workload=_required_scope(value["caller_workload"], 200),
            audience=_required_scope(value["audience"], 200),
            organization_id=_required_scope(value["organization_id"], 200),
            workspace_id=_required_scope(value["workspace_id"], 200),
            expected_issuer=_optional_scope(value.get("expected_issuer"), 500),
            expected_subject=_optional_scope(value.get("expected_subject"), 500),
            context_hmac_key=context_key,
            smtp_username=username,
            smtp_password=password,
        )


def _bounded_text(value: object, maximum: int) -> bool:
    """Return whether a claim is non-empty, bounded text without controls."""

    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _required_scope(value: object, maximum: int) -> str:
    """Validate a configured trusted caller scope without normalizing it."""

    if not _bounded_text(value, maximum):
        raise ConfigurationError("trusted caller scope is invalid")
    return str(value)


def _optional_scope(value: object, maximum: int) -> str | None:
    """Validate an optional identity pin configured by the binding."""

    if value is None:
        return None
    return _required_scope(value, maximum)


def _environment_ref(value: object) -> str:
    """Validate a named environment-variable reference."""

    if not isinstance(value, str) or _ENV_REF.fullmatch(value) is None:
        raise ConfigurationError("secret environment reference is invalid")
    return value


def _host(value: object) -> str:
    """Accept a DNS name or IP literal, never a URL or transport locator."""

    if not isinstance(value, str) or not value or len(value) > 253:
        raise ConfigurationError("SMTP hostname is invalid")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if value.lower() == "localhost":
            return value
        labels = value.rstrip(".").split(".")
        if not labels or any(_DOMAIN_LABEL.fullmatch(label) is None for label in labels):
            raise ConfigurationError("SMTP hostname must be a DNS name or IP address") from None
    return value


def _is_literal_loopback_host(value: str) -> bool:
    """Recognize only loopback IP literals and the reserved localhost name."""

    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _mailbox(value: object) -> str:
    """Validate a binding-owned ASCII mailbox for SMTP headers and commands."""

    if not isinstance(value, str) or len(value) > 320 or value.count("@") != 1:
        raise ConfigurationError("configured SMTP mailbox is invalid")
    local, domain = value.rsplit("@", 1)
    labels = domain.split(".")
    if (
        not local
        or len(local) > 64
        or _LOCAL_PART.fullmatch(local) is None
        or not labels
        or any(_DOMAIN_LABEL.fullmatch(label) is None for label in labels)
    ):
        raise ConfigurationError("configured SMTP mailbox is invalid")
    return value
