"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 authorization.py                                                │
│  Package: wecom_connector                                           │
│  Role: Signed WeCom authorization health and renewal sessions.      │
│                                                                     │
│  模块职责：企业微信 CLI 授权健康检查与续期会话。                       │
│  · 用固定只读探针确认单项授权能力                                      │
│  · 在 binding 私有目录持久化 actor-owned 会话和 generation           │
│  · 仅接受官方来源的扫码链接；不读取或搬运 CLI 凭据                     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import stat
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .cli import WeComCliClient, WeComCliError, WeComCliStatus

CAPABILITY_ID = "wecom.message.aibot.sessions.list"
SESSION_TTL_SECONDS = 300
MAX_URL_LENGTH = 2048
_VENDOR_QR_HOST = "work.weixin.qq.com"
_VENDOR_QR_PATH = "/ai/qc/gen"
_VENDOR_QR_SOURCE = "wecom_cli_external"
_URL_PATTERN = re.compile(r"https://[^\s<>\"']{1,2048}")
_VENDOR_CODE_PATTERN = re.compile(r"(?:errcode|code)\s*[:=]\s*(-?\d+)", re.I)


class AuthorizationSessionError(RuntimeError):
    """Raised when the fixed authorization operation cannot safely proceed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    """One read-only CLI probe result with no tenant response body retained."""

    state: str
    reason_code: str | None
    last_verified_at: datetime | None
    authorization_url: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorizationTrustConfig:
    """Binding-owned expectations and HMAC key for signed tool callers."""

    authorization_ref: str
    key: bytes | None
    caller_workload: str
    audience: str
    issuer: str
    organization_id: str
    workspace_id: str
    operator_subjects: tuple[str, ...]


class _PrivateSessionStore:
    """Store renewal state as private, atomically replaced JSON next to the profile."""

    def __init__(self, profile_dir: str | Path, binding_id: str) -> None:
        profile = Path(profile_dir).expanduser().resolve()
        binding_key = hashlib.sha256(binding_id.encode("utf-8")).hexdigest()[:24]
        self._directory = profile / ".cyrene" / "authorization" / binding_key
        self._state_path = self._directory / "sessions.json"
        self._lock_path = self._directory / "sessions.lock"
        self._thread_lock = threading.RLock()
        self._ensure_private_directory()

    def generation(self) -> int:
        with self._locked_state() as state:
            return int(state["generation"])

    def bound_identity_fingerprint(self) -> str | None:
        with self._locked_state() as state:
            value = state.get("profile_identity_fingerprint")
            return value if isinstance(value, str) and value else None

    def bind_identity_if_empty(self, identity_fingerprint: str) -> bool:
        with self._locked_state() as state:
            current = state.get("profile_identity_fingerprint")
            if current is None:
                state["profile_identity_fingerprint"] = identity_fingerprint
                return True
            return isinstance(current, str) and secrets.compare_digest(
                current, identity_fingerprint
            )

    def active_sessions(self, owner_fingerprint: str) -> tuple[str, ...]:
        with self._locked_state() as state:
            return tuple(
                item["session_ref"]
                for item in state["sessions"]
                if item["state"] in {"WAITING_USER", "VERIFYING"}
                and item["owner_fingerprint"] == owner_fingerprint
            )

    def has_untracked_activation(
        self, tracked_session_refs: set[str] | frozenset[str]
    ) -> bool:
        with self._locked_state() as state:
            return any(
                item["kind"] == "activation"
                and item["state"] in {"WAITING_USER", "VERIFYING"}
                and item.get("activation_started") is True
                and item["session_ref"] not in tracked_session_refs
                for item in state["sessions"]
            )

    def find_idempotent_session(
        self,
        idempotency_key: str,
        owner_fingerprint: str,
        payload_hash: str,
        request_alias: str,
    ) -> dict[str, Any] | None:
        with self._locked_state() as state:
            for item in state["sessions"]:
                if item.get("idempotency_key") != idempotency_key:
                    continue
                if (
                    not secrets.compare_digest(
                        item.get("owner_fingerprint", ""), owner_fingerprint
                    )
                    or not secrets.compare_digest(
                        item.get("payload_hash", ""), payload_hash
                    )
                    or not secrets.compare_digest(
                        item.get("start_request_id", ""), request_alias
                    )
                ):
                    raise AuthorizationSessionError("idempotency_conflict")
                return dict(item)
        return None

    def create_session(
        self,
        *,
        idempotency_key: str,
        payload_hash: str,
        request_alias: str,
        owner_fingerprint: str,
        kind: str,
        created_at: datetime,
        expires_at: datetime,
        identity_fingerprint: str | None,
        authorization_url: str | None,
    ) -> tuple[dict[str, Any], bool]:
        session_ref = str(uuid.uuid4())
        created_text = _format_time(created_at)
        expires_text = _format_time(expires_at)
        with self._locked_state() as state:
            for item in state["sessions"]:
                if item.get("idempotency_key") != idempotency_key:
                    continue
                if (
                    not secrets.compare_digest(
                        item.get("owner_fingerprint", ""), owner_fingerprint
                    )
                    or not secrets.compare_digest(
                        item.get("payload_hash", ""), payload_hash
                    )
                    or not secrets.compare_digest(
                        item.get("start_request_id", ""), request_alias
                    )
                ):
                    raise AuthorizationSessionError("idempotency_conflict")
                return dict(item), False
            active = [
                item
                for item in state["sessions"]
                if item["state"] in {"WAITING_USER", "VERIFYING"}
            ]
            if any(item["owner_fingerprint"] != owner_fingerprint for item in active):
                raise AuthorizationSessionError("session_busy")
            for item in active:
                item["state"] = "CANCELLED"
                item["authorization_url"] = None
            generation = int(state["generation"])
            record = {
                "session_ref": session_ref,
                "idempotency_key": idempotency_key,
                "payload_hash": payload_hash,
                "start_request_id": request_alias,
                "owner_fingerprint": owner_fingerprint,
                "state": "WAITING_USER" if authorization_url else "VERIFYING",
                "kind": kind,
                "created_at": created_text,
                "expires_at": expires_text,
                "authorization_url": authorization_url,
                "identity_fingerprint": identity_fingerprint,
                "generation": generation,
                "activation_started": kind == "activation",
            }
            state["sessions"].append(record)
            return dict(record), True

    def get_session(
        self, session_ref: str, owner_fingerprint: str
    ) -> dict[str, Any] | None:
        with self._locked_state() as state:
            for item in state["sessions"]:
                if item["session_ref"] == session_ref and secrets.compare_digest(
                    item["owner_fingerprint"], owner_fingerprint
                ):
                    return dict(item)
        return None

    def get_session_by_request_alias(
        self, request_alias: str, owner_fingerprint: str
    ) -> dict[str, Any] | None:
        with self._locked_state() as state:
            for item in state["sessions"]:
                if secrets.compare_digest(
                    item.get("start_request_id", ""), request_alias
                ) and secrets.compare_digest(
                    item.get("owner_fingerprint", ""), owner_fingerprint
                ):
                    return dict(item)
        return None

    def update_session(
        self,
        session_ref: str,
        *,
        state_value: str,
        authorization_url: str | None | object = ...,
        generation: int | None = None,
    ) -> dict[str, Any] | None:
        with self._locked_state() as state:
            for item in state["sessions"]:
                if item["session_ref"] != session_ref:
                    continue
                item["state"] = state_value
                if authorization_url is not ...:
                    item["authorization_url"] = authorization_url
                if generation is not None:
                    item["generation"] = generation
                return dict(item)
        return None

    def transition_active_session(
        self,
        session_ref: str,
        *,
        state_value: str,
        clear_authorization_url: bool,
    ) -> dict[str, Any] | None:
        with self._locked_state() as state:
            for item in state["sessions"]:
                if item["session_ref"] != session_ref:
                    continue
                if item["state"] not in {"WAITING_USER", "VERIFYING"}:
                    return dict(item)
                item["state"] = state_value
                if clear_authorization_url:
                    item["authorization_url"] = None
                return dict(item)
        return None

    def cancel_active_sessions(self) -> None:
        with self._locked_state() as state:
            for item in state["sessions"]:
                if item["state"] in {"WAITING_USER", "VERIFYING"}:
                    item["state"] = "CANCELLED"
                    item["authorization_url"] = None

    def advance_generation(
        self, expected: int, identity_fingerprint: str
    ) -> int | None:
        with self._locked_state() as state:
            current = int(state["generation"])
            if current != expected:
                return None
            bound_identity = state.get("profile_identity_fingerprint")
            if bound_identity is not None and (
                not isinstance(bound_identity, str)
                or not secrets.compare_digest(bound_identity, identity_fingerprint)
            ):
                return None
            state["profile_identity_fingerprint"] = identity_fingerprint
            current += 1
            state["generation"] = current
            return current

    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        with self._thread_lock:
            fd = self._open_lock_file()
            try:
                _lock_file(fd)
                state = self._read_state()
                try:
                    yield state
                except Exception:
                    raise
                else:
                    self._write_state(state)
            finally:
                _unlock_file(fd)
                os.close(fd)

    def _ensure_private_directory(self) -> None:
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile = self._directory.parents[2]
        for directory in (
            profile / ".cyrene",
            profile / ".cyrene" / "authorization",
            self._directory,
        ):
            details = directory.lstat()
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise AuthorizationSessionError("private_store_unavailable")
            os.chmod(directory, 0o700)

    def _open_lock_file(self) -> int:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self._lock_path, flags, 0o600)
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                os.close(fd)
                raise AuthorizationSessionError("private_store_unavailable")
            os.fchmod(fd, 0o600)
            return fd
        except OSError as exc:
            raise AuthorizationSessionError("private_store_unavailable") from exc

    def _read_state(self) -> dict[str, Any]:
        try:
            fd = os.open(self._state_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {"schema_version": 1, "generation": 0, "sessions": []}
        except OSError as exc:
            raise AuthorizationSessionError("private_store_unavailable") from exc
        try:
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise AuthorizationSessionError("private_store_unavailable")
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as source:
                state = json.load(source)
        except (OSError, ValueError, TypeError) as exc:
            raise AuthorizationSessionError("private_store_unavailable") from exc
        finally:
            os.close(fd)
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != 1
            or not isinstance(state.get("generation"), int)
            or isinstance(state.get("generation"), bool)
            or state["generation"] < 0
            or not isinstance(state.get("sessions"), list)
        ):
            raise AuthorizationSessionError("private_store_unavailable")
        return state

    def _write_state(self, state: Mapping[str, Any]) -> None:
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._directory,
                prefix=".sessions-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temp_name = temporary.name
                os.fchmod(temporary.fileno(), 0o600)
                json.dump(state, temporary, ensure_ascii=True, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temp_name, self._state_path)
            temp_name = None
            directory_fd = os.open(self._directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise AuthorizationSessionError("private_store_unavailable") from exc
        finally:
            if temp_name:
                with suppress(OSError):
                    os.unlink(temp_name)


class WeComAuthorizationSessions:
    """Expose bounded health evidence and actor-owned renewal lifecycle methods."""

    def __init__(
        self,
        binding_id: str,
        cli_client: WeComCliClient,
        *,
        cli_enabled: bool,
        profile_dir: str | Path | None,
        authorization_ref: str | None = None,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        self._binding_id = binding_id
        self._cli = cli_client
        self._cli_enabled = cli_enabled
        self._authorization_ref = authorization_ref or (
            "wecom-cli:" + hashlib.sha256(binding_id.encode("utf-8")).hexdigest()[:24]
        )
        self._store = (
            _PrivateSessionStore(profile_dir, binding_id)
            if cli_enabled and profile_dir
            else None
        )
        self._session_ttl_seconds = session_ttl_seconds
        self._lock = threading.RLock()
        self._processes: dict[str, Any] = {}
        self._closed = False

    @property
    def authorization_ref(self) -> str:
        return self._authorization_ref

    def health(self) -> dict[str, Any]:
        """Return one bounded read-only capability observation."""
        now = _utc_now()
        if not self._cli_enabled or self._store is None:
            login_state = "UNAVAILABLE"
            auth_state = "UNAVAILABLE"
            evidence = CapabilityEvidence("UNAVAILABLE", "cli_disabled", None)
            generation = 0
        else:
            try:
                generation = self._store.generation()
            except AuthorizationSessionError:
                generation = 0
                login_state = "UNKNOWN"
                auth_state = "UNAVAILABLE"
                evidence = CapabilityEvidence(
                    "UNAVAILABLE", "private_store_unavailable", None
                )
            else:
                status = self._cli.get_status()
                login_state, auth_state, evidence = self._observe_status(status, now)

        return {
            "schema_version": 1,
            "authorization_ref": self._authorization_ref,
            "login_state": login_state,
            "authorization_state": auth_state,
            "checked_at": now,
            "expires_at": None,
            "generation": generation,
            "capabilities": [
                {
                    "capability_id": CAPABILITY_ID,
                    "state": evidence.state,
                    "last_verified_at": evidence.last_verified_at,
                    "expires_at": None,
                    "reason_code": evidence.reason_code,
                }
            ],
        }

    def start(
        self,
        owner_fingerprint: str,
        *,
        idempotency_key: str,
        payload_hash: str,
        request_alias: str,
    ) -> dict[str, Any]:
        """Start a bounded official CLI onboarding flow or a vendor grant session."""
        self._require_available()
        if self._closed:
            raise AuthorizationSessionError("provider_closed")
        if (
            not isinstance(request_alias, str)
            or not request_alias
            or len(request_alias) > 200
        ):
            raise AuthorizationSessionError("invalid_request")

        existing = self._store.find_idempotent_session(
            idempotency_key, owner_fingerprint, payload_hash, request_alias
        )
        if existing is not None:
            return self._poll_session(existing, owner_fingerprint, _utc_now())
        if self._store.has_untracked_activation(self._tracked_process_refs()):
            raise AuthorizationSessionError("authorization_start_ambiguous")

        status = self._cli.get_status()
        now = _utc_now()
        identity_fingerprint: str | None = None
        kind: str
        url: str | None = None
        if not status.available:
            raise AuthorizationSessionError("cli_unavailable")
        if not _supported_cli_version(status.version):
            raise AuthorizationSessionError("cli_version_unsupported")
        if status.auth_status == "unauthorized":
            if self._store.bound_identity_fingerprint() is not None:
                raise AuthorizationSessionError("profile_identity_changed")
            kind = "activation"
        elif status.auth_status == "authorized":
            try:
                identity_fingerprint = self._cli.get_profile_identity_fingerprint(
                    self._binding_id
                )
            except WeComCliError as exc:
                raise AuthorizationSessionError("profile_identity_unknown") from exc
            evidence = self._probe_scope()
            if evidence.state == "VALID":
                raise AuthorizationSessionError("authorization_already_valid")
            if evidence.state not in {"PERMISSION_DENIED", "REAUTHORIZATION_REQUIRED"}:
                raise AuthorizationSessionError("renewal_not_confirmed")
            if not evidence.authorization_url:
                raise AuthorizationSessionError("official_grant_url_unavailable")
            if not self._store.bind_identity_if_empty(identity_fingerprint):
                raise AuthorizationSessionError("profile_identity_changed")
            kind = "grant"
            url = evidence.authorization_url
        else:
            raise AuthorizationSessionError("login_state_unknown")

        expires_at = now + timedelta(seconds=self._session_ttl_seconds)
        for active_ref in self._store.active_sessions(owner_fingerprint):
            self._cancel_process(active_ref)
            self._store.update_session(
                active_ref, state_value="CANCELLED", authorization_url=None
            )
        record, created = self._store.create_session(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            request_alias=request_alias,
            owner_fingerprint=owner_fingerprint,
            kind=kind,
            created_at=now,
            expires_at=expires_at,
            identity_fingerprint=identity_fingerprint,
            authorization_url=url,
        )
        if not created:
            return self._poll_session(record, owner_fingerprint, _utc_now())
        if kind == "activation":
            try:
                process = self._cli.start_auth_init()
            except WeComCliError:
                return self._update_session(record, "FAILED", url=None)
            with self._lock:
                self._processes[record["session_ref"]] = process
            self._start_activation_watcher(
                record["session_ref"],
                process,
                _parse_time(str(record["expires_at"])),
            )
            self._collect_process_url(record["session_ref"], process, wait_seconds=2.0)
        return self._poll_session(record, owner_fingerprint, now)

    def status(self, owner_fingerprint: str, session_ref: str) -> dict[str, Any]:
        """Poll one session after the caller has been freshly authenticated."""
        self._require_available()
        if self._closed:
            raise AuthorizationSessionError("provider_closed")
        if (
            not isinstance(session_ref, str)
            or not session_ref
            or len(session_ref) > 200
        ):
            raise AuthorizationSessionError("session_not_found")
        request_alias = session_ref
        try:
            canonical_ref = str(uuid.UUID(session_ref))
        except ValueError:
            canonical_ref = None
        record = (
            self._store.get_session(canonical_ref, owner_fingerprint)
            if canonical_ref is not None
            else None
        )
        if record is None:
            record = self._store.get_session_by_request_alias(
                request_alias, owner_fingerprint
            )
        if (
            record is None
            and canonical_ref is not None
            and request_alias != canonical_ref
        ):
            record = self._store.get_session_by_request_alias(
                canonical_ref, owner_fingerprint
            )
        if record is None:
            raise AuthorizationSessionError("session_not_found")
        return self._poll_session(record, owner_fingerprint, _utc_now())

    def close(self) -> None:
        """Cancel child CLI processes owned by this provider instance."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            processes = tuple(self._processes.items())
            self._processes.clear()
        for _session_ref, process in processes:
            _terminate_process(process)
        if self._store is not None:
            self._store.cancel_active_sessions()

    def _observe_status(
        self, status: WeComCliStatus, now: datetime
    ) -> tuple[str, str, CapabilityEvidence]:
        if not status.available:
            return (
                "UNAVAILABLE",
                "UNAVAILABLE",
                CapabilityEvidence("UNAVAILABLE", "cli_unavailable", None),
            )
        if not _supported_cli_version(status.version):
            return (
                "UNKNOWN",
                "UNAVAILABLE",
                CapabilityEvidence("UNAVAILABLE", "cli_version_unsupported", None),
            )
        if status.auth_status == "unauthorized":
            if self._store.has_untracked_activation(self._tracked_process_refs()):
                evidence = CapabilityEvidence(
                    "UNKNOWN", "authorization_start_ambiguous", None
                )
                return "MISSING", "UNKNOWN", evidence
            evidence = CapabilityEvidence(
                "REAUTHORIZATION_REQUIRED", "login_missing", None
            )
            return "MISSING", "REAUTHORIZATION_REQUIRED", evidence
        if status.auth_status != "authorized":
            evidence = CapabilityEvidence("UNKNOWN", "login_status_unknown", None)
            return "UNKNOWN", "UNKNOWN", evidence
        try:
            identity_fingerprint = self._cli.get_profile_identity_fingerprint(
                self._binding_id
            )
        except WeComCliError:
            evidence = CapabilityEvidence("UNKNOWN", "profile_identity_unknown", None)
            return "PRESENT", "UNKNOWN", evidence
        bound_identity = self._store.bound_identity_fingerprint()
        if bound_identity and not secrets.compare_digest(
            bound_identity, identity_fingerprint
        ):
            evidence = CapabilityEvidence(
                "REAUTHORIZATION_REQUIRED", "profile_identity_changed", None
            )
            return "PRESENT", "REAUTHORIZATION_REQUIRED", evidence
        evidence = self._probe_scope()
        if evidence.state != "UNKNOWN" and not self._store.bind_identity_if_empty(
            identity_fingerprint
        ):
            identity_evidence = CapabilityEvidence(
                "REAUTHORIZATION_REQUIRED", "profile_identity_changed", None
            )
            return "PRESENT", "REAUTHORIZATION_REQUIRED", identity_evidence
        return "PRESENT", evidence.state, evidence

    def _probe_scope(self) -> CapabilityEvidence:
        now = _utc_now()
        try:
            result = self._cli.execute("message", args=["aibot", "sessions", "list"])
        except WeComCliError as exc:
            code, message = _vendor_error_details(exc)
            if code == 850001:
                return CapabilityEvidence(
                    "PERMISSION_DENIED",
                    "permission_denied",
                    now,
                    profile_authorization_url(exc.stdout, exc.stderr),
                )
            if code == 850003 and message == "authorization expired":
                return CapabilityEvidence(
                    "REAUTHORIZATION_REQUIRED",
                    "authorization_expired",
                    now,
                    profile_authorization_url(exc.stdout, exc.stderr),
                )
            if code == 853004:
                return CapabilityEvidence(
                    "REAUTHORIZATION_REQUIRED",
                    "cli_token_expired",
                    now,
                    profile_authorization_url(exc.stdout, exc.stderr),
                )
            return CapabilityEvidence("UNKNOWN", "probe_result_unknown", None)
        if not _is_successful_observation(result):
            return CapabilityEvidence("UNKNOWN", "probe_result_unknown", None)
        return CapabilityEvidence("VALID", None, now)

    def _poll_session(
        self,
        record: Mapping[str, Any],
        owner_fingerprint: str,
        now: datetime,
    ) -> dict[str, Any]:
        session_ref = str(record["session_ref"])
        state_value = str(record["state"])
        if state_value not in {"WAITING_USER", "VERIFYING"}:
            return dict(record)
        expires_at = _parse_time(str(record["expires_at"]))
        if now >= expires_at:
            self._cancel_process(session_ref)
            return self._update_session(record, "EXPIRED", url=None)

        process = self._get_process(session_ref)
        if process is not None:
            self._collect_process_url(session_ref, process)
            return_code = process.poll()
            if return_code is not None and return_code != 0:
                return self._update_session(record, "FAILED", url=None)
            if return_code is None:
                current = self._store.get_session(session_ref, owner_fingerprint)
                if current is not None:
                    current["state"] = (
                        "WAITING_USER"
                        if current.get("authorization_url")
                        else "VERIFYING"
                    )
                return current or dict(record)

        status = self._cli.get_status()
        if not status.available or status.auth_status != "authorized":
            if (
                process is None
                and record.get("kind") == "activation"
                and status.auth_status == "unauthorized"
            ):
                return self._store.update_session(
                    session_ref,
                    state_value="VERIFYING",
                    authorization_url=record.get("authorization_url"),
                ) or dict(record)
            if status.auth_status == "unauthorized" and process is not None:
                return self._update_session(record, "FAILED", url=None)
            return dict(record)
        if not _supported_cli_version(status.version):
            return self._update_session(record, "FAILED", url=None)
        try:
            identity_fingerprint = self._cli.get_profile_identity_fingerprint(
                self._binding_id
            )
        except WeComCliError:
            return self._update_session(record, "FAILED", url=None)

        baseline = record.get("identity_fingerprint")
        if baseline and not secrets.compare_digest(baseline, identity_fingerprint):
            self._cancel_process(session_ref)
            return self._update_session(record, "FAILED", url=None)

        evidence = self._probe_scope()
        if evidence.state != "VALID":
            current = self._store.get_session(session_ref, owner_fingerprint)
            if current is None:
                raise AuthorizationSessionError("session_not_found")
            current["state"] = (
                "WAITING_USER" if evidence.authorization_url else "VERIFYING"
            )
            if evidence.authorization_url:
                current["authorization_url"] = evidence.authorization_url
                return (
                    self._store.update_session(
                        session_ref,
                        state_value="WAITING_USER",
                        authorization_url=evidence.authorization_url,
                    )
                    or current
                )
            self._store.update_session(session_ref, state_value=str(current["state"]))
            return current

        expected_generation = int(record["generation"])
        generation = self._store.advance_generation(
            expected_generation, identity_fingerprint
        )
        if generation is None:
            self._cancel_process(session_ref)
            return self._update_session(record, "FAILED", url=None)
        result = self._store.update_session(
            session_ref,
            state_value="SUCCEEDED",
            authorization_url=None,
            generation=generation,
        )
        self._cancel_process(session_ref)
        return result or dict(record)

    def _collect_process_url(
        self, session_ref: str, process: Any, *, wait_seconds: float = 0
    ) -> None:
        reader = getattr(process, "authorization_events", None)
        if reader is None:
            return
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                if wait_seconds > 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    event, value = reader.get(timeout=remaining)
                else:
                    event, value = reader.get_nowait()
            except queue.Empty:
                break
            if event != "line" or not value:
                continue
            url = profile_authorization_url(value)
            if url:
                self._store.update_session(
                    session_ref,
                    state_value="WAITING_USER",
                    authorization_url=url,
                )
            elif "https://" in value.lower():
                self._cancel_process(session_ref)
                self._store.update_session(
                    session_ref,
                    state_value="FAILED",
                    authorization_url=None,
                )

    def _cancel_process(self, session_ref: str) -> None:
        with self._lock:
            process = self._processes.pop(session_ref, None)
        if process is not None:
            _terminate_process(process)

    def _get_process(self, session_ref: str) -> Any | None:
        with self._lock:
            return self._processes.get(session_ref)

    def _tracked_process_refs(self) -> set[str]:
        with self._lock:
            return set(self._processes)

    def _start_activation_watcher(
        self, session_ref: str, process: Any, expires_at: datetime
    ) -> None:
        watcher = threading.Thread(
            target=self._watch_activation_process,
            args=(session_ref, process, expires_at),
            name="wecom-auth-session",
            daemon=True,
        )
        watcher.start()

    def _watch_activation_process(
        self, session_ref: str, process: Any, expires_at: datetime
    ) -> None:
        timeout = max(0.0, (expires_at - _utc_now()).total_seconds())
        try:
            return_code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, TimeoutError):
            return_code = None
            _terminate_process(process)
        with self._lock:
            if self._processes.get(session_ref) is not process:
                return
            self._processes.pop(session_ref, None)
            if self._closed:
                return
        if return_code is None:
            state_value = "EXPIRED"
        elif return_code == 0:
            state_value = "VERIFYING"
        else:
            state_value = "FAILED"
        self._store.transition_active_session(
            session_ref,
            state_value=state_value,
            clear_authorization_url=True,
        )

    def _update_session(
        self, record: Mapping[str, Any], state_value: str, *, url: str | None
    ) -> dict[str, Any]:
        session_ref = str(record["session_ref"])
        updated = self._store.update_session(
            session_ref,
            state_value=state_value,
            authorization_url=url,
        )
        return updated or dict(record)

    def _require_available(self) -> None:
        if not self._cli_enabled or self._store is None:
            raise AuthorizationSessionError("cli_disabled")


