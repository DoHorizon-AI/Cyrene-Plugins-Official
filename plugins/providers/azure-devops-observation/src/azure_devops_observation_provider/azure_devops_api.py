"""Fixed-host Azure DevOps REST v7.1 reads and safe observation projections.

Only project-scoped GET requests are issued. Vendor links and arbitrary URLs
are never followed. 中文：不排队、不更新 pipeline，也不调用 shell。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from cyrene_account_center_contracts import canonical_json

from .settings import AuthorizedSource, ProviderSettings

API_VERSION = "7.1"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PAGE_SIZE = 100
RUNS_API_LIMIT = 10_000


class AzureDevOpsApiFailure(RuntimeError):
    """Sanitized classification for a fixed Azure DevOps read failure."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class AzureDevOpsApi:
    """Read authorized project resources from fixed Azure DevOps service hosts."""

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.Client(
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    def close(self) -> None:
        """Close the HTTP client only when this instance created it."""

        if self._owns_client:
            self._client.close()

    def list_pipelines(
        self,
        source: AuthorizedSource,
        token: str,
        *,
        page_size: int,
        continuation_token: str | None,
    ) -> dict[str, Any]:
        """Read one configured project's pipeline page and continuation marker."""

        if source.kind != "project_pipelines":
            raise ValueError("source kind does not permit pipeline inventory")
        params: dict[str, Any] = {"api-version": API_VERSION, "$top": page_size}
        if continuation_token is not None:
            params["continuationToken"] = continuation_token
        host, prefix = self._project_path("dev.azure.com", source)
        result, response = self._get_json(host, f"{prefix}/_apis/pipelines", params, token)
        records = _value_list(result)
        items: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in records:
            if not isinstance(raw, Mapping):
                raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
            pipeline_id = _positive_vendor_id(raw.get("id"))
            if pipeline_id in seen:
                continue
            seen.add(pipeline_id)
            name = _bounded_text(raw.get("name"), 160)
            items.append(
                {
                    "id": pipeline_id,
                    "name": name,
                    "folder": _bounded_text(raw.get("folder"), 256),
                    "revision": _optional_nonnegative_int(raw.get("revision")),
                    "configurationType": _nested_text(raw, "configuration", "type", 64),
                }
            )
        next_token = _continuation_token(response)
        truncated = len(records) >= page_size and next_token is None
        return {
            "items": items,
            "next_token": next_token,
            "complete": next_token is None and not truncated,
            "truncated": truncated,
        }

    def list_pipeline_runs(
        self,
        source: AuthorizedSource,
        token: str,
        *,
        window_start: datetime | None,
        window_end: datetime,
    ) -> tuple[list[dict[str, Any]], bool, int | None]:
        """Read the API's latest 10,000 runs and report whether window coverage is provable."""

        if source.kind != "pipeline_runs" or source.pipeline_id is None:
            raise ValueError("source kind does not permit pipeline run reads")
        host, prefix = self._project_path("dev.azure.com", source)
        result, _response = self._get_json(
            host,
            f"{prefix}/_apis/pipelines/{source.pipeline_id}/runs",
            {"api-version": API_VERSION},
            token,
        )
        records = _value_list(result)
        raw_min_id: int | None = None
        parsed: list[tuple[int, datetime, datetime, bool, dict[str, Any]]] = []
        seen: set[int] = set()
        for raw in records:
            if not isinstance(raw, Mapping):
                raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
            run_id = _positive_vendor_id(raw.get("id"))
            if run_id in seen:
                continue
            seen.add(run_id)
            created = _parse_vendor_time(raw.get("createdDate"))
            finished = _optional_vendor_time(raw.get("finishedDate"))
            occurred = finished or created
            state = _bounded_text(raw.get("state"), 64) or "unknown"
            is_active = state.casefold() != "completed"
            raw_min_id = run_id if raw_min_id is None else min(raw_min_id, run_id)
            parsed.append(
                (
                    run_id,
                    created,
                    occurred,
                    is_active,
                    _pipeline_run_observation(self._settings, source, raw),
                )
            )

        complete_window = True
        if len(records) >= RUNS_API_LIMIT:
            oldest = min((created for _, created, _, _, _ in parsed), default=None)
            complete_window = (
                window_start is not None and oldest is not None and window_start >= oldest
            )
        items = [
            observation
            for _run_id, _created, occurred, is_active, observation in parsed
            if occurred <= window_end
            and (is_active or window_start is None or occurred >= window_start)
        ]
        items.sort(key=lambda item: _run_id_from_event(item["sourceEventId"]))
        return items, complete_window, raw_min_id

    def get_pipeline_run(
        self,
        source: AuthorizedSource,
        run_id: int,
        token: str,
    ) -> dict[str, Any]:
        """Read and project one run from the configured pipeline."""

        if source.kind != "pipeline_runs" or source.pipeline_id is None:
            raise ValueError("source kind does not permit pipeline run reads")
        host, prefix = self._project_path("dev.azure.com", source)
        result, _response = self._get_json(
            host,
            f"{prefix}/_apis/pipelines/{source.pipeline_id}/runs/{run_id}",
            {"api-version": API_VERSION},
            token,
        )
        if not isinstance(result, Mapping):
            raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
        actual_pipeline = result.get("pipeline")
        if isinstance(actual_pipeline, Mapping) and actual_pipeline.get("id") != source.pipeline_id:
            raise AzureDevOpsApiFailure("RESOURCE_SCOPE_MISMATCH")
        actual_run = _positive_vendor_id(result.get("id"))
        if actual_run != run_id:
            raise AzureDevOpsApiFailure("RESOURCE_SCOPE_MISMATCH")
        return _pipeline_run_observation(self._settings, source, result)

    def list_deployments(
        self,
        source: AuthorizedSource,
        token: str,
        *,
        page_size: int,
        continuation_token: str | None,
        window_start: datetime | None,
        window_end: datetime,
    ) -> dict[str, Any]:
        """Read one release deployment page within the configured project/filter."""

        if source.kind != "release_deployments":
            raise ValueError("source kind does not permit release deployment reads")
        params: dict[str, Any] = {
            "api-version": API_VERSION,
            "$top": page_size,
            "queryOrder": "ascending",
            "maxModifiedTime": _format_time(window_end),
        }
        if window_start is not None:
            params["minModifiedTime"] = _format_time(window_start)
        if source.definition_id is not None:
            params["definitionId"] = source.definition_id
        if source.definition_environment_id is not None:
            params["definitionEnvironmentId"] = source.definition_environment_id
        if continuation_token is not None:
            params["continuationToken"] = int(continuation_token)
        host, prefix = self._project_path("vsrm.dev.azure.com", source)
        result, response = self._get_json(
            host,
            f"{prefix}/_apis/release/deployments",
            params,
            token,
        )
        records = _value_list(result)
        items: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in records:
            if not isinstance(raw, Mapping):
                raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
            deployment_id = _positive_vendor_id(raw.get("id"))
            if deployment_id in seen:
                continue
            seen.add(deployment_id)
            items.append(_deployment_observation(self._settings, source, raw))
        next_token = _continuation_token(response)
        if next_token is not None and (not next_token.isdigit() or int(next_token) > 2_147_483_647):
            raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
        truncated = len(records) >= page_size and next_token is None
        return {
            "items": items,
            "next_token": next_token,
            "complete": next_token is None and not truncated,
            "truncated": truncated,
        }

    def get_release_environment(
        self,
        source: AuthorizedSource,
        release_id: int,
        environment_id: int,
        token: str,
    ) -> dict[str, Any]:
        """Read one release environment and expose status without task details."""

        if source.kind != "release_deployments":
            raise ValueError("source kind does not permit release environment reads")
        host, prefix = self._project_path("vsrm.dev.azure.com", source)
        result, _response = self._get_json(
            host,
            f"{prefix}/_apis/release/releases/{release_id}/environments/{environment_id}",
            {"api-version": API_VERSION},
            token,
        )
        if not isinstance(result, Mapping):
            raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
        actual_release_id = _nested_positive_id(result, "release", "id")
        actual_environment_id = _positive_vendor_id(result.get("id"))
        if actual_release_id != release_id or actual_environment_id != environment_id:
            raise AzureDevOpsApiFailure("RESOURCE_SCOPE_MISMATCH")
        modified = _optional_vendor_time(result.get("modifiedOn"))
        occurred = modified or _latest_deployment_time(result.get("deploySteps"))
        if occurred is None:
            raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
        status = _bounded_text(result.get("status"), 64) or "unknown"
        payload = {
            "releaseId": release_id,
            "environmentId": environment_id,
            "status": status,
            "modifiedOn": _format_time(occurred),
        }
        semantic_state = _status_semantic_state(status)
        semantic_fingerprint = _semantic_fingerprint(
            {"resourceType": "release_environment", "status": status.casefold()}
        )
        version = _source_version(payload)
        resource_uri = _release_environment_uri(
            self._settings.organization,
            source.project_id,
            release_id,
            environment_id,
        )
        return {
            **_event_base(
                self._settings,
                source,
                source_event_id=(
                    f"azure-devops:{_scope_key(self._settings, source)}:release-environment:"
                    f"{version}:{release_id}:{environment_id}"
                ),
                resource_uri=resource_uri,
                event_type="release_environment_status",
                occurred_at=_format_time(occurred),
                version_payload=payload,
            ),
            "title": _bounded_text(result.get("name"), 160) or f"Release {release_id} environment",
            "summary": f"Environment status: {status}"[:500],
            "severity": _severity(status),
            "state": semantic_state,
            "semanticFingerprint": semantic_fingerprint,
            "metadataReferences": [
                {"kind": "azure_devops.release", "id": str(release_id)},
                {"kind": "azure_devops.release_environment", "id": str(environment_id)},
            ],
        }

    def _get_json(
        self,
        host: str,
        path: str,
        params: Mapping[str, Any],
        token: str,
    ) -> tuple[Any, httpx.Response]:
        """Issue a bounded HTTPS GET without redirects or environment proxies."""

        if host not in {"dev.azure.com", "vsrm.dev.azure.com"}:
            raise AzureDevOpsApiFailure("INVALID_PROVIDER_CONFIGURATION")
        url = f"https://{host}/{path.lstrip('/')}"
        try:
            with self._client.stream(
                "GET",
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as response:
                if response.is_redirect:
                    raise AzureDevOpsApiFailure("UNSUPPORTED_REDIRECT")
                if response.status_code >= 400:
                    raise _http_failure(response)
                body = bytearray()
                for chunk in response.iter_bytes(64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise AzureDevOpsApiFailure("PROVIDER_RESPONSE_TOO_LARGE")
                try:
                    result = json.loads(body)
                except (ValueError, json.JSONDecodeError) as error:
                    raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE") from error
                return result, response
        except httpx.TimeoutException as error:
            raise AzureDevOpsApiFailure("UPSTREAM_TIMEOUT", retryable=True) from error
        except httpx.HTTPError as error:
            raise AzureDevOpsApiFailure("UPSTREAM_UNAVAILABLE", retryable=True) from error

    def _project_path(self, host: str, source: AuthorizedSource) -> tuple[str, str]:
        """Construct a path from validated host activation fields only."""

        org = quote(self._settings.organization, safe="")
        project = quote(source.project_id, safe="")
        return host, f"{org}/{project}"


def _pipeline_run_observation(
    settings: ProviderSettings,
    source: AuthorizedSource,
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one run to bounded event facts; omit variables, YAML, and raw links."""

    run_id = _positive_vendor_id(raw.get("id"))
    created = _parse_vendor_time(raw.get("createdDate"))
    finished = _optional_vendor_time(raw.get("finishedDate"))
    occurred = finished or created
    state = _bounded_text(raw.get("state"), 64) or "unknown"
    result = _bounded_text(raw.get("result"), 64) or "unknown"
    pipeline = raw.get("pipeline")
    pipeline_name = (
        _bounded_text(pipeline.get("name"), 160) if isinstance(pipeline, Mapping) else None
    )
    payload = {
        "runId": run_id,
        "pipelineId": source.pipeline_id,
        "state": state,
        "result": result,
        "createdDate": _format_time(created),
        "finishedDate": _format_time(finished) if finished else None,
    }
    version = _source_version(payload)
    resource_uri = _pipeline_run_uri(
        settings.organization,
        source.project_id,
        source.pipeline_id or 0,
        run_id,
    )
    title = _bounded_text(raw.get("name"), 160) or f"Pipeline run {run_id}"
    if pipeline_name:
        title = f"{pipeline_name}: {title}"[:160]
    summary = f"Run state: {state}; result: {result}"[:500]
    semantic_state = _pipeline_semantic_state(state, result)
    semantic_fingerprint = _semantic_fingerprint(
        {
            "resourceType": "pipeline_run",
            "state": state.casefold(),
            "result": result.casefold(),
        }
    )
    return {
        **_event_base(
            settings,
            source,
            source_event_id=(
                f"azure-devops:{_scope_key(settings, source)}:pipeline-run:"
                f"{version}:{source.pipeline_id}:{run_id}"
            ),
            resource_uri=resource_uri,
            event_type="pipeline_run_status",
            occurred_at=_format_time(occurred),
            version_payload=payload,
        ),
        "title": title,
        "summary": summary,
        "severity": _severity(result if state == "completed" else state),
        "state": semantic_state,
        "semanticFingerprint": semantic_fingerprint,
        "metadataReferences": [
            {"kind": "azure_devops.pipeline", "id": str(source.pipeline_id)},
            {"kind": "azure_devops.pipeline_run", "id": str(run_id)},
        ],
    }


def _deployment_observation(
    settings: ProviderSettings,
    source: AuthorizedSource,
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one release deployment to status facts without identity details."""

    deployment_id = _positive_vendor_id(raw.get("id"))
    release_id = _nested_positive_id(raw, "release", "id")
    environment_id = _nested_positive_id(raw, "releaseEnvironment", "id")
    last_modified = _optional_vendor_time(raw.get("lastModifiedOn"))
    completed = _optional_vendor_time(raw.get("completedOn"))
    started = _optional_vendor_time(raw.get("startedOn"))
    queued = _optional_vendor_time(raw.get("queuedOn"))
    occurred = last_modified or completed or started or queued
    if occurred is None:
        raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
    deployment_status = _bounded_text(raw.get("deploymentStatus"), 64) or "unknown"
    operation_status = _bounded_text(raw.get("operationStatus"), 64) or "unknown"
    attempt = _optional_nonnegative_int(raw.get("attempt"))
    release_definition = raw.get("releaseDefinition")
    environment = raw.get("releaseEnvironment")
    definition_name = (
        _bounded_text(release_definition.get("name"), 120)
        if isinstance(release_definition, Mapping)
        else None
    )
    environment_name = (
        _bounded_text(environment.get("name"), 120) if isinstance(environment, Mapping) else None
    )
    title = " / ".join(item for item in (definition_name, environment_name) if item)
    if not title:
        title = f"Release {release_id} deployment"
    payload = {
        "deploymentId": deployment_id,
        "releaseId": release_id,
        "environmentId": environment_id,
        "deploymentStatus": deployment_status,
        "operationStatus": operation_status,
        "attempt": attempt,
        "lastModifiedOn": _format_time(last_modified) if last_modified else None,
    }
    version = _source_version(payload)
    semantic_state = _deployment_semantic_state(deployment_status, operation_status)
    semantic_fingerprint = _semantic_fingerprint(
        {
            "resourceType": "release_deployment",
            "deploymentStatus": deployment_status.casefold(),
            "operationStatus": operation_status.casefold(),
            "attempt": attempt,
        }
    )
    return {
        **_event_base(
            settings,
            source,
            source_event_id=(
                f"azure-devops:{_scope_key(settings, source)}:release-deployment:"
                f"{version}:{deployment_id}"
            ),
            resource_uri=_release_environment_uri(
                settings.organization,
                source.project_id,
                release_id,
                environment_id,
            ),
            event_type="release_deployment_status",
            occurred_at=_format_time(occurred),
            version_payload=payload,
        ),
        "title": title[:160],
        "summary": f"Deployment status: {deployment_status}; operation: {operation_status}"[:500],
        "severity": _severity(deployment_status),
        "state": semantic_state,
        "semanticFingerprint": semantic_fingerprint,
        "metadataReferences": [
            {"kind": "azure_devops.release", "id": str(release_id)},
            {"kind": "azure_devops.release_environment", "id": str(environment_id)},
            {"kind": "azure_devops.release_deployment", "id": str(deployment_id)},
        ],
    }


def _event_base(
    settings: ProviderSettings,
    source: AuthorizedSource,
    *,
    source_event_id: str,
    resource_uri: str,
    event_type: str,
    occurred_at: str,
    version_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the canonical Work API event fields with a deterministic source version."""

    version = _source_version(version_payload)
    return {
        "source": "azure_devops",
        "sourceEventId": source_event_id,
        "resourceUri": resource_uri,
        "sourceVersion": version,
        "eventType": event_type,
        "occurredAt": occurred_at,
    }


def _source_version(version_payload: Mapping[str, Any]) -> str:
    """Hash normalized source facts so each immutable status version has a stable ID."""

    return hashlib.sha256(canonical_json(dict(version_payload))).hexdigest()


def _scope_key(settings: ProviderSettings, source: AuthorizedSource) -> str:
    """Create a compact namespace that changes with the configured source scope."""

    scope = {
        "organization": settings.organization,
        "project_id": source.project_id,
        "source_id": source.source_id,
        "kind": source.kind,
        "pipeline_id": source.pipeline_id,
        "definition_id": source.definition_id,
        "definition_environment_id": source.definition_environment_id,
    }
    return hashlib.sha256(canonical_json(scope)).hexdigest()[:16]


def _semantic_fingerprint(value: Mapping[str, Any]) -> str:
    """Hash semantic status facts without timestamps or source-version identifiers."""

    return hashlib.sha256(canonical_json(dict(value))).hexdigest()


def _pipeline_semantic_state(run_state: str, result: str) -> str:
    """Classify pipeline state for workflow incident deduplication."""

    normalized_state = run_state.casefold()
    normalized_result = result.casefold()
    if normalized_state == "completed" and normalized_result == "succeeded":
        return "healthy"
    if normalized_state == "completed" and normalized_result in {"failed", "canceled"}:
        return "incident"
    return "neutral"


def _deployment_semantic_state(deployment_status: str, operation_status: str) -> str:
    """Classify deployment state while keeping queued and running stages neutral."""

    deployment = deployment_status.casefold()
    operation = operation_status.casefold()
    if deployment == "succeeded" and operation in {"phasesucceeded", "all", "undefined"}:
        return "healthy"
    if deployment in {"failed", "partiallysucceeded", "canceled", "cancelled", "rejected"}:
        return "incident"
    if operation in {"phasefailed", "gatefailed", "rejected", "canceled", "phasecanceled"}:
        return "incident"
    return "neutral"


def _status_semantic_state(status: str) -> str:
    """Classify release environment status for workflow incident deduplication."""

    normalized = status.casefold()
    if normalized == "succeeded":
        return "healthy"
    if normalized in {"failed", "rejected", "canceled", "cancelled", "partiallysucceeded"}:
        return "incident"
    return "neutral"


def _pipeline_run_uri(organization: str, project_id: str, pipeline_id: int, run_id: int) -> str:
    """Build the fixed Azure DevOps run results page from configured identifiers."""

    org = quote(organization, safe="")
    project = quote(project_id, safe="")
    return f"https://dev.azure.com/{org}/{project}/_build/results?buildId={run_id}&view=results"


def _release_environment_uri(
    organization: str,
    project_id: str,
    release_id: int,
    environment_id: int,
) -> str:
    """Build a fixed release progress URI without following vendor-supplied links."""

    org = quote(organization, safe="")
    project = quote(project_id, safe="")
    query = urlencode(
        {
            "_a": "release-pipeline-progress",
            "releaseId": release_id,
            "environmentId": environment_id,
        }
    )
    return f"https://dev.azure.com/{org}/{project}/_releaseProgress?{query}"


def _http_failure(response: httpx.Response) -> AzureDevOpsApiFailure:
    """Map HTTP status to a stable error without exposing vendor response text."""

    status = response.status_code
    if status == 401:
        return AzureDevOpsApiFailure("AUTHENTICATION_FAILED")
    if status == 403:
        return AzureDevOpsApiFailure("AUTHORIZATION_DENIED")
    if status == 404:
        return AzureDevOpsApiFailure("RESOURCE_NOT_FOUND")
    if status == 429:
        return AzureDevOpsApiFailure(
            "UPSTREAM_THROTTLED", retryable=True, retry_after_seconds=_retry_after(response)
        )
    if status >= 500:
        return AzureDevOpsApiFailure("UPSTREAM_UNAVAILABLE", retryable=True)
    return AzureDevOpsApiFailure("UPSTREAM_REJECTED")


def _retry_after(response: httpx.Response) -> int | None:
    """Read a bounded integer Retry-After hint from the upstream response."""

    value = response.headers.get("Retry-After")
    if value is None or not value.isdigit():
        return None
    return min(int(value), 3600)


def _value_list(value: Any) -> list[Any]:
    """Accept the documented array response or a standard value envelope."""

    if isinstance(value, list):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("value"), list):
        return value["value"]
    raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")


def _continuation_token(response: httpx.Response) -> str | None:
    """Extract the opaque continuation response header without following it."""

    value = response.headers.get("x-ms-continuationtoken")
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_vendor_time(value: Any) -> datetime:
    """Parse an upstream RFC3339 timestamp and require an explicit UTC offset."""

    parsed = _optional_vendor_time(value)
    if parsed is None:
        raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
    return parsed


def _optional_vendor_time(value: Any) -> datetime | None:
    """Parse an optional aware upstream timestamp."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
    return parsed.astimezone(UTC)


def _format_time(value: datetime) -> str:
    """Render a timestamp as canonical UTC RFC3339."""

    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_text(value: Any, maximum: int) -> str | None:
    """Return a normalized bounded text projection, never arbitrary objects."""

    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:maximum] if normalized else None


def _nested_text(value: Mapping[str, Any], parent: str, key: str, maximum: int) -> str | None:
    """Read bounded text from a vendor object only when its parent has the right shape."""

    nested = value.get(parent)
    return _bounded_text(nested.get(key), maximum) if isinstance(nested, Mapping) else None


def _positive_vendor_id(value: Any) -> int:
    """Require a positive 32-bit integer identifier from Azure DevOps."""

    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
        raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
    return value


def _nested_positive_id(value: Mapping[str, Any], parent: str, key: str) -> int:
    """Require a positive integer identifier inside a vendor reference object."""

    nested = value.get(parent)
    return _positive_vendor_id(nested.get(key)) if isinstance(nested, Mapping) else _raise_invalid()


def _optional_nonnegative_int(value: Any) -> int | None:
    """Return an optional nonnegative integer without accepting booleans."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
    return value


def _latest_deployment_time(value: Any) -> datetime | None:
    """Find the latest status timestamp in a release environment's deployment steps."""

    if not isinstance(value, list):
        return None
    timestamps: list[datetime] = []
    for step in value:
        if not isinstance(step, Mapping):
            continue
        for field in ("lastModifiedOn", "finishTime", "startTime", "queuedOn"):
            parsed = _optional_vendor_time(step.get(field))
            if parsed is not None:
                timestamps.append(parsed)
    return max(timestamps, default=None)


def _severity(status: str) -> str:
    """Map explicit failure states to a bounded event severity."""

    normalized = status.casefold()
    if normalized in {"failed", "rejected", "gatefailed", "canceled", "cancelled"}:
        return "critical"
    if normalized in {"partiallysucceeded", "unknown", "undefined"}:
        return "warning"
    return "info"


def _run_id_from_event(source_event_id: str) -> int:
    """Sort run events by their terminal numeric component."""

    try:
        return int(source_event_id.rsplit(":", 1)[1])
    except (IndexError, ValueError) as error:
        raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE") from error


def _raise_invalid() -> Any:
    """Raise the common invalid provider response classification."""

    raise AzureDevOpsApiFailure("INVALID_PROVIDER_RESPONSE")
