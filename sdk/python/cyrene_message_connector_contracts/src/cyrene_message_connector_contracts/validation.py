"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 validation.py                                                    │
│  Module: cyrene_message_connector_contracts                          │
│  Role: Enforce canonical message.connector.v1 input boundaries.      │
│                                                                      │
│  模块职责：校验 message.connector.v1 规范输入边界。                   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ._generated import message_connector_pb2 as wire

MAX_JSON_PAYLOAD_BYTES = 16 * 1024
MAX_VENDOR_FACTS = 32
MAX_VENDOR_FACT_NAME_BYTES = 64
MAX_VENDOR_FACT_VALUE_BYTES = 2_048
MAX_VENDOR_FACT_TOTAL_BYTES = 8_192

_REQUEST_KINDS = frozenset({"friend", "group_invite"})
_DECISIONS = frozenset({"approve", "reject"})
_VENDOR_REQUEST_FIELDS = frozenset({"sub_type", "group_id", "user_id"})


class MessageConnectorValidationError(ValueError):
    """A message.connector.v1 value violates the frozen contract boundary."""


def validate_conversation_scope(scope: wire.ConversationScope) -> None:
    """Require a complete, recognized vendor account and conversation scope.

    中文：确保 vendor、账号、会话身份完整，且会话类型已识别。
    """

    _bounded_text(scope.vendor, "conversation.vendor", 64)
    _bounded_text(scope.account_id, "conversation.account_id", 512)
    _bounded_text(scope.conversation_id, "conversation.conversation_id", 512)
    if scope.kind not in {
        wire.CONVERSATION_KIND_PRIVATE,
        wire.CONVERSATION_KIND_GROUP,
        wire.CONVERSATION_KIND_CHANNEL,
    }:
        raise MessageConnectorValidationError("conversation.kind is unspecified or invalid")


def validate_vendor_extension(extension: wire.VendorExtension) -> None:
    """Enforce the proto's explicit bounded vendor-fact limits."""

    _bounded_text(extension.vendor, "vendor_extension.vendor", 64)
    if len(extension.facts) > MAX_VENDOR_FACTS:
        raise MessageConnectorValidationError(
            "vendor_extension.facts must contain at most 32 facts"
        )
    names: set[str] = set()
    total = 0
    for index, fact in enumerate(extension.facts):
        name_size = len(fact.name.encode("utf-8"))
        value_size = len(fact.value.encode("utf-8"))
        if not fact.name or name_size > MAX_VENDOR_FACT_NAME_BYTES:
            raise MessageConnectorValidationError(
                f"vendor_extension.facts[{index}].name exceeds 64 UTF-8 bytes"
            )
        if value_size > MAX_VENDOR_FACT_VALUE_BYTES:
            raise MessageConnectorValidationError(
                f"vendor_extension.facts[{index}].value exceeds 2048 UTF-8 bytes"
            )
        if fact.name in names:
            raise MessageConnectorValidationError("vendor_extension fact names must be unique")
        names.add(fact.name)
        total += name_size + value_size
    if total > MAX_VENDOR_FACT_TOTAL_BYTES:
        raise MessageConnectorValidationError("vendor_extension facts exceed 8192 aggregate bytes")


