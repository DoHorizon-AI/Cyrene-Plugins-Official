"""
┌──────────────────────────────────────────────────────────────────────┐
│  plugin.py                                                           │
│  Module: email_notification_connector.plugin                         │
│  Role: Serve two fixed tools with signed context and typed results.   │
│                                                                      │
│  模块职责：通过可信签名上下文提供两个固定邮件工具。                      │
│  · 验签后才连接 SMTP  · 不接收任意邮箱、host、URL 或 shell 输入           │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cyrene_integration_contracts import (
    TOOL_OPERATIONS,
    EmailNotificationSendArguments,
    EmailSubmission,
    EmailSubmissionStatus,
    TrustedCallerContext,
    parse_email_connection_health_arguments,
    parse_email_notification_send_arguments,
    parse_email_submission,
    parse_tool_envelope,
    payload_sha256,
    schema_for,
    serialize_email_submission,
    verify_context,
    write_replay_identity,
)
from google.protobuf.message import DecodeError
from jsonschema import ValidationError

from ._generated import tool_provider_pb2 as wire
from .ledger import LedgerFailure, ReplayConflict, ReplayIdentity, SubmissionLedger
from .settings import ConfigurationError, ProviderSettings
from .smtp_transport import SMTPPreSendFailure, SMTPTransport

CAPABILITY_ID = "tool.provider.v1"
INTERFACE_VERSION = "1"
PLUGIN_ID = "cyrene.connectors.email-notification"
PLUGIN_VERSION = "0.1.0"
LIST_TOOLS_METHOD = "list_tools"
CALL_TOOL_METHOD = "call_tool"
LIST_TOOLS_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsRequest"
LIST_TOOLS_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.ListToolsResponse"
CALL_TOOL_REQUEST_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolRequest"
CALL_TOOL_RESPONSE_TYPE_URL = "type.cyrene.io/cyrene.tool.provider.v1.CallToolResponse"

SEND_TOOL_ID = "email_notification_send"
HEALTH_TOOL_ID = "email_connection_health"
_TOOL_IDS = frozenset({SEND_TOOL_ID, HEALTH_TOOL_ID})
_REQUEST_REF = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_MAX_ARGUMENTS_JSON_BYTES = 64 * 1024

_HEALTH_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["status", "checked_at", "security_mode", "authentication"],
    "properties": {
        "status": {"type": "string", "enum": ["AVAILABLE", "UNAVAILABLE"]},
        "checked_at": {"type": "string", "format": "date-time"},
        "security_mode": {"type": "string", "enum": ["TLS", "LOOPBACK_TEST_PLAIN"]},
        "authentication": {"type": "string", "enum": ["CONFIGURED", "NOT_CONFIGURED"]},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class _TypedPayload:
    """Expose bytes and a type URL to the generic direct Plugin runtime."""

    value: bytes
    type_url: str


class EmailNotificationProvider:
    """Implement bounded SMTP notification and health tools for one binding."""

    capabilities = (CAPABILITY_ID,)
    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION

    def __init__(
        self,
        *,
        settings: ProviderSettings | None = None,
        smtp_transport: SMTPTransport | None = None,
        ledger: SubmissionLedger | None = None,
    ) -> None:
        self._settings = settings or _settings_from_environment()
        self._smtp = smtp_transport or SMTPTransport(self._settings)
        self._ledger = ledger or SubmissionLedger(self._settings.state_db_path)

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        **_: Any,
    ) -> tuple[bool, Any]:
        """Dispatch only the canonical list and call methods for Tool Provider v1."""

        if capability != CAPABILITY_ID:
            return False, "INVALID_REQUEST: unsupported capability"
        if action == LIST_TOOLS_METHOD:
            if request_type_url not in (None, LIST_TOOLS_REQUEST_TYPE_URL):
                return False, "INVALID_REQUEST: catalog payload type mismatch"
            return self._list_tools(payload)
        if action == CALL_TOOL_METHOD:
            if request_type_url not in (None, CALL_TOOL_REQUEST_TYPE_URL):
                return False, "INVALID_REQUEST: call payload type mismatch"
            return self._call_tool(payload)
        return False, "METHOD_NOT_FOUND: unsupported provider method"

    def _list_tools(self, payload: bytes) -> tuple[bool, Any]:
        """Return the two canonical tool descriptors for this exact binding."""

        request = wire.ListToolsRequest()
        try:
            request.ParseFromString(payload)
        except DecodeError:
            return False, "INVALID_REQUEST: catalog request is malformed"
        if request.HasField("binding_id") and request.binding_id != self._settings.binding_id:
            return False, "INVALID_REQUEST: binding identity does not match"

        response = wire.ListToolsResponse()
        response.catalog.catalog_version = PLUGIN_VERSION
        for definition in _canonical_tools():
            descriptor = response.catalog.tools.add()
            descriptor.binding_id = self._settings.binding_id
            descriptor.provider_tool_id = str(definition["provider_tool_id"])
            descriptor.display_name = str(definition["display_name"])
            descriptor.description = str(definition["description"])
            descriptor.input_schema_json = json.dumps(
                definition["input_schema"], ensure_ascii=False, separators=(",", ":")
            )
            descriptor.output_schema_json = json.dumps(
                definition["output_schema"], ensure_ascii=False, separators=(",", ":")
            )
        return True, _TypedPayload(response.SerializeToString(), LIST_TOOLS_RESPONSE_TYPE_URL)

    def _call_tool(self, payload: bytes) -> tuple[bool, Any]:
        """Verify a signed raw argument object before using the SMTP transport."""

        request = wire.CallToolRequest()
        try:
            request.ParseFromString(payload)
        except DecodeError:
            return False, "INVALID_REQUEST: tool request is malformed"
        if request.binding_id != self._settings.binding_id:
            return True, _tool_error(
                wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS,
                "tool binding does not match this provider",
            )
        if request.provider_tool_id not in _TOOL_IDS:
            return True, _tool_error(
                wire.TOOL_PROVIDER_ERROR_CODE_TOOL_NOT_FOUND,
                "provider tool is not available",
            )

        try:
            raw_envelope = _decode_json_object(request.arguments_json)
            envelope = parse_tool_envelope(raw_envelope)
        except (TypeError, ValueError, json.JSONDecodeError, ValidationError):
            return True, _tool_error(
                wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS,
                "signed tool arguments are malformed",
            )

        tool_id = request.provider_tool_id
        operation_id = TOOL_OPERATIONS[tool_id]
        if not verify_context(
            method=tool_id,
            operation_id=operation_id,
            payload=dict(envelope.arguments),
            context=envelope.caller_context,
            key=self._settings.context_hmac_key,
            expected_caller_workload=self._settings.caller_workload,
            expected_audience=self._settings.audience,
            expected_binding_id=self._settings.binding_id,
            expected_organization_id=self._settings.organization_id,
            expected_workspace_id=self._settings.workspace_id,
            expected_issuer=self._settings.expected_issuer,
            expected_subject=self._settings.expected_subject,
        ):
            return True, _tool_error(
                wire.TOOL_PROVIDER_ERROR_CODE_PROVIDER_ERROR,
                "trusted caller context was rejected",
            )

        if tool_id == SEND_TOOL_ID:
            return self._send(envelope.arguments, envelope.caller_context)
        return self._health(envelope.arguments)

    def _send(
        self,
        raw_arguments: Mapping[str, Any],
        caller_context: TrustedCallerContext,
    ) -> tuple[bool, Any]:
        """Validate bounded content, resolve the allowlist alias and submit once."""

        try:
            arguments = parse_email_notification_send_arguments(raw_arguments)
            recipient = self._settings.recipients[arguments.recipient_ref]
            subject, text = _sanitize_message(arguments)
        except (KeyError, TypeError, ValueError, ValidationError):
            return True, _tool_error(
                wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS,
                "email arguments are outside the configured bounds",
            )

        try:
            identity_digest, payload_digest = write_replay_identity(caller_context, raw_arguments)
        except (TypeError, ValueError, ValidationError):
            return True, _tool_error(
                wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS,
                "email replay identity is malformed",
            )
        identity = ReplayIdentity(
            identity_sha256=identity_digest,
            request_id_sha256=_request_id_digest(caller_context),
            payload_sha256=payload_digest,
            request_ref=arguments.request_ref,
        )
        try:
            previous = self._ledger.reserve(identity)
        except ReplayConflict:
            return True, _tool_error(
                wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS,
                "email request identity was already used with different content",
            )
        except LedgerFailure:
            return True, _tool_error(
                wire.TOOL_PROVIDER_ERROR_CODE_PROVIDER_ERROR,
                "private email submission ledger is unavailable",
                retryable=True,
            )
        if previous is not None:
            return True, _submission_response(previous)

        try:
            submission = self._smtp.submit(
                recipient=recipient,
                subject=subject,
                text=text,
                request_ref=arguments.request_ref,
            )
        except SMTPPreSendFailure as error:
            if error.retryable:
                try:
                    self._ledger.mark_retryable(identity)
                except LedgerFailure:
                    return True, _submission_response(
                        EmailSubmission(
                            request_ref=arguments.request_ref,
                            status=EmailSubmissionStatus.UNKNOWN,
                            vendor_receipt_ref=None,
                            delivery_confirmed=False,
                        )
                    )
                return True, _tool_error(
                    wire.TOOL_PROVIDER_ERROR_CODE_TIMEOUT
                    if error.code == "SMTP_UNAVAILABLE"
                    else wire.TOOL_PROVIDER_ERROR_CODE_PROVIDER_ERROR,
                    "SMTP connection failed before message content was sent",
                    retryable=True,
                )
            submission = EmailSubmission(
                request_ref=arguments.request_ref,
                status=EmailSubmissionStatus.REJECTED,
                vendor_receipt_ref=None,
                delivery_confirmed=False,
            )
        except (UnicodeError, ValueError):
            submission = EmailSubmission(
                request_ref=arguments.request_ref,
                status=EmailSubmissionStatus.REJECTED,
                vendor_receipt_ref=None,
                delivery_confirmed=False,
            )

        try:
            stored_submission = self._ledger.complete(identity, submission)
        except LedgerFailure:
            stored_submission = EmailSubmission(
                request_ref=arguments.request_ref,
                status=EmailSubmissionStatus.UNKNOWN,
                vendor_receipt_ref=None,
                delivery_confirmed=False,
            )
        return True, _submission_response(stored_submission)

    def _health(self, raw_arguments: Mapping[str, Any]) -> tuple[bool, Any]:
        """Check SMTP without sending a message or exposing endpoint details."""

        try:
            parse_email_connection_health_arguments(raw_arguments)
        except (TypeError, ValueError, ValidationError):
            return True, _tool_error(
                wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS,
                "email health arguments are malformed",
            )
        health = self._smtp.check_connection()
        result = {
            "status": "AVAILABLE" if health.available else "UNAVAILABLE",
            "checked_at": health.checked_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "security_mode": health.security_mode,
            "authentication": health.authentication,
        }
        response = wire.CallToolResponse()
        response.outcome.content.add().json.json = json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        )
        response.outcome.is_error = not health.available
        return True, _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


def _settings_from_environment() -> ProviderSettings:
    """Construct one provider from the worker's standard activation variables."""

    from cyrene_plugin_runtime.configuration import read_environment_settings

    try:
        settings = read_environment_settings()
    except (TypeError, ValueError):
        raise ConfigurationError("Plugin activation configuration is invalid") from None
    if settings is None:
        raise ConfigurationError("Plugin activation configuration is required")
    try:
        decoded = json.loads(settings["config"])
    except json.JSONDecodeError:
        raise ConfigurationError("Plugin activation configuration is invalid") from None
    if not isinstance(decoded, Mapping):
        raise ConfigurationError("Plugin activation configuration must be an object")
    return ProviderSettings.from_mapping(decoded, binding_id=settings["binding_id"])


