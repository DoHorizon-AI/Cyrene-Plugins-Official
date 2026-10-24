"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_resources_provider.cloud_api                         │
│  Role: Read one configured target through a fixed vendor REST route. │
│                                                                      │
│  模块职责：通过固定供应商 REST 路由读取一个已配置目标。               │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from .settings import (
    AzureVmTarget,
    CloudTarget,
    CredentialMissingError,
    GcpInstanceTarget,
    HuggingFaceTarget,
    ProviderSettings,
    RunpodPodTarget,
    TailscaleDeviceTarget,
)

AZURE_MANAGEMENT_HOST = "management.azure.com"
GCP_COMPUTE_HOST = "compute.googleapis.com"
HF_HUB_HOST = "huggingface.co"
TAILSCALE_API_HOST = "api.tailscale.com"
RUNPOD_API_HOST = "api.runpod.io"
_AZURE_SCOPE = "https://management.azure.com/.default"
_GCP_SCOPE = "https://www.googleapis.com/auth/compute.readonly"
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
_TAILSCALE_TOKEN_URL = f"https://{TAILSCALE_API_HOST}/api/v2/oauth/token"
_TAILSCALE_READ_SCOPE = "devices:core:read"
_MAX_RESPONSE_BYTES = 262_144
_MAX_STATUS_ITEMS = 128
_MAX_HF_TAGS = 128
_MAX_BEARER_BYTES = 8192
_AZURE_POWER_STATES = frozenset(
    {"STARTING", "RUNNING", "STOPPING", "STOPPED", "DEALLOCATING", "DEALLOCATED"}
)
_GCP_INSTANCE_STATES = frozenset(
    {
        "PROVISIONING",
        "STAGING",
        "RUNNING",
        "STOPPING",
        "SUSPENDING",
        "SUSPENDED",
        "RESUMING",
        "TERMINATED",
        "REPAIRING",
    }
)
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})\Z")


class CloudProviderFailure(RuntimeError):
    """A sanitized provider or transport failure safe to return across the Plugin boundary."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        credential_present: bool | None = None,
        expires_at: datetime | None = None,
        failure_phase: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.credential_present = credential_present
        self.expires_at = expires_at
        self.failure_phase = failure_phase


@dataclass(frozen=True, slots=True)
class BearerCredential:
    """An in-memory access token and only the expiry the source reports."""

    token: str | None = field(repr=False)
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CloudObservationData:
    """Provider-neutral observation values before the shared SDK DTO conversion."""

    external_ref: str
    kind: str
    display_name: str
    status: str
    provider_status: str | None
    collected_at: datetime
    observed_at: datetime | None
    attributes: Mapping[str, str | int | float | bool | None]
    platform_resource_ref: str | None = None
    platform_generation: int | None = None


class JsonTransport(Protocol):
    """Read bounded JSON from one fixed vendor origin."""

    def get_json(
        self, url: str, *, expected_host: str, token: str | None, timeout: float
    ) -> Mapping[str, Any]:
        """Return one bounded JSON object or a sanitized provider failure."""

    def post_form(
        self, url: str, *, expected_host: str, form: Mapping[str, str], timeout: float
    ) -> Mapping[str, Any]:
        """Post one fixed form to an official token endpoint with the same bounds."""


class CredentialAcquirer(Protocol):
    """Acquire an access token using the binding's explicit provider mode."""

    def acquire(self, target: CloudTarget) -> BearerCredential:
        """Return an in-memory provider token without exposing it to callers."""


