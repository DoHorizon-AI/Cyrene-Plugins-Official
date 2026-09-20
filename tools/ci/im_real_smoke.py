"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 im_real_smoke.py                                                │
│  Module: Cyrene protected QQNT direct smoke                         │
│  Role: Run one authorized Linux x86_64 QQ Host smoke scenario.       │
│                                                                     │
│  模块职责：在受保护 runner 上使用真实 QQ Host 验证直连边界；           │
│  不接受 fake Host、模糊安装、原始密码或任意 Service/Method。          │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class SmokeConfigurationError(RuntimeError):
    """Raised when a protected smoke environment is not fully configured."""


def _repository_root() -> Path:
    """Return the checkout root from this script's stable repository location."""

    return Path(__file__).resolve().parents[2]


def _configure_import_path() -> None:
    """Expose the checked-out runtime and connector without installing the repo."""

    root = _repository_root()
    for relative in (
        "sdk/python/cyrene_plugin_runtime/src",
        "plugins/connectors/im/src",
    ):
        value = str(root / relative)
        if value not in sys.path:
            sys.path.insert(0, value)


_configure_import_path()

from qq_connector._generated import message_connector_pb2 as message_contract
from qq_connector.qqnt_direct import (
    QQNTDirectConfig,
    QQNTDirectConnector,
)
from qq_connector.qqnt_direct_operations import (
    CALLBACK_ONLY_OPERATION_NAMES,
    QQ_OPERATION_BY_NAME,
    get_qq_operation,
)
from qq_connector.support import ConnectorError

_REQUIRED_REAL_SMOKE_OPERATIONS = frozenset(
    {
        "qq.message.history_include_self",
        "qq.message.by_id",
        "qq.message.recall",
        "qq.message.forward",
        "qq.media.download",
        "qq.file.list",
        "qq.group.modify_remark",
        "qq.search.contact",
        "qq.profile.long_nick",
        "qq.online.devices",
    }
)
_RESERVED_SCENARIO_KEYS = frozenset(
    {"password", "token", "raw_payload", "service", "method"}
)
_MAX_SCENARIO_BYTES = 512 * 1024
_MAX_SCENARIO_OPERATIONS = 64
_MAX_MARKER_BYTES = 256
_MAX_EVENTS = 128


class RecordingEmitter:
    """Capture only bounded canonical events for the protected smoke process."""

    def __init__(self) -> None:
        self.events: list[tuple[str, bytes, str]] = []

    def emit(self, event_type: str, payload: bytes, type_url: str = "") -> bool:
        """Record one event and return the worker emitter's accepted result."""

        if len(self.events) < _MAX_EVENTS:
            self.events.append((event_type, bytes(payload), type_url))
        return True


def _required_env(name: str) -> str:
    """Read one required protected environment value without logging its content."""

    value = os.environ.get(name, "").strip()
    if not value:
        raise SmokeConfigurationError(f"missing protected environment value: {name}")
    return value


def _absolute_path(name: str, value: str) -> Path:
    """Validate one absolute path and return its canonical path."""

    path = Path(value)
    if not path.is_absolute():
        raise SmokeConfigurationError(f"{name} must be absolute")
    return path


def _host_args() -> list[str]:
    """Decode bounded Host arguments supplied by the protected environment."""

    encoded = os.environ.get("QQNT_HOST_ARGS_JSON", "[]")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise SmokeConfigurationError("QQNT_HOST_ARGS_JSON is not valid JSON") from exc
    if not isinstance(value, list) or len(value) > 32:
        raise SmokeConfigurationError(
            "QQNT_HOST_ARGS_JSON must be a list of at most 32 items"
        )
    if any(not isinstance(item, str) or len(item) > 512 for item in value):
        raise SmokeConfigurationError(
            "QQNT_HOST_ARGS_JSON contains an invalid argument"
        )
    return value