def _canonical_tools() -> tuple[dict[str, Any], ...]:
    """Build fixed descriptors when catalog discovery is requested."""

    return (
        {
            "provider_tool_id": SEND_TOOL_ID,
            "display_name": "Send Email Notification",
            "description": (
                "Send plain-text notification to a binding-configured recipient_ref. "
                "A signed caller context is required. ACCEPTED confirms SMTP acceptance, "
                "not delivery."
            ),
            "input_schema": schema_for("EmailNotificationSendEnvelope"),
            "output_schema": schema_for("EmailSubmission"),
        },
        {
            "provider_tool_id": HEALTH_TOOL_ID,
            "display_name": "Check Email Connection",
            "description": (
                "Check the binding-configured SMTP connection using a signed caller context."
            ),
            "input_schema": schema_for("EmailConnectionHealthEnvelope"),
            "output_schema": _HEALTH_OUTPUT_SCHEMA,
        },
    )


def _decode_json_object(value: str) -> dict[str, Any]:
    """Decode bounded JSON and reject duplicate keys before signature checking."""

    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_ARGUMENTS_JSON_BYTES:
        raise ValueError("tool arguments exceed the request limit")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("tool arguments contain duplicate object keys")
            result[key] = item
        return result

    decoded = json.loads(value, object_pairs_hook=reject_duplicate_keys)
    if not isinstance(decoded, dict):
        raise ValueError("tool arguments must be a JSON object")
    return decoded