class HttpJsonTransport:
    """Issue a single HTTPS GET with fixed host, size, redirect, and time bounds."""

    def __init__(self, *, httpx_transport: httpx.BaseTransport | None = None) -> None:
        self._httpx_transport = httpx_transport

    def get_json(
        self, url: str, *, expected_host: str, token: str | None, timeout: float
    ) -> Mapping[str, Any]:
        """Perform one authorized or anonymous GET and project only JSON objects."""

        _validate_endpoint(url, expected_host)
        headers = {"Accept": "application/json"}
        if token is not None:
            _validate_bearer_token(token)
            headers["Authorization"] = f"Bearer {token}"
        try:
            with (
                httpx.Client(
                    transport=self._httpx_transport,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx.Timeout(timeout),
                ) as client,
                client.stream(
                    "GET",
                    url,
                    headers=headers,
                    timeout=httpx.Timeout(timeout),
                    follow_redirects=False,
                ) as response,
            ):
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise CloudProviderFailure("REDIRECT_REJECTED")
                _raise_for_status(response.status_code)
                content_type = response.headers.get("content-type", "").lower()
                if content_type and "json" not in content_type:
                    raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
                payload = _read_limited(response.iter_bytes(), _MAX_RESPONSE_BYTES)
        except CloudProviderFailure:
            raise
        except httpx.TimeoutException as error:
            raise CloudProviderFailure("PROVIDER_TIMEOUT", retryable=True) from error
        except httpx.HTTPError as error:
            raise CloudProviderFailure("PROVIDER_UNAVAILABLE", retryable=True) from error
        try:
            document = json.loads(payload, object_pairs_hook=_unique_response_object)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE") from error
        if not isinstance(document, Mapping):
            raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
        return document

    def post_form(
        self, url: str, *, expected_host: str, form: Mapping[str, str], timeout: float
    ) -> Mapping[str, Any]:
        """Post a fixed credential-exchange form without placing secrets in its URL."""

        _validate_endpoint(url, expected_host)
        try:
            with (
                httpx.Client(
                    transport=self._httpx_transport,
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx.Timeout(timeout),
                ) as client,
                client.stream(
                    "POST",
                    url,
                    data=dict(form),
                    headers={"Accept": "application/json"},
                    timeout=httpx.Timeout(timeout),
                    follow_redirects=False,
                ) as response,
            ):
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise CloudProviderFailure("REDIRECT_REJECTED")
                _raise_for_status(response.status_code)
                content_type = response.headers.get("content-type", "").lower()
                if content_type and "json" not in content_type:
                    raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
                payload = _read_limited(response.iter_bytes(), _MAX_RESPONSE_BYTES)
        except CloudProviderFailure:
            raise
        except httpx.TimeoutException as error:
            raise CloudProviderFailure("PROVIDER_TIMEOUT", retryable=True) from error
        except httpx.HTTPError as error:
            raise CloudProviderFailure("PROVIDER_UNAVAILABLE", retryable=True) from error
        try:
            document = json.loads(payload, object_pairs_hook=_unique_response_object)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE") from error
        if not isinstance(document, Mapping):
            raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
        return document


