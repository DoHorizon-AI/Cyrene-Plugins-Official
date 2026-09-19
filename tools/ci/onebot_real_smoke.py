#!/usr/bin/env python3
"""
Run the protected real OneBot v11 smoke against the packaged Native AOT host.

The runner supplies three operator-owned OneBot endpoints.  This script sends
one bounded marker through each profile and requires a matching inbound event
for both WebSocket profiles.  It never prints endpoint URLs, access tokens,
message contents, or process diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAPABILITY = "message.connector.v1"
SEND_METHOD = "send_message"
SEND_REQUEST_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.SendMessageRequest"
)
DELIVERY_RESULT_TYPE_URL = (
    "type.cyrene.io/cyrene.message.connector.v1.DeliveryResult"
)
MESSAGE_TYPE_URL = "type.cyrene.io/cyrene.message.connector.v1.InboundMessagePayload"
PROFILE_NAMES = ("http_api", "forward_websocket", "reverse_websocket")


class SmokeConfigurationError(RuntimeError):
    """Raised when the protected OneBot environment is incomplete."""


def _required_env(name: str) -> str:
    """Read one required value without exposing its content."""

    value = os.environ.get(name, "").strip()
    if not value:
        raise SmokeConfigurationError(f"missing protected environment value: {name}")
    return value


def _bounded_env(name: str, default: str, maximum: int = 512) -> str:
    """Read one bounded protected value."""

    value = os.environ.get(name, default).strip()
    if not value or len(value.encode("utf-8")) > maximum:
        raise SmokeConfigurationError(f"invalid protected environment value: {name}")
    return value


def _parse_args() -> argparse.Namespace:
    """Parse the packaged binary and redacted evidence destination."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    return parser.parse_args()