def owner_fingerprint(context: Any, binding_id: str) -> str:
    """Hash verified ownership claims without persisting tenant or actor values."""
    claims = {
        "binding_id": binding_id,
        "caller_workload": getattr(context, "caller_workload", None),
        "audience": getattr(context, "audience", None),
        "issuer": getattr(context, "issuer", None),
        "subject": getattr(context, "subject", None),
        "organization_id": getattr(context, "organization_id", None),
        "workspace_id": getattr(context, "workspace_id", None),
    }
    canonical = json.dumps(
        claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def profile_authorization_url(*values: str) -> str | None:
    """Extract one supported vendor QR page and normalize its fixed query shape."""
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        for candidate in _URL_PATTERN.findall(value):
            sanitized = _sanitize_vendor_qr_url(candidate.rstrip('.,;:!?)]"}'))
            if sanitized:
                return sanitized
    return None


def _sanitize_vendor_qr_url(value: str) -> str | None:
    if len(value) > MAX_URL_LENGTH:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        query_pairs = urllib.parse.parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=True
        )
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != _VENDOR_QR_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path != _VENDOR_QR_PATH
        or parsed.fragment
        or parsed.netloc != _VENDOR_QR_HOST
    ):
        return None
    if len(query_pairs) != 2 or len({key for key, _ in query_pairs}) != 2:
        return None
    query = dict(query_pairs)
    scode = query.get("scode", "")
    if query.get("source") != _VENDOR_QR_SOURCE or not re.fullmatch(
        r"[A-Za-z0-9_-]{8,256}", scode
    ):
        return None
    return (
        f"https://{_VENDOR_QR_HOST}{_VENDOR_QR_PATH}?source="
        f"{_VENDOR_QR_SOURCE}&scode={urllib.parse.quote(scode, safe='-_')}"
    )