class DefaultCredentialAcquirer:
    """Use explicitly selected provider credentials without ambient tool discovery."""

    def __init__(
        self, settings: ProviderSettings, *, transport: JsonTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport or HttpJsonTransport()

    def acquire(self, target: CloudTarget) -> BearerCredential:
        """Acquire an Azure, GCP, or optional Hugging Face token for one target."""

        if isinstance(target, AzureVmTarget):
            return self._azure_token()
        if isinstance(target, GcpInstanceTarget):
            return self._gcp_token()
        if isinstance(target, HuggingFaceTarget):
            value = self._settings.secret_value("cloud-resources/hf-read-token")
            if value is not None and len(value.encode("utf-8")) > _MAX_BEARER_BYTES:
                raise CloudProviderFailure("INVALID_CREDENTIAL_CONFIGURATION")
            return BearerCredential(value or None, None)
        if isinstance(target, TailscaleDeviceTarget):
            return self._tailscale_token()
        if isinstance(target, RunpodPodTarget):
            token = self._settings.secret_value("cloud-resources/runpod-api-key")
            if token is None:
                raise CredentialMissingError("configured Runpod Read Only API key is missing")
            if len(token.encode("utf-8")) > _MAX_BEARER_BYTES:
                raise CloudProviderFailure("INVALID_CREDENTIAL_CONFIGURATION")
            return BearerCredential(token, None)
        raise CloudProviderFailure("UNSUPPORTED_TARGET")

    def _azure_token(self) -> BearerCredential:
        """Acquire only the ARM scope through the configured managed identity."""

        try:
            from azure.identity import ManagedIdentityCredential

            credential = ManagedIdentityCredential(
                client_id=self._settings.azure_client_id,
                connection_timeout=self._settings.timeout_seconds,
                read_timeout=self._settings.timeout_seconds,
                retry_total=0,
                retry_connect=0,
                retry_read=0,
                retry_status=0,
                use_env_settings=False,
            )
            try:
                token = credential.get_token(_AZURE_SCOPE)
            finally:
                credential.close()
        except Exception as error:
            # SDK messages can contain endpoints or identity metadata; expose a stable code only.
            raise CloudProviderFailure("CREDENTIAL_PROVIDER_UNAVAILABLE", retryable=True) from error
        if (
            not isinstance(token.token, str)
            or not token.token
            or len(token.token) > _MAX_BEARER_BYTES
        ):
            raise CloudProviderFailure("CREDENTIAL_PROVIDER_UNAVAILABLE", retryable=True)
        return BearerCredential(token.token, _epoch_datetime(getattr(token, "expires_on", None)))

    def _gcp_token(self) -> BearerCredential:
        """Refresh a compute metadata or host-injected service-account credential."""

        try:
            import google.auth.compute_engine.credentials
            import google.auth.transport.requests
            import requests
            from google.oauth2 import service_account

            if self._settings.gcp_auth_mode == "metadata":
                credentials = google.auth.compute_engine.credentials.Credentials(
                    scopes=[_GCP_SCOPE]
                )
            elif self._settings.gcp_auth_mode == "service_account_json":
                secret = self._settings.secret_value("cloud-resources/gcp-service-account-json")
                if secret is None:
                    raise CredentialMissingError("configured GCP service-account JSON is missing")
                if len(secret.encode("utf-8")) > 65_536:
                    raise CloudProviderFailure("INVALID_CREDENTIAL_CONFIGURATION")
                try:
                    info = json.loads(secret, object_pairs_hook=_unique_response_object)
                except (json.JSONDecodeError, ValueError) as error:
                    raise CloudProviderFailure("INVALID_CREDENTIAL_CONFIGURATION") from error
                if (
                    not isinstance(info, Mapping)
                    or info.get("type") != "service_account"
                    or info.get("token_uri") != _GOOGLE_TOKEN_URI
                ):
                    raise CloudProviderFailure("INVALID_CREDENTIAL_CONFIGURATION")
                credentials = service_account.Credentials.from_service_account_info(
                    dict(info), scopes=[_GCP_SCOPE]
                )
            else:
                raise CloudProviderFailure("INVALID_CREDENTIAL_CONFIGURATION")

            session = requests.Session()
            session.trust_env = False
            request = _BoundedGoogleRequest(
                session=session,
                timeout_seconds=self._settings.timeout_seconds,
            )
            try:
                if not credentials.valid:
                    credentials.refresh(request)
            finally:
                session.close()
        except CredentialMissingError:
            raise
        except CloudProviderFailure:
            raise
        except Exception as error:
            raise CloudProviderFailure("CREDENTIAL_PROVIDER_UNAVAILABLE", retryable=True) from error
        if (
            not isinstance(credentials.token, str)
            or not credentials.token
            or len(credentials.token) > _MAX_BEARER_BYTES
        ):
            raise CloudProviderFailure("CREDENTIAL_PROVIDER_UNAVAILABLE", retryable=True)
        return BearerCredential(credentials.token, _aware_utc(getattr(credentials, "expiry", None)))

    def _tailscale_token(self) -> BearerCredential:
        """Exchange the configured OAuth client secret for a devices-read token."""

        client_id = self._settings.tailscale_client_id
        client_secret = self._settings.secret_value("cloud-resources/tailscale-client-secret")
        if not client_id or not client_secret:
            raise CredentialMissingError("configured Tailscale OAuth credential is missing")
        if len(client_secret.encode("utf-8")) > _MAX_BEARER_BYTES:
            raise CloudProviderFailure("INVALID_CREDENTIAL_CONFIGURATION")
        try:
            document = self._transport.post_form(
                _TAILSCALE_TOKEN_URL,
                expected_host=TAILSCALE_API_HOST,
                form={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": _TAILSCALE_READ_SCOPE,
                },
                timeout=self._settings.timeout_seconds,
            )
            token = document.get("access_token")
            token_type = document.get("token_type")
            expires_in = document.get("expires_in")
            scope = document.get("scope")
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 8192
                or token_type not in (None, "Bearer")
                or isinstance(expires_in, bool)
                or not isinstance(expires_in, int)
                or not 1 <= expires_in <= 3600
                or (
                    scope is not None
                    and (not isinstance(scope, str) or scope.split() != [_TAILSCALE_READ_SCOPE])
                )
            ):
                raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
            expires_at = datetime.now(UTC).timestamp() + expires_in
            return BearerCredential(token, _epoch_datetime(expires_at))
        except CloudProviderFailure as error:
            error.credential_present = True
            error.failure_phase = "credential_exchange"
            raise


