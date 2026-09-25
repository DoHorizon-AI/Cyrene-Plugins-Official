"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 qqnt_direct.py                                                  │
│  Module: qq_connector.qqnt_direct                                    │
│  Role: Direct QQNT adapter with canonical OneBot-compatible mapping.  │
│                                                                     │
│  模块职责：将 QQ Host 原生结果直接映射到 message.connector.v1，         │
│  并以固定 qq.client.v1 操作暴露 QQ 独有能力。                         │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cyrene_plugin_runtime.configuration import read_environment_settings
from google.protobuf.message import DecodeError

from ._generated import message_connector_pb2 as message_contract
from .qqnt_direct_host import (
    QQHostClient,
    QQHostError,
    QQHostLaunchConfig,
)
from .qqnt_direct_operations import (
    QQOperationValidationError,
    get_qq_operation,
    validate_qq_parameters,
    validate_qq_result,
)
from .support import (
    DELIVERY_RESULT_TYPE_URL,
    INBOUND_MESSAGE_EVENT_TYPE,
    INBOUND_MESSAGE_TYPE_URL,
    INBOUND_REQUEST_EVENT_TYPE,
    RESPOND_RESULT_TYPE_URL,
    ApplicationEventEmitter,
    CancellationToken,
    ConnectorError,
    _raise_if_cancelled,
)

QQ_CAPABILITY_ID = "qq.client.v1"
QQ_VENDOR = "qq"
QQ_RUNTIME_PROFILE = "qqnt-direct"
QQ_REQUEST_TYPE_URL = "type.cyrene.io/qq.client.v1.Request"
QQ_RESPONSE_TYPE_URL = "type.cyrene.io/qq.client.v1.Response"
QQ_CALLBACK_TYPE_URL = "type.cyrene.io/qq.client.v1.Callback"
_BINDING_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_EVENT_IDS = 2_048
_MAX_CALLBACK_REQUESTS = 2_048
_MAX_EXTENSION_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_MEDIA_REFERENCE_BYTES = 4 * 1024
_CALLBACK_OPERATION_BY_EVENT = {
    "message.send_completion": ("qq.message.send_completion", {"qq.message.send"}),
    "qq.message.send_completion": ("qq.message.send_completion", {"qq.message.send"}),
    "media.download_complete": (
        "qq.media.download_complete",
        {"qq.media.download", "qq.file.download"},
    ),
    "qq.media.download_complete": (
        "qq.media.download_complete",
        {"qq.media.download", "qq.file.download"},
    ),
}
_SUPPORTED_EVENT_NAMES = frozenset(
    {
        "message.received",
        "request.received",
        *tuple(_CALLBACK_OPERATION_BY_EVENT),
    }
)
_QQ_PEER_FACT_TO_NATIVE_FIELD = {
    "qq_peer_uid": "peer_uid",
    "qq_peer_uin": "peer_uin",
    "qq_group_code": "group_code",
    "qq_user_uid": "user_uid",
    "qq_user_uin": "user_uin",
}


@dataclass(frozen=True, slots=True)
class QQNTDirectConfig:
    """Binding-scoped direct QQ runtime configuration.

    The executable and data directory are explicit operator inputs. No QQ
    installation is guessed, and no password or session file is accepted as a
    configuration value.

        中文：此配置限定到单个 binding 的 direct QQ runtime。可执行文件与数据目录必须由操作人员显式提供；不会猜测 QQ 安装位置，也不接受密码或会话文件作为配置值。
    """

    binding_id: str
    host_executable: str
    host_args: tuple[str, ...]
    data_dir: Path
    required_client_version: str
    required_host_abi: str
    account_id: str | None = None
    login_policy: str = "existing_session"
    platform: str = "linux-x86_64"
    timeout_seconds: float = 10.0
    startup_timeout_seconds: float = 30.0
    shutdown_timeout_seconds: float = 2.0
    secret_refs: tuple[str, ...] = ()
    max_restart_attempts: int = 2
    restart_window_seconds: float = 60.0
    restart_backoff_seconds: float = 0.25
    restart_backoff_max_seconds: float = 5.0
    crash_circuit_cooldown_seconds: float = 60.0
    installation_manifest: Path | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> QQNTDirectConfig:
        """Validate one direct binding and reject all OneBot endpoint fields.

            中文：校验一个 direct binding，并拒绝所有 OneBot Endpoint 字段。
        """

        if not isinstance(value, Mapping):
            raise ConnectorError(
                "INVALID_REQUEST", "qqnt-direct config must be an object"
            )
        allowed = {
            "runtime_profile",
            "binding_id",
            "host_executable",
            "host_args",
            "data_dir",
            "required_client_version",
            "required_host_abi",
            "account_id",
            "self_account_id",
            "login_policy",
            "platform",
            "timeout_seconds",
            "startup_timeout_seconds",
            "shutdown_timeout_seconds",
            "secret_refs",
            "max_restart_attempts",
            "restart_window_seconds",
            "restart_backoff_seconds",
            "restart_backoff_max_seconds",
            "crash_circuit_cooldown_seconds",
            "installation_manifest",
        }
        unknown = set(value).difference(allowed)
        if unknown:
            raise ConnectorError(
                "INVALID_REQUEST",
                f"qqnt-direct config contains unknown fields: {sorted(unknown)}",
            )
        forbidden = {
            "http_base_url",
            "websocket_url",
            "access_token",
            "transport_profile",
            "reverse_listen_host",
            "reverse_listen_port",
        }
        present_forbidden = forbidden.intersection(value)
        if present_forbidden:
            raise ConnectorError(
                "INVALID_REQUEST",
                "qqnt-direct does not accept OneBot endpoint or token fields",
            )
        if value.get("runtime_profile", QQ_RUNTIME_PROFILE) != QQ_RUNTIME_PROFILE:
            raise ConnectorError(
                "INVALID_REQUEST", "runtime_profile must be qqnt-direct"
            )

        binding_id = _required_text(value.get("binding_id"), "binding_id")
        if _BINDING_PATTERN.fullmatch(binding_id) is None:
            raise ConnectorError("INVALID_REQUEST", "binding_id has an invalid format")
        executable = _required_text(value.get("host_executable"), "host_executable")
        executable_path = Path(executable)
        if not executable_path.is_absolute():
            raise ConnectorError(
                "INVALID_REQUEST", "host_executable must be an absolute path"
            )
        host_args = value.get("host_args", ())
        if not isinstance(host_args, Sequence) or isinstance(host_args, (str, bytes)):
            raise ConnectorError("INVALID_REQUEST", "host_args must be an ordered list")
        if any(not isinstance(item, str) for item in host_args):
            raise ConnectorError("INVALID_REQUEST", "host_args must contain text")
        data_dir = Path(_required_text(value.get("data_dir"), "data_dir"))
        if not data_dir.is_absolute():
            raise ConnectorError("INVALID_REQUEST", "data_dir must be an absolute path")
        required_version = _required_text(
            value.get("required_client_version"), "required_client_version"
        )
        required_host_abi = _required_text(
            value.get("required_host_abi"), "required_host_abi"
        )
        platform = _required_text(value.get("platform", "linux-x86_64"), "platform")
        if platform != "linux-x86_64":
            raise ConnectorError(
                "UNSUPPORTED_VERSION", "the first qqnt-direct target is linux-x86_64"
            )
        configured_account = value.get("account_id")
        legacy_account = value.get("self_account_id")
        if (
            configured_account is not None
            and legacy_account is not None
            and str(configured_account) != str(legacy_account)
        ):
            raise ConnectorError(
                "INVALID_REQUEST", "account_id and self_account_id must match"
            )
        account_id = (
            configured_account if configured_account is not None else legacy_account
        )
        if account_id is not None:
            account_id = _required_identifier(account_id, "account_id")
        login_policy = value.get("login_policy", "existing_session")
        if login_policy not in {"existing_session", "qr"}:
            raise ConnectorError(
                "INVALID_REQUEST", "login_policy must be existing_session or qr"
            )
        timeout_seconds = _positive_number(
            value.get("timeout_seconds", 10.0), "timeout_seconds"
        )
        startup_timeout = _positive_number(
            value.get("startup_timeout_seconds", 30.0), "startup_timeout_seconds"
        )
        shutdown_timeout = _positive_number(
            value.get("shutdown_timeout_seconds", 2.0),
            "shutdown_timeout_seconds",
        )
        secret_refs = value.get("secret_refs", ())
        if not isinstance(secret_refs, Sequence) or isinstance(
            secret_refs, (str, bytes)
        ):
            raise ConnectorError(
                "INVALID_REQUEST", "secret_refs must be an ordered list"
            )
        if any(not isinstance(item, str) or not item.strip() for item in secret_refs):
            raise ConnectorError("INVALID_REQUEST", "secret_refs must contain names")
        max_restart_attempts = _bounded_integer(
            value.get("max_restart_attempts", 2),
            "max_restart_attempts",
            minimum=0,
            maximum=5,
        )
        restart_window_seconds = _bounded_number(
            value.get("restart_window_seconds", 60.0),
            "restart_window_seconds",
            minimum=0.1,
            maximum=3_600.0,
        )
        restart_backoff_seconds = _bounded_number(
            value.get("restart_backoff_seconds", 0.25),
            "restart_backoff_seconds",
            minimum=0.0,
            maximum=60.0,
        )
        restart_backoff_max_seconds = _bounded_number(
            value.get("restart_backoff_max_seconds", 5.0),
            "restart_backoff_max_seconds",
            minimum=0.0,
            maximum=300.0,
        )
        if restart_backoff_max_seconds < restart_backoff_seconds:
            raise ConnectorError(
                "INVALID_REQUEST",
                "restart_backoff_max_seconds must not be below restart_backoff_seconds",
            )
        crash_circuit_cooldown_seconds = _bounded_number(
            value.get("crash_circuit_cooldown_seconds", 60.0),
            "crash_circuit_cooldown_seconds",
            minimum=0.1,
            maximum=3_600.0,
        )
        installation_manifest_value = value.get("installation_manifest")
        installation_manifest: Path | None = None
        if installation_manifest_value is not None:
            installation_manifest = Path(
                _required_text(installation_manifest_value, "installation_manifest")
            )
            if not installation_manifest.is_absolute():
                raise ConnectorError(
                    "INVALID_REQUEST", "installation_manifest must be absolute"
                )
        return cls(
            binding_id=binding_id,
            host_executable=executable,
            host_args=tuple(host_args),
            data_dir=data_dir,
            required_client_version=required_version,
            required_host_abi=required_host_abi,
            account_id=account_id,
            login_policy=login_policy,
            platform=platform,
            timeout_seconds=timeout_seconds,
            startup_timeout_seconds=startup_timeout,
            shutdown_timeout_seconds=shutdown_timeout,
            secret_refs=tuple(secret_refs),
            max_restart_attempts=max_restart_attempts,
            restart_window_seconds=restart_window_seconds,
            restart_backoff_seconds=restart_backoff_seconds,
            restart_backoff_max_seconds=restart_backoff_max_seconds,
            crash_circuit_cooldown_seconds=crash_circuit_cooldown_seconds,
            installation_manifest=installation_manifest,
        )

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> QQNTDirectConfig:
        """Build direct configuration from the generic worker environment.

            中文：根据通用 Worker 环境构造 direct 配置。
        """

        config: dict[str, Any] = {}
        encoded = settings.get("config")
        if encoded:
            try:
                decoded = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ConnectorError(
                    "INVALID_REQUEST", "config must contain valid JSON"
                ) from exc
            if not isinstance(decoded, Mapping):
                raise ConnectorError("INVALID_REQUEST", "config must be an object")
            config.update(decoded)
        if "binding_id" in settings:
            config["binding_id"] = settings["binding_id"]
        return cls.from_mapping(config)

    def host_launch(self) -> QQHostLaunchConfig:
        """Return the immutable process launch configuration for this binding.

            中文：返回此 binding 不可变的进程启动配置。
        """

        return QQHostLaunchConfig(
            binding_id=self.binding_id,
            command=(self.host_executable, *self.host_args),
            data_dir=self.data_dir,
            required_client_version=self.required_client_version,
            required_host_abi=self.required_host_abi,
            platform=self.platform,
            timeout_seconds=self.timeout_seconds,
            startup_timeout_seconds=self.startup_timeout_seconds,
            shutdown_timeout_seconds=self.shutdown_timeout_seconds,
            max_restart_attempts=self.max_restart_attempts,
            restart_window_seconds=self.restart_window_seconds,
            restart_backoff_seconds=self.restart_backoff_seconds,
            restart_backoff_max_seconds=self.restart_backoff_max_seconds,
            crash_circuit_cooldown_seconds=self.crash_circuit_cooldown_seconds,
            installation_manifest=self.installation_manifest,
        )


