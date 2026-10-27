"""Signed provider, HTTP classification, and source paging regression tests.

All Azure DevOps requests use synthetic fixtures and mock transports.
中文：测试不访问真实组织、项目或账号。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from cyrene_account_center_contracts import AuthMaterial, payload_sha256, sign_context

from azure_devops_observation_provider import azure_devops_api as ado
from azure_devops_observation_provider._generated import tool_provider_pb2 as wire
from azure_devops_observation_provider.plugin import (
    CALL_TOOL_RESPONSE_TYPE_URL,
    AzureDevOpsObservationProvider,
    _lease_target,
    _profile_target_scope,
)
from azure_devops_observation_provider.settings import AuthorizedSource, ProviderSettings

EXCHANGE_KEY = b"x" * 32
PROVIDER_KEY = b"p" * 32
PROFILE_REF = "00000000-0000-4000-8000-000000000001"
RELEASE_PROFILE_REF = "00000000-0000-4000-8000-000000000013"
PROJECT_PROFILE_REF = "00000000-0000-4000-8000-000000000014"
LEASE_REF = "00000000-0000-4000-8000-000000000002"
TENANT_ID = "00000000-0000-4000-8000-000000000012"
OBJECT_ID = "00000000-0000-4000-8000-000000000021"
ACTOR = {
    "issuer": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
    "subject": OBJECT_ID,
    "organization_id": "org-4",
    "workspace_id": "workspace-8",
}
PROJECT_ID = "00000000-0000-4000-8000-000000000003"


def make_settings() -> ProviderSettings:
    """Build one synthetic binding with exact project and pipeline scopes."""

    return ProviderSettings(
        binding_id="azure-devops.binding.test",
        account_center_binding_id="account-center.binding.test",
        account_center_connection_ref="grpc://127.0.0.1:40000",
        account_center_transport="local",
        account_center_root_cert_ref=None,
        account_center_client_cert_ref=None,
        account_center_client_key_ref=None,
        host_secret_names={},
        organization="synthetic-org",
        tenant_id=TENANT_ID,
        sources=(
            AuthorizedSource(
                source_id="pipeline-source",
                kind="pipeline_runs",
                project_id=PROJECT_ID,
                project_name="Synthetic Project",
                profile_ref=PROFILE_REF,
                pipeline_id=23,
            ),
            AuthorizedSource(
                source_id="release-source",
                kind="release_deployments",
                project_id=PROJECT_ID,
                project_name="Synthetic Project",
                profile_ref=RELEASE_PROFILE_REF,
                definition_id=4,
                definition_environment_id=8,
            ),
            AuthorizedSource(
                source_id="project-source",
                kind="project_pipelines",
                project_id=PROJECT_ID,
                project_name="Synthetic Project",
                profile_ref=PROJECT_PROFILE_REF,
            ),
        ),
        provider_caller_id="azure-devops-provider",
        exchange_caller_id="exchange",
        exchange_verification_key=EXCHANGE_KEY,
        provider_signing_key=PROVIDER_KEY,
    )


class FakeAccountCenter:
    """Capture one-use profile lease requests and return synthetic auth material."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.used: set[str] = set()
        self.scope_digest_override: str | None = None
        self.external_subject_override: str | None = None

    def redeem_provider_credential_lease(
        self,
        payload: Mapping[str, object],
        trusted_context: Mapping[str, str | int | None],
    ) -> AuthMaterial:
        lease_ref = str(payload["lease_ref"])
        if lease_ref in self.used:
            raise RuntimeError("synthetic lease reused")
        self.used.add(lease_ref)
        self.calls.append((dict(payload), dict(trusted_context)))
        target = payload["target"]
        assert isinstance(target, Mapping)
        profile_scope = {
            key: target[key]
            for key in (
                "tenant_id",
                "organization",
                "project_id",
                "source_id",
                "source_kind",
                "pipeline_id",
                "definition_id",
                "definition_environment_id",
            )
            if key in target
        }
        return AuthMaterial(
            access_token="synthetic-ado-token",
            token_type="Bearer",
            external_subject=(
                self.external_subject_override or f"{TENANT_ID}:{OBJECT_ID}"
            ),
            expires_at=int(time.time()) + 300,
            profile_scope_sha256=self.scope_digest_override or payload_sha256(profile_scope),
        )