class _BoundedGoogleRequest:
    """Apply the Plugin's short timeout and redirect policy to google-auth refreshes."""

    def __init__(self, *, session: Any, timeout_seconds: float) -> None:
        import google.auth.transport.requests

        self._request = google.auth.transport.requests.Request(session=session)
        self._timeout = timeout_seconds

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Forward one auth transport request with redirects disabled and timeout capped."""

        allowed_timeout = self._timeout if timeout is None else min(timeout, self._timeout)
        kwargs["allow_redirects"] = False
        return self._request(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=allowed_timeout,
            **kwargs,
        )


class CloudResourceReader:
    """Read bounded Azure, Google, Hugging Face, Tailscale, and Runpod observations."""

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        transport: JsonTransport | None = None,
        credentials: CredentialAcquirer | None = None,
        clock: Any = None,
    ) -> None:
        self._settings = settings
        self._transport = transport or HttpJsonTransport()
        self._credentials = credentials or DefaultCredentialAcquirer(
            settings, transport=self._transport
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def observe(self, target_ref: str) -> tuple[CloudObservationData, BearerCredential]:
        """Fetch one configured target and return safe state plus credential expiry."""

        target = self._settings.target(target_ref)
        credential = self._credentials.acquire(target)
        try:
            if isinstance(target, AzureVmTarget):
                data = self._observe_azure(target, credential)
            elif isinstance(target, GcpInstanceTarget):
                data = self._observe_gcp(target, credential)
            elif isinstance(target, HuggingFaceTarget):
                data = self._observe_hf(target, credential)
            elif isinstance(target, TailscaleDeviceTarget):
                data = self._observe_tailscale(target, credential)
            elif isinstance(target, RunpodPodTarget):
                data = self._observe_runpod(target, credential)
            else:
                raise CloudProviderFailure("UNSUPPORTED_TARGET")
        except CloudProviderFailure as error:
            error.credential_present = credential.token is not None
            error.expires_at = credential.expires_at
            raise
        return data, credential

    def _observe_tailscale(
        self, target: TailscaleDeviceTarget, credential: BearerCredential
    ) -> CloudObservationData:
        """Read one fixed device using the OAuth client's read-only device scope."""

        device_id = quote(target.device_id, safe="-_")
        url = f"https://{TAILSCALE_API_HOST}/api/v2/device/{device_id}"
        document = self._transport.get_json(
            url,
            expected_host=TAILSCALE_API_HOST,
            token=_required_token(credential),
            timeout=self._settings.timeout_seconds,
        )
        if document.get("id") != target.device_id:
            raise CloudProviderFailure("TARGET_IDENTITY_MISMATCH")
        authorized = document.get("authorized")
        online = document.get("isOnline")
        if authorized is not None and not isinstance(authorized, bool):
            raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
        if online is not None and not isinstance(online, bool):
            raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
        if authorized is False:
            status, provider_status = "UNAVAILABLE", "UNAUTHORIZED"
        elif online is True:
            status, provider_status = "AVAILABLE", "ONLINE"
        elif online is False:
            status, provider_status = "UNAVAILABLE", "OFFLINE"
        else:
            status, provider_status = "UNKNOWN", None
        attributes: dict[str, str | int | float | bool | None] = {}
        for source_name, output_name in (("os", "os"), ("clientVersion", "client_version")):
            value = document.get(source_name)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
                attributes[output_name] = value
        last_seen = _source_datetime(document.get("lastSeen"))
        if last_seen is not None:
            attributes["last_seen"] = _format_source_datetime(last_seen)
        collected = _aware_utc(self._clock()) or datetime.now(UTC)
        return CloudObservationData(
            external_ref=target.target_ref,
            kind=target.kind,
            display_name=target.display_name,
            status=status,
            provider_status=provider_status,
            collected_at=collected,
            observed_at=None,
            attributes=attributes,
        )

    def _observe_runpod(
        self, target: RunpodPodTarget, credential: BearerCredential
    ) -> CloudObservationData:
        """Read one Pod and preserve desired status without asserting actual runtime state."""

        pod_id = quote(target.pod_id, safe="-_")
        url = f"https://{RUNPOD_API_HOST}/v2/pods/{pod_id}"
        document = self._transport.get_json(
            url,
            expected_host=RUNPOD_API_HOST,
            token=_required_token(credential),
            timeout=self._settings.timeout_seconds,
        )
        if document.get("id") != target.pod_id:
            raise CloudProviderFailure("TARGET_IDENTITY_MISMATCH")
        desired_status = document.get("desiredStatus")
        if desired_status is not None and not isinstance(desired_status, str):
            raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
        safe_statuses = {"RUNNING", "EXITED", "TERMINATED"}
        attributes: dict[str, str | int | float | bool | None] = {}
        provider_status: str | None = None
        if isinstance(desired_status, str) and desired_status in safe_statuses:
            attributes["desired_status"] = desired_status
            provider_status = f"desiredStatus/{desired_status}"
        last_started_at = _source_datetime(document.get("lastStartedAt"))
        if last_started_at is not None:
            attributes["last_started_at"] = _format_source_datetime(last_started_at)
        collected = _aware_utc(self._clock()) or datetime.now(UTC)
        return CloudObservationData(
            external_ref=target.target_ref,
            kind=target.kind,
            display_name=target.display_name,
            status="UNKNOWN",
            provider_status=provider_status,
            collected_at=collected,
            observed_at=None,
            attributes=attributes,
        )

    def _observe_azure(
        self, target: AzureVmTarget, credential: BearerCredential
    ) -> CloudObservationData:
        """Read the fixed Azure VM instance-view endpoint and normalize power state."""

        subscription = quote(target.subscription_id, safe="-")
        group = quote(target.resource_group, safe="-._()")
        vm = quote(target.vm_name, safe="-._")
        url = (
            f"https://{AZURE_MANAGEMENT_HOST}/subscriptions/{subscription}/resourceGroups/{group}"
            f"/providers/Microsoft.Compute/virtualMachines/{vm}/instanceView?api-version=2024-07-01"
        )
        document = self._transport.get_json(
            url,
            expected_host=AZURE_MANAGEMENT_HOST,
            token=_required_token(credential),
            timeout=self._settings.timeout_seconds,
        )
        statuses = document.get("statuses")
        if statuses is None:
            statuses = []
        if not isinstance(statuses, list) or len(statuses) > _MAX_STATUS_ITEMS:
            raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
        power_state: str | None = None
        observed_at: datetime | None = None
        for item in statuses:
            if not isinstance(item, Mapping):
                raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
            code = item.get("code")
            if isinstance(code, str) and code.startswith("PowerState/"):
                if power_state is not None:
                    raise CloudProviderFailure("INVALID_PROVIDER_RESPONSE")
                parsed_state = code.removeprefix("PowerState/").upper()
                if parsed_state in _AZURE_POWER_STATES:
                    power_state = parsed_state
                    observed_at = _source_datetime(item.get("time"))
        normalized = {
            "RUNNING": "RUNNING",
            "STOPPED": "STOPPED",
            "DEALLOCATED": "STOPPED",
        }.get(power_state or "", "UNKNOWN")
        collected = _aware_utc(self._clock()) or datetime.now(UTC)
        return CloudObservationData(
            external_ref=target.target_ref,
            kind=target.kind,
            display_name=target.display_name,
            status=normalized,
            provider_status=f"PowerState/{power_state}" if power_state is not None else None,
            collected_at=collected,
            observed_at=observed_at,
            attributes={"power_state": power_state.lower()} if power_state is not None else {},
        )

    def _observe_gcp(
        self, target: GcpInstanceTarget, credential: BearerCredential
    ) -> CloudObservationData:
        """Read one fixed zonal Compute Engine instance with read-only scope."""

        project = quote(target.project_id, safe="-")
        zone = quote(target.zone, safe="-")
        instance = quote(target.instance_name, safe="-")
        url = (
            f"https://{GCP_COMPUTE_HOST}/compute/v1/projects/{project}/zones/{zone}"
            f"/instances/{instance}"
        )
        document = self._transport.get_json(
            url,
            expected_host=GCP_COMPUTE_HOST,
            token=_required_token(credential),
            timeout=self._settings.timeout_seconds,
        )
        if document.get("name") != target.instance_name:
            raise CloudProviderFailure("TARGET_IDENTITY_MISMATCH")
        actual_zone = document.get("zone")
        if (
            not isinstance(actual_zone, str)
            or actual_zone.rstrip("/").rsplit("/", 1)[-1] != target.zone
        ):
            raise CloudProviderFailure("TARGET_IDENTITY_MISMATCH")
        provider_status = document.get("status")
        if not isinstance(provider_status, str) or provider_status not in _GCP_INSTANCE_STATES:
            provider_status = None
        normalized = {
            "RUNNING": "RUNNING",
            "TERMINATED": "STOPPED",
            "SUSPENDED": "STOPPED",
        }.get(provider_status or "", "UNKNOWN")
        machine_type = document.get("machineType")
        attributes: dict[str, str | int | float | bool | None] = {}
        if isinstance(machine_type, str):
            type_name = machine_type.rstrip("/").rsplit("/", 1)[-1]
            if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", type_name):
                attributes["machine_type"] = type_name
        collected = _aware_utc(self._clock()) or datetime.now(UTC)
        return CloudObservationData(
            external_ref=target.target_ref,
            kind=target.kind,
            display_name=target.display_name,
            status=normalized,
            provider_status=provider_status,
            collected_at=collected,
            observed_at=None,
            attributes=attributes,
        )

    def _observe_hf(
        self, target: HuggingFaceTarget, credential: BearerCredential
    ) -> CloudObservationData:
        """Read repository metadata only; never resolve files or Space runtimes."""

        repo_id = quote(target.repo_id, safe="/")
        repo_type = {"hf.model": "models", "hf.dataset": "datasets", "hf.space": "spaces"}[
            target.kind
        ]
        url = f"https://{HF_HUB_HOST}/api/{repo_type}/{repo_id}"
        document = self._transport.get_json(
            url,
            expected_host=HF_HUB_HOST,
            token=credential.token,
            timeout=self._settings.timeout_seconds,
        )
        response_id = document.get("id")
        if not isinstance(response_id, str) or response_id.casefold() != target.repo_id.casefold():
            raise CloudProviderFailure("TARGET_IDENTITY_MISMATCH")
        attributes: dict[str, str | int | float | bool | None] = {}
        private = document.get("private")
        if isinstance(private, bool):
            attributes["private"] = private
        disabled = document.get("disabled")
        if isinstance(disabled, bool):
            attributes["disabled"] = disabled
        gated = document.get("gated")
        if isinstance(gated, bool):
            attributes["gated"] = gated
        elif isinstance(gated, str):
            attributes["gated"] = bool(gated)
        last_modified = _source_datetime(document.get("lastModified"))
        if last_modified is not None:
            attributes["last_modified"] = _format_source_datetime(last_modified)
        tags = document.get("tags")
        if (
            isinstance(tags, list)
            and len(tags) <= _MAX_HF_TAGS
            and all(isinstance(tag, str) and len(tag) <= 128 for tag in tags)
        ):
            attributes["tag_count"] = len(tags)
        for field_name in ("downloads", "likes"):
            value = document.get(field_name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                attributes[field_name] = value
        collected = _aware_utc(self._clock()) or datetime.now(UTC)
        return CloudObservationData(
            external_ref=target.target_ref,
            kind=target.kind,
            display_name=target.display_name,
            status="UNAVAILABLE" if disabled is True else "AVAILABLE",
            provider_status="DISABLED" if disabled is True else "METADATA_AVAILABLE",
            collected_at=collected,
            observed_at=None,
            attributes=attributes,
        )


def _read_limited(chunks: Iterable[bytes], maximum: int) -> bytes:
    """Read a response stream up to a strict byte limit."""

    collected = bytearray()
    for chunk in chunks:
        collected.extend(chunk)
        if len(collected) > maximum:
            raise CloudProviderFailure("PROVIDER_RESPONSE_TOO_LARGE")
    return bytes(collected)


def _validate_endpoint(url: str, expected_host: str) -> None:
    """Reject non-HTTPS, redirected, userinfo, port, or off-origin endpoints."""

    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CloudProviderFailure("UNSAFE_PROVIDER_ENDPOINT")


def _validate_bearer_token(token: str) -> None:
    """Reject oversized or control-bearing tokens before placing them in a header."""

    if (
        not token
        or len(token.encode("utf-8")) > _MAX_BEARER_BYTES
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in token
        )
    ):
        raise CloudProviderFailure("INVALID_CREDENTIAL_CONFIGURATION")


