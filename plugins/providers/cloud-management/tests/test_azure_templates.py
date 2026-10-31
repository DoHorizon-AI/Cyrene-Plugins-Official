"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud-management provider ARM scope tests                  │
│  Role: Verify deployment templates stay in the bound resource group.│
│                                                                      │
│  模块职责：验证部署模板不会离开绑定的 Azure 资源组。                   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from typing import Any

import pytest

from cloud_management_provider.azure_templates import validate_deployment_template

_SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"
_RESOURCE_GROUP = "cyrene-test-rg"
_GROUP_SCOPE = f"/subscriptions/{_SUBSCRIPTION}/resourceGroups/{_RESOURCE_GROUP}"


def _nested_deployment(
    template: dict[str, Any],
    *,
    subscription_id: str | None = None,
    resource_group: str | None = None,
    **properties: Any,
) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "type": "Microsoft.Resources/deployments",
        "apiVersion": "2022-09-01",
        "name": "child-deployment",
        "properties": {"mode": "Incremental", "template": template, **properties},
    }
    if subscription_id is not None:
        resource["subscriptionId"] = subscription_id
    if resource_group is not None:
        resource["resourceGroup"] = resource_group
    return resource


def test_accepts_mixed_resources_and_inline_same_group_deployments() -> None:
    template = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "resources": {
            "storage": {
                "type": "Microsoft.Storage/storageAccounts",
                "apiVersion": "2023-05-01",
                "name": "[format('cyrenetest{0}', uniqueString(resourceGroup().id))]",
                "location": "[resourceGroup().location]",
                "properties": {
                    "metadata": {
                        "scope": "[tenant().tenantId]",
                        "resourceGroup": "elsewhere",
                    }
                },
            },
            "nested": _nested_deployment(
                {
                    "targetScope": "resourceGroup",
                    "resources": [
                        {
                            "type": "Microsoft.Network/virtualNetworks",
                            "apiVersion": "2023-09-01",
                            "name": "[parameters('vnetName')]",
                            "properties": {"addressSpace": {"addressPrefixes": ["10.40.0.0/16"]}},
                        },
                        {
                            "type": "Microsoft.Resources/deployments",
                            "apiVersion": "2022-09-01",
                            "name": "second-level",
                            "subscriptionId": _SUBSCRIPTION,
                            "resourceGroup": _RESOURCE_GROUP,
                            "properties": {
                                "mode": "Incremental",
                                "template": {
                                    "resources": [
                                        {
                                            "type": "Microsoft.Compute/virtualMachines",
                                            "apiVersion": "2024-03-01",
                                            "name": "[parameters('vmName')]",
                                            "properties": {
                                                "hardwareProfile": {"vmSize": "Standard_B1s"}
                                            },
                                        }
                                    ]
                                },
                            },
                        },
                    ],
                },
                subscription_id=_SUBSCRIPTION,
                resource_group=_RESOURCE_GROUP,
            ),
        },
    }

    validate_deployment_template(template, _SUBSCRIPTION, _RESOURCE_GROUP)


