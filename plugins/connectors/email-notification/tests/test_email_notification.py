"""
┌──────────────────────────────────────────────────────────────────────┐
│  test_email_notification.py                                           │
│  Module: email_notification_connector.tests                           │
│  Role: Exercise signed tool calls against a synthetic loopback SMTP.   │
│                                                                      │
│  模块职责：通过合成 loopback SMTP 验证签名工具调用和发送结果。            │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import socketserver
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cyrene_integration_contracts import (
    TOOL_OPERATIONS,
    parse_trusted_caller_context,
    schema_for,
    serialize_trusted_caller_context,
    sign_context,
    verify_context,
    write_replay_identity,
)
from jsonschema import Draft202012Validator, FormatChecker

import email_notification_connector.plugin as email_plugin
from email_notification_connector import EmailNotificationProvider
from email_notification_connector._generated import tool_provider_pb2 as wire
from email_notification_connector.ledger import LedgerFailure, ReplayIdentity, SubmissionLedger
from email_notification_connector.plugin import (
    CALL_TOOL_REQUEST_TYPE_URL,
    CAPABILITY_ID,
    LIST_TOOLS_REQUEST_TYPE_URL,
    SEND_TOOL_ID,
    _request_id_digest,
)
from email_notification_connector.settings import ConfigurationError, ProviderSettings

_KEY_HEX = bytes(range(32)).hex()


class _SMTPFixtureServer(socketserver.ThreadingTCPServer):
    """Local SMTP protocol fixture with configurable final response behavior."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        *,
        reject_rcpt_code: int | None = None,
        reject_rcpt_once: bool = False,
        final_code: int | None = 250,
        final_delay_seconds: float = 0,
    ) -> None:
        self.reject_rcpt_code = reject_rcpt_code
        self.reject_rcpt_remaining = 1 if reject_rcpt_once else -1
        self.final_code = final_code
        self.final_delay_seconds = final_delay_seconds
        self.commands: list[str] = []
        self.message_data: list[bytes] = []
        self.connections = 0
        self._state_lock = threading.Lock()
        self.data_received = threading.Event()
        super().__init__(("127.0.0.1", 0), _SMTPFixtureHandler)

    @property
    def port(self) -> int:
        """Return the operating-system-selected fixture port."""

        return int(self.server_address[1])

    def __enter__(self) -> _SMTPFixtureServer:
        """Start one background listener for the current test."""

        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        """Stop the listener without waiting on an intentionally silent peer."""

        self.shutdown()
        self.server_close()


class _SMTPFixtureHandler(socketserver.StreamRequestHandler):
    """Answer the minimal SMTP commands exercised by the connector."""

    def handle(self) -> None:
        """Record SMTP commands and simulate acceptance, refusal, or lost reply."""

        server = self.server
        assert isinstance(server, _SMTPFixtureServer)
        with server._state_lock:
            server.connections += 1
        self.wfile.write(b"220 synthetic smtp fixture\r\n")
        while line := self.rfile.readline():
            command = line.decode("ascii", errors="replace").rstrip("\r\n")
            server.commands.append(command)
            verb, _, argument = command.partition(" ")
            verb = verb.upper()
            if verb in {"EHLO", "HELO"}:
                self.wfile.write(b"250-synthetic smtp fixture\r\n250 HELP\r\n")
            elif verb == "MAIL":
                self.wfile.write(b"250 sender accepted\r\n")
            elif verb == "RCPT":
                reject = server.reject_rcpt_code is not None and (
                    server.reject_rcpt_remaining < 0 or server.reject_rcpt_remaining > 0
                )
                if server.reject_rcpt_remaining > 0:
                    server.reject_rcpt_remaining -= 1
                    reject = True
                if not reject:
                    self.wfile.write(b"250 recipient accepted\r\n")
                else:
                    self.wfile.write(f"{server.reject_rcpt_code} recipient refused\r\n".encode())
            elif verb == "DATA":
                self.wfile.write(b"354 send data\r\n")
                data_lines: list[bytes] = []
                while data_line := self.rfile.readline():
                    if data_line == b".\r\n":
                        break
                    data_lines.append(data_line)
                server.message_data.append(b"".join(data_lines))
                server.data_received.set()
                if server.final_delay_seconds:
                    time.sleep(server.final_delay_seconds)
                if server.final_code is not None:
                    self.wfile.write(f"{server.final_code} fixture final response\r\n".encode())
                else:
                    return
            elif verb == "QUIT":
                self.wfile.write(b"221 closing\r\n")
                return
            else:
                self.wfile.write(b"250 command accepted\r\n")