def _sanitize_message(arguments: EmailNotificationSendArguments) -> tuple[str, str]:
    """Bound and sanitize message content after the raw arguments are verified."""

    subject = arguments.subject.strip()
    if (
        not subject
        or len(subject) > 255
        or len(subject.encode("utf-8")) > 1024
        or any(unicodedata.category(character) == "Cc" for character in subject)
    ):
        raise ValueError("email subject is outside the configured bounds")

    text = arguments.text
    if len(text) > 10_000 or len(text.encode("utf-8")) > 40_000 or not text.strip():
        raise ValueError("email text is outside the configured bounds")
    if any(
        unicodedata.category(character) == "Cc" and character not in "\r\n\t" for character in text
    ):
        raise ValueError("email text contains unsupported control characters")
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")

    if _REQUEST_REF.fullmatch(arguments.request_ref) is None:
        raise ValueError("email request reference is invalid")
    return subject, normalized_text


def _request_id_digest(caller_context: TrustedCallerContext) -> str:
    """Bind a signed context request ID to its verified caller and actor scope."""

    return payload_sha256(
        {
            "method": caller_context.method,
            "operation_id": caller_context.operation_id,
            "binding_id": caller_context.binding_id,
            "caller_workload": caller_context.caller_workload,
            "audience": caller_context.audience,
            "issuer": caller_context.issuer,
            "subject": caller_context.subject,
            "organization_id": caller_context.organization_id,
            "workspace_id": caller_context.workspace_id,
            "request_id": caller_context.request_id,
        }
    )


def _submission_response(submission: EmailSubmission) -> _TypedPayload:
    """Encode one typed SMTP result without inventing a delivery receipt."""

    result = parse_email_submission(serialize_email_submission(submission))
    response = wire.CallToolResponse()
    response.outcome.is_error = result.status is EmailSubmissionStatus.REJECTED
    response.outcome.content.add().json.json = json.dumps(
        serialize_email_submission(result), ensure_ascii=False, separators=(",", ":")
    )
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)


def _tool_error(
    code: int,
    message: str,
    *,
    retryable: bool = False,
) -> _TypedPayload:
    """Return a bounded ToolProviderError with no SMTP or secret detail."""

    response = wire.CallToolResponse()
    response.error.code = code
    response.error.message = message
    response.error.retryable = retryable
    return _TypedPayload(response.SerializeToString(), CALL_TOOL_RESPONSE_TYPE_URL)