def _read_ready_announcement(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    """Read the bounded JSON readiness announcement from the child process."""

    if process.stdout is None:
        raise RuntimeError("Native AOT host stdout is not captured")
    descriptor = process.stdout.fileno()
    deadline = time.monotonic() + 20
    pending = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([descriptor], [], [], 0.1)
        if not ready:
            if process.poll() is not None:
                break
            continue
        chunk = os.read(descriptor, 4096)
        if not chunk:
            break
        pending += chunk
        while b"\n" in pending:
            raw_line, pending = pending.split(b"\n", 1)
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and value.get("event") == "direct_plugin_ready":
                return value
    raise RuntimeError("Native AOT host did not announce readiness")


def _conversation_kind(message_wire: Any, value: str) -> int:
    """Map the protected conversation kind to the canonical enum."""

    return {
        "private": message_wire.CONVERSATION_KIND_PRIVATE,
        "group": message_wire.CONVERSATION_KIND_GROUP,
    }.get(value, 0)


def _load_configuration() -> dict[str, Any]:
    """Load and validate the operator-owned real smoke configuration."""

    if _required_env("ONEBOT_REAL_SMOKE_APPROVED") != "YES":
        raise SmokeConfigurationError(
            "ONEBOT_REAL_SMOKE_APPROVED must be exactly YES"
        )
    conversation_kind = _bounded_env(
        "ONEBOT_REAL_CONVERSATION_KIND", "group", maximum=16
    )
    if conversation_kind not in {"private", "group"}:
        raise SmokeConfigurationError(
            "ONEBOT_REAL_CONVERSATION_KIND must be private or group"
        )
    try:
        reverse_port = int(_required_env("ONEBOT_REAL_REVERSE_LISTEN_PORT"))
    except ValueError as error:
        raise SmokeConfigurationError(
            "ONEBOT_REAL_REVERSE_LISTEN_PORT must be an integer"
        ) from error
    if reverse_port < 1 or reverse_port > 65_535:
        raise SmokeConfigurationError(
            "ONEBOT_REAL_REVERSE_LISTEN_PORT must be between 1 and 65535"
        )
    return {
        "http_base_url": _required_env("ONEBOT_REAL_HTTP_BASE_URL"),
        "websocket_url": _required_env("ONEBOT_REAL_FORWARD_WEBSOCKET_URL"),
        "reverse_listen_host": _bounded_env(
            "ONEBOT_REAL_REVERSE_LISTEN_HOST", "127.0.0.1", maximum=128
        ),
        "reverse_listen_port": reverse_port,
        "access_token": os.environ.get("ONEBOT_REAL_ACCESS_TOKEN", ""),
        "account_id": _required_env("ONEBOT_REAL_ACCOUNT_ID"),
        "conversation_id": _required_env("ONEBOT_REAL_CONVERSATION_ID"),
        "conversation_kind": conversation_kind,
        "marker_prefix": _bounded_env(
            "ONEBOT_REAL_MARKER_PREFIX", "cyrene-onebot-real", maximum=128
        ),
        "event_timeout": float(
            _bounded_env("ONEBOT_REAL_EVENT_TIMEOUT_SECONDS", "30", maximum=16)
        ),
    }


def _canonical_send_request(message_wire: Any, configuration: dict[str, Any], marker: str) -> Any:
    """Build one canonical message request for the dedicated smoke target."""

    return message_wire.SendMessageRequest(
        conversation=message_wire.ConversationScope(
            vendor="onebot.v11",
            account_id=configuration["account_id"],
            conversation_id=configuration["conversation_id"],
            kind=_conversation_kind(message_wire, configuration["conversation_kind"]),
        ),
        content=[message_wire.MessageContentPart(text=message_wire.TextContent(text=marker))],
    )


def _event_contains_marker(message_wire: Any, payload: Any, configuration: dict[str, Any], marker: str) -> bool:
    """Require a normalized inbound event for the same account and target."""

    if payload.type_url != MESSAGE_TYPE_URL:
        return False
    inbound = message_wire.InboundMessagePayload.FromString(payload.value)
    if inbound.conversation.account_id != configuration["account_id"]:
        return False
    if inbound.conversation.conversation_id != configuration["conversation_id"]:
        return False
    return any(
        part.HasField("text") and marker in part.text.text for part in inbound.content
    )


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Stop one child process without emitting its diagnostics."""

    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _invoke_profile(
    binary: Path,
    profile: str,
    configuration: dict[str, Any],
    run_id: str,
    runtime_wire: Any,
    runtime_wire_grpc: Any,
    message_wire: Any,
    grpc_module: Any,
) -> dict[str, Any]:
    """Start one binding, verify Health, send a marker, and observe WebSocket events."""

    binding_id = f"onebot-real-{run_id}-{profile}"
    profile_config = {
        "binding_id": binding_id,
        "runtime_profile": "onebot-v11",
        "transport_profile": profile,
        "access_token": configuration["access_token"],
        "self_account_id": configuration["account_id"],
        "timeout_seconds": min(configuration["event_timeout"], 60),
    }
    if profile == "http_api":
        profile_config["http_base_url"] = configuration["http_base_url"]
    elif profile == "forward_websocket":
        profile_config["websocket_url"] = configuration["websocket_url"]
    else:
        profile_config.update(
            {
                "reverse_listen_host": configuration["reverse_listen_host"],
                "reverse_listen_port": configuration["reverse_listen_port"],
            }
        )

    marker = f"{configuration['marker_prefix']}-{run_id}-{profile}"
    environment = os.environ.copy()
    environment["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(profile_config)
    environment["CYRENE_CAPABILITY_BINDING_ID"] = binding_id
    process = subprocess.Popen(
        [str(binary), "--listen", "127.0.0.1:0"],
        cwd=binary.parent.parent,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    channel = None
    stream = None
    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    reader: threading.Thread | None = None
    try:
        announcement = _read_ready_announcement(process)
        connection_ref = announcement.get("connection_ref")
        if not isinstance(connection_ref, str) or not connection_ref.startswith("grpc://"):
            raise RuntimeError("invalid Native AOT readiness announcement")
        channel = grpc_module.insecure_channel(connection_ref.removeprefix("grpc://"))
        grpc_module.channel_ready_future(channel).result(timeout=10)
        client = runtime_wire_grpc.DirectPluginRuntimeStub(channel)
        health = client.Health(runtime_wire.HealthRequest(), timeout=5)
        if health.status != runtime_wire.HealthResponse.STATUS_SERVING:
            raise RuntimeError("Native AOT Host did not report SERVING")

        if profile != "http_api":
            stream = client.InvokeStream(
                runtime_wire.DirectInvocationRequest(
                    capability=CAPABILITY,
                    interface_version="1",
                    method="events",
                    payload_type_url="type.cyrene.io/cyrene.message.connector.v1.Filter",
                    payload=b"{}",
                    request_id=f"{binding_id}-events",
                    stream_mode=runtime_wire.DIRECT_STREAM_MODE_SUBSCRIPTION,
                ),
                timeout=configuration["event_timeout"] + 10,
            )

            def consume() -> None:
                try:
                    for item in stream:
                        events.put(("item", item))
                except Exception as error:  # noqa: BLE001 - recorded as a smoke failure
                    events.put(("error", error))

            reader = threading.Thread(target=consume, daemon=True)
            reader.start()

        response = client.Invoke(
            runtime_wire.DirectInvocationRequest(
                capability=CAPABILITY,
                interface_version="1",
                method=SEND_METHOD,
                payload_type_url=SEND_REQUEST_TYPE_URL,
                payload=_canonical_send_request(
                    message_wire, configuration, marker
                ).SerializeToString(),
                request_id=f"{binding_id}-send",
            ),
            timeout=configuration["event_timeout"],
        )
        if response.WhichOneof("result") != "payload":
            raise RuntimeError("real OneBot send_message returned an error")
        if response.payload.type_url != DELIVERY_RESULT_TYPE_URL:
            raise RuntimeError("real OneBot delivery type URL is not canonical")
        delivery = message_wire.DeliveryResult.FromString(response.payload.value)
        if not delivery.vendor_message_id:
            raise RuntimeError("real OneBot delivery did not return a message id")

        event_observed = False
        if profile != "http_api":
            deadline = time.monotonic() + configuration["event_timeout"]
            while time.monotonic() < deadline:
                try:
                    kind, item = events.get(timeout=min(1, deadline - time.monotonic()))
                except queue.Empty:
                    continue
                if kind == "error":
                    raise RuntimeError("real OneBot event subscription failed")
                if item.WhichOneof("event") == "payload" and _event_contains_marker(
                    message_wire, item.payload, configuration, marker
                ):
                    event_observed = True
                    break
            if not event_observed:
                raise RuntimeError("real OneBot WebSocket marker event was not observed")

        return {
            "profile": profile,
            "health": runtime_wire.HealthResponse.Status.Name(health.status),
            "send_message": {
                "status": message_wire.DeliveryStatus.Name(delivery.status),
                "vendor_message_id_present": True,
            },
            "marker_sha256": hashlib.sha256(marker.encode()).hexdigest(),
            "event_marker_observed": event_observed,
        }
    finally:
        if stream is not None:
            stream.cancel()
        if reader is not None:
            reader.join(timeout=2)
        if channel is not None:
            channel.close()
        _stop(process)


def main() -> int:
    """Run all three real profiles and write only redacted evidence."""

    args = _parse_args()
    binary = args.binary.resolve(strict=True)
    configuration = _load_configuration()
    sys.path.insert(0, str(REPOSITORY_ROOT / "sdk/python/cyrene_plugin_runtime/src"))
    sys.path.insert(0, str(REPOSITORY_ROOT / "plugins/connectors/onebot-v11/src"))
    import grpc
    from cyrene_plugin_runtime._generated import (
        direct_plugin_runtime_pb2 as runtime_wire,
    )
    from cyrene_plugin_runtime._generated import (
        direct_plugin_runtime_pb2_grpc as runtime_wire_grpc,
    )
    from onebot_v11_connector._generated import message_connector_pb2 as message_wire

    run_id = _bounded_env("GITHUB_RUN_ID", "local", maximum=64)
    evidence = {
        "schema": "cyrene.onebot.real-smoke.v1",
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "profiles": [],
    }
    for profile in PROFILE_NAMES:
        evidence["profiles"].append(
            _invoke_profile(
                binary,
                profile,
                configuration,
                run_id,
                runtime_wire,
                runtime_wire_grpc,
                message_wire,
                grpc,
            )
        )
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