@pytest.fixture
def settings_factory(tmp_path):
    """Build one loopback-only test binding and optional alternate scopes."""

    private_dir = tmp_path / "email-private"
    private_dir.mkdir(mode=0o700)
    state_db_path = private_dir / "submissions.sqlite3"

    def create(port: int, **overrides: Any) -> ProviderSettings:
        config: dict[str, Any] = {
            "smtp_host": "127.0.0.1",
            "smtp_port": port,
            "tls_mode": "loopback_test_plain",
            "allow_loopback_test_transport": True,
            "timeout_seconds": 0.12,
            "sender": "notifications@example.test",
            "recipients": {"operator": "operator@example.test"},
            "state_db_path": str(state_db_path),
            "context_hmac_key_env_ref": "EMAIL_NOTIFICATION_CONTEXT_HMAC_KEY",
            "caller_workload": "cyrene-exchange",
            "audience": "cyrene.connectors.email-notification",
            "organization_id": "org-test",
            "workspace_id": "workspace-test",
            "expected_issuer": "cyrene-exchange",
            "expected_subject": "notification-worker",
        }
        config.update(overrides)
        return ProviderSettings.from_mapping(
            config,
            binding_id="email-binding-test",
            environ={"EMAIL_NOTIFICATION_CONTEXT_HMAC_KEY": _KEY_HEX},
        )

    return create


def _signed_envelope(
    settings: ProviderSettings,
    *,
    tool_id: str,
    arguments: dict[str, Any],
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    **claim_overrides: Any,
) -> dict[str, Any]:
    """Create a test-only signed inner argument object for one exact tool."""

    issued = issued_at or datetime.now(UTC)
    claims: dict[str, Any] = {
        "method": tool_id,
        "operation_id": TOOL_OPERATIONS[tool_id],
        "payload": arguments,
        "caller_workload": settings.caller_workload,
        "audience": settings.audience,
        "issuer": "cyrene-exchange",
        "subject": "notification-worker",
        "organization_id": settings.organization_id,
        "workspace_id": settings.workspace_id,
        "binding_id": settings.binding_id,
        "request_id": "tool-request-001",
        "key": settings.context_hmac_key,
        "issued_at": issued,
    }
    if expires_at is not None:
        claims["expires_at"] = expires_at
    claims.update(claim_overrides)
    context = sign_context(**claims)
    return {
        "caller_context": serialize_trusted_caller_context(context),
        "arguments": arguments,
    }


def _invoke(
    provider: EmailNotificationProvider,
    *,
    tool_id: str,
    envelope: dict[str, Any],
) -> wire.CallToolResponse:
    """Call the canonical tool-provider protobuf operation and decode its result."""

    request = wire.CallToolRequest(
        binding_id=provider._settings.binding_id,
        provider_tool_id=tool_id,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )
    success, result = provider.on_invoke(
        CAPABILITY_ID,
        "call_tool",
        request.SerializeToString(),
        request_type_url=CALL_TOOL_REQUEST_TYPE_URL,
    )
    assert success is True
    return wire.CallToolResponse.FromString(result.value)


def _submission(response: wire.CallToolResponse) -> dict[str, Any]:
    """Decode the typed email result returned inside ToolCallOutcome."""

    assert response.HasField("outcome")
    assert len(response.outcome.content) == 1
    return json.loads(response.outcome.content[0].json.json)


