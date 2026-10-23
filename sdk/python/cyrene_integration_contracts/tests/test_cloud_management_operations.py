"""Tests for the fixed signed operations exposed by cloud-management tools.

中文：验证 cloud-management 工具使用固定且精确的签名操作标识。
"""

from datetime import UTC, datetime, timedelta

import pytest
from cyrene_integration_contracts import TOOL_OPERATIONS, sign_context, verify_context

_CLOUD_MANAGEMENT_OPERATIONS = {
    "cloud_provider_health": "cloud.provider.health.read",
    "cloud_resources_list": "cloud.resources.list",
    "cloud_resource_get": "cloud.resource.get",
    "cloud_resource_create": "cloud.resource.create",
    "cloud_resource_deploy": "cloud.resource.deploy",
    "cloud_resource_start": "cloud.resource.start",
    "cloud_resource_stop": "cloud.resource.stop",
    "cloud_resource_restart": "cloud.resource.restart",
    "cloud_resource_update": "cloud.resource.update",
    "cloud_resource_delete": "cloud.resource.delete",
    "cloud_operation_status": "cloud.operation.status.read",
    "cloud_operation_logs": "cloud.operation.logs.read",
    "cloud_operation_cancel": "cloud.operation.cancel",
    "cloud_vendor_cli": "cloud.vendor.cli.execute",
}


@pytest.mark.parametrize(
    ("method", "operation_id"), _CLOUD_MANAGEMENT_OPERATIONS.items()
)
def test_cloud_management_operation_is_fixed_and_signs_exact_arguments(
    method: str, operation_id: str
) -> None:
    """Bind profile, target, operation parameters, and actor to one signature."""

    key = b"m" * 32
    payload = {
        "profile_ref": "profile-1",
        "target": {"resource_id": "resource-1"},
        "parameters": {"size": "small"},
    }
    now = datetime.now(UTC)
    context = sign_context(
        method=method,
        operation_id=operation_id,
        payload=payload,
        caller_workload="exchange",
        audience="cloud-management",
        issuer="https://directory.example",
        subject="actor-1",
        organization_id="org-1",
        workspace_id="workspace-1",
        binding_id="binding-1",
        request_id="request-1",
        key=key,
        issued_at=now,
        expires_at=now + timedelta(seconds=240),
    )

    assert TOOL_OPERATIONS[method] == operation_id
    assert verify_context(
        method=method,
        operation_id=operation_id,
        payload=payload,
        context=context,
        key=key,
        expected_caller_workload="exchange",
        expected_audience="cloud-management",
        expected_binding_id="binding-1",
        expected_organization_id="org-1",
        expected_workspace_id="workspace-1",
        expected_issuer="https://directory.example",
        expected_subject="actor-1",
    )
    assert not verify_context(
        method=method,
        operation_id=operation_id,
        payload={**payload, "target": {"resource_id": "resource-2"}},
        context=context,
        key=key,
        expected_caller_workload="exchange",
        expected_audience="cloud-management",
        expected_binding_id="binding-1",
        expected_organization_id="org-1",
        expected_workspace_id="workspace-1",
        expected_issuer="https://directory.example",
        expected_subject="actor-1",
    )


def test_cloud_management_signature_rejects_wrong_operation_pair() -> None:
    """Do not allow a valid method to be reused for a different grant action."""

    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="fixed tool pair"):
        sign_context(
            method="cloud_resource_delete",
            operation_id="cloud.resource.start",
            payload={"profile_ref": "profile-1", "target": "resource-1"},
            caller_workload="exchange",
            audience="cloud-management",
            issuer="https://directory.example",
            subject="actor-1",
            organization_id="org-1",
            workspace_id="workspace-1",
            binding_id="binding-1",
            request_id="request-2",
            key=b"m" * 32,
            issued_at=now,
            expires_at=now + timedelta(seconds=240),
        )
