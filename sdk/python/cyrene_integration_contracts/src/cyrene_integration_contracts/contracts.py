"""
┌──────────────────────────────────────────────────────────────────────┐
│  contracts.py                                                        │
│  Module: cyrene_integration_contracts.contracts                      │
│  Role: Typed integration DTOs, JSON codecs, and caller signatures.    │
│                                                                      │
│  模块职责：定义集成 DTO、JSON 编解码与可信调用方签名。                   │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any

import jsonschema

MAX_CONTEXT_TTL = timedelta(seconds=300)
DEFAULT_CONTEXT_TTL = timedelta(seconds=120)
MAX_FUTURE_SKEW = timedelta(seconds=30)
CONTEXT_KEY_BYTES = 32
TOOL_OPERATIONS: Mapping[str, str] = MappingProxyType(
    {
        "wecom_authorization_health": "wecom.authorization.health.read",
        "wecom_reauthorization_start": "wecom.authorization.renewal.start",
        "wecom_reauthorization_status": "wecom.authorization.renewal.status",
        "cloud_resources_query": "cloud.resources.query",
        "cloud_authorization_health": "cloud.authorization.health.read",
        "email_notification_send": "email.notification.send",
        "email_connection_health": "email.connection.health.read",
    }
)

_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_CONTEXT_FIELDS = (
    "method",
    "operation_id",
    "caller_workload",
    "audience",
    "issuer",
    "subject",
    "organization_id",
    "workspace_id",
    "binding_id",
    "request_id",
    "issued_at",
    "expires_at",
    "payload_sha256",
)


class LoginState(StrEnum):
    """Provider login presence, separate from authorization validity."""

    PRESENT = "PRESENT"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class AuthorizationState(StrEnum):
    """Provider authorization state for one account or capability."""

    VALID = "VALID"
    REAUTHORIZATION_REQUIRED = "REAUTHORIZATION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class RenewalState(StrEnum):
    """Lifecycle state of an operator-authorized renewal session."""

    WAITING_USER = "WAITING_USER"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class CloudResourceKind(StrEnum):
    """Stable cloud resource kinds supported by integration support v1."""

    AZURE_VM = "azure.vm"
    GCP_INSTANCE = "gcp.instance"
    HF_MODEL = "hf.model"
    HF_DATASET = "hf.dataset"
    HF_SPACE = "hf.space"
    RUNPOD_POD = "runpod.pod"
    TAILSCALE_DEVICE = "tailscale.device"


class CloudResourceState(StrEnum):
    """Observed provider status normalized without inventing authority."""

    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class EmailSubmissionStatus(StrEnum):
    """Submission result that does not imply delivery without evidence."""

    ACCEPTED = "ACCEPTED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class CapabilityAuthorizationHealth:
    """Authorization state for one provider capability."""

    capability_id: str
    state: AuthorizationState
    last_verified_at: datetime | None
    expires_at: datetime | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class AuthorizationHealth:
    """Typed authorization and login facts for one opaque authorization ref."""

    schema_version: int
    authorization_ref: str
    login_state: LoginState
    authorization_state: AuthorizationState
    checked_at: datetime
    expires_at: datetime | None
    generation: int
    capabilities: tuple[CapabilityAuthorizationHealth, ...]


@dataclass(frozen=True, slots=True)
class RenewalSession:
    """Operator-authorized renewal state with opaque attachment references."""

    session_ref: str
    authorization_ref: str
    state: RenewalState
    created_at: datetime
    expires_at: datetime | None
    authorization_url: str | None
    qr_attachment_ref: str | None
    generation: int


CloudAttributeValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class CloudObservation:
    """A bounded observation returned by a configured cloud provider."""

    external_ref: str
    kind: CloudResourceKind
    display_name: str
    status: CloudResourceState
    provider_status: str | None
    collected_at: datetime
    observed_at: datetime | None
    attributes: Mapping[str, CloudAttributeValue]
    platform_resource_ref: str | None
    platform_generation: int | None


@dataclass(frozen=True, slots=True)
class CloudQueryResult:
    """Provider observations and the time at which the query completed."""

    provider: str
    checked_at: datetime
    resources: tuple[CloudObservation, ...]


@dataclass(frozen=True, slots=True)
class EmailSubmission:
    """A bounded email submission result with honest delivery evidence."""

    request_ref: str
    status: EmailSubmissionStatus
    vendor_receipt_ref: str | None
    delivery_confirmed: bool


@dataclass(frozen=True, slots=True)
class TrustedCallerContext:
    """HMAC-authenticated workload, audience, tenant, operation, and body hash."""

    method: str
    operation_id: str
    caller_workload: str
    audience: str
    issuer: str
    subject: str
    organization_id: str
    workspace_id: str | None
    binding_id: str
    request_id: str
    issued_at: datetime
    expires_at: datetime
    payload_sha256: str
    signature: str

    def as_dict(self) -> dict[str, Any]:
        """Return the canonical wire-shaped context, including no key material."""

        return serialize_trusted_caller_context(self)


@dataclass(frozen=True, slots=True)
class WeComAuthorizationHealthArguments:
    """Opaque authorization reference selected by an authorized caller."""

    authorization_ref: str


@dataclass(frozen=True, slots=True)
class WeComReauthorizationStartArguments:
    """Authorization reference for an operator-approved renewal start."""

    authorization_ref: str


@dataclass(frozen=True, slots=True)
class WeComReauthorizationStatusArguments:
    """Opaque renewal session reference for a status read."""

    session_ref: str


@dataclass(frozen=True, slots=True)
class CloudResourcesQueryArguments:
    """Configured opaque selector for one cloud resource query."""

    target_ref: str


@dataclass(frozen=True, slots=True)
class CloudAuthorizationHealthArguments:
    """Configured opaque selector for one cloud authorization check."""

    target_ref: str


@dataclass(frozen=True, slots=True)
class EmailNotificationSendArguments:
    """Allowlisted recipient alias and bounded notification content."""

    recipient_ref: str
    subject: str
    text: str
    request_ref: str


@dataclass(frozen=True, slots=True)
class EmailConnectionHealthArguments:
    """Empty request for a binding-configured email health check."""


@dataclass(frozen=True, slots=True)
class ToolEnvelope:
    """Signed tool context and the exact inner arguments object."""

    caller_context: TrustedCallerContext
    arguments: Mapping[str, Any]


def _normalize_json(value: Any) -> Any:
    """Reject non-JSON values and normalize mapping proxies before encoding."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _normalize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    raise TypeError("value is not a JSON value")