def _raise_for_status(status_code: int) -> None:
    """Convert HTTP status into stable auth, scope, and availability classes."""

    if 200 <= status_code < 300:
        return
    if status_code == 401:
        raise CloudProviderFailure("AUTHENTICATION_REQUIRED")
    if status_code == 403:
        raise CloudProviderFailure("PERMISSION_DENIED")
    if status_code == 404:
        raise CloudProviderFailure("TARGET_NOT_FOUND")
    if status_code == 429:
        raise CloudProviderFailure("PROVIDER_RATE_LIMITED", retryable=True)
    if status_code >= 500:
        raise CloudProviderFailure("PROVIDER_UNAVAILABLE", retryable=True)
    raise CloudProviderFailure("PROVIDER_REQUEST_REJECTED")


def _required_token(credential: BearerCredential) -> str:
    """Require an acquired token for cloud APIs that have no anonymous scope."""

    if not credential.token:
        raise CredentialMissingError("provider access token is missing")
    return credential.token


def _source_datetime(value: object) -> datetime | None:
    """Parse only a timezone-aware RFC3339 source timestamp."""

    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        return None
    try:
        return _aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _format_source_datetime(value: datetime) -> str:
    """Serialize a validated provider timestamp as RFC3339 UTC."""

    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _unique_response_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate fields in provider and host-injected credential JSON."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object contains duplicate keys")
        result[key] = value
    return result


def _aware_utc(value: object) -> datetime | None:
    """Normalize an aware datetime to UTC without assigning missing timezone data."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _epoch_datetime(value: object) -> datetime | None:
    """Convert a finite source epoch expiry to an aware UTC datetime."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None