def _vendor_error_details(exc: WeComCliError) -> tuple[int | None, str]:
    """Read only the vendor code and exact known expiry phrase from CLI error text."""
    for raw in (exc.stdout, exc.stderr):
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                code = error.get("code", error.get("errcode"))
                message = error.get("message")
                if _is_integer(code):
                    return (
                        code,
                        message.lower().strip() if isinstance(message, str) else "",
                    )
            code = payload.get("errcode", payload.get("code"))
            message = payload.get("errmsg", payload.get("message"))
            if _is_integer(code):
                return (
                    code,
                    message.lower().strip() if isinstance(message, str) else "",
                )
        match = _VENDOR_CODE_PATTERN.search(raw)
        if match:
            code = int(match.group(1))
            message = (
                "authorization expired"
                if "authorization expired" in raw.lower()
                else ""
            )
            return code, message
    return None, ""


def _is_successful_observation(value: Any) -> bool:
    if isinstance(value, Mapping):
        if "output" in value or "error" in value:
            return False
        code = value.get("errcode", value.get("code"))
        return code in (None, 0, "0")
    return isinstance(value, list)


def _supported_cli_version(value: str) -> bool:
    match = re.search(r"(?:^|\D)(\d+)\.(\d+)\.(\d+)", value or "")
    if not match:
        return False
    return tuple(map(int, match.groups())) >= (1, 2, 1)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _lock_file(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_file(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _terminate_process(process: Any) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except Exception:
                process.kill()
                process.wait(timeout=2.0)
    except Exception:
        pass