def canonical_json(value: Any) -> bytes:
    """Encode JSON using deterministic UTF-8 key ordering and compact spacing."""

    return json.dumps(
        _normalize_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def payload_sha256(value: Any) -> str:
    """Return the lowercase SHA-256 digest of canonical JSON semantics."""

    return hashlib.sha256(canonical_json(value)).hexdigest()


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    """Load the bundled copy of the canonical integration schema on demand."""

    raw = files(__package__).joinpath("schema.json").read_text(encoding="utf-8")
    schema = json.loads(raw)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def schema_for(name: str) -> dict[str, Any]:
    """Return an isolated `$ref` schema for one public `$defs` entry.

    Args:
        name: Case-sensitive public definition name such as `AuthorizationHealth`.
    Returns:
        JSON Schema document that validates the named definition.
    Raises:
        ValueError: If the definition is not part of this contract version.
    """

    definitions = _load_schema()["$defs"]
    if not isinstance(name, str) or name not in definitions:
        raise ValueError("unknown integration-support schema")
    return {
        "$schema": _load_schema()["$schema"],
        "$id": _load_schema()["$id"],
        "$defs": deepcopy(definitions),
        "$ref": f"#/$defs/{name}",
    }


def validate_schema(name: str, value: Any) -> None:
    """Validate a value against one bundled contract definition.

    Args:
        name: Case-sensitive public `$defs` entry.
        value: JSON value to validate.
    Raises:
        ValueError: If the schema name is unknown.
        jsonschema.ValidationError: If the value violates the contract.
    """

    validator = jsonschema.Draft202012Validator(
        schema_for(name), format_checker=jsonschema.FormatChecker()
    )
    validator.validate(_normalize_json(value))


def _parse_timestamp(value: Any) -> datetime:
    """Parse one strict RFC3339 UTC timestamp into an aware datetime."""

    if not isinstance(value, str) or _TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError("timestamp must be an RFC3339 UTC string ending in Z")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("timestamp is invalid") from error
    return parsed.astimezone(UTC)


def _serialize_timestamp(value: datetime) -> str:
    """Format an aware UTC datetime in the contract's RFC3339 wire form."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    offset = value.utcoffset()
    if offset != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    normalized = value.astimezone(UTC)
    rendered = normalized.strftime("%Y-%m-%dT%H:%M:%S")
    if normalized.microsecond:
        rendered += f".{normalized.microsecond:06d}"
    return rendered + "Z"


def _enum(enum_type: type[StrEnum], value: Any) -> Any:
    """Construct a contract enum or report an invalid wire value."""

    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise ValueError("enum value is outside the integration contract") from error


def parse_capability_authorization_health(
    value: Mapping[str, Any],
) -> CapabilityAuthorizationHealth:
    """Parse a strict capability-level authorization health object."""

    validate_schema("CapabilityAuthorizationHealth", value)
    return CapabilityAuthorizationHealth(
        capability_id=value["capability_id"],
        state=_enum(AuthorizationState, value["state"]),
        last_verified_at=(
            None
            if value["last_verified_at"] is None
            else _parse_timestamp(value["last_verified_at"])
        ),
        expires_at=(
            None
            if value["expires_at"] is None
            else _parse_timestamp(value["expires_at"])
        ),
        reason_code=value["reason_code"],
    )


def serialize_capability_authorization_health(
    value: CapabilityAuthorizationHealth,
) -> dict[str, Any]:
    """Serialize capability authorization health with explicit nullable fields."""

    if not isinstance(value, CapabilityAuthorizationHealth):
        raise TypeError("value must be CapabilityAuthorizationHealth")
    result = {
        "capability_id": value.capability_id,
        "state": value.state.value,
        "last_verified_at": (
            None
            if value.last_verified_at is None
            else _serialize_timestamp(value.last_verified_at)
        ),
        "expires_at": (
            None if value.expires_at is None else _serialize_timestamp(value.expires_at)
        ),
        "reason_code": value.reason_code,
    }
    validate_schema("CapabilityAuthorizationHealth", result)
    return result


def parse_authorization_health(value: Mapping[str, Any]) -> AuthorizationHealth:
    """Parse the fixed authorization health DTO and its capability list."""

    validate_schema("AuthorizationHealth", value)
    return AuthorizationHealth(
        schema_version=value["schema_version"],
        authorization_ref=value["authorization_ref"],
        login_state=_enum(LoginState, value["login_state"]),
        authorization_state=_enum(AuthorizationState, value["authorization_state"]),
        checked_at=_parse_timestamp(value["checked_at"]),
        expires_at=(
            None
            if value["expires_at"] is None
            else _parse_timestamp(value["expires_at"])
        ),
        generation=value["generation"],
        capabilities=tuple(
            parse_capability_authorization_health(item)
            for item in value["capabilities"]
        ),
    )


def serialize_authorization_health(value: AuthorizationHealth) -> dict[str, Any]:
    """Serialize authorization health with its complete stable field set."""

    if not isinstance(value, AuthorizationHealth):
        raise TypeError("value must be AuthorizationHealth")
    result = {
        "schema_version": value.schema_version,
        "authorization_ref": value.authorization_ref,
        "login_state": value.login_state.value,
        "authorization_state": value.authorization_state.value,
        "checked_at": _serialize_timestamp(value.checked_at),
        "expires_at": (
            None if value.expires_at is None else _serialize_timestamp(value.expires_at)
        ),
        "generation": value.generation,
        "capabilities": [
            serialize_capability_authorization_health(item)
            for item in value.capabilities
        ],
    }
    validate_schema("AuthorizationHealth", result)
    return result


def parse_renewal_session(value: Mapping[str, Any]) -> RenewalSession:
    """Parse one renewal session without accepting credential material."""

    validate_schema("RenewalSession", value)
    return RenewalSession(
        session_ref=value["session_ref"],
        authorization_ref=value["authorization_ref"],
        state=_enum(RenewalState, value["state"]),
        created_at=_parse_timestamp(value["created_at"]),
        expires_at=(
            None
            if value["expires_at"] is None
            else _parse_timestamp(value["expires_at"])
        ),
        authorization_url=value["authorization_url"],
        qr_attachment_ref=value["qr_attachment_ref"],
        generation=value["generation"],
    )


def serialize_renewal_session(value: RenewalSession) -> dict[str, Any]:
    """Serialize the fixed renewal-session field set with null expiry intact."""

    if not isinstance(value, RenewalSession):
        raise TypeError("value must be RenewalSession")
    result = {
        "session_ref": value.session_ref,
        "authorization_ref": value.authorization_ref,
        "state": value.state.value,
        "created_at": _serialize_timestamp(value.created_at),
        "expires_at": (
            None if value.expires_at is None else _serialize_timestamp(value.expires_at)
        ),
        "authorization_url": value.authorization_url,
        "qr_attachment_ref": value.qr_attachment_ref,
        "generation": value.generation,
    }
    validate_schema("RenewalSession", result)
    return result


def _freeze_attributes(
    value: Mapping[str, CloudAttributeValue],
) -> Mapping[str, CloudAttributeValue]:
    """Copy bounded observation metadata into an immutable mapping."""

    return MappingProxyType(dict(value))


def parse_cloud_observation(value: Mapping[str, Any]) -> CloudObservation:
    """Parse one bounded provider observation, preserving unknown timestamps."""

    validate_schema("CloudObservation", value)
    attributes = dict(value["attributes"])
    if any(
        isinstance(item, float) and not math.isfinite(item)
        for item in attributes.values()
    ):
        raise ValueError("cloud attributes must contain finite numbers")
    return CloudObservation(
        external_ref=value["external_ref"],
        kind=_enum(CloudResourceKind, value["kind"]),
        display_name=value["display_name"],
        status=_enum(CloudResourceState, value["status"]),
        provider_status=value["provider_status"],
        collected_at=_parse_timestamp(value["collected_at"]),
        observed_at=(
            None
            if value["observed_at"] is None
            else _parse_timestamp(value["observed_at"])
        ),
        attributes=_freeze_attributes(attributes),
        platform_resource_ref=value["platform_resource_ref"],
        platform_generation=value["platform_generation"],
    )


def serialize_cloud_observation(value: CloudObservation) -> dict[str, Any]:
    """Serialize an observation with only bounded scalar metadata."""

    if not isinstance(value, CloudObservation):
        raise TypeError("value must be CloudObservation")
    result = {
        "external_ref": value.external_ref,
        "kind": value.kind.value,
        "display_name": value.display_name,
        "status": value.status.value,
        "provider_status": value.provider_status,
        "collected_at": _serialize_timestamp(value.collected_at),
        "observed_at": (
            None
            if value.observed_at is None
            else _serialize_timestamp(value.observed_at)
        ),
        "attributes": dict(value.attributes),
        "platform_resource_ref": value.platform_resource_ref,
        "platform_generation": value.platform_generation,
    }
    validate_schema("CloudObservation", result)
    return result


def parse_cloud_query_result(value: Mapping[str, Any]) -> CloudQueryResult:
    """Parse a provider result and all its typed cloud observations."""

    validate_schema("CloudQueryResult", value)
    return CloudQueryResult(
        provider=value["provider"],
        checked_at=_parse_timestamp(value["checked_at"]),
        resources=tuple(parse_cloud_observation(item) for item in value["resources"]),
    )


def serialize_cloud_query_result(value: CloudQueryResult) -> dict[str, Any]:
    """Serialize a query result without deriving Platform inventory authority."""

    if not isinstance(value, CloudQueryResult):
        raise TypeError("value must be CloudQueryResult")
    result = {
        "provider": value.provider,
        "checked_at": _serialize_timestamp(value.checked_at),
        "resources": [serialize_cloud_observation(item) for item in value.resources],
    }
    validate_schema("CloudQueryResult", result)
    return result


def parse_email_submission(value: Mapping[str, Any]) -> EmailSubmission:
    """Parse a submission result whose delivery claim remains explicit."""

    validate_schema("EmailSubmission", value)
    return EmailSubmission(
        request_ref=value["request_ref"],
        status=_enum(EmailSubmissionStatus, value["status"]),
        vendor_receipt_ref=value["vendor_receipt_ref"],
        delivery_confirmed=value["delivery_confirmed"],
    )


def serialize_email_submission(value: EmailSubmission) -> dict[str, Any]:
    """Serialize email submission facts without inventing delivery evidence."""

    if not isinstance(value, EmailSubmission):
        raise TypeError("value must be EmailSubmission")
    result = {
        "request_ref": value.request_ref,
        "status": value.status.value,
        "vendor_receipt_ref": value.vendor_receipt_ref,
        "delivery_confirmed": value.delivery_confirmed,
    }
    validate_schema("EmailSubmission", result)
    return result


def serialize_trusted_caller_context(value: TrustedCallerContext) -> dict[str, Any]:
    """Serialize the signed claims with UTC timestamps and no signing key."""

    if not isinstance(value, TrustedCallerContext):
        raise TypeError("value must be TrustedCallerContext")
    result = {
        "method": value.method,
        "operation_id": value.operation_id,
        "caller_workload": value.caller_workload,
        "audience": value.audience,
        "issuer": value.issuer,
        "subject": value.subject,
        "organization_id": value.organization_id,
        "workspace_id": value.workspace_id,
        "binding_id": value.binding_id,
        "request_id": value.request_id,
        "issued_at": _serialize_timestamp(value.issued_at),
        "expires_at": _serialize_timestamp(value.expires_at),
        "payload_sha256": value.payload_sha256,
        "signature": value.signature,
    }
    validate_schema("TrustedCallerContext", result)
    return result


def parse_trusted_caller_context(value: Mapping[str, Any]) -> TrustedCallerContext:
    """Parse a strict signed context from untrusted JSON before verification."""

    validate_schema("TrustedCallerContext", value)
    return TrustedCallerContext(
        method=value["method"],
        operation_id=value["operation_id"],
        caller_workload=value["caller_workload"],
        audience=value["audience"],
        issuer=value["issuer"],
        subject=value["subject"],
        organization_id=value["organization_id"],
        workspace_id=value["workspace_id"],
        binding_id=value["binding_id"],
        request_id=value["request_id"],
        issued_at=_parse_timestamp(value["issued_at"]),
        expires_at=_parse_timestamp(value["expires_at"]),
        payload_sha256=value["payload_sha256"],
        signature=value["signature"],
    )


def _parse_argument(name: str, value: Mapping[str, Any], cls: type[Any]) -> Any:
    """Validate an exact argument schema before constructing its DTO."""

    validate_schema(name, value)
    return cls(**dict(value))


def parse_wecom_authorization_health_arguments(
    value: Mapping[str, Any],
) -> WeComAuthorizationHealthArguments:
    """Parse the configured WeCom authorization reference argument."""

    return _parse_argument(
        "WeComAuthorizationHealthArguments", value, WeComAuthorizationHealthArguments
    )


def serialize_wecom_authorization_health_arguments(
    value: WeComAuthorizationHealthArguments,
) -> dict[str, Any]:
    """Serialize WeCom authorization health arguments."""

    result = {"authorization_ref": value.authorization_ref}
    validate_schema("WeComAuthorizationHealthArguments", result)
    return result


def parse_wecom_reauthorization_start_arguments(
    value: Mapping[str, Any],
) -> WeComReauthorizationStartArguments:
    """Parse the reference for an operator-authorized WeCom renewal."""

    return _parse_argument(
        "WeComReauthorizationStartArguments", value, WeComReauthorizationStartArguments
    )


def serialize_wecom_reauthorization_start_arguments(
    value: WeComReauthorizationStartArguments,
) -> dict[str, Any]:
    """Serialize WeCom renewal-start arguments."""

    result = {"authorization_ref": value.authorization_ref}
    validate_schema("WeComReauthorizationStartArguments", result)
    return result


def parse_wecom_reauthorization_status_arguments(
    value: Mapping[str, Any],
) -> WeComReauthorizationStatusArguments:
    """Parse the opaque renewal session status selector."""

    return _parse_argument(
        "WeComReauthorizationStatusArguments",
        value,
        WeComReauthorizationStatusArguments,
    )


def serialize_wecom_reauthorization_status_arguments(
    value: WeComReauthorizationStatusArguments,
) -> dict[str, Any]:
    """Serialize WeCom renewal status arguments."""

    result = {"session_ref": value.session_ref}
    validate_schema("WeComReauthorizationStatusArguments", result)
    return result


def parse_cloud_resources_query_arguments(
    value: Mapping[str, Any],
) -> CloudResourcesQueryArguments:
    """Parse the configured opaque target selector for resource reads."""

    return _parse_argument(
        "CloudResourcesQueryArguments", value, CloudResourcesQueryArguments
    )


def serialize_cloud_resources_query_arguments(
    value: CloudResourcesQueryArguments,
) -> dict[str, Any]:
    """Serialize cloud resource query arguments."""

    result = {"target_ref": value.target_ref}
    validate_schema("CloudResourcesQueryArguments", result)
    return result


def parse_cloud_authorization_health_arguments(
    value: Mapping[str, Any],
) -> CloudAuthorizationHealthArguments:
    """Parse the configured target selector for cloud authorization health."""

    return _parse_argument(
        "CloudAuthorizationHealthArguments", value, CloudAuthorizationHealthArguments
    )


def serialize_cloud_authorization_health_arguments(
    value: CloudAuthorizationHealthArguments,
) -> dict[str, Any]:
    """Serialize cloud authorization health arguments."""

    result = {"target_ref": value.target_ref}
    validate_schema("CloudAuthorizationHealthArguments", result)
    return result


def parse_email_notification_send_arguments(
    value: Mapping[str, Any],
) -> EmailNotificationSendArguments:
    """Parse an allowlisted email notification request."""

    return _parse_argument(
        "EmailNotificationSendArguments", value, EmailNotificationSendArguments
    )


def serialize_email_notification_send_arguments(
    value: EmailNotificationSendArguments,
) -> dict[str, Any]:
    """Serialize email send arguments with recipient alias only."""

    result = {
        "recipient_ref": value.recipient_ref,
        "subject": value.subject,
        "text": value.text,
        "request_ref": value.request_ref,
    }
    validate_schema("EmailNotificationSendArguments", result)
    return result


def parse_email_connection_health_arguments(
    value: Mapping[str, Any],
) -> EmailConnectionHealthArguments:
    """Parse the empty email health-check request."""

    return _parse_argument(
        "EmailConnectionHealthArguments", value, EmailConnectionHealthArguments
    )


def serialize_email_connection_health_arguments(
    value: EmailConnectionHealthArguments,
) -> dict[str, Any]:
    """Serialize the empty email health-check request."""

    result: dict[str, Any] = {}
    validate_schema("EmailConnectionHealthArguments", result)
    return result


def parse_tool_envelope(value: Mapping[str, Any]) -> ToolEnvelope:
    """Parse a fixed tool envelope while preserving exact inner JSON arguments."""

    validate_schema("ToolEnvelope", value)
    arguments = value["arguments"]
    return ToolEnvelope(
        caller_context=parse_trusted_caller_context(value["caller_context"]),
        arguments=MappingProxyType(dict(arguments)),
    )


def serialize_tool_envelope(value: ToolEnvelope) -> dict[str, Any]:
    """Serialize a tool envelope without changing its signed argument object."""

    if not isinstance(value, ToolEnvelope):
        raise TypeError("value must be ToolEnvelope")
    result = {
        "caller_context": serialize_trusted_caller_context(value.caller_context),
        "arguments": dict(value.arguments),
    }
    validate_schema("ToolEnvelope", result)
    return result


def write_replay_identity(
    context: TrustedCallerContext | Mapping[str, Any], payload: Mapping[str, Any]
) -> tuple[str, str]:
    """Return the scoped replay key and payload hash for a supported write tool.

    This helper derives identity only; callers must first verify the HMAC context
    with `verify_context`. Persist the returned replay key and payload hash before
    side effects. An exact replay returns the stored result; a matching replay key
    with a different hash is rejected. 中文：先验签，再持久化幂等记录后执行副作用。

    Args:
        context: Verified trusted caller context or its wire representation.
        payload: Exact inner argument object covered by the trusted context.
    Returns:
        `(replay_key, payload_sha256)` for the caller's private durable ledger.
    Raises:
        ValueError: If the tool is not a supported write, or the context is invalid.
        jsonschema.ValidationError: If the payload violates its write-tool schema.
    """

    parsed = (
        context
        if isinstance(context, TrustedCallerContext)
        else parse_trusted_caller_context(context)
    )
    if parsed.method == "wecom_reauthorization_start":
        validate_schema("WeComReauthorizationStartArguments", payload)
        replay_ref = parsed.request_id
    elif parsed.method == "email_notification_send":
        validate_schema("EmailNotificationSendArguments", payload)
        replay_ref = payload["request_ref"]
    else:
        raise ValueError("tool does not have a write replay identity")
    if TOOL_OPERATIONS.get(parsed.method) != parsed.operation_id:
        raise ValueError("method and operation are not a fixed tool pair")
    identity = {
        "binding_id": parsed.binding_id,
        "caller_workload": parsed.caller_workload,
        "audience": parsed.audience,
        "organization_id": parsed.organization_id,
        "workspace_id": parsed.workspace_id,
        "issuer": parsed.issuer,
        "subject": parsed.subject,
        "method": parsed.method,
        "operation_id": parsed.operation_id,
        "replay_ref": replay_ref,
    }
    return (
        hashlib.sha256(canonical_json(identity)).hexdigest(),
        payload_sha256(payload),
    )


def _signing_claims(
    *,
    method: str,
    operation_id: str,
    payload: Any,
    caller_workload: str,
    audience: str,
    issuer: str,
    subject: str,
    organization_id: str,
    workspace_id: str | None,
    binding_id: str,
    request_id: str,
    issued_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """Build the exact claim object covered by the HMAC signature."""

    claims = {
        "method": method,
        "operation_id": operation_id,
        "caller_workload": caller_workload,
        "audience": audience,
        "issuer": issuer,
        "subject": subject,
        "organization_id": organization_id,
        "workspace_id": workspace_id,
        "binding_id": binding_id,
        "request_id": request_id,
        "issued_at": _serialize_timestamp(issued_at),
        "expires_at": _serialize_timestamp(expires_at),
        "payload_sha256": payload_sha256(payload),
    }
    if any(
        not isinstance(claims[field], str)
        or not claims[field]
        or len(claims[field]) > maximum
        for field, maximum in (
            ("method", 100),
            ("operation_id", 200),
            ("caller_workload", 200),
            ("audience", 200),
            ("issuer", 500),
            ("subject", 500),
            ("organization_id", 200),
            ("binding_id", 200),
            ("request_id", 200),
        )
    ) or (
        workspace_id is not None
        and (
            not isinstance(workspace_id, str)
            or not workspace_id
            or len(workspace_id) > 200
        )
    ):
        raise ValueError("caller context claims are invalid")
    if TOOL_OPERATIONS.get(method) != operation_id:
        raise ValueError("method and operation are not a fixed tool pair")
    return claims


def sign_context(
    *,
    method: str,
    operation_id: str,
    payload: Any,
    caller_workload: str,
    audience: str,
    issuer: str,
    subject: str,
    organization_id: str,
    workspace_id: str | None,
    binding_id: str,
    request_id: str,
    key: bytes,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> TrustedCallerContext:
    """Sign one exact method, operation, caller scope, and payload.

    Args:
        method: Fixed `tool.provider.v1` tool ID.
        operation_id: Fixed operation paired with `method` in `TOOL_OPERATIONS`.
        payload: Exact inner `arguments` JSON object.
        caller_workload: Trusted configured caller workload identity.
        audience: Exact provider workload audience.
        issuer: Authenticated identity issuer.
        subject: Authenticated actor subject.
        organization_id: Authenticated organization scope.
        workspace_id: Authenticated workspace scope or `None`.
        binding_id: Exact configured provider binding.
        request_id: Unique request correlation ID.
        key: Private 32-byte HMAC key from trusted configuration.
        issued_at: Optional aware UTC issue time; defaults to current UTC.
        expires_at: Optional aware UTC expiry; defaults to 120 seconds after issue.
    Returns:
        A typed context containing the digest and HMAC signature, never the key.
    Raises:
        ValueError: If claims, time bounds, tool pair, or key length are invalid.
    """

    if not isinstance(key, bytes) or len(key) != CONTEXT_KEY_BYTES:
        raise ValueError("caller context key must be exactly 32 bytes")
    issued = datetime.now(UTC) if issued_at is None else issued_at
    if (
        not isinstance(issued, datetime)
        or issued.tzinfo is None
        or issued.utcoffset() != timedelta(0)
    ):
        raise ValueError("issued_at must be timezone-aware UTC")
    expires = issued + DEFAULT_CONTEXT_TTL if expires_at is None else expires_at
    if (
        not isinstance(expires, datetime)
        or expires.tzinfo is None
        or expires.utcoffset() != timedelta(0)
    ):
        raise ValueError("expires_at must be timezone-aware UTC")
    current = datetime.now(UTC)
    if (
        issued > current + MAX_FUTURE_SKEW
        or expires <= current
        or expires <= issued
        or expires - issued > MAX_CONTEXT_TTL
    ):
        raise ValueError("caller context lifetime is invalid")
    fields = _signing_claims(
        method=method,
        operation_id=operation_id,
        payload=payload,
        caller_workload=caller_workload,
        audience=audience,
        issuer=issuer,
        subject=subject,
        organization_id=organization_id,
        workspace_id=workspace_id,
        binding_id=binding_id,
        request_id=request_id,
        issued_at=issued,
        expires_at=expires,
    )
    signature = hmac.new(key, canonical_json(fields), hashlib.sha256).hexdigest()
    context = TrustedCallerContext(
        method=method,
        operation_id=operation_id,
        caller_workload=caller_workload,
        audience=audience,
        issuer=issuer,
        subject=subject,
        organization_id=organization_id,
        workspace_id=workspace_id,
        binding_id=binding_id,
        request_id=request_id,
        issued_at=issued.astimezone(UTC),
        expires_at=expires.astimezone(UTC),
        payload_sha256=fields["payload_sha256"],
        signature=signature,
    )
    serialize_trusted_caller_context(context)
    return context


def verify_context(
    *,
    method: str,
    operation_id: str,
    payload: Any,
    context: TrustedCallerContext | Mapping[str, Any],
    key: bytes,
    expected_caller_workload: str,
    expected_audience: str,
    expected_binding_id: str,
    expected_organization_id: str,
    expected_workspace_id: str | None,
    expected_issuer: str | None = None,
    expected_subject: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Verify configured key, exact bindings, payload, actor scope, and short TTL.

    Args:
        method: Expected fixed tool ID.
        operation_id: Expected operation paired with the tool ID.
        payload: Exact inner `arguments` JSON object received from the tool call.
        context: Untrusted signed context DTO or wire mapping.
        key: Private 32-byte HMAC key selected from binding configuration.
        expected_caller_workload: Configured caller workload identity.
        expected_audience: Configured provider workload audience.
        expected_binding_id: Configured binding identity.
        expected_organization_id: Authenticated organization scope.
        expected_workspace_id: Authenticated workspace scope or `None`.
        expected_issuer: Optional issuer pin from workload policy.
        expected_subject: Optional subject pin from workload policy.
        now: Optional aware UTC current time, useful for deterministic checks.
    Returns:
        `True` only when every signature, freshness, method, operation, and scope
        check succeeds; otherwise `False`.
    """

    if (
        not isinstance(key, bytes)
        or len(key) != CONTEXT_KEY_BYTES
        or TOOL_OPERATIONS.get(method) != operation_id
        or not all(
            isinstance(value, str) and value
            for value in (
                expected_caller_workload,
                expected_audience,
                expected_binding_id,
                expected_organization_id,
            )
        )
        or (
            expected_workspace_id is not None
            and (
                not isinstance(expected_workspace_id, str) or not expected_workspace_id
            )
        )
        or (
            expected_issuer is not None
            and (not isinstance(expected_issuer, str) or not expected_issuer)
        )
        or (
            expected_subject is not None
            and (not isinstance(expected_subject, str) or not expected_subject)
        )
    ):
        return False
    if isinstance(context, TrustedCallerContext):
        values = serialize_trusted_caller_context(context)
    elif isinstance(context, Mapping):
        values = dict(context)
    else:
        return False
    try:
        parsed = parse_trusted_caller_context(values)
        current = datetime.now(UTC) if now is None else now
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() != timedelta(0)
        ):
            return False
        if (
            parsed.method != method
            or parsed.operation_id != operation_id
            or parsed.caller_workload != expected_caller_workload
            or parsed.audience != expected_audience
            or parsed.binding_id != expected_binding_id
            or parsed.organization_id != expected_organization_id
            or parsed.workspace_id != expected_workspace_id
            or (expected_issuer is not None and parsed.issuer != expected_issuer)
            or (expected_subject is not None and parsed.subject != expected_subject)
            or parsed.issued_at > current + MAX_FUTURE_SKEW
            or parsed.expires_at <= current
            or parsed.expires_at <= parsed.issued_at
            or parsed.expires_at - parsed.issued_at > MAX_CONTEXT_TTL
            or not hmac.compare_digest(parsed.payload_sha256, payload_sha256(payload))
        ):
            return False
        unsigned = {field: values[field] for field in _CONTEXT_FIELDS}
        expected_signature = hmac.new(
            key, canonical_json(unsigned), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(parsed.signature, expected_signature)
    except (TypeError, ValueError, jsonschema.ValidationError):
        return False
