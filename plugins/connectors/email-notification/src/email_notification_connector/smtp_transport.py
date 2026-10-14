"""
┌──────────────────────────────────────────────────────────────────────┐
│  smtp_transport.py                                                   │
│  Module: email_notification_connector.smtp_transport                │
│  Role: Send one message through the binding-owned SMTP endpoint.      │
│                                                                      │
│  模块职责：通过绑定持有的 SMTP endpoint 发送一封邮件。                   │
│  · DATA 前失败可安全重试  · DATA 后断连必须保留 UNKNOWN                 │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP

from cyrene_integration_contracts import EmailSubmission, EmailSubmissionStatus

from .settings import ProviderSettings

logger = logging.getLogger(__name__)


class SMTPPreSendFailure(RuntimeError):
    """A sanitized error raised before SMTP begins message content transfer."""

    def __init__(self, *, retryable: bool, code: str) -> None:
        super().__init__("SMTP setup failed before message content was sent")
        self.retryable = retryable
        self.code = code


@dataclass(frozen=True, slots=True)
class SMTPHealth:
    """Safe connection diagnostics that omit endpoint and credential values."""

    available: bool
    checked_at: datetime
    security_mode: str
    authentication: str


class SMTPTransport:
    """Use stdlib SMTP with TLS and explicit handling of the acceptance boundary."""

    def __init__(self, settings: ProviderSettings) -> None:
        self._settings = settings
        self._tls_context = ssl.create_default_context()

    def check_connection(self) -> SMTPHealth:
        """Perform an authenticated SMTP greeting check without sending mail."""

        session: smtplib.SMTP | None = None
        available = False
        try:
            session = self._open_session()
            available = True
        except SMTPPreSendFailure:
            available = False
        finally:
            if session is not None:
                _close_session(session)
        return SMTPHealth(
            available=available,
            checked_at=datetime.now(UTC),
            security_mode=(
                "LOOPBACK_TEST_PLAIN" if self._settings.tls_mode == "loopback_test_plain" else "TLS"
            ),
            authentication=(
                "CONFIGURED" if self._settings.smtp_username is not None else "NOT_CONFIGURED"
            ),
        )

    def submit(
        self,
        *,
        recipient: str,
        subject: str,
        text: str,
        request_ref: str,
    ) -> EmailSubmission:
        """Submit one message and preserve explicit versus ambiguous outcomes.

        Args:
            recipient: Mailbox selected from the configured recipient allowlist.
            subject: Validated single-line subject.
            text: Validated plain-text body.
            request_ref: Opaque Exchange request reference returned to its caller.
        Returns:
            A typed submission result; `ACCEPTED` requires a final SMTP 250 reply.
        Raises:
            SMTPPreSendFailure: If setup or connection fails before `DATA` begins.
        """

        message = _build_message(
            sender=self._settings.sender,
            recipient=recipient,
            subject=subject,
            text=text,
        )
        wire_data = _smtp_data(message.as_bytes(policy=SMTP))
        session = self._open_session()
        data_started = False
        try:
            code, _ = session.mail(self._settings.sender)
            if not _is_success(code):
                _raise_temporary_pre_send(code)
                return _submission(request_ref, EmailSubmissionStatus.REJECTED)

            code, _ = session.rcpt(recipient)
            if code not in (250, 251, 252):
                _raise_temporary_pre_send(code)
                return _submission(request_ref, EmailSubmissionStatus.REJECTED)

            code, _ = session.docmd("DATA")
            if code != 354:
                _raise_temporary_pre_send(code)
                return _submission(request_ref, EmailSubmissionStatus.REJECTED)

            # SMTP acceptance is unknown as soon as message content may have left
            # this process; never retry this phase after a timeout or disconnect.
            data_started = True
            try:
                session.send(wire_data)
                code, _ = session.getreply()
            except (OSError, smtplib.SMTPException, TimeoutError):
                return _submission(request_ref, EmailSubmissionStatus.UNKNOWN)

            status = (
                EmailSubmissionStatus.ACCEPTED if code == 250 else EmailSubmissionStatus.REJECTED
            )
            return _submission(request_ref, status)
        except SMTPPreSendFailure:
            raise
        except ssl.SSLError as error:
            if data_started:
                return _submission(request_ref, EmailSubmissionStatus.UNKNOWN)
            raise SMTPPreSendFailure(retryable=False, code="SMTP_TLS_FAILED") from error
        except (OSError, smtplib.SMTPException, TimeoutError) as error:
            if data_started:
                return _submission(request_ref, EmailSubmissionStatus.UNKNOWN)
            retryable = _is_retryable_setup_error(error)
            raise SMTPPreSendFailure(
                retryable=retryable,
                code="SMTP_UNAVAILABLE" if retryable else "SMTP_SETUP_FAILED",
            ) from error
        finally:
            _close_session(session)

    def _open_session(self) -> smtplib.SMTP:
        """Open, greet, secure and optionally authenticate one SMTP session."""

        session: smtplib.SMTP | None = None
        try:
            if self._settings.tls_mode == "implicit":
                session = smtplib.SMTP_SSL(
                    self._settings.smtp_host,
                    self._settings.smtp_port,
                    timeout=self._settings.timeout_seconds,
                    context=self._tls_context,
                )
            else:
                session = smtplib.SMTP(
                    self._settings.smtp_host,
                    self._settings.smtp_port,
                    timeout=self._settings.timeout_seconds,
                )

            code, _ = session.ehlo()
            if code != 250:
                raise SMTPPreSendFailure(retryable=400 <= code < 500, code="SMTP_GREETING_FAILED")
            if self._settings.tls_mode == "starttls":
                if not session.has_extn("starttls"):
                    raise SMTPPreSendFailure(retryable=False, code="SMTP_STARTTLS_REQUIRED")
                session.starttls(context=self._tls_context)
                code, _ = session.ehlo()
                if code != 250:
                    raise SMTPPreSendFailure(
                        retryable=400 <= code < 500, code="SMTP_GREETING_FAILED"
                    )
            if self._settings.smtp_username is not None:
                session.login(self._settings.smtp_username, self._settings.smtp_password or "")
            return session
        except SMTPPreSendFailure:
            if session is not None:
                _close_session(session)
            raise
        except smtplib.SMTPAuthenticationError as error:
            if session is not None:
                _close_session(session)
            raise SMTPPreSendFailure(retryable=False, code="SMTP_AUTHENTICATION_FAILED") from error
        except ssl.SSLError as error:
            if session is not None:
                _close_session(session)
            raise SMTPPreSendFailure(retryable=False, code="SMTP_TLS_FAILED") from error
        except smtplib.SMTPResponseException as error:
            if session is not None:
                _close_session(session)
            raise SMTPPreSendFailure(
                retryable=400 <= error.smtp_code < 500,
                code="SMTP_GREETING_FAILED",
            ) from error
        except (OSError, TimeoutError, smtplib.SMTPServerDisconnected) as error:
            if session is not None:
                _close_session(session)
            raise SMTPPreSendFailure(retryable=True, code="SMTP_UNAVAILABLE") from error
        except smtplib.SMTPException as error:
            if session is not None:
                _close_session(session)
            raise SMTPPreSendFailure(retryable=False, code="SMTP_SETUP_FAILED") from error


def _build_message(*, sender: str, recipient: str, subject: str, text: str) -> EmailMessage:
    """Construct a plain-text message from validated binding and caller values."""

    message = EmailMessage(policy=SMTP)
    message["From"] = Address(addr_spec=sender)
    message["To"] = Address(addr_spec=recipient)
    message["Subject"] = subject
    message.set_content(text, subtype="plain", charset="utf-8")
    return message


def _smtp_data(message: bytes) -> bytes:
    """Dot-stuff the serialized RFC 5322 message and append SMTP termination."""

    normalized = message.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if normalized.endswith(b"\n"):
        normalized = normalized[:-1]
    lines = normalized.split(b"\n")
    stuffed = [b"." + line if line.startswith(b".") else line for line in lines]
    return b"\r\n".join(stuffed) + b"\r\n.\r\n"


def _submission(request_ref: str, status: EmailSubmissionStatus) -> EmailSubmission:
    """Create an honest result without inventing a vendor receipt or delivery."""

    return EmailSubmission(
        request_ref=request_ref,
        status=status,
        vendor_receipt_ref=None,
        delivery_confirmed=False,
    )


def _is_success(code: int) -> bool:
    """Return whether an SMTP command has a successful two-hundred response."""

    return 200 <= code < 300


def _close_session(session: smtplib.SMTP) -> None:
    """Close one socket without leaking close-error text over the tool boundary."""

    try:
        session.close()
    except OSError:
        # SMTP acceptance is decided by the protocol reply; close errors expose no new fact.
        logger.debug("SMTP session close failed")


def _raise_temporary_pre_send(code: int) -> None:
    """Surface an explicit temporary SMTP refusal as a safe retry outcome."""

    if 400 <= code < 500:
        raise SMTPPreSendFailure(retryable=True, code="SMTP_TEMPORARY_REJECTION")


def _is_retryable_setup_error(error: BaseException) -> bool:
    """Identify connection loss or temporary setup refusal before message data."""

    if isinstance(error, smtplib.SMTPResponseException):
        return 400 <= error.smtp_code < 500
    return isinstance(
        error,
        (socket.timeout, TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected),
    )
