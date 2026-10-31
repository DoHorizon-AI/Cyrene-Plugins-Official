"""Vendor fixture and transport-boundary tests for resource observations.

中文:使用供应商响应夹具验证观察映射和固定传输边界。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from cloud_resources_provider.cloud_api import (
    AZURE_MANAGEMENT_HOST,
    GCP_COMPUTE_HOST,
    HF_HUB_HOST,
    RUNPOD_API_HOST,
    TAILSCALE_API_HOST,
    BearerCredential,
    CloudProviderFailure,
    CloudResourceReader,
    DefaultCredentialAcquirer,
    HttpJsonTransport,
)
from cloud_resources_provider.settings import (
    AzureVmTarget,
    GcpInstanceTarget,
    HuggingFaceTarget,
    ProviderSettings,
    RunpodPodTarget,
    TailscaleDeviceTarget,
)

CHECKED_AT = datetime(2026, 10, 14, 14, 30, tzinfo=UTC)
ACCESS_TOKEN = "fixture-provider-access-token"


class FixtureTransport:
    """Return fixture JSON and record the exact fixed vendor requests."""

    def __init__(self, document: dict[str, Any] | CloudProviderFailure) -> None:
        self.document = document
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []
        self.oauth_document: dict[str, Any] = {
            "access_token": "fixture-tailscale-bearer-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "devices:core:read",
        }

    def get_json(
        self, url: str, *, expected_host: str, token: str | None, timeout: float
    ) -> dict[str, Any]:
        """Record one bounded GET and return the configured vendor fixture."""

        self.get_calls.append(
            {"url": url, "host": expected_host, "token": token, "timeout": timeout}
        )
        if isinstance(self.document, CloudProviderFailure):
            raise self.document
        return self.document

    def post_form(
        self, url: str, *, expected_host: str, form: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        """Record a token exchange in a form body, never in a query string."""

        self.post_calls.append(
            {"url": url, "host": expected_host, "form": dict(form), "timeout": timeout}
        )
        return self.oauth_document


class FixtureCredentials:
    """Provide a fixture bearer token without contacting an identity service."""

    def acquire(self, target: Any) -> BearerCredential:
        """Return a token valid for the provider scope under test."""

        return BearerCredential(ACCESS_TOKEN, CHECKED_AT.replace(hour=16))


def make_settings(
    targets: tuple[Any, ...],
    *,
    gcp_mode: str = "metadata",
    gcp_env_name: str | None = None,
    tailscale_client_id: str | None = None,
    tailscale_env_name: str | None = None,
    runpod_env_name: str | None = None,
    environment: dict[str, str] | None = None,
) -> ProviderSettings:
    """Construct one validated settings object for adapter-focused tests."""

    secret_names = {"cloud-resources/exchange-verification-key": "TEST_EXCHANGE_KEY"}
    if gcp_env_name is not None:
        secret_names["cloud-resources/gcp-service-account-json"] = gcp_env_name
    if tailscale_env_name is not None:
        secret_names["cloud-resources/tailscale-client-secret"] = tailscale_env_name
    if runpod_env_name is not None:
        secret_names["cloud-resources/runpod-api-key"] = runpod_env_name
    source = environment or {"TEST_EXCHANGE_KEY": "11" * 32}
    return ProviderSettings(
        binding_id="cloud.binding.fixture",
        authorization_generation=0,
        trusted_callers={},
        azure_client_id=None,
        gcp_auth_mode=gcp_mode,
        gcp_credential_environment_name=gcp_env_name,
        hf_token_environment_name=None,
        tailscale_client_id=tailscale_client_id,
        tailscale_client_secret_environment_name=tailscale_env_name,
        tailscale_tailnet="example.com" if tailscale_client_id is not None else None,
        runpod_api_key_environment_name=runpod_env_name,
        host_secret_names=secret_names,
        targets=targets,
        timeout_seconds=3.0,
        environ=source,
    )


def test_azure_vm_power_state_and_source_timestamp_are_preserved() -> None:
    """Azure power state comes only from instanceView status codes."""

    target = AzureVmTarget(
        "vm-prod",
        "Production VM",
        "11111111-2222-3333-4444-555555555555",
        "compute-prod",
        "worker-01",
    )
    fixture = FixtureTransport(
        {
            "statuses": [
                {"code": "ProvisioningState/succeeded", "time": "2026-10-14T14:29:00Z"},
                {"code": "PowerState/running", "time": "2026-10-14T14:29:37+00:00"},
            ]
        }
    )
    reader = CloudResourceReader(
        make_settings((target,)),
        transport=fixture,
        credentials=FixtureCredentials(),
        clock=lambda: CHECKED_AT,
    )

    observation, credential = reader.observe("vm-prod")

    assert observation.status == "RUNNING"
    assert observation.provider_status == "PowerState/RUNNING"
    assert observation.observed_at == datetime(2026, 10, 14, 14, 29, 37, tzinfo=UTC)
    assert observation.collected_at == CHECKED_AT
    assert observation.attributes == {"power_state": "running"}
    assert observation.platform_resource_ref is None
    assert credential.expires_at == CHECKED_AT.replace(hour=16)
    assert fixture.get_calls[0]["host"] == AZURE_MANAGEMENT_HOST
    assert fixture.get_calls[0]["url"].endswith("/instanceView?api-version=2024-07-01")
    assert fixture.get_calls[0]["token"] == ACCESS_TOKEN


def test_missing_azure_power_state_is_unknown_not_stopped() -> None:
    """A successful but state-less source response remains an honest UNKNOWN."""

    target = AzureVmTarget(
        "vm-prod",
        "Production VM",
        "11111111-2222-3333-4444-555555555555",
        "compute-prod",
        "worker-01",
    )
    reader = CloudResourceReader(
        make_settings((target,)),
        transport=FixtureTransport({"computerName": "worker-01"}),
        credentials=FixtureCredentials(),
        clock=lambda: CHECKED_AT,
    )

    observation, _ = reader.observe("vm-prod")

    assert observation.status == "UNKNOWN"
    assert observation.provider_status is None
    assert observation.observed_at is None
    assert observation.attributes == {}


def test_gcp_instance_status_maps_only_known_source_states() -> None:
    """GCE TERMINATED maps to STOPPED and retains no invented observation time."""

    target = GcpInstanceTarget(
        "gce-prod", "GCE worker", "project-fixture", "us-central1-a", "worker-1"
    )
    fixture = FixtureTransport(
        {
            "name": "worker-1",
            "zone": "https://www.googleapis.com/compute/v1/projects/project-fixture/zones/us-central1-a",
            "status": "TERMINATED",
            "machineType": "https://www.googleapis.com/compute/v1/projects/project-fixture/zones/us-central1-a/machineTypes/n2-standard-4",
            "lastStartTimestamp": "2026-10-14T12:00:00Z",
        }
    )
    reader = CloudResourceReader(
        make_settings((target,)),
        transport=fixture,
        credentials=FixtureCredentials(),
        clock=lambda: CHECKED_AT,
    )

    observation, _ = reader.observe("gce-prod")

    assert observation.status == "STOPPED"
    assert observation.provider_status == "TERMINATED"
    assert observation.observed_at is None
    assert observation.attributes == {"machine_type": "n2-standard-4"}
    assert fixture.get_calls[0]["host"] == GCP_COMPUTE_HOST
    assert (
        "/projects/project-fixture/zones/us-central1-a/instances/worker-1"
        in fixture.get_calls[0]["url"]
    )


def test_gcp_identity_mismatch_fails_instead_of_returning_another_instance() -> None:
    """Provider output must match the configured project-zone-instance selector."""

    target = GcpInstanceTarget(
        "gce-prod", "GCE worker", "project-fixture", "us-central1-a", "worker-1"
    )
    reader = CloudResourceReader(
        make_settings((target,)),
        transport=FixtureTransport({"name": "other-worker"}),
        credentials=FixtureCredentials(),
    )

    with pytest.raises(CloudProviderFailure, match="TARGET_IDENTITY_MISMATCH"):
        reader.observe("gce-prod")


@pytest.mark.parametrize(
    ("kind", "expected_path"),
    [
        ("hf.model", "/api/models/org-fixture/model-fixture"),
        ("hf.dataset", "/api/datasets/org-fixture/dataset-fixture"),
        ("hf.space", "/api/spaces/org-fixture/space-fixture"),
    ],
)
def test_hugging_face_reads_metadata_only_for_fixed_repository_type(
    kind: str, expected_path: str
) -> None:
    """HF repository details never select files, job endpoints, or runtime state."""

    repo_name = kind.split(".", maxsplit=1)[1]
    repo_id = f"org-fixture/{repo_name}-fixture"
    target = HuggingFaceTarget("hf-target", "Hub metadata", kind, repo_id)
    fixture = FixtureTransport(
        {
            "id": repo_id,
            "private": False,
            "gated": False,
            "disabled": False,
            "lastModified": "2026-10-10T12:00:00Z",
            "downloads": 17,
            "likes": 3,
            "tags": ["fixture", "metadata"],
            "siblings": [{"rfilename": "model.safetensors"}],
        }
    )
    reader = CloudResourceReader(
        make_settings((target,)),
        transport=fixture,
        credentials=FixtureCredentials(),
        clock=lambda: CHECKED_AT,
    )

    observation, _ = reader.observe("hf-target")

    assert observation.status == "AVAILABLE"
    assert observation.observed_at is None
    assert observation.attributes == {
        "private": False,
        "disabled": False,
        "gated": False,
        "last_modified": "2026-10-10T12:00:00.000000Z",
        "tag_count": 2,
        "downloads": 17,
        "likes": 3,
    }
    call = fixture.get_calls[0]
    assert call["host"] == HF_HUB_HOST
    assert call["url"].endswith(expected_path)
    assert "model.safetensors" not in str(observation.attributes)
    assert call["token"] == ACCESS_TOKEN


def test_tailscale_oauth_requests_only_read_scope_and_never_puts_secret_in_url() -> None:
    """OAuth access tokens are acquired with the fixed read-only scope in form data."""

    target = TailscaleDeviceTarget("device-prod", "Build runner", "example.com", "device-42")
    settings = make_settings(
        (target,),
        tailscale_client_id="client-fixture",
        tailscale_env_name="TEST_TAILSCALE_SECRET",
        environment={
            "TEST_EXCHANGE_KEY": "11" * 32,
            "TEST_TAILSCALE_SECRET": "private-tailscale-client-secret",
        },
    )
    transport = FixtureTransport({})
    credential = DefaultCredentialAcquirer(settings, transport=transport).acquire(target)

    assert credential.token == "fixture-tailscale-bearer-token"
    assert credential.expires_at is not None
    request = transport.post_calls[0]
    assert request["host"] == TAILSCALE_API_HOST
    assert request["url"] == "https://api.tailscale.com/api/v2/oauth/token"
    assert "private-tailscale-client-secret" not in request["url"]
    assert request["form"]["scope"] == "devices:core:read"


def test_tailscale_device_observation_omits_addresses_and_hostname() -> None:
    """Device presence and source last-seen time are safe metadata facts."""

    target = TailscaleDeviceTarget("device-prod", "Build runner", "example.com", "device-42")
    fixture = FixtureTransport(
        {
            "id": "device-42",
            "authorized": True,
            "isOnline": False,
            "lastSeen": "2026-10-14T14:28:00Z",
            "hostname": "secret-host.internal.example.com",
            "addresses": ["100.64.0.1"],
            "os": "linux",
            "clientVersion": "1.90.0",
        }
    )
    reader = CloudResourceReader(
        make_settings((target,)),
        transport=fixture,
        credentials=FixtureCredentials(),
        clock=lambda: CHECKED_AT,
    )

    observation, _ = reader.observe("device-prod")

    assert observation.status == "UNAVAILABLE"
    assert observation.provider_status == "OFFLINE"
    assert observation.observed_at is None
    assert observation.attributes == {
        "os": "linux",
        "client_version": "1.90.0",
        "last_seen": "2026-10-14T14:28:00.000000Z",
    }
    assert fixture.get_calls[0]["host"] == TAILSCALE_API_HOST
    assert "device-42" in fixture.get_calls[0]["url"]
    assert fixture.get_calls[0]["token"] == ACCESS_TOKEN


def test_runpod_expected_status_stays_unknown_and_uses_a_bearer_header_path() -> None:
    """Runpod desiredStatus is not treated as actual running compute."""

    target = RunpodPodTarget("pod-prod", "Training pod", "pod-42_fixture")
    fixture = FixtureTransport(
        {
            "id": "pod-42_fixture",
            "desiredStatus": "RUNNING",
            "lastStartedAt": "2026-10-14T13:00:00.000Z",
            "publicIp": "100.64.0.9",
            "env": {"SECRET": "private"},
            "image": "private-registry.example/model:latest",
        }
    )
    settings = make_settings(
        (target,),
        runpod_env_name="TEST_RUNPOD_KEY",
        environment={"TEST_EXCHANGE_KEY": "11" * 32, "TEST_RUNPOD_KEY": "fixture-runpod-key"},
    )
    reader = CloudResourceReader(
        settings,
        transport=fixture,
        credentials=DefaultCredentialAcquirer(settings, transport=fixture),
        clock=lambda: CHECKED_AT,
    )

    observation, _ = reader.observe("pod-prod")

    assert observation.status == "UNKNOWN"
    assert observation.provider_status == "desiredStatus/RUNNING"
    assert observation.observed_at is None
    assert observation.attributes == {
        "desired_status": "RUNNING",
        "last_started_at": "2026-10-14T13:00:00.000000Z",
    }
    call = fixture.get_calls[0]
    assert call["host"] == RUNPOD_API_HOST
    assert call["url"] == "https://api.runpod.io/v2/pods/pod-42_fixture"
    assert call["token"] == "fixture-runpod-key"
    assert "fixture-runpod-key" not in call["url"]


def test_runpod_v2_actual_status_does_not_claim_desired_or_observed_state() -> None:
    """Runpod v2 runtime status remains UNKNOWN in this observation connector."""

    target = RunpodPodTarget("pod-prod", "Training pod", "pod-42_fixture")
    fixture = FixtureTransport(
        {
            "id": "pod-42_fixture",
            "status": "RUNNING",
            "startedAt": "2026-10-14T13:00:00.000Z",
            "publicIp": "100.64.0.9",
            "env": {"SECRET": "private"},
            "image": "private-registry.example/model:latest",
        }
    )
    settings = make_settings(
        (target,),
        runpod_env_name="TEST_RUNPOD_KEY",
        environment={"TEST_EXCHANGE_KEY": "11" * 32, "TEST_RUNPOD_KEY": "fixture-runpod-key"},
    )
    reader = CloudResourceReader(
        settings,
        transport=fixture,
        credentials=DefaultCredentialAcquirer(settings, transport=fixture),
        clock=lambda: CHECKED_AT,
    )

    observation, _ = reader.observe("pod-prod")

    assert observation.status == "UNKNOWN"
    assert observation.provider_status is None
    assert observation.observed_at is None
    assert observation.attributes == {}
    assert fixture.get_calls[0]["host"] == "api.runpod.io"
    assert fixture.get_calls[0]["url"] == "https://api.runpod.io/v2/pods/pod-42_fixture"


def test_provider_403_remains_permission_denied_not_stopped() -> None:
    """An IAM denial is preserved as a typed failure and never rewritten as VM state."""

    target = AzureVmTarget(
        "vm-prod",
        "Production VM",
        "11111111-2222-3333-4444-555555555555",
        "compute-prod",
        "worker-01",
    )
    reader = CloudResourceReader(
        make_settings((target,)),
        transport=FixtureTransport(CloudProviderFailure("PERMISSION_DENIED")),
        credentials=FixtureCredentials(),
    )

    with pytest.raises(CloudProviderFailure) as error:
        reader.observe("vm-prod")

    assert error.value.code == "PERMISSION_DENIED"
    assert error.value.credential_present is True
    assert str(error.value) != "STOPPED"


def test_http_transport_rejects_redirect_and_never_places_bearer_in_url() -> None:
    """A vendor redirect cannot forward credentials to a new origin."""

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://attacker.example/collect"},
            json={"detail": "fixture"},
        )

    transport = HttpJsonTransport(httpx_transport=httpx.MockTransport(handler))

    with pytest.raises(CloudProviderFailure, match="REDIRECT_REJECTED"):
        transport.get_json(
            "https://management.azure.com/fixed/resource?api-version=2024-07-01",
            expected_host=AZURE_MANAGEMENT_HOST,
            token=ACCESS_TOKEN,
            timeout=1.0,
        )

    assert len(calls) == 1
    assert ACCESS_TOKEN not in str(calls[0].url)
    assert calls[0].headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_http_transport_hides_auth_response_body_and_rejects_off_origin_urls() -> None:
    """Error bodies and caller-provided hosts never cross the provider boundary."""

    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "private-response-detail"})

    transport = HttpJsonTransport(httpx_transport=httpx.MockTransport(unauthorized))
    with pytest.raises(CloudProviderFailure) as error:
        transport.get_json(
            "https://management.azure.com/fixed/resource",
            expected_host=AZURE_MANAGEMENT_HOST,
            token=ACCESS_TOKEN,
            timeout=1.0,
        )
    assert error.value.code == "AUTHENTICATION_REQUIRED"
    assert "private-response-detail" not in str(error.value)
    assert ACCESS_TOKEN not in str(error.value)

    with pytest.raises(CloudProviderFailure, match="UNSAFE_PROVIDER_ENDPOINT"):
        transport.get_json(
            "https://attacker.example/fixed/resource",
            expected_host=AZURE_MANAGEMENT_HOST,
            token=ACCESS_TOKEN,
            timeout=1.0,
        )


def test_http_transport_rejects_duplicate_response_keys_and_header_controls() -> None:
    """Ambiguous provider JSON and control-bearing bearer values fail closed."""

    calls: list[httpx.Request] = []

    def duplicate_key_response(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"id":"first","id":"second"}',
        )

    transport = HttpJsonTransport(httpx_transport=httpx.MockTransport(duplicate_key_response))
    with pytest.raises(CloudProviderFailure, match="INVALID_PROVIDER_RESPONSE"):
        transport.get_json(
            "https://management.azure.com/fixed/resource",
            expected_host=AZURE_MANAGEMENT_HOST,
            token=ACCESS_TOKEN,
            timeout=1.0,
        )
    assert len(calls) == 1

    with pytest.raises(CloudProviderFailure, match="INVALID_CREDENTIAL_CONFIGURATION"):
        transport.get_json(
            "https://management.azure.com/fixed/resource",
            expected_host=AZURE_MANAGEMENT_HOST,
            token="unsafe\r\nbearer",
            timeout=1.0,
        )
    assert len(calls) == 1