@dataclass(slots=True)
class _Subscription:
    """One direct event subscription owned by the worker activation.

        中文：由 Worker activation 所拥有的一个 direct 事件订阅。
    """

    emitter: ApplicationEventEmitter
    filter: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _TypedPayload:
    """Typed result wrapper consumed by the generic direct runtime.

        中文：供通用 direct runtime 使用的类型化结果包装器。
    """

    value: bytes
    type_url: str


class QQNTDirectConnector:
    """Direct QQ adapter for one binding and one QQ Host generation.

        中文：针对单个 binding 和单个 QQ Host 代次的 direct QQ 适配器。
    """

    plugin_id = "cyrene.connectors.im"
    version = "0.1.0"
    capabilities = ("message.connector.v1", QQ_CAPABILITY_ID)

    def __init__(
        self,
        config: Mapping[str, Any] | QQNTDirectConfig | None = None,
        *,
        host: QQHostClient | Any | None = None,
    ) -> None:
        self._config: QQNTDirectConfig | None = None
        self._host = host
        self._session_started = False
        self._session_bootstrap_stage = 0
        self._session_generation: int | None = None
        self._subscriptions: dict[str, _Subscription] = {}
        self._subscription_generation: int | None = None
        self._callback_requests: dict[str, str] = {}
        self._callback_lock = threading.Lock()
        self._seen_events: set[tuple[str, int, str]] = set()
        self._state = "CREATED"
        if config is not None:
            self.configure(config, host=host)
        else:
            settings = read_environment_settings()
            if settings is not None:
                self.configure(QQNTDirectConfig.from_settings(settings), host=host)

    @property
    def configured_binding_id(self) -> str | None:
        """Return stable binding identity, never the worker generation.

            中文：返回稳定的 binding 身份，不包含 Worker 代次。
        """

        return self._config.binding_id if self._config is not None else None

    @property
    def runtime_profile(self) -> str:
        """Return the immutable direct runtime profile.

            中文：返回不可变的 direct runtime profile。
        """

        return QQ_RUNTIME_PROFILE

    @property
    def generation(self) -> int:
        """Return the current Host generation, or zero before startup.

            中文：返回当前 Host 代次；启动前返回 0。
        """

        return self._host.generation if self._host is not None else 0

    @property
    def state(self) -> str:
        """Return the connector lifecycle state.

            中文：返回 connector 生命周期状态。
        """

        if self._host is not None and self._host.state == "FAILED":
            return "FAILED"
        return self._state

    @property
    def compatibility(self) -> Mapping[str, Any]:
        """Return the negotiated QQ Host compatibility report.

            中文：返回已协商的 QQ Host 兼容性报告。
        """

        return self._host.compatibility if self._host is not None else {}

    def configure(
        self,
        config: Mapping[str, Any] | QQNTDirectConfig,
        *,
        host: QQHostClient | Any | None = None,
    ) -> None:
        """Configure one direct worker activation without selecting another binding.

            中文：配置一次 direct Worker activation，不会另行选择 binding。
        """

        parsed = (
            config
            if isinstance(config, QQNTDirectConfig)
            else QQNTDirectConfig.from_mapping(config)
        )
        if self._config is not None and self._config.binding_id != parsed.binding_id:
            raise ConnectorError(
                "INVALID_REQUEST", "a worker activation cannot switch binding_id"
            )
        self._config = parsed
        if host is not None:
            self._host = host
        if self._host is None:
            self._host = QQHostClient(
                parsed.host_launch(), event_handler=self._on_host_event
            )

    def on_configure(self, settings: Mapping[str, str]) -> str | None:
        """Apply one standard worker configuration and return a typed failure.

            中文：应用一份标准 Worker 配置，并返回类型化失败结果。
        """

        try:
            self.configure(QQNTDirectConfig.from_settings(settings))
        except ConnectorError as exc:
            return f"{exc.code}: {exc.message}"
        return None

    def send_message(
        self,
        request: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """Map canonical ordered content directly to the QQ native send operation.

            中文：将规范的有序内容直接映射到 QQ 原生发送操作。
        """

        config = self._require_configured()
        if not isinstance(request, Mapping):
            raise ConnectorError(
                "INVALID_REQUEST", "send_message request must be an object"
            )
        _raise_if_cancelled(cancellation)
        conversation = _conversation_for_send(request, config)
        elements = _build_native_elements(request)
        self._ensure_ready()
        params: dict[str, Any] = {
            "peer": conversation,
            "elements": elements,
        }
        reply = request.get("reply")
        if reply is not None:
            reply_map = _mapping(reply, "reply")
            params["reply"] = {
                "message_id": _required_identifier(
                    reply_map.get("message_id"), "reply.message_id"
                )
            }
        result = self._call_operation(
            "qq.message.send", params, cancellation=cancellation
        )
        if not isinstance(result, Mapping):
            raise ConnectorError(
                "PROTOCOL_MISMATCH", "QQ send result must be an object"
            )
        vendor_message_id = _optional_identifier(result.get("message_id"))
        if vendor_message_id is None:
            raise ConnectorError(
                "PROTOCOL_MISMATCH", "QQ send result omitted message_id"
            )
        return {
            "status": "accepted",
            "vendor_message_id": vendor_message_id,
            "vendor_extension": {
                "vendor": QQ_VENDOR,
                "facts": _facts_from_result(result),
            },
        }

    def respond_request(
        self,
        request: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """Map canonical approval requests to fixed QQ friend or group operations.

            中文：将规范审批请求映射为固定的 QQ 好友或群组操作。
        """

        config = self._require_configured()
        value = _mapping(request, "respond_request")
        unknown = set(value).difference(
            {"request_id", "request_kind", "decision", "comment", "vendor_request"}
        )
        if unknown:
            raise ConnectorError(
                "INVALID_REQUEST", "respond_request contains unknown fields"
            )
        request_id = _required_identifier(value.get("request_id"), "request_id")
        request_kind = value.get("request_kind")
        if request_kind not in {"friend", "group_invite"}:
            raise ConnectorError("INVALID_REQUEST", "unsupported request_kind")
        decision = value.get("decision")
        if decision not in {"approve", "reject"}:
            raise ConnectorError(
                "INVALID_REQUEST", "decision must be approve or reject"
            )
        comment = value.get("comment", "")
        if not isinstance(comment, str) or len(comment.encode("utf-8")) > 2_048:
            raise ConnectorError(
                "INVALID_REQUEST", "comment must be at most 2048 UTF-8 bytes"
            )
        vendor_request = _mapping(value.get("vendor_request", {}), "vendor_request")
        operation = (
            "qq.friend.approve" if request_kind == "friend" else "qq.group.approve"
        )
        params = {
            "request_id": request_id,
            "approve": decision == "approve",
            "comment": comment,
            "vendor_request": dict(vendor_request),
        }
        _raise_if_cancelled(cancellation)
        self._ensure_ready()
        result = self._call_operation(operation, params, cancellation=cancellation)
        _raise_if_cancelled(cancellation)
        return {
            "status": "accepted",
            "request_id": request_id,
            "request_kind": request_kind,
            "decision": decision,
            "result": result if isinstance(result, Mapping) else {},
            "account_id": config.account_id or "",
        }

    def invoke_extension(
        self,
        operation: str,
        params: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """Invoke one allow-listed QQ operation with no service/method passthrough.

            中文：调用一个允许列表中的 QQ 操作，不允许透传 service 或 method。
        """

        config = self._require_configured()
        spec = get_qq_operation(operation)
        if spec is None:
            raise ConnectorError(
                "UNKNOWN_OPERATION", f"unsupported QQ operation {operation}"
            )
        if not spec.requestable:
            raise ConnectorError(
                "UNSUPPORTED_OPERATION",
                f"QQ operation {operation} is callback-only",
            )
        if not isinstance(params, Mapping):
            raise ConnectorError(
                "INVALID_REQUEST", "QQ operation params must be an object"
            )
        try:
            checked_params = validate_qq_parameters(operation, params)
        except QQOperationValidationError as exc:
            raise ConnectorError("INVALID_REQUEST", str(exc)) from exc
        if operation == "qq.login.password" and (
            not isinstance(checked_params.get("secret_ref"), str)
            or checked_params.get("secret_ref") not in config.secret_refs
        ):
            raise ConnectorError(
                "INVALID_REQUEST",
                "password login requires a configured secret_ref and never raw "
                "password",
            )
        _raise_if_cancelled(cancellation)
        if spec.mapping == "session":
            # Explicit lifecycle actions are authoritative. Do not invoke the
            # complete bootstrap sequence before the action requested by the
            # caller, otherwise create/init/startNT would be duplicated.
            # 中文：生命周期操作显式指定时，以该操作为准。不要在执行调用方请求的操作前运行完整 bootstrap 流程，否则会重复执行 create/init/startNT。
            self._ensure_host_started()
        else:
            self._ensure_started()
        if spec.priority != "P0" or spec.mapping not in {"session", "login"}:
            self._ensure_ready()
        result = self._call_operation(
            operation, checked_params, cancellation=cancellation
        )
        _raise_if_cancelled(cancellation)
        if spec.mapping == "session":
            stage_by_operation = {
                "qq.session.create": 1,
                "qq.session.init": 2,
                "qq.session.start_nt": 3,
            }
            self._session_bootstrap_stage = max(
                self._session_bootstrap_stage, stage_by_operation[operation]
            )
            self._session_started = self._session_bootstrap_stage >= 3
            if operation == "qq.session.start_nt":
                self._update_session_state(result)
        if operation == "qq.login.offline":
            self._state = "LOGIN_REQUIRED"
        elif operation in {
            "qq.session.start_nt",
            "qq.login.connect",
            "qq.login.online",
            "qq.login.password",
            "qq.login.poll",
            "qq.login.quick",
            "qq.login.self_status",
        }:
            self._update_session_state(result)
        return {
            "operation": operation,
            "status": "accepted",
            "result": result,
            "priority": spec.priority,
            "mapping": {
                "service": spec.service,
                "method": spec.method,
            },
        }

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: CancellationToken | None = None,
        request_type_url: str | None = None,
        request_id: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, Any]:
        """Adapt canonical protobuf and explicit QQ JSON calls to direct operations.

            中文：将规范 protobuf 调用和显式 QQ JSON 调用适配为 direct 操作。
        """

        del request_id, stream_results
        try:
            if capability == "message.connector.v1" and action == "send_message":
                if request_type_url not in {
                    None,
                    "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest",
                }:
                    raise ConnectorError(
                        "INVALID_REQUEST", "send_message type URL is invalid"
                    )
                request = message_contract.SendMessageRequest.FromString(payload)
                result = self.send_message(
                    _canonical_send_request(request), cancellation=cancellation
                )
                return True, _TypedPayload(
                    _delivery_payload(result), DELIVERY_RESULT_TYPE_URL
                )
            if capability == "message.connector.v1" and action == "respond_request":
                if request_type_url not in {
                    None,
                    "type.cyrene.io/message.connector.v1.respond_request.request",
                }:
                    raise ConnectorError(
                        "INVALID_REQUEST", "respond_request type URL is invalid"
                    )
                request = _decode_json_object(payload, "respond_request")
                result = self.respond_request(request, cancellation=cancellation)
                return True, _TypedPayload(
                    json.dumps(result, separators=(",", ":")).encode("utf-8"),
                    RESPOND_RESULT_TYPE_URL,
                )
            if capability == QQ_CAPABILITY_ID:
                if request_type_url not in {None, QQ_REQUEST_TYPE_URL}:
                    raise ConnectorError(
                        "INVALID_REQUEST", "qq.client.v1 type URL is invalid"
                    )
                request = _decode_json_object(payload, "qq.client.v1 request")
                if set(request) != {"params"}:
                    raise ConnectorError(
                        "INVALID_REQUEST",
                        "qq.client.v1 request must contain only params",
                    )
                params = _mapping(request["params"], "params")
                result = self.invoke_extension(
                    action, params, cancellation=cancellation
                )
                encoded = json.dumps(
                    result, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                if len(encoded) > _MAX_EXTENSION_RESPONSE_BYTES:
                    raise ConnectorError(
                        "EXECUTION_FAILED", "QQ extension result is too large"
                    )
                return True, _TypedPayload(encoded, QQ_RESPONSE_TYPE_URL)
            raise ConnectorError(
                "INVALID_REQUEST",
                f"unsupported capability or action: {capability}/{action}",
            )
        except ConnectorError as exc:
            return False, f"{exc.code}: {exc.message}"
        except (DecodeError, ValueError, TypeError) as exc:
            return False, f"INVALID_REQUEST: malformed {action} payload: {exc}"

    def on_subscribe(
        self,
        subscription_id: str,
        capability: str,
        filter_payload: bytes,
        emitter: ApplicationEventEmitter,
    ) -> str | None:
        """Attach one binding-local event stream and enable the native listener.

            中文：附加一个 binding 本地事件流，并启用原生 listener。
        """

        if capability not in {"message.connector.v1", QQ_CAPABILITY_ID}:
            return f"INVALID_REQUEST: unsupported subscription capability {capability}"
        previous = self._subscriptions.get(subscription_id)
        previous_generation = self._subscription_generation
        try:
            filter_value = _decode_filter(filter_payload)
            self._ensure_started()
            self._subscriptions[subscription_id] = _Subscription(emitter, filter_value)
            if self._subscription_generation != self.generation:
                self._call_operation(
                    "qq.message.subscribe",
                    {"events": ["message.received", "request.received"]},
                )
                self._subscription_generation = self.generation
        except ConnectorError as exc:
            if previous is None:
                self._subscriptions.pop(subscription_id, None)
            else:
                self._subscriptions[subscription_id] = previous
            self._subscription_generation = previous_generation
            return f"{exc.code}: {exc.message}"
        return None

    def on_unsubscribe(self, subscription_id: str, reason: str) -> None:
        """Remove one subscription without touching another binding or generation.

            中文：移除一个订阅，不影响其他 binding 或代次。
        """

        del reason
        self._subscriptions.pop(subscription_id, None)

    def on_cancel(self, request_id: str, reason: str) -> None:
        """Acknowledge the runtime cancellation callback without duplicating it.

        The ``DirectPluginRuntime`` already passes a cancellation token to
        ``on_invoke``. The in-flight Host request observes that token and sends
        the generation-scoped cancel frame; this callback remains intentionally
        side-effect free so it cannot race with the same cancellation path.

        DirectPluginRuntime 已通过 ``on_invoke`` 传入取消信号；这里保持无副作用，避免
        与同一取消路径竞争或重复发送 cancel frame。
        """

        del request_id, reason

    def on_shutdown(self, grace_period_ms: int) -> None:
        """Close the QQ Host process tree within the worker shutdown budget.

            中文：在 Worker 关闭预算内关闭整个 QQ Host 进程树。
        """

        del grace_period_ms
        self.close()

    def close(self) -> None:
        """Drain subscriptions and reap the binding-local QQ Host child.

            中文：排空订阅，并回收 binding 本地的 QQ Host 子进程。
        """

        self._subscriptions.clear()
        self._subscription_generation = None
        if self._host is not None:
            self._host.close()
        self._state = "STOPPED"

    def publish_inbound_event(self, event: Mapping[str, Any]) -> int:
        """Normalize one direct native event and emit it to matching subscribers.

            中文：规范化一个 direct 原生事件，并将其发给匹配的订阅者。
        """

        return self._on_host_event(event)

    def _ensure_started(self) -> None:
        config = self._require_configured()
        self._ensure_host_started()
        if not self._session_started:
            stages = (
                "qq.session.create",
                "qq.session.init",
                "qq.session.start_nt",
            )
            for index, operation in enumerate(stages, start=1):
                if self._session_bootstrap_stage >= index:
                    continue
                try:
                    result = self._host.request(
                        operation,
                        {"login_policy": config.login_policy},
                        timeout_seconds=config.startup_timeout_seconds,
                    )
                except QQHostError as exc:
                    self._state = "FAILED"
                    raise _connector_host_error(exc) from exc
                self._session_bootstrap_stage = index
                if operation == "qq.session.start_nt":
                    self._update_session_state(result)
            self._session_started = self._session_bootstrap_stage >= len(stages)
        self._finish_startup()

    def _ensure_host_started(self) -> None:
        """Start or recover the Host without implicitly bootstrapping QQ.

            中文：启动或恢复 Host，但不隐式 bootstrap QQ。
        """

        if self._host is None:
            raise ConnectorError("CAPABILITY_UNAVAILABLE", "QQ Host is not configured")
        recovered = False
        if self._host.state == "FAILED":
            recover = getattr(self._host, "recover", None)
            if not callable(recover):
                raise ConnectorError("CAPABILITY_UNAVAILABLE", "QQ Host is unavailable")
            try:
                recover()
                recovered = True
            except QQHostError as exc:
                self._state = "FAILED"
                raise _connector_host_error(exc) from exc
        elif self._host.state == "STOPPED":
            raise ConnectorError("CAPABILITY_UNAVAILABLE", "QQ Host is unavailable")
        if not recovered:
            try:
                self._host.start()
            except QQHostError as exc:
                self._state = "FAILED"
                raise _connector_host_error(exc) from exc
        if self._session_generation != self._host.generation:
            self._session_started = False
            self._session_bootstrap_stage = 0
            self._session_generation = self._host.generation
            self._subscription_generation = None
            with self._callback_lock:
                self._callback_requests.clear()
            self._state = "NATIVE_READY"

    def _finish_startup(self) -> None:
        """Restore subscriptions after a generation has completed startup.

            中文：在某个代次完成启动后恢复订阅。
        """

        if (
            self._state == "READY"
            and self._subscriptions
            and self._subscription_generation != self._host.generation
        ):
            self._call_operation(
                "qq.message.subscribe",
                {"events": ["message.received", "request.received"]},
            )
            self._subscription_generation = self._host.generation
        if self._state == "CREATED":
            self._state = "NATIVE_READY"

    def _ensure_ready(self) -> None:
        self._ensure_started()
        if self._state != "READY":
            raise ConnectorError(
                "LOGIN_REQUIRED"
                if self._state == "LOGIN_REQUIRED"
                else "CAPABILITY_UNAVAILABLE",
                f"QQ direct session is {self._state.lower()}",
            )

    def _call_operation(
        self,
        operation: str,
        params: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> Any:
        if get_qq_operation(operation) is None:
            raise ConnectorError(
                "UNKNOWN_OPERATION", f"unsupported QQ operation {operation}"
            )
        if self._host is None:
            raise ConnectorError("CAPABILITY_UNAVAILABLE", "QQ Host is not configured")
        request_id_sink: Callable[[str], None] | None = None
        callback_request_id: str | None = None
        if operation in {
            "qq.message.send",
            "qq.media.download",
            "qq.file.download",
        }:

            def remember_request(request_id: str) -> None:
                """Bind a callback-capable native call to its originating request.

                    中文：将一个支持回调的原生调用关联到其发起请求。
                """

                nonlocal callback_request_id
                callback_request_id = request_id
                self._remember_callback_request(request_id, operation)

            request_id_sink = remember_request
        try:
            result = self._host.request(
                operation,
                params,
                timeout_seconds=self._config.timeout_seconds if self._config else None,
                cancellation=cancellation,
                request_id_sink=request_id_sink,
            )
            return validate_qq_result(operation, result)
        except QQOperationValidationError as exc:
            self._forget_callback_request(callback_request_id)
            raise ConnectorError("PROTOCOL_MISMATCH", str(exc)) from exc
        except QQHostError as exc:
            self._forget_callback_request(callback_request_id)
            if exc.code in {"LOGIN_FAILED", "ACCOUNT_MISMATCH"}:
                self._state = "FAILED"
            raise _connector_host_error(exc) from exc

    def _remember_callback_request(self, request_id: str, operation: str) -> None:
        """Remember callback-capable request identities for this generation.

            中文：记录此代次中支持回调的请求标识。
        """

        if not isinstance(request_id, str) or not request_id:
            return
        with self._callback_lock:
            self._callback_requests[request_id] = operation
            overflow = len(self._callback_requests) - _MAX_CALLBACK_REQUESTS
            for old_request_id in tuple(self._callback_requests)[: max(0, overflow)]:
                del self._callback_requests[old_request_id]

    def _forget_callback_request(self, request_id: str | None) -> None:
        """Discard a callback identity when its originating request failed.

            中文：当发起请求失败时，丢弃对应的回调标识。
        """

        if not request_id:
            return
        with self._callback_lock:
            self._callback_requests.pop(request_id, None)

    def _update_session_state(self, result: Any) -> None:
        if not isinstance(result, Mapping):
            return
        account_id = result.get("account_id")
        expected = self._config.account_id if self._config else None
        if (
            expected is not None
            and account_id is not None
            and str(account_id) != expected
        ):
            self._state = "FAILED"
            raise ConnectorError(
                "ACCOUNT_MISMATCH", "QQ Host account does not match binding"
            )
        state = result.get("state")
        ready = state in {"ready", "online", "logged_in"} or result.get("ready") is True
        if (
            ready
            and expected is not None
            and (account_id is None or str(account_id) != expected)
        ):
            self._state = "FAILED"
            raise ConnectorError(
                "ACCOUNT_MISMATCH", "QQ Host ready account is missing or mismatched"
            )
        if ready:
            self._state = "READY"
        elif state in {"login_required", "qr_required", "offline"}:
            self._state = "LOGIN_REQUIRED"
        elif state == "failed":
            self._state = "FAILED"

    def _on_host_event(self, event: Mapping[str, Any]) -> int:
        if not isinstance(event, Mapping):
            return 0
        event_name = event.get("event")
        payload = event.get("payload", event)
        if not isinstance(event_name, str) or not isinstance(payload, Mapping):
            return 0
        if event_name not in _SUPPORTED_EVENT_NAMES:
            return 0
        if (
            event.get("binding_id") != self.configured_binding_id
            or event.get("generation") != self.generation
        ):
            return 0
        event_id = (
            event.get("event_id")
            or payload.get("message_id")
            or payload.get("request_id")
        )
        if event_id is not None:
            key = (self.configured_binding_id or "", self.generation, str(event_id))
            if key in self._seen_events:
                return 0
            self._seen_events.add(key)
            if len(self._seen_events) > _MAX_EVENT_IDS:
                self._seen_events = set(
                    tuple(item)
                    for item in list(self._seen_events)[-(_MAX_EVENT_IDS // 2) :]
                )
        try:
            if event_name == "message.received":
                normalized = _normalize_native_message(
                    payload, self._require_configured(), self.generation
                )
                return self._emit(
                    INBOUND_MESSAGE_EVENT_TYPE, normalized, INBOUND_MESSAGE_TYPE_URL
                )
            if event_name == "request.received":
                normalized_request = _normalize_native_request(
                    payload, self._require_configured()
                )
                return self._emit(
                    INBOUND_REQUEST_EVENT_TYPE,
                    normalized_request,
                    "type.cyrene.io/message.connector.v1.InboundRequestPayload",
                )
            callback_spec = _CALLBACK_OPERATION_BY_EVENT.get(event_name)
            if callback_spec is not None:
                callback_operation, originating_operations = callback_spec
                request_id = event.get("request_id")
                if not isinstance(request_id, str):
                    request_id = payload.get("request_id")
                if not isinstance(request_id, str):
                    # A callback without a generation-scoped originating
                    # request is ambiguous; never expose it as a generic event.
                    # 中文：没有按代次关联的发起请求时，无法判定回调属于哪个操作；绝不能将其作为通用事件公开。
                    return 0
                with self._callback_lock:
                    originating_operation = self._callback_requests.get(request_id)
                if originating_operation not in originating_operations:
                    # A callback without a generation-scoped originating
                    # request is ambiguous; never expose it as a generic event.
                    # 中文：没有按代次关联的发起请求时，无法判定回调属于哪个操作；绝不能将其作为通用事件公开。
                    return 0
                normalized_callback = _normalize_callback(
                    callback_operation, request_id, event.get("event_id"), payload
                )
                with self._callback_lock:
                    # Completion callbacks are terminal records. Consuming the
                    # request identity makes a second callback with a different
                    # event_id harmless as well as making same-id duplicates
                    # harmless through the event-id deduplication above.
                    # 中文：完成回调是终态记录。消费请求标识后，即使后续回调使用不同的 event_id 也会被安全忽略；上方的 event-id 去重逻辑同样会忽略相同 ID 的重复回调。
                    self._callback_requests.pop(request_id, None)
                return self._emit(
                    "qq_callback", normalized_callback, QQ_CALLBACK_TYPE_URL
                )
        except ConnectorError:
            return 0
        return 0

    def _emit(self, event_type: str, value: Any, type_url: str) -> int:
        if event_type in {INBOUND_MESSAGE_EVENT_TYPE, INBOUND_REQUEST_EVENT_TYPE}:
            if event_type == INBOUND_MESSAGE_EVENT_TYPE:
                encoded = _inbound_message_payload(value)
            else:
                encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        else:
            encoded = json.dumps(
                value, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        delivered = 0
        for subscription in tuple(self._subscriptions.values()):
            matches = _filter_matches(subscription.filter, value, event_type)
            if matches and subscription.emitter.emit(event_type, encoded, type_url):
                delivered += 1
        return delivered

    def _require_configured(self) -> QQNTDirectConfig:
        if self._config is None:
            raise ConnectorError(
                "CAPABILITY_UNAVAILABLE", "qqnt-direct is not configured"
            )
        return self._config


def _connector_host_error(error: QQHostError) -> ConnectorError:
    """Map Host process errors to the existing generic connector error vocabulary.

        中文：将 Host 进程错误映射为现有的通用 connector 错误词汇。
    """

    mapping = {
        "CANCELLED": "CANCELLED",
        "TIMEOUT": "TIMEOUT",
        "INVALID_REQUEST": "INVALID_REQUEST",
        "PROTOCOL_MISMATCH": "PROTOCOL_MISMATCH",
        "UNSUPPORTED_VERSION": "UNSUPPORTED_VERSION",
        "ACCOUNT_MISMATCH": "ACCOUNT_MISMATCH",
        "LOGIN_REQUIRED": "LOGIN_REQUIRED",
        "LOGIN_FAILED": "CAPABILITY_UNAVAILABLE",
        "UNKNOWN_OPERATION": "UNKNOWN_OPERATION",
        "UNSUPPORTED_OPERATION": "UNSUPPORTED_OPERATION",
    }
    return ConnectorError(
        mapping.get(error.code, "CAPABILITY_UNAVAILABLE"), error.message
    )


def _canonical_send_request(
    request: message_contract.SendMessageRequest,
) -> dict[str, Any]:
    """Convert canonical protobuf fields to Python data without a OneBot JSON hop.

        中文：将规范 protobuf 字段转换为 Python 数据，全程不经过 OneBot JSON。
    """

    conversation = request.conversation
    kind = {
        message_contract.CONVERSATION_KIND_PRIVATE: "private",
        message_contract.CONVERSATION_KIND_GROUP: "group",
    }.get(conversation.kind)
    if kind is None:
        raise ConnectorError("INVALID_REQUEST", "unsupported conversation kind")
    content: list[dict[str, Any]] = []
    for part in request.content:
        which = part.WhichOneof("kind")
        if which == "text":
            content.append({"text": {"text": part.text.text}})
        elif which == "mention":
            target = (
                "everyone"
                if part.mention.target == message_contract.MENTION_TARGET_EVERYONE
                else "user"
            )
            content.append(
                {
                    "mention": {
                        "target": target,
                        "target_id": part.mention.target_id,
                        "display_name": part.mention.display_name,
                    }
                }
            )
        elif which in {"image", "file"}:
            source = getattr(part, which)
            reference = _reference_from_proto(source.reference)
            key = "image" if which == "image" else "file"
            item: dict[str, Any] = {"reference": reference}
            if which == "file":
                item.update(
                    {"file_name": source.file_name, "mime_type": source.mime_type}
                )
            else:
                item["mime_type"] = source.mime_type
            content.append({key: item})
        else:
            raise ConnectorError("INVALID_REQUEST", "message content part is empty")
    result: dict[str, Any] = {
        "conversation": {
            "vendor": conversation.vendor,
            "account_id": conversation.account_id,
            "conversation_id": conversation.conversation_id,
            "kind": kind,
        },
        "content": content,
    }
    if request.HasField("reply"):
        result["reply"] = {"message_id": request.reply.message_id}
    if request.HasField("vendor_extension"):
        result["vendor_extension"] = {
            "vendor": request.vendor_extension.vendor,
            "facts": [
                {"name": fact.name, "value": fact.value}
                for fact in request.vendor_extension.facts
            ],
        }
    return result


def _reference_from_proto(
    reference: message_contract.AttachmentReference,
) -> dict[str, Any]:
    """Preserve remote and vendor media references as typed direct data.

        中文：将远程媒体引用和供应商媒体引用保留为类型化 direct 数据。
    """

    which = reference.WhichOneof("location")
    if which == "remote_uri":
        return {
            "remote_uri": _validated_remote_uri(
                reference.remote_uri, "attachment.reference.remote_uri"
            )
        }
    if which == "vendor_media":
        return {
            "vendor_media": {
                "vendor": reference.vendor_media.vendor,
                "account_id": reference.vendor_media.account_id,
                "media_id": reference.vendor_media.media_id,
            }
        }
    raise ConnectorError("INVALID_REQUEST", "attachment reference is empty")


def _delivery_payload(result: Mapping[str, Any]) -> bytes:
    """Build the canonical DeliveryResult protobuf from a native result.

        中文：根据原生结果构造规范 DeliveryResult protobuf。
    """

    response = message_contract.DeliveryResult(
        status=message_contract.DELIVERY_STATUS_ACCEPTED,
        vendor_message_id=str(result.get("vendor_message_id", "")),
    )
    extension = result.get("vendor_extension")
    if extension is not None:
        extension_map = _mapping(extension, "vendor_extension")
        response.vendor_extension.vendor = _required_text(
            extension_map.get("vendor"), "vendor_extension.vendor"
        )
        facts = extension_map.get("facts", ())
        if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
            raise ConnectorError(
                "INVALID_REQUEST", "vendor_extension.facts must be a list"
            )
        for index, raw_fact in enumerate(facts):
            fact = _mapping(raw_fact, f"vendor_extension.facts[{index}]")
            target = response.vendor_extension.facts.add()
            target.name = _required_text(
                fact.get("name"), f"vendor_extension.facts[{index}].name"
            )
            target.value = _required_text(
                fact.get("value"), f"vendor_extension.facts[{index}].value"
            )
    return response.SerializeToString()


def _conversation_for_send(
    request: Mapping[str, Any], config: QQNTDirectConfig
) -> dict[str, Any]:
    """Validate direct conversation identity and preserve QQ peer identifiers.

        中文：校验 direct 会话身份，并保留 QQ 对端标识。
    """

    conversation = _mapping(request.get("conversation"), "conversation")
    if conversation.get("vendor") != QQ_VENDOR:
        raise ConnectorError("INVALID_REQUEST", "conversation.vendor must be qq")
    account_id = _required_identifier(conversation.get("account_id"), "account_id")
    if config.account_id and account_id != config.account_id:
        raise ConnectorError(
            "INVALID_REQUEST", "conversation account does not match binding"
        )
    kind = conversation.get("kind")
    if kind in {"private", "CONVERSATION_KIND_PRIVATE", 1}:
        kind = "private"
    elif kind in {"group", "CONVERSATION_KIND_GROUP", 2}:
        kind = "group"
    else:
        raise ConnectorError("INVALID_REQUEST", "unsupported conversation kind")
    conversation_id = _required_identifier(
        conversation.get("conversation_id"), "conversation_id"
    )
    result: dict[str, Any] = {
        "kind": kind,
        "conversation_id": conversation_id,
    }
    for native_field, value in _qq_peer_identity_facts(request).items():
        result[native_field] = value
    return result


def _qq_peer_identity_facts(request: Mapping[str, Any]) -> dict[str, str]:
    """Extract the explicitly supplied QQ peer identities for native send.

    ``conversation_id`` remains the canonical connector identifier.  These
    optional vendor facts carry the independent QQ UID/UIN/peerUid/group-code
    values when the caller has them, so the adapter never has to guess which
    native identifier a generic conversation ID represents.

        中文：提取原生发送操作明确提供的 QQ 对端身份。`conversation_id` 始终是规范 connector 标识符；如果调用方提供了可选的供应商事实，这些事实会携带彼此独立的 QQ UID/UIN/peerUid/群号。适配器绝不会猜测通用会话 ID 对应哪个原生标识。
    """

    extension = request.get("vendor_extension")
    if extension is None:
        return {}
    extension_map = _mapping(extension, "vendor_extension")
    if extension_map.get("vendor") != QQ_VENDOR:
        raise ConnectorError("INVALID_REQUEST", "vendor_extension.vendor must be qq")
    facts = extension_map.get("facts", ())
    if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
        raise ConnectorError("INVALID_REQUEST", "vendor_extension.facts must be a list")
    result: dict[str, str] = {}
    for index, raw_fact in enumerate(facts):
        fact = _mapping(raw_fact, f"vendor_extension.facts[{index}]")
        if set(fact) != {"name", "value"}:
            raise ConnectorError(
                "INVALID_REQUEST",
                f"vendor_extension.facts[{index}] must contain name and value",
            )
        name = _required_text(fact.get("name"), f"vendor_extension.facts[{index}].name")
        native_field = _QQ_PEER_FACT_TO_NATIVE_FIELD.get(name)
        if native_field is None:
            continue
        if native_field in result:
            raise ConnectorError(
                "INVALID_REQUEST", f"duplicate QQ identity fact {name}"
            )
        result[native_field] = _required_identifier(
            fact.get("value"), f"vendor_extension.facts[{index}].value"
        )
    return result


def _build_native_elements(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Map canonical ordered parts to direct native element objects.

        中文：将规范有序内容部分映射为 direct 原生元素对象。
    """

    content = request.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        raise ConnectorError("INVALID_REQUEST", "content must be an ordered list")
    elements: list[dict[str, Any]] = []
    for index, raw in enumerate(content):
        part = _mapping(raw, f"content[{index}]")
        if len(part) != 1:
            raise ConnectorError(
                "INVALID_REQUEST", f"content[{index}] must contain one kind"
            )
        kind, value = next(iter(part.items()))
        item = _mapping(value, f"content[{index}].{kind}")
        if kind == "text":
            elements.append(
                {"type": "text", "text": _required_text(item.get("text"), "text.text")}
            )
        elif kind == "mention":
            target = item.get("target", "user")
            if target in {"everyone", "MENTION_TARGET_EVERYONE", 2}:
                elements.append({"type": "mention", "target": "everyone"})
            else:
                elements.append(
                    {
                        "type": "mention",
                        "target": "user",
                        "target_id": _required_identifier(
                            item.get("target_id"), "mention.target_id"
                        ),
                    }
                )
        elif kind in {"image", "file"}:
            element: dict[str, Any] = {
                "type": kind,
                "reference": _native_reference(
                    item.get("reference"), f"{kind}.reference"
                ),
            }
            if kind == "file":
                element["file_name"] = str(item.get("file_name", ""))
            elements.append(element)
        else:
            raise ConnectorError(
                "INVALID_REQUEST", f"unsupported message content kind {kind!r}"
            )
    if not elements:
        raise ConnectorError("INVALID_REQUEST", "message content must not be empty")
    return elements


def _native_reference(value: Any, field: str) -> dict[str, str]:
    """Validate a binding-safe remote or QQ media reference.

        中文：校验适用于当前 binding 的远程媒体引用或 QQ 媒体引用。
    """

    reference = _mapping(value, field)
    remote = reference.get("remote_uri")
    if remote is not None:
        return {"remote_uri": _validated_remote_uri(remote, f"{field}.remote_uri")}
    vendor_media = _mapping(reference.get("vendor_media"), f"{field}.vendor_media")
    if vendor_media.get("vendor") != QQ_VENDOR:
        raise ConnectorError(
            "INVALID_REQUEST", f"{field}.vendor_media.vendor must be qq"
        )
    return {
        "vendor": QQ_VENDOR,
        "account_id": _required_identifier(
            vendor_media.get("account_id"), f"{field}.account_id"
        ),
        "media_id": _required_identifier(
            vendor_media.get("media_id"), f"{field}.media_id"
        ),
    }


def _normalize_native_message(
    payload: Mapping[str, Any], config: QQNTDirectConfig, generation: int
) -> dict[str, Any]:
    """Normalize a Host-native message without serializing through OneBot.

        中文：规范化 Host 原生消息，不通过 OneBot 序列化。
    """

    account_id = _required_identifier(payload.get("account_id"), "account_id")
    if config.account_id and account_id != config.account_id:
        raise ConnectorError(
            "ACCOUNT_MISMATCH", "native message account does not match binding"
        )
    peer = _mapping(payload.get("peer"), "peer")
    kind = peer.get("kind")
    if kind not in {"private", "group"}:
        raise ConnectorError(
            "INVALID_REQUEST", "native message peer kind is unsupported"
        )
    peer_uid = _required_identifier(peer.get("peer_uid"), "peer.peer_uid")
    if kind == "group":
        conversation_id = _required_identifier(
            peer.get("group_code"), "peer.group_code"
        )
    else:
        conversation_id = _required_identifier(
            peer.get("user_uid", peer.get("user_uin")), "peer.user_uid"
        )
    sender = _mapping(payload.get("sender"), "sender")
    sender_uid = _optional_identifier(sender.get("uid"))
    sender_uin = _optional_identifier(sender.get("uin"))
    sender_id = sender_uid or sender_uin
    if sender_id is None:
        raise ConnectorError(
            "INVALID_REQUEST", "native message sender identity is missing"
        )
    content: list[dict[str, Any]] = []
    reply: dict[str, str] | None = None
    elements = payload.get("elements")
    if not isinstance(elements, Sequence) or isinstance(elements, (str, bytes)):
        raise ConnectorError(
            "INVALID_REQUEST", "native message elements must be ordered"
        )
    for index, element_raw in enumerate(elements):
        element = _mapping(element_raw, f"elements[{index}]")
        element_type = _required_text(element.get("type"), f"elements[{index}].type")
        if element_type == "text":
            content.append(
                {"text": {"text": _required_text(element.get("text"), "element.text")}}
            )
        elif element_type == "mention":
            target = element.get("target")
            if target == "everyone":
                content.append({"mention": {"target": "everyone", "target_id": ""}})
            else:
                content.append(
                    {
                        "mention": {
                            "target": "user",
                            "target_id": _required_identifier(
                                element.get("target_id"), "mention.target_id"
                            ),
                            "display_name": str(element.get("display_name", "")),
                        }
                    }
                )
        elif element_type in {"image", "file"}:
            reference = _native_inbound_reference(element, account_id)
            item = {"reference": reference}
            if element_type == "image":
                item["mime_type"] = str(element.get("mime_type", ""))
                content.append({"image": item})
            else:
                item.update(
                    {
                        "file_name": str(element.get("file_name", "")),
                        "mime_type": str(element.get("mime_type", "")),
                    }
                )
                content.append({"file": item})
        elif element_type == "reply":
            if reply is not None:
                raise ConnectorError(
                    "INVALID_REQUEST", "native message contains duplicate replies"
                )
            reply = {
                "message_id": _required_identifier(
                    element.get("message_id"), "reply.message_id"
                )
            }
        else:
            raise ConnectorError(
                "UNSUPPORTED_OPERATION",
                f"native message element type is not supported: {element_type}",
            )
    if not content and reply is None:
        raise ConnectorError(
            "INVALID_REQUEST", "native message has no supported content"
        )
    facts = [
        {"name": "qq_binding_id", "value": config.binding_id},
        {"name": "qq_worker_generation", "value": str(generation)},
        {"name": "qq_peer_uid", "value": peer_uid},
    ]
    for fact_name, peer_field in (
        ("qq_peer_uin", "peer_uin"),
        ("qq_group_code", "group_code"),
        ("qq_user_uid", "user_uid"),
        ("qq_user_uin", "user_uin"),
    ):
        value = peer.get(peer_field)
        if value is not None:
            facts.append(
                {"name": fact_name, "value": _required_identifier(value, peer_field)}
            )
    for name, value in (
        ("qq_sender_uid", sender_uid),
        ("qq_sender_uin", sender_uin),
        ("qq_sequence", payload.get("sequence")),
        ("qq_random", payload.get("random")),
        ("qq_timestamp", payload.get("timestamp")),
    ):
        if value is not None:
            facts.append({"name": name, "value": str(value)})
    result: dict[str, Any] = {
        "message_id": _required_identifier(payload.get("message_id"), "message_id"),
        "conversation": {
            "vendor": QQ_VENDOR,
            "account_id": account_id,
            "conversation_id": conversation_id,
            "kind": kind,
        },
        "sender_id": sender_id,
        "sender_display_name": str(sender.get("display_name", sender_id)),
        "content": content,
        "vendor_extension": {"vendor": QQ_VENDOR, "facts": facts},
    }
    if reply is not None:
        result["reply"] = reply
    return result


def _native_inbound_reference(
    element: Mapping[str, Any], account_id: str
) -> dict[str, Any]:
    """Convert a normalized native media object to a canonical attachment reference.

        中文：将规范化的原生媒体对象转换为规范附件引用。
    """

    remote = element.get("remote_uri")
    if remote is not None:
        return {"remote_uri": _validated_remote_uri(remote, "remote_uri")}
    media_id = _required_identifier(
        element.get("media_id", element.get("file_id")), "media_id"
    )
    return {
        "vendor_media": {
            "vendor": QQ_VENDOR,
            "account_id": account_id,
            "media_id": media_id,
        }
    }


def _normalize_native_request(
    payload: Mapping[str, Any], config: QQNTDirectConfig
) -> dict[str, Any]:
    """Normalize a direct friend/group request for the canonical approval seam.

        中文：规范化 direct 好友／群组请求，供规范审批接口使用。
    """

    account_id = _required_identifier(payload.get("account_id"), "account_id")
    if config.account_id and account_id != config.account_id:
        raise ConnectorError(
            "ACCOUNT_MISMATCH", "request account does not match binding"
        )
    request_kind = payload.get("request_kind")
    if request_kind not in {"friend", "group_invite"}:
        raise ConnectorError("INVALID_REQUEST", "unsupported native request kind")
    vendor_request = {
        key: _required_identifier(payload[key], key)
        for key in ("group_id", "user_id", "sub_type")
        if key in payload and payload[key] is not None
    }
    return {
        "account_id": account_id,
        "vendor": QQ_VENDOR,
        "request_id": _required_identifier(payload.get("request_id"), "request_id"),
        "request_kind": request_kind,
        "vendor_request": vendor_request,
    }


def _inbound_message_payload(value: Mapping[str, Any]) -> bytes:
    """Build the canonical protobuf event from normalized direct data.

        中文：根据规范化的 direct 数据构造规范 protobuf 事件。
    """

    payload = message_contract.InboundMessagePayload()
    payload.message_id = value["message_id"]
    conversation = value["conversation"]
    payload.conversation.vendor = conversation["vendor"]
    payload.conversation.account_id = conversation["account_id"]
    payload.conversation.conversation_id = conversation["conversation_id"]
    payload.conversation.kind = {
        "private": message_contract.CONVERSATION_KIND_PRIVATE,
        "group": message_contract.CONVERSATION_KIND_GROUP,
    }[conversation["kind"]]
    payload.sender_id = value["sender_id"]
    payload.sender_display_name = value["sender_display_name"]
    for raw_part in value["content"]:
        part = payload.content.add()
        kind, item = next(iter(raw_part.items()))
        if kind == "text":
            part.text.text = item["text"]
        elif kind == "mention":
            part.mention.target = (
                message_contract.MENTION_TARGET_EVERYONE
                if item["target"] == "everyone"
                else message_contract.MENTION_TARGET_USER
            )
            part.mention.target_id = item.get("target_id", "")
            part.mention.display_name = item.get("display_name", "")
        elif kind in {"image", "file"}:
            destination = getattr(part, kind)
            reference = item["reference"]
            if "remote_uri" in reference:
                destination.reference.remote_uri = reference["remote_uri"]
            else:
                media = reference["vendor_media"]
                destination.reference.vendor_media.vendor = media["vendor"]
                destination.reference.vendor_media.account_id = media["account_id"]
                destination.reference.vendor_media.media_id = media["media_id"]
            if kind == "image":
                destination.mime_type = item.get("mime_type", "")
            else:
                destination.file_name = item.get("file_name", "")
                destination.mime_type = item.get("mime_type", "")
    if "reply" in value:
        payload.reply.message_id = value["reply"]["message_id"]
    for fact in value.get("vendor_extension", {}).get("facts", ()):
        item = payload.vendor_extension.facts.add()
        item.name = fact["name"]
        item.value = fact["value"]
    payload.vendor_extension.vendor = QQ_VENDOR
    return payload.SerializeToString()


def _decode_json_object(payload: bytes, field: str) -> dict[str, Any]:
    """Decode one bounded JSON object used by an explicit owner-scoped method.

        中文：解码显式 owner-scoped 方法使用的一个有界 JSON 对象。
    """

    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError("INVALID_REQUEST", f"{field} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ConnectorError("INVALID_REQUEST", f"{field} must be an object")
    return value


def _decode_filter(payload: bytes) -> dict[str, Any]:
    """Validate the small binding-local subscription filter.

        中文：校验精简的 binding 本地订阅过滤器。
    """

    if not payload:
        return {}
    value = _decode_json_object(payload, "subscription filter")
    allowed = {"event_type", "conversation_id", "kind", "request_kind"}
    if set(value).difference(allowed):
        raise ConnectorError(
            "INVALID_REQUEST", "subscription filter contains unknown fields"
        )
    return value


def _filter_matches(
    filter_value: Mapping[str, Any], payload: Mapping[str, Any], event_type: str
) -> bool:
    """Apply event-type and conversation filters without payload-based routing.

        中文：应用事件类型和会话过滤条件，不按负载内容路由。
    """

    if filter_value.get("event_type") not in {None, event_type}:
        return False
    conversation = payload.get("conversation")
    if isinstance(conversation, Mapping):
        return filter_value.get("conversation_id") in {
            None,
            conversation.get("conversation_id"),
        } and filter_value.get("kind") in {None, conversation.get("kind")}
    return (
        filter_value.get("conversation_id") is None
        and filter_value.get("kind") is None
        and filter_value.get("request_kind") in {None, payload.get("request_kind")}
    )


def _facts_from_result(result: Mapping[str, Any]) -> list[dict[str, str]]:
    """Keep only bounded identity facts from a native send result.

        中文：仅保留原生发送结果中有界的身份事实。
    """

    facts: list[dict[str, str]] = []
    for name in (
        "sequence",
        "random",
        "peer_uid",
        "peer_uin",
        "group_code",
        "user_uid",
        "user_uin",
    ):
        value = result.get(name)
        if value is not None:
            facts.append({"name": f"qq_{name}", "value": str(value)[:2_048]})
    return facts


def _normalize_callback(
    operation: str,
    request_id: str,
    event_id: Any,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize only typed callback facts correlated to an originating call.

        中文：仅规范化与发起调用相关联的类型化回调事实。
    """

    result: dict[str, Any] = {
        "operation": operation,
        "request_id": _required_text(request_id, "callback.request_id"),
    }
    if event_id is not None:
        result["event_id"] = _required_identifier(event_id, "callback.event_id")
    identifier_fields = {
        "account_id",
        "message_id",
        "peer_uid",
        "peer_uin",
        "group_code",
        "user_uid",
        "user_uin",
        "group_id",
        "user_id",
        "member_uid",
        "member_uin",
        "media_id",
        "file_id",
        "file_uuid",
        "element_id",
    }
    for key in (
        "account_id",
        "message_id",
        "peer_uid",
        "peer_uin",
        "group_code",
        "user_uid",
        "user_uin",
        "group_id",
        "user_id",
        "member_uid",
        "member_uin",
        "media_id",
        "file_id",
        "file_uuid",
        "element_id",
        "sequence",
        "random",
        "status",
        "progress",
        "local_result_reference",
    ):
        value = payload.get(key)
        if value is None:
            continue
        if key in identifier_fields:
            result[key] = _required_identifier(value, f"callback.{key}")
        elif key == "local_result_reference":
            result[key] = _validated_local_result_reference(
                value, "callback.local_result_reference"
            )
        elif isinstance(value, bool):
            result[key] = value
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise ConnectorError("INVALID_REQUEST", f"callback.{key} is not finite")
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:2_048]
        else:
            raise ConnectorError("INVALID_REQUEST", f"callback.{key} has invalid type")
    if payload.get("remote_uri") is not None:
        result["remote_uri"] = _validated_remote_uri(
            payload["remote_uri"], "callback.remote_uri"
        )
    error = payload.get("error")
    if error is not None:
        error_map = _mapping(error, "callback.error")
        result["error"] = {
            "code": _required_text(error_map.get("code"), "callback.error.code")[:128],
            "message": _required_text(
                error_map.get("message"), "callback.error.message"
            )[:512],
        }
    return result


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConnectorError("INVALID_REQUEST", f"{field} must be an object")
    return value


def _validated_remote_uri(value: Any, field: str) -> str:
    """Accept only bounded HTTP(S) references across the canonical seam.

        中文：只接受有界的 HTTP(S) 引用，通过规范接口传递。
    """

    remote = _required_text(value, field)
    if len(remote.encode("utf-8")) > _MAX_MEDIA_REFERENCE_BYTES:
        raise ConnectorError("INVALID_REQUEST", f"{field} is too long")
    parsed = urlparse(remote)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConnectorError("INVALID_REQUEST", f"{field} must be http(s)")
    return remote


def _validated_local_result_reference(value: Any, field: str) -> str:
    """Accept only bounded binding-private references for local media results.

        中文：只接受用于本地媒体结果的有界 binding 私有引用。
    """

    reference = _required_text(value, field)
    if len(reference.encode("utf-8")) > _MAX_MEDIA_REFERENCE_BYTES:
        raise ConnectorError("INVALID_REQUEST", f"{field} is too long")
    if not reference.startswith(("qq://", "staging://")):
        raise ConnectorError("INVALID_REQUEST", f"{field} must be binding-private")
    return reference


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError("INVALID_REQUEST", f"{field} must be non-empty text")
    return value.strip()


def _required_identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConnectorError("INVALID_REQUEST", f"{field} must be a string or integer")
    text = str(value).strip()
    if not text:
        raise ConnectorError("INVALID_REQUEST", f"{field} must be non-empty")
    return text


def _optional_identifier(value: Any) -> str | None:
    if value is None:
        return None
    return _required_identifier(value, "identifier")


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError as exc:
            raise ConnectorError("INVALID_REQUEST", f"{field} must be numeric") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConnectorError("INVALID_REQUEST", f"{field} must be positive")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ConnectorError("INVALID_REQUEST", f"{field} must be positive")
    return result


def _bounded_number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    """Validate one finite numeric setting against a strict safety range.

        中文：根据严格的安全范围校验一个有限数值设置。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConnectorError("INVALID_REQUEST", f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ConnectorError(
            "INVALID_REQUEST",
            f"{field} must be between {minimum} and {maximum}",
        )
    return result


def _bounded_integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    """Validate one bounded integer setting without accepting booleans.

        中文：校验一个有界整数设置，并拒绝布尔值。
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ConnectorError("INVALID_REQUEST", f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise ConnectorError(
            "INVALID_REQUEST",
            f"{field} must be between {minimum} and {maximum}",
        )
    return value