def test_tool_list_exposes_only_fixed_signed_email_tools(settings_factory) -> None:
    """List the two fixed tools and validate their strict published schemas."""

    with _SMTPFixtureServer() as smtp:
        provider = EmailNotificationProvider(settings=settings_factory(smtp.port))
        request = wire.ListToolsRequest(binding_id="email-binding-test")
        success, result = provider.on_invoke(
            CAPABILITY_ID,
            "list_tools",
            request.SerializeToString(),
            request_type_url=LIST_TOOLS_REQUEST_TYPE_URL,
        )

    assert success is True
    response = wire.ListToolsResponse.FromString(result.value)
    assert [tool.provider_tool_id for tool in response.catalog.tools] == [
        "email_notification_send",
        "email_connection_health",
    ]
    for tool in response.catalog.tools:
        schema = json.loads(tool.input_schema_json)
        Draft202012Validator.check_schema(schema)
    send_schema = json.loads(response.catalog.tools[0].input_schema_json)
    Draft202012Validator(send_schema, format_checker=FormatChecker())
    assert send_schema == schema_for("EmailNotificationSendEnvelope")


def test_settings_repr_redacts_private_binding_details(settings_factory) -> None:
    """Keep endpoint, recipient, tenant scope and signing key out of diagnostics."""

    settings = settings_factory(2525)
    rendered = repr(settings)
    for private_value in (
        "127.0.0.1",
        "operator@example.test",
        "email-binding-test",
        "org-test",
        _KEY_HEX,
    ):
        assert private_value not in rendered


def test_entrypoint_loads_the_standard_activation_environment(tmp_path, monkeypatch) -> None:
    """Construct the packaged class from the Plugin runtime's normal activation seam."""

    private_directory = tmp_path / "runtime-state"
    private_directory.mkdir(mode=0o700)
    private_directory.chmod(0o700)
    config = {
        "smtp_host": "smtp.example.test",
        "smtp_port": 465,
        "sender": "notifications@example.test",
        "recipients": {"operator": "operator@example.test"},
        "state_db_path": str(private_directory / "submissions.sqlite3"),
        "context_hmac_key_env_ref": "EMAIL_NOTIFICATION_CONTEXT_HMAC_KEY",
        "caller_workload": "cyrene-exchange",
        "audience": "cyrene.connectors.email-notification",
        "organization_id": "org-test",
        "workspace_id": "workspace-test",
    }
    monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "email-runtime-binding")
    monkeypatch.setenv("CYRENE_CAPABILITY_CONFIGURATION_JSON", json.dumps(config))
    monkeypatch.setenv("EMAIL_NOTIFICATION_CONTEXT_HMAC_KEY", _KEY_HEX)

    provider = EmailNotificationProvider()

    assert provider.plugin_id == "cyrene.connectors.email-notification"
    assert provider.capabilities == ("tool.provider.v1",)
    assert provider._settings.tls_mode == "implicit"
    assert provider._settings.binding_id == "email-runtime-binding"


def test_private_ledger_rejects_public_directory(tmp_path) -> None:
    """Fail closed when persistent replay facts would be world-readable."""

    public_directory = tmp_path / "public-state"
    public_directory.mkdir(mode=0o755)
    public_directory.chmod(0o755)

    with pytest.raises(LedgerFailure, match="permissions are unsafe"):
        SubmissionLedger(public_directory / "submissions.sqlite3")
    assert not (public_directory / "submissions.sqlite3").exists()