@pytest.mark.parametrize(
    ("selector", "value"),
    [
        ("subscriptionId", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        ("resourceGroup", "other-resource-group"),
        ("scope", "/subscriptions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/resourceGroups/other"),
    ],
)
def test_rejects_nested_deployment_outside_bound_scope(selector: str, value: str) -> None:
    nested = _nested_deployment({"resources": []})
    nested[selector] = value

    with pytest.raises(ValueError, match="outside the bound"):
        validate_deployment_template({"resources": [nested]}, _SUBSCRIPTION, _RESOURCE_GROUP)


@pytest.mark.parametrize("selector", ["tenantId", "managementGroupId"])
def test_rejects_tenant_and_management_group_deployments(selector: str) -> None:
    nested = _nested_deployment({"resources": []})
    nested[selector] = "11111111-2222-3333-4444-555555555555"

    with pytest.raises(ValueError, match="tenant and management-group"):
        validate_deployment_template({"resources": [nested]}, _SUBSCRIPTION, _RESOURCE_GROUP)


@pytest.mark.parametrize(
    ("selector", "value"),
    [
        ("subscriptionId", "[parameters('subscriptionId')]"),
        ("resourceGroup", "[resourceGroup().name]"),
        ("scope", "[resourceId('Microsoft.Storage/storageAccounts', 'x')]"),
    ],
)
def test_rejects_scope_expressions_that_cannot_be_proven_local(selector: str, value: str) -> None:
    nested = _nested_deployment({"resources": []})
    nested[selector] = value

    with pytest.raises(ValueError, match="must be a literal bound scope"):
        validate_deployment_template({"resources": [nested]}, _SUBSCRIPTION, _RESOURCE_GROUP)


@pytest.mark.parametrize(
    "hidden_reference",
    [
        {"templateLink": {"uri": "https://example.invalid/template.json"}},
        {
            "templateSpecId": (
                "/subscriptions/other/resourceGroups/rg/providers/"
                "Microsoft.Resources/templateSpecs/x/versions/1"
            )
        },
        {"templateSpec": {"id": "uninspectable-template-spec"}},
    ],
)
def test_rejects_linked_templates_and_template_specs(hidden_reference: dict[str, Any]) -> None:
    nested = _nested_deployment({"resources": []}, **hidden_reference)

    with pytest.raises(ValueError, match="cannot be inspected"):
        validate_deployment_template({"resources": [nested]}, _SUBSCRIPTION, _RESOURCE_GROUP)


@pytest.mark.parametrize("target_scope", ["subscription", "managementGroup", "tenant"])
def test_rejects_non_resource_group_template_target_scope(target_scope: str) -> None:
    with pytest.raises(ValueError, match="targetScope must be resourceGroup"):
        validate_deployment_template(
            {"targetScope": target_scope, "resources": []}, _SUBSCRIPTION, _RESOURCE_GROUP
        )


def test_accepts_literal_same_group_resource_scope() -> None:
    validate_deployment_template(
        {
            "resources": [
                {
                    "type": "Microsoft.Authorization/roleAssignments",
                    "apiVersion": "2022-04-01",
                    "name": "[guid(resourceGroup().id, 'assignment')]",
                    "scope": _GROUP_SCOPE,
                    "properties": {"roleDefinitionId": "[parameters('roleDefinitionId')]"},
                }
            ]
        },
        _SUBSCRIPTION,
        _RESOURCE_GROUP,
    )


def test_accepts_relative_literal_child_resource_types() -> None:
    template = {
        "resources": [
            {
                "type": "Microsoft.Storage/storageAccounts",
                "name": "[parameters('storageName')]",
                "resources": [
                    {
                        "type": "blobServices",
                        "name": "default",
                        "resources": [
                            {
                                "type": "containers",
                                "name": "artifacts",
                                "properties": {"publicAccess": "None"},
                            }
                        ],
                    }
                ],
            }
        ]
    }

    validate_deployment_template(template, _SUBSCRIPTION, _RESOURCE_GROUP)


def test_rejects_dynamic_resource_type_that_could_hide_external_deployment() -> None:
    template = {
        "resources": [
            {
                "type": (
                    "[if(parameters('deploy'), 'Microsoft.Resources/deployments', "
                    "'Microsoft.Storage/storageAccounts')]"
                ),
                "name": "dynamic-type",
                "subscriptionId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "properties": {"templateLink": {"uri": "https://example.invalid/hidden.json"}},
            }
        ]
    }

    with pytest.raises(ValueError, match="resource type must be a nonempty provable literal"):
        validate_deployment_template(template, _SUBSCRIPTION, _RESOURCE_GROUP)


@pytest.mark.parametrize("resource_type", [None, 7, [], {}, ""])
def test_rejects_missing_or_non_string_resource_type(resource_type: Any) -> None:
    template = {"resources": [{"type": resource_type, "name": "ordinary-resource"}]}

    with pytest.raises(ValueError, match="resource type must be a nonempty provable literal"):
        validate_deployment_template(template, _SUBSCRIPTION, _RESOURCE_GROUP)


def test_rejects_excessively_nested_templates() -> None:
    nested_template: dict[str, Any] = {"resources": []}
    for _index in range(34):
        nested_template = {
            "resources": [_nested_deployment(nested_template, resource_group=_RESOURCE_GROUP)]
        }

    with pytest.raises(ValueError, match="depth exceeds the inspection limit"):
        validate_deployment_template(nested_template, _SUBSCRIPTION, _RESOURCE_GROUP)