def validate_message_content_part(
    part: wire.MessageContentPart, scope: wire.ConversationScope
) -> None:
    """Validate one selected content arm and bind opaque media to its scope."""

    selected = part.WhichOneof("kind")
    if selected is None:
        raise MessageConnectorValidationError("content part must select one kind")
    if selected == "text":
        if not part.text.text.strip():
            raise MessageConnectorValidationError("text content must not be empty")
        return
    if selected == "mention":
        mention = part.mention
        if mention.target == wire.MENTION_TARGET_USER:
            _bounded_text(mention.target_id, "mention.target_id", 512)
        elif mention.target == wire.MENTION_TARGET_EVERYONE:
            if mention.target_id:
                raise MessageConnectorValidationError(
                    "everyone mentions must not specify target_id"
                )
        else:
            raise MessageConnectorValidationError("mention.target is unspecified or invalid")
        return
    if selected in {"image", "file"}:
        media = part.image if selected == "image" else part.file
        if selected == "image":
            _bounded_text(media.mime_type, "image.mime_type", 256)
        else:
            _bounded_text(media.file_name, "file.file_name", 512)
            _bounded_text(media.mime_type, "file.mime_type", 256)
        reference = media.reference
        location = reference.WhichOneof("location")
        if location == "remote_uri":
            _validate_remote_uri(reference.remote_uri)
        elif location == "vendor_media":
            vendor_media = reference.vendor_media
            _bounded_text(vendor_media.vendor, "vendor_media.vendor", 64)
            _bounded_text(vendor_media.account_id, "vendor_media.account_id", 512)
            _bounded_text(vendor_media.media_id, "vendor_media.media_id", 512)
            if (
                vendor_media.vendor != scope.vendor
                or vendor_media.account_id != scope.account_id
            ):
                raise MessageConnectorValidationError(
                    "vendor_media must match the message conversation account"
                )
        else:
            raise MessageConnectorValidationError("attachment reference must select a location")
        return
    raise MessageConnectorValidationError("content part kind is unsupported")


def validate_send_message_request(request: wire.SendMessageRequest) -> None:
    """Validate the destination and each outbound content part."""

    if not request.HasField("conversation"):
        raise MessageConnectorValidationError("send_message conversation is required")
    validate_conversation_scope(request.conversation)
    if not request.content:
        raise MessageConnectorValidationError("send_message content must not be empty")
    for item in request.content:
        validate_message_content_part(item, request.conversation)
    if request.HasField("reply"):
        _bounded_text(request.reply.message_id, "reply.message_id", 512)
    if request.HasField("vendor_extension"):
        validate_vendor_extension(request.vendor_extension)


def validate_delivery_result(result: wire.DeliveryResult) -> None:
    """Reject a delivery result that does not identify an outcome."""

    if result.status not in {
        wire.DELIVERY_STATUS_ACCEPTED,
        wire.DELIVERY_STATUS_REJECTED,
        wire.DELIVERY_STATUS_RATE_LIMITED,
    }:
        raise MessageConnectorValidationError("delivery status is unspecified or invalid")
    if result.HasField("vendor_extension"):
        validate_vendor_extension(result.vendor_extension)


def validate_inbound_message(message: wire.InboundMessagePayload) -> None:
    """Validate identity and content on an inbound message event."""

    _bounded_text(message.message_id, "message_id", 512)
    if not message.HasField("conversation"):
        raise MessageConnectorValidationError("inbound conversation is required")
    validate_conversation_scope(message.conversation)
    _bounded_text(message.sender_id, "sender_id", 512)
    if not message.content:
        raise MessageConnectorValidationError("inbound message content must not be empty")
    for item in message.content:
        validate_message_content_part(item, message.conversation)
    if message.HasField("reply"):
        _bounded_text(message.reply.message_id, "reply.message_id", 512)
    if message.HasField("vendor_extension"):
        validate_vendor_extension(message.vendor_extension)