def test_local_smtp_acceptance_returns_no_delivery_claim(settings_factory) -> None:
    """Persist ACCEPTED and return the same result after a provider restart."""

    with _SMTPFixtureServer() as smtp:
        settings = settings_factory(smtp.port)
        provider = EmailNotificationProvider(settings=settings)
        arguments = {
            "recipient_ref": "operator",
            "subject": "Integration alert",
            "text": "Resource needs attention.\nPlease review.",
            "request_ref": "notification-42",
        }
        envelope = _signed_envelope(settings, tool_id=SEND_TOOL_ID, arguments=arguments)
        response = _invoke(
            provider,
            tool_id=SEND_TOOL_ID,
            envelope=envelope,
        )
        replay_response = _invoke(
            EmailNotificationProvider(settings=settings),
            tool_id=SEND_TOOL_ID,
            envelope=envelope,
        )

    result = _submission(response)
    assert result == {
        "request_ref": "notification-42",
        "status": "ACCEPTED",
        "vendor_receipt_ref": None,
        "delivery_confirmed": False,
    }
    assert _submission(replay_response) == result
    assert "rcpt TO:<operator@example.test>" in smtp.commands
    assert b"Subject: Integration alert" in smtp.message_data[0]
    assert b"Resource needs attention." in smtp.message_data[0]
    assert b"Bcc:" not in smtp.message_data[0]
    assert len(smtp.message_data) == 1
    assert smtp.connections == 1
    assert stat.S_IMODE(settings.state_db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.state_db_path.parent.stat().st_mode) == 0o700
    database = sqlite3.connect(settings.state_db_path)
    try:
        stored = database.execute("SELECT status FROM email_submissions").fetchone()
        assert stored == ("ACCEPTED",)
        assert (
            database.execute("SELECT COUNT(*) FROM email_submission_request_ids").fetchone()[0] == 1
        )
    finally:
        database.close()
    database_bytes = settings.state_db_path.read_bytes()
    wal_path = settings.state_db_path.with_name(settings.state_db_path.name + "-wal")
    if wal_path.exists():
        database_bytes += wal_path.read_bytes()
    assert b"notification-42" not in database_bytes
    assert b"Resource needs attention" not in database_bytes
    assert b"operator@example.test" not in database_bytes


def test_explicit_recipient_rejection_returns_rejected(settings_factory) -> None:
    """Keep a known SMTP refusal distinct from ambiguous delivery."""

    with _SMTPFixtureServer(reject_rcpt_code=550) as smtp:
        settings = settings_factory(smtp.port)
        provider = EmailNotificationProvider(settings=settings)
        arguments = {
            "recipient_ref": "operator",
            "subject": "Rejected notification",
            "text": "No message data is accepted.",
            "request_ref": "notification-rejected",
        }
        response = _invoke(
            provider,
            tool_id=SEND_TOOL_ID,
            envelope=_signed_envelope(settings, tool_id=SEND_TOOL_ID, arguments=arguments),
        )

    result = _submission(response)
    assert result["status"] == "REJECTED"
    assert result["vendor_receipt_ref"] is None
    assert result["delivery_confirmed"] is False
    assert smtp.message_data == []


def test_temporary_pre_data_refusal_is_safe_to_retry(settings_factory) -> None:
    """Retry only after a recorded temporary refusal before message DATA."""

    with _SMTPFixtureServer(reject_rcpt_code=450, reject_rcpt_once=True) as smtp:
        settings = settings_factory(smtp.port)
        provider = EmailNotificationProvider(settings=settings)
        arguments = {
            "recipient_ref": "operator",
            "subject": "Temporary refusal",
            "text": "No message data was submitted.",
            "request_ref": "notification-retryable",
        }
        envelope = _signed_envelope(settings, tool_id=SEND_TOOL_ID, arguments=arguments)
        response = _invoke(
            provider,
            tool_id=SEND_TOOL_ID,
            envelope=envelope,
        )
        retry_response = _invoke(provider, tool_id=SEND_TOOL_ID, envelope=envelope)

    assert response.HasField("error")
    assert response.error.retryable is True
    assert response.error.message == "SMTP connection failed before message content was sent"
    assert _submission(retry_response)["status"] == "ACCEPTED"
    assert len(smtp.message_data) == 1