def signed_context(
    settings: ProviderSettings,
    arguments: Mapping[str, Any],
    tool_id: str,
    *,
    operation_id: str | None = None,
    profile_ref: str = PROFILE_REF,
    lease_ref: str = LEASE_REF,
    actor_claims: Mapping[str, str] | None = None,
    envelope_claims: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Sign the six provider claims plus actor, workspace, and operation context."""

    claims = _envelope_claims(
        settings,
        arguments,
        tool_id,
        profile_ref=profile_ref,
        lease_ref=lease_ref,
    )
    if envelope_claims is not None:
        claims.update(envelope_claims)
    payload = {
        "payload": dict(arguments),
        "profile_ref": profile_ref,
        "lease_ref": lease_ref,
        **claims,
    }
    claims = actor_claims or ACTOR
    now = int(time.time())
    return sign_context(
        method="call_tool",
        payload=payload,
        caller_id=settings.exchange_caller_id,
        issuer=claims["issuer"],
        subject=claims["subject"],
        organization_id=claims["organization_id"],
        workspace_id=claims["workspace_id"],
        binding_id=settings.binding_id,
        request_id="request-123",
        operation_id=operation_id or tool_id,
        key=settings.exchange_verification_key,
        issued_at=now,
        expires_at=now + 60,
    ).as_dict()


def invoke(
    provider: AzureDevOpsObservationProvider,
    settings: ProviderSettings,
    tool_id: str,
    arguments: Mapping[str, Any],
    *,
    profile_ref: str | None = None,
    lease_ref: str = LEASE_REF,
    context: Mapping[str, Any] | None = None,
    envelope_claims: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Call the provider through its canonical protobuf request and decode JSON."""

    if profile_ref is None:
        target = arguments.get("target")
        source_id = target.get("id") if isinstance(target, Mapping) else None
        profile_ref = next(
            (source.profile_ref for source in settings.sources if source.source_id == source_id),
            PROFILE_REF,
        )
    claims = _envelope_claims(
        settings,
        arguments,
        tool_id,
        profile_ref=profile_ref,
        lease_ref=lease_ref,
    )
    if envelope_claims is not None:
        claims.update(envelope_claims)
    envelope = {
        "payload": dict(arguments),
        "profile_ref": profile_ref,
        "lease_ref": lease_ref,
        **claims,
        "trusted_context": dict(
            context
            or signed_context(
                settings,
                arguments,
                tool_id,
                profile_ref=profile_ref,
                lease_ref=lease_ref,
                envelope_claims=claims,
            )
        ),
    }
    request = wire.CallToolRequest(
        binding_id=settings.binding_id,
        provider_tool_id=tool_id,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )
    ok, result = provider.on_invoke("tool.provider.v1", "call_tool", request.SerializeToString())
    assert ok is True
    assert result.type_url == CALL_TOOL_RESPONSE_TYPE_URL
    response = wire.CallToolResponse.FromString(result.value)
    return {
        "is_error": response.outcome.is_error,
        "value": json.loads(response.outcome.content[0].json.json),
    }


def _envelope_claims(
    settings: ProviderSettings,
    arguments: Mapping[str, Any],
    tool_id: str,
    *,
    profile_ref: str,
    lease_ref: str,
) -> dict[str, object]:
    """Build the signed target, full-payload digest, and activation-scope digest."""

    del profile_ref, lease_ref
    target = arguments.get("target")
    assert isinstance(target, Mapping)
    try:
        source = settings.source(str(target["id"]), str(target["kind"]))
    except ValueError:
        return {
            "target": dict(target),
            "arguments_sha256": payload_sha256(dict(arguments)),
            "profile_scope_sha256": "0" * 64,
        }
    return {
        "target": _lease_target(settings, source, tool_id, arguments),
        "arguments_sha256": payload_sha256(dict(arguments)),
        "profile_scope_sha256": payload_sha256(_profile_target_scope(settings, source)),
    }


def page_args(
    source_id: str,
    kind: str,
    *,
    page_size: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Build one fixed source page request over a UTC test window."""

    return {
        "target": {"id": source_id, "kind": kind, "bindingId": "azure-devops.binding.test"},
        "pageSize": page_size,
        "pageCursor": cursor,
        "windowStart": "2026-10-01T00:00:00Z",
        "windowEnd": "2026-10-28T00:00:00Z",
    }


def run_record(
    run_id: int,
    *,
    state: str = "completed",
    result: str = "succeeded",
) -> dict[str, Any]:
    """Return one synthetic pipeline run that contains fields the provider must omit."""

    created_date = f"2026-10-{(run_id % 20) + 1:02d}T10:00:00Z"
    return {
        "id": run_id,
        "name": f"run-{run_id}",
        "createdDate": created_date,
        "finishedDate": (
            f"2026-10-{(run_id % 20) + 1:02d}T10:05:00Z" if state == "completed" else None
        ),
        "state": state,
        "result": result,
        "pipeline": {"id": 23, "name": "Synthetic CI"},
        "variables": {"SECRET": {"value": "must-not-leak"}},
        "finalYaml": "token: must-not-leak",
    }


def deployment_record(deployment_id: int, status: str = "succeeded") -> dict[str, Any]:
    """Return one sanitized projection fixture for a release deployment."""

    return {
        "id": deployment_id,
        "attempt": 1,
        "deploymentStatus": status,
        "operationStatus": "phaseSucceeded",
        "lastModifiedOn": "2026-10-27T10:05:00Z",
        "release": {"id": deployment_id + 100, "name": f"release-{deployment_id}"},
        "releaseEnvironment": {"id": 8, "name": "Synthetic Test"},
        "releaseDefinition": {"id": 4, "name": "Synthetic Release"},
        "requestedBy": {"displayName": "Should not leak"},
    }


def provider_with_handler(
    handler: Any,
) -> tuple[AzureDevOpsObservationProvider, ProviderSettings, FakeAccountCenter]:
    """Build a provider around an injected HTTPS mock transport."""

    settings = make_settings()
    account_center_client = FakeAccountCenter()
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)
    api = ado.AzureDevOpsApi(settings, client=http_client)
    return (
        AzureDevOpsObservationProvider(
            settings=settings,
            account_center_client=account_center_client,
            api=api,
        ),
        settings,
        account_center_client,
    )


def test_signed_run_page_uses_fixed_host_and_omits_raw_yaml_and_variables() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[run_record(101, result="failed")])

    provider, settings, account_center_client = provider_with_handler(handler)
    arguments = page_args("pipeline-source", "pipeline_runs", page_size=10)
    response = invoke(provider, settings, "azure_devops.observations.poll", arguments)

    assert response["is_error"] is False
    page = response["value"]
    assert page["complete"] is True
    assert page["truncated"] is False
    assert page["pageCursor"] is None
    assert page["items"][0]["source"] == "azure_devops"
    assert page["items"][0]["eventType"] == "pipeline_run_status"
    assert page["items"][0]["severity"] == "critical"
    assert "must-not-leak" not in json.dumps(page)
    assert requests[0].method == "GET"
    assert requests[0].url.host == "dev.azure.com"
    assert requests[0].url.path.endswith(f"/{PROJECT_ID}/_apis/pipelines/23/runs")
    assert requests[0].url.params.get("api-version") == "7.1"
    assert requests[0].headers["Authorization"] == "Bearer synthetic-ado-token"
    lease, signed = account_center_client.calls[0]
    assert lease["operation_id"] == "azure_devops.observations.poll"
    assert lease["target"] == {
        "tenant_id": settings.tenant_id,
        "organization": "synthetic-org",
        "project_id": PROJECT_ID,
        "source_id": "pipeline-source",
        "source_kind": "pipeline_runs",
        "pipeline_id": 23,
    }
    assert signed["binding_id"] == settings.account_center_binding_id


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "AUTHENTICATION_FAILED"), (403, "AUTHORIZATION_DENIED")],
)
def test_authentication_and_project_forbidden_are_explicit_errors(
    status: int,
    expected: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="private upstream details that must not leak")

    provider, settings, account_center_client = provider_with_handler(handler)
    response = invoke(
        provider,
        settings,
        "azure_devops.pipeline_runs.list",
        page_args("pipeline-source", "pipeline_runs", page_size=10),
    )

    assert response["is_error"] is True
    assert response["value"]["error"]["code"] == expected
    assert response["value"]["error"]["retryable"] is False
    assert "private upstream" not in json.dumps(response)
    assert len(account_center_client.calls) == 1


def test_invalid_signed_operation_and_unconfigured_scope_fail_before_lease() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid invocation must not reach Azure DevOps")

    provider, settings, account_center_client = provider_with_handler(handler)
    arguments = page_args("pipeline-source", "pipeline_runs", page_size=10)
    wrong_operation = signed_context(
        settings,
        arguments,
        "azure_devops.pipeline_runs.list",
        operation_id="azure_devops.deployments.list",
    )
    denied = invoke(
        provider,
        settings,
        "azure_devops.pipeline_runs.list",
        arguments,
        context=wrong_operation,
    )
    assert denied["is_error"] is True
    assert denied["value"]["error"]["code"] == "AUTHENTICATION_DENIED"
    assert account_center_client.calls == []

    outside_scope = page_args("unconfigured-source", "pipeline_runs", page_size=10)
    denied_scope = invoke(provider, settings, "azure_devops.pipeline_runs.list", outside_scope)
    assert denied_scope["is_error"] is True
    assert denied_scope["value"]["error"]["code"] == "SOURCE_SCOPE_DENIED"
    assert account_center_client.calls == []


@pytest.mark.parametrize(
    ("claim", "error_code"),
    [
        ("target", "LEASE_BINDING_MISMATCH"),
        ("arguments_sha256", "LEASE_BINDING_MISMATCH"),
        ("profile_scope_sha256", "PROFILE_SCOPE_MISMATCH"),
    ],
)
def test_signed_target_argument_and_scope_claims_match_activation_before_redemption(
    claim: str,
    error_code: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid signed claim must not reach Azure DevOps")

    provider, settings, account_center_client = provider_with_handler(handler)
    arguments = page_args("pipeline-source", "pipeline_runs", page_size=10)
    expected_claims = _envelope_claims(
        settings,
        arguments,
        "azure_devops.pipeline_runs.list",
        profile_ref=PROFILE_REF,
        lease_ref=LEASE_REF,
    )
    if claim == "target":
        changed: object = {**expected_claims[claim], "source_id": "another-source"}
    else:
        changed = "0" * 64

    response = invoke(
        provider,
        settings,
        "azure_devops.pipeline_runs.list",
        arguments,
        envelope_claims={claim: changed},
    )

    assert response["is_error"] is True
    assert response["value"]["error"]["code"] == error_code
    assert account_center_client.calls == []


@pytest.mark.parametrize("claim", ["target", "arguments_sha256", "profile_scope_sha256"])
def test_exchange_signature_covers_each_non_context_lease_claim(claim: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("unsigned claim mutation must not reach Azure DevOps")

    provider, settings, account_center_client = provider_with_handler(handler)
    arguments = page_args("pipeline-source", "pipeline_runs", page_size=10)
    context = signed_context(settings, arguments, "azure_devops.pipeline_runs.list")
    original = _envelope_claims(
        settings,
        arguments,
        "azure_devops.pipeline_runs.list",
        profile_ref=PROFILE_REF,
        lease_ref=LEASE_REF,
    )
    if claim == "target":
        changed: object = {**original[claim], "source_id": "another-source"}
    else:
        changed = "0" * 64

    response = invoke(
        provider,
        settings,
        "azure_devops.pipeline_runs.list",
        arguments,
        context=context,
        envelope_claims={claim: changed},
    )

    assert response["is_error"] is True
    assert response["value"]["error"]["code"] == "AUTHENTICATION_DENIED"
    assert account_center_client.calls == []


def test_missing_trusted_entra_mapping_is_denied_before_profile_lease() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("actor mapping failure must not reach Azure DevOps")

    provider, settings, account_center_client = provider_with_handler(handler)
    arguments = page_args("pipeline-source", "pipeline_runs", page_size=10)
    context = signed_context(
        settings,
        arguments,
        "azure_devops.pipeline_runs.list",
        actor_claims={
            **ACTOR,
            "issuer": "https://identity.example.test",
            "subject": "member-27",
        },
    )

    response = invoke(
        provider,
        settings,
        "azure_devops.pipeline_runs.list",
        arguments,
        context=context,
    )

    assert response["is_error"] is True
    assert response["value"]["error"]["code"] == "TRUSTED_ACTOR_MAPPING_REQUIRED"
    assert account_center_client.calls == []


def test_redeemed_subject_must_match_original_entra_actor_before_rest() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("cross-user lease must not reach Azure DevOps")

    provider, settings, account_center_client = provider_with_handler(handler)
    account_center_client.external_subject_override = (
        f"{TENANT_ID}:00000000-0000-4000-8000-000000000099"
    )
    response = invoke(
        provider,
        settings,
        "azure_devops.pipeline_runs.list",
        page_args("pipeline-source", "pipeline_runs", page_size=10),
    )

    assert response["is_error"] is True
    assert response["value"]["error"]["code"] == "TRUSTED_ACTOR_PROFILE_MISMATCH"
    assert len(account_center_client.calls) == 1


def test_account_center_profile_scope_digest_must_match_exact_source_before_rest() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("profile scope mismatch must not reach Azure DevOps")

    provider, settings, account_center_client = provider_with_handler(handler)
    account_center_client.scope_digest_override = "0" * 64
    response = invoke(
        provider,
        settings,
        "azure_devops.pipeline_runs.list",
        page_args("pipeline-source", "pipeline_runs", page_size=10),
    )

    assert response["is_error"] is True
    assert response["value"]["error"]["code"] == "PROFILE_SCOPE_MISMATCH"
    assert len(account_center_client.calls) == 1


def test_pipeline_run_cursor_paginates_bounded_results_and_is_scope_bound() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[run_record(101), run_record(102), run_record(103)])

    provider, settings, account_center_client = provider_with_handler(handler)
    first_args = page_args("pipeline-source", "pipeline_runs", page_size=2)
    first = invoke(provider, settings, "azure_devops.pipeline_runs.list", first_args)
    assert first["is_error"] is False
    assert [item["sourceEventId"].rsplit(":", 1)[1] for item in first["value"]["items"]] == [
        "101",
        "102",
    ]
    assert first["value"]["complete"] is False
    assert first["value"]["truncated"] is False
    cursor = first["value"]["nextCursor"]
    assert isinstance(cursor, str)

    second_args = page_args("pipeline-source", "pipeline_runs", page_size=2, cursor=cursor)
    second = invoke(
        provider,
        settings,
        "azure_devops.pipeline_runs.list",
        second_args,
        lease_ref="00000000-0000-4000-8000-000000000004",
    )
    assert second["is_error"] is False
    assert [item["sourceEventId"].rsplit(":", 1)[1] for item in second["value"]["items"]] == ["103"]
    assert second["value"]["pageCursor"] == cursor
    assert second["value"]["nextCursor"] is None
    assert second["value"]["complete"] is True
    assert second["value"]["truncated"] is False

    changed_source = page_args("release-source", "release_deployments", page_size=2, cursor=cursor)
    denied = invoke(provider, settings, "azure_devops.observations.poll", changed_source)
    assert denied["is_error"] is True
    assert denied["value"]["error"]["code"] == "INVALID_CURSOR"
    assert calls == 2
    assert len(account_center_client.calls) == 2

    other_workspace = {**ACTOR, "workspace_id": "workspace-9"}
    other_actor_context = signed_context(
        settings,
        second_args,
        "azure_devops.pipeline_runs.list",
        lease_ref="00000000-0000-4000-8000-000000000010",
        actor_claims=other_workspace,
    )
    denied_workspace = invoke(
        provider,
        settings,
        "azure_devops.pipeline_runs.list",
        second_args,
        lease_ref="00000000-0000-4000-8000-000000000010",
        context=other_actor_context,
    )
    assert denied_workspace["is_error"] is True
    assert denied_workspace["value"]["error"]["code"] == "INVALID_CURSOR"
    assert len(account_center_client.calls) == 2


def test_release_deployment_paging_deduplicates_within_and_across_pages() -> None:
    requests: list[httpx.Request] = []
    responses = [
        httpx.Response(
            200,
            headers={"x-ms-continuationtoken": "17"},
            json={
                "count": 3,
                "value": [
                    deployment_record(201),
                    deployment_record(202),
                    deployment_record(202),
                ],
            },
        ),
        httpx.Response(
            200,
            json={"count": 2, "value": [deployment_record(202), deployment_record(203)]},
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    provider, settings, _account_center_client = provider_with_handler(handler)
    first_args = page_args("release-source", "release_deployments", page_size=3)
    first = invoke(provider, settings, "azure_devops.observations.poll", first_args)
    assert first["is_error"] is False
    assert len(first["value"]["items"]) == 2
    assert first["value"]["complete"] is False
    assert first["value"]["truncated"] is False

    cursor = first["value"]["nextCursor"]
    assert isinstance(cursor, str)
    second = invoke(
        provider,
        settings,
        "azure_devops.observations.poll",
        page_args("release-source", "release_deployments", page_size=3, cursor=cursor),
        lease_ref="00000000-0000-4000-8000-000000000005",
    )
    assert second["is_error"] is False
    assert len(second["value"]["items"]) == 1
    assert second["value"]["items"][0]["sourceEventId"].endswith(":203")
    assert second["value"]["complete"] is True
    assert second["value"]["truncated"] is False
    assert requests[0].url.host == "vsrm.dev.azure.com"
    assert requests[0].url.params.get("api-version") == "7.1"
    assert requests[1].url.params.get("continuationToken") == "17"
    assert requests[0].url.params.get("definitionId") == "4"
    assert requests[0].url.params.get("definitionEnvironmentId") == "8"


def test_pipeline_inventory_and_get_tools_use_fixed_v71_project_paths() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/_apis/pipelines"):
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "value": [
                        {
                            "id": 23,
                            "name": "Synthetic CI",
                            "folder": "\\",
                            "revision": 5,
                            "configuration": {"type": "yaml"},
                        }
                    ],
                },
            )
        if request.url.path.endswith("/_apis/pipelines/23/runs/501"):
            return httpx.Response(200, json=run_record(501))
        if request.url.path.endswith("/_apis/release/releases/801/environments/8"):
            return httpx.Response(
                200,
                json={
                    "id": 8,
                    "name": "Synthetic Test",
                    "release": {"id": 801},
                    "status": "inProgress",
                    "modifiedOn": "2026-10-27T11:00:00Z",
                },
            )
        pytest.fail(f"unexpected fixed endpoint path: {request.url.path}")

    provider, settings, _account_center_client = provider_with_handler(handler)
    pipeline_list = invoke(
        provider,
        settings,
        "azure_devops.pipelines.list",
        {
            "target": {
                "id": "project-source",
                "kind": "project_pipelines",
                "bindingId": settings.binding_id,
            },
            "pageSize": 2,
            "pageCursor": None,
        },
    )
    run_get = invoke(
        provider,
        settings,
        "azure_devops.pipeline_run.get",
        {
            "target": {
                "id": "pipeline-source",
                "kind": "pipeline_runs",
                "bindingId": settings.binding_id,
            },
            "runId": 501,
        },
        lease_ref="00000000-0000-4000-8000-000000000006",
    )
    environment_get = invoke(
        provider,
        settings,
        "azure_devops.release_environment.get",
        {
            "target": {
                "id": "release-source",
                "kind": "release_deployments",
                "bindingId": settings.binding_id,
            },
            "releaseId": 801,
            "environmentId": 8,
        },
        lease_ref="00000000-0000-4000-8000-000000000007",
    )

    assert pipeline_list["is_error"] is False
    assert pipeline_list["value"]["items"][0]["id"] == 23
    assert run_get["is_error"] is False
    assert run_get["value"]["item"]["sourceEventId"].endswith(":23:501")
    assert environment_get["is_error"] is False
    assert environment_get["value"]["item"]["eventType"] == "release_environment_status"
    assert [request.url.host for request in requests] == [
        "dev.azure.com",
        "dev.azure.com",
        "vsrm.dev.azure.com",
    ]
    assert requests[0].url.path.endswith(f"/{PROJECT_ID}/_apis/pipelines")
    assert requests[1].url.path.endswith(f"/{PROJECT_ID}/_apis/pipelines/23/runs/501")
    assert requests[2].url.path.endswith(f"/{PROJECT_ID}/_apis/release/releases/801/environments/8")
    assert all(request.url.params.get("api-version") == "7.1" for request in requests)


def test_pipeline_status_versions_have_distinct_immutable_event_ids() -> None:
    observations = [
        run_record(601, state="inProgress", result="unknown"),
        run_record(601, state="completed", result="succeeded"),
        run_record(601, state="completed", result="succeeded"),
    ]
    observations[2]["finishedDate"] = "2026-10-02T10:06:00Z"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=observations.pop(0))

    provider, settings, _account_center_client = provider_with_handler(handler)
    arguments = {
        "target": {
            "id": "pipeline-source",
            "kind": "pipeline_runs",
            "bindingId": settings.binding_id,
        },
        "runId": 601,
    }
    active = invoke(
        provider,
        settings,
        "azure_devops.pipeline_run.get",
        arguments,
        lease_ref="00000000-0000-4000-8000-000000000008",
    )["value"]["item"]
    completed = invoke(
        provider,
        settings,
        "azure_devops.pipeline_run.get",
        arguments,
        lease_ref="00000000-0000-4000-8000-000000000009",
    )["value"]["item"]
    repeated_healthy = invoke(
        provider,
        settings,
        "azure_devops.pipeline_run.get",
        arguments,
        lease_ref="00000000-0000-4000-8000-000000000011",
    )["value"]["item"]

    assert active["resourceUri"] == completed["resourceUri"]
    assert active["sourceEventId"] != completed["sourceEventId"]
    assert active["sourceVersion"] != completed["sourceVersion"]
    assert active["state"] == "neutral"
    assert completed["state"] == "healthy"
    assert active["semanticFingerprint"] != completed["semanticFingerprint"]
    assert repeated_healthy["sourceEventId"] != completed["sourceEventId"]
    assert repeated_healthy["semanticFingerprint"] == completed["semanticFingerprint"]


def test_runs_api_cap_marks_unprovable_window_truncated_without_cursor_advance() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        records = [run_record(run_id) for run_id in range(1, ado.RUNS_API_LIMIT + 1)]
        return httpx.Response(200, json=records)

    provider, settings, _account_center_client = provider_with_handler(handler)
    arguments = page_args("pipeline-source", "pipeline_runs", page_size=10)
    arguments["windowStart"] = "2020-01-01T00:00:00Z"
    response = invoke(provider, settings, "azure_devops.pipeline_runs.list", arguments)

    assert response["is_error"] is False
    page = response["value"]
    assert page["complete"] is False
    assert page["truncated"] is True
    assert page["nextCursor"] is None
    assert len(page["items"]) == 10


def test_provider_catalog_contains_only_fixed_read_operations() -> None:
    settings = make_settings()
    provider = AzureDevOpsObservationProvider(
        settings=settings,
        account_center_client=FakeAccountCenter(),
    )
    request = wire.ListToolsRequest(binding_id=settings.binding_id)

    ok, payload = provider.on_invoke("tool.provider.v1", "list_tools", request.SerializeToString())
    assert ok is True
    catalog = wire.ListToolsResponse.FromString(payload.value).catalog
    identifiers = [tool.provider_tool_id for tool in catalog.tools]
    assert identifiers == [
        "azure_devops.observations.poll",
        "azure_devops.pipelines.list",
        "azure_devops.pipeline_runs.list",
        "azure_devops.pipeline_run.get",
        "azure_devops.deployments.list",
        "azure_devops.release_environment.get",
    ]
    assert all("run_pipeline" not in item and "create_release" not in item for item in identifiers)


def test_configuration_schema_rejects_caller_endpoint_fields() -> None:
    from pathlib import Path

    import jsonschema

    package = Path(__file__).resolve().parents[1]
    schema = json.loads((package / "configuration.schema.json").read_text())
    config = {
        "exchange_caller_id": "exchange",
        "exchange_verification_key_ref": "azure-devops-observation/exchange-verification-key",
        "account_center": {
            "binding_id": "ac-binding",
            "connection_ref": "grpc://127.0.0.1:40000",
            "transport": "local",
            "caller_id": "provider",
            "signing_key_ref": "azure-devops-observation/account-center-provider-signing-key",
        },
        "organization": "synthetic-org",
        "tenant_id": "00000000-0000-4000-8000-000000000012",
        "authorized_sources": [
            {
                "source_id": "pipeline-source",
                "kind": "pipeline_runs",
                "project_id": PROJECT_ID,
                "project_name": "Synthetic Project",
                "profile_ref": PROFILE_REF,
                "pipeline_id": 23,
            }
        ],
        "hostSecretRefs": {
            "azure-devops-observation/exchange-verification-key": "EXCHANGE_KEY",
            "azure-devops-observation/account-center-provider-signing-key": "PROVIDER_KEY",
        },
    }
    jsonschema.validate(config, schema)
    config["endpoint"] = "https://caller-controlled.invalid"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(config, schema)