def validate_inbound_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the frozen inbound-request JSON shape and return a copy."""

    required = {"account_id", "vendor", "request_id", "request_kind", "vendor_request"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise MessageConnectorValidationError("inbound_request has an invalid field set")
    _json_text(value["account_id"], "account_id", 1, 512)
    _json_text(value["vendor"], "vendor", 1, 64)
    _json_text(value["request_id"], "request_id", 1, 512)
    request_kind = _json_text(value["request_kind"], "request_kind", 1, 32)
    if request_kind not in _REQUEST_KINDS:
        raise MessageConnectorValidationError("request_kind is unsupported in V1")
    vendor_request = _validate_vendor_request(value["vendor_request"])
    return {
        "account_id": value["account_id"],
        "vendor": value["vendor"],
        "request_id": value["request_id"],
        "request_kind": request_kind,
        "vendor_request": vendor_request,
    }


def validate_request_response(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the frozen respond_request JSON shape and return a copy."""

    required = {"request_id", "request_kind", "decision"}
    optional = {"comment", "vendor_request"}
    if (
        not isinstance(value, Mapping)
        or not required.issubset(value)
        or set(value) - required - optional
    ):
        raise MessageConnectorValidationError("respond_request has an invalid field set")
    request_id = _json_text(value["request_id"], "request_id", 1, 512)
    request_kind = _json_text(value["request_kind"], "request_kind", 1, 32)
    decision = _json_text(value["decision"], "decision", 1, 16)
    if request_kind not in _REQUEST_KINDS:
        raise MessageConnectorValidationError("request_kind is unsupported in V1")
    if decision not in {"approve", "reject"}:
        raise MessageConnectorValidationError("decision must be approve or reject")
    result: dict[str, Any] = {
        "request_id": request_id,
        "request_kind": request_kind,
        "decision": decision,
    }
    if "comment" in value:
        result["comment"] = _json_text(value["comment"], "comment", 0, 2_048)
    if "vendor_request" in value:
        result["vendor_request"] = _validate_vendor_request(value["vendor_request"])
    return result


def _validate_vendor_request(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise MessageConnectorValidationError("vendor_request must be an object")
    if set(value) - _VENDOR_REQUEST_FIELDS:
        raise MessageConnectorValidationError("vendor_request contains unsupported fields")
    result: dict[str, str] = {}
    if "sub_type" in value:
        subtype = _json_text(value["sub_type"], "vendor_request.sub_type", 1, 16)
        if subtype not in {"add", "invite"}:
            raise MessageConnectorValidationError("vendor_request.sub_type is unsupported")
        result["sub_type"] = subtype
    for key in ("group_id", "user_id"):
        if key in value:
            result[key] = _json_text(value[key], f"vendor_request.{key}", 0, 512)
    return result


def decode_json_object(payload: bytes | str, label: str) -> dict[str, Any]:
    """Parse one bounded JSON object while rejecting duplicate keys/constants."""

    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MessageConnectorValidationError(f"{label} must be UTF-8 JSON") from error
    elif isinstance(payload, str):
        text = payload
    else:
        raise MessageConnectorValidationError(f"{label} must be JSON bytes or text")
    if not text or len(text.encode("utf-8")) > MAX_JSON_PAYLOAD_BYTES:
        raise MessageConnectorValidationError("JSON connector payload exceeds 16 KiB")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise MessageConnectorValidationError(f"{label} contains duplicate fields")
            result[key] = item
        return result

    def invalid_constant(_: str) -> None:
        raise MessageConnectorValidationError(f"{label} contains an invalid JSON constant")

    try:
        result = json.loads(text, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except json.JSONDecodeError as error:
        raise MessageConnectorValidationError(f"{label} is malformed JSON") from error
    if not isinstance(result, dict):
        raise MessageConnectorValidationError(f"{label} must be a JSON object")
    return result


def _validate_remote_uri(value: str) -> None:
    _bounded_text(value, "attachment.remote_uri", 4_096)
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise MessageConnectorValidationError(
            "remote_uri must be an absolute HTTP(S) URI without credentials"
        )


def _bounded_text(value: str, label: str, max_bytes: int) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise MessageConnectorValidationError(f"{label} must be non-empty text")
    if len(value.encode("utf-8")) > max_bytes:
        raise MessageConnectorValidationError(f"{label} exceeds {max_bytes} UTF-8 bytes")


def _json_text(value: Any, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or len(value) < minimum or len(value) > maximum:
        raise MessageConnectorValidationError(
            f"{label} must be text with {minimum}..{maximum} characters"
        )
    return value