def test_disconnect_after_data_is_unknown_and_not_retried(settings_factory) -> None:
    """Treat a lost final response after DATA as uncertain with one send attempt."""

    with _SMTPFixtureServer(final_code=None, final_delay_seconds=0.25) as smtp:
        settings = settings_factory(smtp.port)
        provider = EmailNotificationProvider(settings=settings)
        arguments = {
            "recipient_ref": "operator",
            "subject": "Uncertain notification",
            "text": "The fixture accepts DATA then withholds its final reply.",
            "request_ref": "notification-unknown",
        }
        envelope = _signed_envelope(settings, tool_id=SEND_TOOL_ID, arguments=arguments)
        response = _invoke(
            provider,
            tool_id=SEND_TOOL_ID,
            envelope=envelope,
        )
        time.sleep(0.3)
        replay_envelope = _signed_envelope(
            settings,
            tool_id=SEND_TOOL_ID,
            arguments=arguments,
            request_id="tool-request-002",
        )
        replay_response = _invoke(
            EmailNotificationProvider(settings=settings),
            tool_id=SEND_TOOL_ID,
            envelope=replay_envelope,
        )

    result = _submission(response)
    assert result["status"] == "UNKNOWN"
    assert result["vendor_receipt_ref"] is None
    assert result["delivery_confirmed"] is False
    assert _submission(replay_response) == result
    assert len(smtp.message_data) == 1
    assert len([command for command in smtp.commands if command.startswith("DATA")]) == 1


def test_concurrent_duplicate_does_not_report_sending_as_accepted(settings_factory) -> None:
    """Return UNKNOWN to a duplicate during DATA, then retain one accepted send."""

    with _SMTPFixtureServer(final_delay_seconds=0.25) as smtp:
        settings = settings_factory(smtp.port, timeout_seconds=2.0)
        first_provider = EmailNotificationProvider(settings=settings)
        replay_provider = EmailNotificationProvider(settings=settings)
        arguments = {
            "recipient_ref": "operator",
            "subject": "Concurrent replay",
            "text": "Only one SMTP DATA exchange is allowed.",
            "request_ref": "notification-concurrent",
        }
        envelope = _signed_envelope(settings, tool_id=SEND_TOOL_ID, arguments=arguments)
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(
                _invoke,
                first_provider,
                tool_id=SEND_TOOL_ID,
                envelope=envelope,
            )
            assert smtp.data_received.wait(timeout=2)
            concurrent_replay = _invoke(
                replay_provider,
                tool_id=SEND_TOOL_ID,
                envelope=envelope,
            )
            first_result = first_future.result(timeout=2)
        final_replay = _invoke(
            EmailNotificationProvider(settings=settings),
            tool_id=SEND_TOOL_ID,
            envelope=envelope,
        )

    assert _submission(concurrent_replay)["status"] == "UNKNOWN"
    assert _submission(first_result)["status"] == "ACCEPTED"
    assert _submission(final_replay)["status"] == "ACCEPTED"
    assert len(smtp.message_data) == 1


def test_inflight_intent_after_restart_becomes_unknown(settings_factory) -> None:
    """Simulate a process crash after SENDING is committed and before SMTP runs."""

    with _SMTPFixtureServer() as smtp:
        settings = settings_factory(smtp.port)
        arguments = {
            "recipient_ref": "operator",
            "subject": "Restart recovery",
            "text": "A persisted SENDING intent is ambiguous after a crash.",
            "request_ref": "notification-crash",
        }
        envelope = _signed_envelope(settings, tool_id=SEND_TOOL_ID, arguments=arguments)
        caller_context = parse_trusted_caller_context(envelope["caller_context"])
        identity_digest, payload_digest = write_replay_identity(caller_context, arguments)
        identity = ReplayIdentity(
            identity_sha256=identity_digest,
            request_id_sha256=_request_id_digest(caller_context),
            payload_sha256=payload_digest,
            request_ref=arguments["request_ref"],
        )
        assert SubmissionLedger(settings.state_db_path).reserve(identity) is None

        response = _invoke(
            EmailNotificationProvider(settings=settings),
            tool_id=SEND_TOOL_ID,
            envelope=envelope,
        )

    assert _submission(response)["status"] == "UNKNOWN"
    assert smtp.connections == 0
    assert smtp.message_data == []