def _reject_reserved_keys(value: Any) -> None:
    """Reject credential or passthrough fields anywhere in the scenario tree."""

    if isinstance(value, Mapping):
        if _RESERVED_SCENARIO_KEYS.intersection(value):
            raise SmokeConfigurationError("smoke scenario contains a reserved field")
        for item in value.values():
            _reject_reserved_keys(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_reserved_keys(item)


def _load_scenario() -> dict[str, Any]:
    """Load one operator-authored scenario from outside the checked-out source."""

    path = _absolute_path(
        "QQNT_SMOKE_SCENARIO_PATH", _required_env("QQNT_SMOKE_SCENARIO_PATH")
    )
    try:
        resolved = path.resolve(strict=True)
        raw = resolved.read_bytes()
    except OSError as exc:
        raise SmokeConfigurationError(
            "QQNT_SMOKE_SCENARIO_PATH cannot be read"
        ) from exc
    if len(raw) > _MAX_SCENARIO_BYTES:
        raise SmokeConfigurationError("QQNT smoke scenario exceeds the size limit")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeConfigurationError("QQNT smoke scenario is not UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise SmokeConfigurationError("QQNT smoke scenario must be an object")
    _reject_reserved_keys(decoded)
    operations = decoded.get("operations")
    if not isinstance(operations, dict) or not operations:
        raise SmokeConfigurationError("smoke scenario must declare operations")
    if len(operations) > _MAX_SCENARIO_OPERATIONS:
        raise SmokeConfigurationError("smoke scenario declares too many operations")
    for operation, params in operations.items():
        if not isinstance(operation, str) or get_qq_operation(operation) is None:
            raise SmokeConfigurationError(
                "smoke scenario contains an unknown operation"
            )
        if operation in CALLBACK_ONLY_OPERATION_NAMES:
            raise SmokeConfigurationError(
                "smoke scenario cannot invoke callback-only operations"
            )
        if not isinstance(params, dict):
            raise SmokeConfigurationError(
                "each smoke operation must have an object params value"
            )
    missing = _REQUIRED_REAL_SMOKE_OPERATIONS.difference(operations)
    if missing:
        raise SmokeConfigurationError(
            "smoke scenario does not cover required P1/P2 operations"
        )
    return decoded


def _required_text(value: Any, field: str, *, maximum: int = 512) -> str:
    """Validate one bounded scenario text field."""

    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
    ):
        raise SmokeConfigurationError(f"scenario field {field} must be bounded text")
    return value.strip()


def _conversation(
    value: Any,
    *,
    field: str,
    kind: str,
    account_id: str,
) -> dict[str, str]:
    """Validate one private or group target while preserving vendor identity."""

    if not isinstance(value, Mapping):
        raise SmokeConfigurationError(f"scenario field {field} must be an object")
    conversation_id = _required_text(
        value.get("conversation_id"), f"{field}.conversation_id"
    )
    peer_uid = _required_text(value.get("peer_uid"), f"{field}.peer_uid")
    return {
        "vendor": "qq",
        "account_id": account_id,
        "conversation_id": conversation_id,
        "kind": kind,
        "peer_uid": peer_uid,
    }


def _substitute(value: Any, replacements: Mapping[str, str]) -> Any:
    """Substitute only documented smoke identifiers in operation parameters."""

    if isinstance(value, str):
        result = value
        for key, replacement in replacements.items():
            result = result.replace(key, replacement)
        if "$" in result:
            raise SmokeConfigurationError(
                "scenario contains an unresolved smoke placeholder"
            )
        return result
    if isinstance(value, list):
        return [_substitute(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, replacements) for key, item in value.items()}
    return value


def _operation_result(
    connector: QQNTDirectConnector,
    operation: str,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Invoke one fixed operation and return only its bounded result envelope."""

    result = connector.invoke_extension(operation, params)
    if result.get("status") != "accepted":
        raise SmokeConfigurationError(f"operation was not accepted: {operation}")
    return result


def _send_message(
    connector: QQNTDirectConnector,
    conversation: Mapping[str, str],
    marker: str,
) -> str:
    """Send one canonical text message and return its native vendor message ID."""

    result = connector.send_message(
        {
            "conversation": dict(conversation),
            "content": [{"text": {"text": marker}}],
            "vendor_extension": {
                "facts": [{"name": "qq_peer_uid", "value": conversation["peer_uid"]}]
            },
        }
    )
    message_id = result.get("vendor_message_id")
    if (
        result.get("status") != "accepted"
        or not isinstance(message_id, str)
        or not message_id
    ):
        raise SmokeConfigurationError(
            "QQ send_message did not return a vendor message ID"
        )
    return message_id


def _inbound_text(payload: bytes) -> tuple[str, str, str, str]:
    """Decode one canonical inbound event into identity and text evidence."""

    message = message_contract.InboundMessagePayload.FromString(payload)
    if not message.message_id or not message.conversation.conversation_id:
        raise SmokeConfigurationError("inbound event is missing canonical identity")
    parts = []
    for content in message.content:
        if content.WhichOneof("kind") == "text":
            parts.append(content.text.text)
    kind = {
        message_contract.CONVERSATION_KIND_PRIVATE: "private",
        message_contract.CONVERSATION_KIND_GROUP: "group",
    }.get(message.conversation.kind)
    if kind is None:
        raise SmokeConfigurationError("inbound event has an unknown conversation kind")
    return (
        kind,
        str(message.conversation.conversation_id),
        message.message_id,
        "".join(parts),
    )


def _wait_for_inbound(
    emitter: RecordingEmitter,
    expected: Sequence[Mapping[str, Any]],
    timeout_seconds: float,
) -> None:
    """Wait for each configured private/group canonical inbound assertion."""

    deadline = time.monotonic() + timeout_seconds
    matched: set[int] = set()
    while time.monotonic() < deadline:
        for event_type, payload, _type_url in tuple(emitter.events):
            if event_type != "inbound_message":
                continue
            try:
                conversation_kind, conversation_id, _message_id, text = _inbound_text(
                    payload
                )
            except (SmokeConfigurationError, ValueError):
                continue
            for index, assertion in enumerate(expected):
                if index in matched:
                    continue
                if assertion.get("conversation_id") != conversation_id:
                    continue
                if assertion.get("conversation_kind") != conversation_kind:
                    continue
                text_contains = _required_text(
                    assertion.get("text_contains"),
                    f"expected_inbound[{index}].text_contains",
                )
                if text_contains in text:
                    matched.add(index)
        if len(matched) == len(expected):
            return
        time.sleep(0.05)
    raise SmokeConfigurationError("expected canonical inbound event was not observed")


def _assert_ready(connector: QQNTDirectConnector, account_id: str) -> Mapping[str, Any]:
    """Verify exact Host compatibility and the configured account identity."""

    compatibility = connector.compatibility
    if compatibility.get("platform") != "linux-x86_64":
        raise SmokeConfigurationError("QQ Host reported an unexpected platform")
    if (
        compatibility.get("client_version")
        != os.environ["QQNT_REQUIRED_CLIENT_VERSION"]
    ):
        raise SmokeConfigurationError("QQ Host reported an unexpected client build")
    if compatibility.get("abi") != os.environ["QQNT_REQUIRED_HOST_ABI"]:
        raise SmokeConfigurationError("QQ Host reported an unexpected ABI")
    if connector.state != "READY":
        raise SmokeConfigurationError("QQ Host session did not reach READY")
    status = _operation_result(
        connector, "qq.login.self_status", {"account_id": account_id}
    )
    result = status.get("result")
    if not isinstance(result, Mapping) or result.get("account_id") != account_id:
        raise SmokeConfigurationError("QQ Host returned a mismatched account")
    return result


def _build_config(scenario: Mapping[str, Any], account_id: str) -> dict[str, Any]:
    """Build the binding configuration from protected inputs and the scenario."""

    binding_id = _required_text(
        os.environ.get("QQNT_SMOKE_BINDING_ID", "qq-real-smoke"),
        "QQNT_SMOKE_BINDING_ID",
        maximum=128,
    )
    marker_prefix = _required_text(scenario.get("marker_prefix"), "marker_prefix")
    if len(marker_prefix.encode("utf-8")) > _MAX_MARKER_BYTES:
        raise SmokeConfigurationError("marker_prefix is too long")
    del marker_prefix
    return {
        "runtime_profile": "qqnt-direct",
        "binding_id": binding_id,
        "host_executable": _required_env("QQNT_HOST_EXECUTABLE"),
        "host_args": _host_args(),
        "data_dir": _required_env("QQNT_DATA_DIR"),
        "required_client_version": _required_env("QQNT_REQUIRED_CLIENT_VERSION"),
        "required_host_abi": _required_env("QQNT_REQUIRED_HOST_ABI"),
        "account_id": account_id,
        "login_policy": "existing_session",
        "platform": "linux-x86_64",
        "timeout_seconds": 15.0,
        "startup_timeout_seconds": 60.0,
        "shutdown_timeout_seconds": 5.0,
        "max_restart_attempts": 0,
    }


def _run_smoke() -> dict[str, Any]:
    """Execute the protected scenario and return redacted acceptance evidence."""

    if sys.platform != "linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise SmokeConfigurationError("protected QQ smoke requires Linux x86_64")
    if _required_env("QQNT_REAL_SMOKE_APPROVED").lower() != "yes":
        raise SmokeConfigurationError(
            "QQNT_REAL_SMOKE_APPROVED must be set to yes by the protected environment"
        )
    account_id = _required_text(_required_env("QQNT_ACCOUNT_ID"), "QQNT_ACCOUNT_ID")
    scenario = _load_scenario()
    private = _conversation(
        scenario.get("private_conversation"),
        field="private_conversation",
        kind="private",
        account_id=account_id,
    )
    group = _conversation(
        scenario.get("group_conversation"),
        field="group_conversation",
        kind="group",
        account_id=account_id,
    )
    expected_inbound = scenario.get("expected_inbound")
    if not isinstance(expected_inbound, list) or len(expected_inbound) != 2:
        raise SmokeConfigurationError(
            "expected_inbound must contain one private and one group assertion"
        )
    for item in expected_inbound:
        if not isinstance(item, dict):
            raise SmokeConfigurationError("expected_inbound entries must be objects")
    marker_prefix = _required_text(scenario.get("marker_prefix"), "marker_prefix")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    marker = f"{marker_prefix}-{run_id}-{int(time.time())}"
    config = _build_config(scenario, account_id)
    emitter = RecordingEmitter()
    connector = QQNTDirectConnector(QQNTDirectConfig.from_mapping(config))
    evidence: dict[str, Any] = {
        "schema": "cyrene.qq.real-smoke-evidence.v1",
        "platform": "linux-x86_64",
        "client_version": config["required_client_version"],
        "host_abi": config["required_host_abi"],
        "binding_id": config["binding_id"],
        "operations": {},
        "send": {},
        "inbound_events": 0,
        "restart": {},
    }
    try:
        subscription_error = connector.on_subscribe(
            "qq-real-smoke-events", "message.connector.v1", b"{}", emitter
        )
        if subscription_error is not None:
            raise SmokeConfigurationError("message subscription failed")
        _assert_ready(connector, account_id)
        evidence["compatibility"] = dict(connector.compatibility)

        for operation in ("qq.account.core", "qq.account.simple"):
            result = _operation_result(connector, operation, {"account_id": account_id})
            spec = QQ_OPERATION_BY_NAME[operation]
            evidence["operations"][operation] = {
                "status": result["status"],
                "priority": spec.priority,
                "service": spec.service,
                "method": spec.method,
            }

        private_message_id = _send_message(connector, private, f"{marker}-private")
        group_message_id = _send_message(connector, group, f"{marker}-group")
        evidence["send"] = {
            "private": "accepted",
            "group": "accepted",
            "private_message_id_present": bool(private_message_id),
            "group_message_id_present": bool(group_message_id),
        }

        replacements = {
            "$ACCOUNT_ID": account_id,
            "$PRIVATE_CONVERSATION_ID": private["conversation_id"],
            "$PRIVATE_PEER_UID": private["peer_uid"],
            "$PRIVATE_MESSAGE_ID": private_message_id,
            "$GROUP_CONVERSATION_ID": group["conversation_id"],
            "$GROUP_PEER_UID": group["peer_uid"],
            "$GROUP_MESSAGE_ID": group_message_id,
            "$MARKER": marker,
        }
        operations = scenario["operations"]
        for operation, params in operations.items():
            resolved_params = _substitute(params, replacements)
            result = _operation_result(connector, operation, resolved_params)
            spec = QQ_OPERATION_BY_NAME[operation]
            evidence["operations"][operation] = {
                "status": result["status"],
                "priority": spec.priority,
                "service": spec.service,
                "method": spec.method,
            }
        _wait_for_inbound(
            emitter,
            expected_inbound,
            float(scenario.get("event_timeout_seconds", 30.0)),
        )
        evidence["inbound_events"] = len(
            [event for event in emitter.events if event[0] == "inbound_message"]
        )
        offline = _operation_result(
            connector, "qq.login.offline", {"account_id": account_id}
        )
        evidence["offline"] = {"status": offline["status"]}
        if connector.state != "LOGIN_REQUIRED":
            raise SmokeConfigurationError(
                "QQ Host offline operation did not enter LOGIN_REQUIRED"
            )
    finally:
        connector.close()
    if connector.state != "STOPPED":
        raise SmokeConfigurationError("QQ Host did not stop cleanly")

    restarted = QQNTDirectConnector(QQNTDirectConfig.from_mapping(config))
    try:
        restart_emitter = RecordingEmitter()
        subscription_error = restarted.on_subscribe(
            "qq-real-smoke-restart", "message.connector.v1", b"{}", restart_emitter
        )
        if subscription_error is not None:
            raise SmokeConfigurationError("QQ Host restart subscription failed")
        restart_status = _assert_ready(restarted, account_id)
        evidence["restart"] = {
            "status": "accepted",
            "binding_id_preserved": restarted.configured_binding_id
            == config["binding_id"],
            "account_id_preserved": restart_status.get("account_id") == account_id,
        }
    finally:
        restarted.close()
    if restarted.state != "STOPPED":
        raise SmokeConfigurationError("restarted QQ Host did not stop cleanly")
    if (
        not evidence["restart"]["binding_id_preserved"]
        or not evidence["restart"]["account_id_preserved"]
    ):
        raise SmokeConfigurationError(
            "QQ binding identity was not preserved after restart"
        )
    return evidence


def main() -> int:
    """Run the protected smoke and write only redacted evidence."""

    evidence_path_value = os.environ.get("QQNT_SMOKE_EVIDENCE_PATH", "").strip()
    evidence_path = Path(evidence_path_value) if evidence_path_value else None
    try:
        evidence = _run_smoke()
    except (
        ConnectorError,
        SmokeConfigurationError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        if evidence_path is not None:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(
                json.dumps(
                    {
                        "schema": "cyrene.qq.real-smoke-evidence.v1",
                        "status": "NOT_RUN"
                        if isinstance(exc, SmokeConfigurationError)
                        else "FAIL",
                        "error_type": type(exc).__name__,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        print(
            f"QQ_REAL_SMOKE: {'NOT_RUN' if isinstance(exc, SmokeConfigurationError) else 'FAIL'}"
        )
        print(f"reason_type={type(exc).__name__}")
        return 2
    evidence["status"] = "PASS"
    if evidence_path is not None:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print("QQ_REAL_SMOKE: PASS")
    print(f"operations_verified={len(evidence['operations'])}")
    print(f"inbound_events={evidence['inbound_events']}")
    print("credentials=not_logged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