def test_changed_content_reusing_request_reference_is_rejected(
    settings_factory, monkeypatch
) -> None:
    """Reject altered content under the stable scoped request_ref without resending."""

    with _SMTPFixtureServer() as smtp:
        settings = settings_factory(smtp.port)
        provider = EmailNotificationProvider(settings=settings)
        arguments = {
            "recipient_ref": "operator",
            "subject": "Original content",
            "text": "This first message is accepted.",
            "request_ref": "notification-stable-ref",
        }
        first_envelope = _signed_envelope(settings, tool_id=SEND_TOOL_ID, arguments=arguments)
        changed_arguments = dict(arguments, text="Changed content under same request_ref")
        changed_envelope = _signed_envelope(
            settings,
            tool_id=SEND_TOOL_ID,
            arguments=changed_arguments,
            request_id="tool-request-002",
        )
        changed_request_arguments = dict(arguments, request_ref="different-request-ref")
        changed_request_envelope = _signed_envelope(
            settings,
            tool_id=SEND_TOOL_ID,
            arguments=changed_request_arguments,
        )

        def verify_at_issued_at(**kwargs: Any) -> bool:
            """Run the SDK's full verifier against this fresh signature time."""

            return verify_context(**kwargs, now=kwargs["context"].issued_at)

        with monkeypatch.context() as verifier_clock:
            verifier_clock.setattr(email_plugin, "verify_context", verify_at_issued_at)
            first = _invoke(provider, tool_id=SEND_TOOL_ID, envelope=first_envelope)
            changed = _invoke(provider, tool_id=SEND_TOOL_ID, envelope=changed_envelope)
            reused_request_id = _invoke(
                provider,
                tool_id=SEND_TOOL_ID,
                envelope=changed_request_envelope,
            )

    assert _submission(first)["status"] == "ACCEPTED"
    assert changed.HasField("error")
    assert changed.error.code == wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS
    assert reused_request_id.HasField("error")
    assert reused_request_id.error.code == wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS
    assert smtp.connections == 1
    assert len(smtp.message_data) == 1


@pytest.mark.parametrize(
    ("claim_name", "claim_value"),
    [
        ("binding_id", "other-binding"),
        ("caller_workload", "other-workload"),
        ("audience", "other-audience"),
        ("organization_id", "other-organization"),
        ("workspace_id", "other-workspace"),
        ("issuer", "other-issuer"),
        ("subject", "other-subject"),
        ("key", b"x" * 32),
    ],
    ids=[
        "binding",
        "workload",
        "audience",
        "organization",
        "workspace",
        "issuer",
        "actor",
        "hmac-key",
    ],
)
def test_caller_scope_mismatch_is_rejected_before_smtp(
    settings_factory, claim_name: str, claim_value: object
) -> None:
    """Reject a valid or wrong-key signature outside this binding's caller scope."""

    with _SMTPFixtureServer() as smtp:
        settings = settings_factory(smtp.port)
        provider = EmailNotificationProvider(settings=settings)
        arguments = {
            "recipient_ref": "operator",
            "subject": "Scope check",
            "text": "This should not reach SMTP.",
            "request_ref": "notification-scope",
        }
        envelope = _signed_envelope(
            settings,
            tool_id=SEND_TOOL_ID,
            arguments=arguments,
            **{claim_name: claim_value},
        )
        response = _invoke(provider, tool_id=SEND_TOOL_ID, envelope=envelope)
        assert response.HasField("error")
        assert response.error.retryable is False
        assert smtp.connections == 0


def test_unlisted_address_and_header_injection_are_rejected(settings_factory) -> None:
    """Reject caller-selected mailboxes and subjects before opening SMTP."""

    with _SMTPFixtureServer() as smtp:
        settings = settings_factory(smtp.port)
        provider = EmailNotificationProvider(settings=settings)
        arguments = {
            "recipient_ref": "target@example.test",
            "subject": "hello",
            "text": "No arbitrary recipients.",
            "request_ref": "notification-address",
        }
        response = _invoke(
            provider,
            tool_id=SEND_TOOL_ID,
            envelope=_signed_envelope(settings, tool_id=SEND_TOOL_ID, arguments=arguments),
        )
        assert response.HasField("error")
        assert response.error.code == wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS

        arguments["recipient_ref"] = "operator"
        arguments["subject"] = "hello\r\nBcc: target@example.test"
        response = _invoke(
            provider,
            tool_id=SEND_TOOL_ID,
            envelope=_signed_envelope(settings, tool_id=SEND_TOOL_ID, arguments=arguments),
        )
        assert response.HasField("error")
        assert response.error.code == wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS
        assert smtp.connections == 0


def test_expired_context_and_unknown_fields_fail_closed(settings_factory, monkeypatch) -> None:
    """Reject expired signed calls and strict-envelope extensions before SMTP."""

    with _SMTPFixtureServer() as smtp:
        settings = settings_factory(smtp.port)
        provider = EmailNotificationProvider(settings=settings)
        arguments = {
            "recipient_ref": "operator",
            "subject": "Expired request",
            "text": "No SMTP session should start.",
            "request_ref": "notification-expired",
        }
        expired = _signed_envelope(
            settings,
            tool_id=SEND_TOOL_ID,
            arguments=arguments,
        )
        expired_context = parse_trusted_caller_context(expired["caller_context"])

        def verify_after_expiration(**kwargs: Any) -> bool:
            """Use an explicit UTC verifier time beyond the signed expiry."""

            return verify_context(
                **kwargs,
                now=expired_context.expires_at + timedelta(seconds=1),
            )

        with monkeypatch.context() as verifier_clock:
            verifier_clock.setattr(email_plugin, "verify_context", verify_after_expiration)
            response = _invoke(provider, tool_id=SEND_TOOL_ID, envelope=expired)
        assert response.HasField("error")
        assert response.error.retryable is False

        malformed = _signed_envelope(settings, tool_id=SEND_TOOL_ID, arguments=arguments)
        malformed["extra"] = "not allowed"
        response = _invoke(provider, tool_id=SEND_TOOL_ID, envelope=malformed)
        assert response.HasField("error")
        assert response.error.code == wire.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS
        assert smtp.connections == 0


def test_health_checks_connection_without_sending(settings_factory) -> None:
    """Use the signed health tool to inspect SMTP without sending a message."""

    with _SMTPFixtureServer() as smtp:
        settings = settings_factory(smtp.port)
        provider = EmailNotificationProvider(settings=settings)
        tool_id = "email_connection_health"
        arguments: dict[str, Any] = {}
        response = _invoke(
            provider,
            tool_id=tool_id,
            envelope=_signed_envelope(settings, tool_id=tool_id, arguments=arguments),
        )

    assert response.HasField("outcome")
    health = json.loads(response.outcome.content[0].json.json)
    assert health["status"] == "AVAILABLE"
    assert health["security_mode"] == "LOOPBACK_TEST_PLAIN"
    assert health["authentication"] == "NOT_CONFIGURED"
    assert smtp.message_data == []


def test_starttls_binding_fails_closed_when_server_does_not_offer_tls(settings_factory) -> None:
    """Refuse an SMTP server without STARTTLS and report only safe health facts."""

    with _SMTPFixtureServer() as smtp:
        settings = settings_factory(
            smtp.port,
            tls_mode="starttls",
            allow_loopback_test_transport=False,
        )
        provider = EmailNotificationProvider(settings=settings)
        tool_id = "email_connection_health"
        response = _invoke(
            provider,
            tool_id=tool_id,
            envelope=_signed_envelope(settings, tool_id=tool_id, arguments={}),
        )

    health = json.loads(response.outcome.content[0].json.json)
    assert health["status"] == "UNAVAILABLE"
    assert health["security_mode"] == "TLS"
    assert smtp.message_data == []


def test_plain_transport_is_rejected_for_non_loopback_hosts(settings_factory) -> None:
    """Require the explicit plain test mode to target a loopback-only endpoint."""

    with pytest.raises(ConfigurationError, match="loopback"):
        settings_factory(25, smtp_host="mail.example.test")
