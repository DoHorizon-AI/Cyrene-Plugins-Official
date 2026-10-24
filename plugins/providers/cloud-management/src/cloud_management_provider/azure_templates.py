"""
┌─────────────────────────────────────────────────────────────────────┐
│  Module: cloud_management_provider.azure_templates                  │
│  Role: Keep ARM template deployments inside a bound resource group. │
│                                                                      │
│  模块职责：确保 ARM 模板部署保持在绑定的资源组范围内。                 │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_MAX_TEMPLATE_DEPTH = 32
_MAX_RESOURCE_NODES = 10_000
_DEPLOYMENT_TYPE = "microsoft.resources/deployments"
_ARM_TYPE_SEGMENT = re.compile(r"[A-Za-z0-9._-]+\Z")
_ARM_PROVIDER_NAMESPACE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)*\Z")
_SCOPE_FIELD_NAMES = {
    "managementgroupid": "managementGroupId",
    "resourcegroup": "resourceGroup",
    "scope": "scope",
    "subscriptionid": "subscriptionId",
    "tenantid": "tenantId",
}


def validate_deployment_template(
    template: Mapping[str, Any], subscription_id: str, resource_group: str
) -> None:
    """Reject ARM deployment scopes that escape the selected subscription and group.

    This checks only scope selectors and externally referenced nested templates. Azure remains
    responsible for validating the rest of the ARM template schema and resource properties.

    Args:
        template: The inline ARM template sent to the resource-group deployment endpoint.
        subscription_id: The subscription fixed by the authenticated provider profile.
        resource_group: The resource group fixed by the authenticated provider profile.

    Raises:
        ValueError: If a deployment scope is outside the profile or cannot be proven local.
    """

    if not isinstance(template, Mapping):
        raise ValueError("ARM deployment template must be an object")
    if not isinstance(subscription_id, str) or not subscription_id:
        raise ValueError("ARM deployment requires a bound subscription")
    if not isinstance(resource_group, str) or not resource_group:
        raise ValueError("ARM deployment requires a bound resource group")

    expected_subscription = subscription_id.casefold()
    expected_group = resource_group.casefold()
    expected_scope = f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}".casefold()
    visited_nodes = 0
    active_templates: set[int] = set()
    active_resource_collections: set[int] = set()
    missing = object()

    def require_same_scope(value: Any, field: str) -> None:
        """Accept only literal values that identify the configured subscription/group."""

        if not isinstance(value, str) or value.startswith("[") or value.endswith("]"):
            raise ValueError(f"ARM deployment {field} must be a literal bound scope")
        if field == "subscriptionId":
            matches = value.casefold() == expected_subscription
        elif field == "resourceGroup":
            matches = value.casefold() == expected_group
        else:
            matches = value.rstrip("/").casefold() == expected_scope
        if not matches:
            raise ValueError(
                "ARM deployment targets outside the bound subscription or resource group"
            )

    def scope_fields(value: Mapping[str, Any]) -> dict[str, Any]:
        """Return recognized scope keys without descending into arbitrary resource data."""

        fields: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() in _SCOPE_FIELD_NAMES:
                normalized = _SCOPE_FIELD_NAMES[key.casefold()]
                if normalized in fields:
                    raise ValueError("ARM deployment has ambiguous scope selectors")
                fields[normalized] = item
        return fields

    def member(value: Mapping[str, Any], name: str) -> Any:
        """Read an ARM field case-insensitively and reject ambiguous spellings."""

        result = missing
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() == name.casefold():
                if result is not missing:
                    raise ValueError("ARM template contains ambiguous field names")
                result = item
        return result

    def resources_from(value: Any) -> Sequence[Any]:
        """Read ARM resource arrays and symbolic-name resource maps."""

        if value is None:
            return ()
        if isinstance(value, list):
            return value
        if isinstance(value, Mapping):
            return tuple(value.values())
        raise ValueError("ARM template resources must be an array or symbolic resource map")

    def resolve_resource_type(value: Any, parent_type: str | None) -> str:
        """Resolve a literal provider type or a relative child type under its parent."""

        if not isinstance(value, str) or not value or "[" in value or "]" in value:
            raise ValueError("ARM resource type must be a nonempty provable literal")
        parts = value.split("/")
        if any(not _ARM_TYPE_SEGMENT.fullmatch(part) for part in parts):
            raise ValueError("ARM resource type has invalid literal syntax")

        # Provider-qualified types are absolute; nested child types inherit their parent type.
        provider_qualified = (
            len(parts) > 1
            and _ARM_PROVIDER_NAMESPACE.fullmatch(parts[0])
            and ("." in parts[0] or parts[0].casefold() == "microsoft")
        )
        if provider_qualified:
            return value
        if parent_type is None:
            raise ValueError("ARM root resource type must include a provider namespace")
        return f"{parent_type}/{value}"

    def visit_resource_collection(
        resources: Any, depth: int, parent_type: str | None = None
    ) -> None:
        nonlocal visited_nodes
        if depth > _MAX_TEMPLATE_DEPTH:
            raise ValueError("ARM deployment resource nesting exceeds the inspection limit")
        if isinstance(resources, (Mapping, list)):
            identity = id(resources)
            if identity in active_resource_collections:
                raise ValueError("ARM deployment resource structure contains a cycle")
            active_resource_collections.add(identity)
        else:
            identity = None

        try:
            for resource in resources_from(resources):
                visited_nodes += 1
                if visited_nodes > _MAX_RESOURCE_NODES:
                    raise ValueError("ARM deployment resource count exceeds the inspection limit")
                if not isinstance(resource, Mapping):
                    raise ValueError("ARM template resources must be objects")
                visit_resource(resource, depth, parent_type)
        finally:
            if identity is not None:
                active_resource_collections.remove(identity)

    def visit_resource(resource: Mapping[str, Any], depth: int, parent_type: str | None) -> None:
        fields = scope_fields(resource)
        resource_type = member(resource, "type")
        resolved_type = resolve_resource_type(resource_type, parent_type)
        is_deployment = resolved_type.casefold() == _DEPLOYMENT_TYPE

        # Resource-level scope is used by extension resources and must identify the same group.
        if "scope" in fields:
            require_same_scope(fields["scope"], "scope")

        if is_deployment:
            if "tenantId" in fields or "managementGroupId" in fields:
                raise ValueError(
                    "ARM tenant and management-group deployment scopes are not allowed"
                )
            if "subscriptionId" in fields:
                require_same_scope(fields["subscriptionId"], "subscriptionId")
            if "resourceGroup" in fields:
                require_same_scope(fields["resourceGroup"], "resourceGroup")

            properties = member(resource, "properties")
            if not isinstance(properties, Mapping):
                raise ValueError("ARM nested deployment properties must be an object")
            if any(
                member(properties, name) is not missing
                for name in ("templateLink", "templateSpecId", "templateSpec")
            ):
                raise ValueError(
                    "ARM linked templates and template specifications cannot be inspected"
                )
            inline_template = member(properties, "template")
            if not isinstance(inline_template, Mapping):
                raise ValueError(
                    "ARM nested deployment must contain an inspectable inline template"
                )
            visit_template(inline_template, depth + 1)

        nested_resources = member(resource, "resources")
        if nested_resources is not missing and nested_resources is not None:
            visit_resource_collection(nested_resources, depth + 1, resolved_type)

    def visit_template(current: Mapping[str, Any], depth: int) -> None:
        nonlocal visited_nodes
        if depth > _MAX_TEMPLATE_DEPTH:
            raise ValueError("ARM nested template depth exceeds the inspection limit")
        identity = id(current)
        if identity in active_templates:
            raise ValueError("ARM nested template structure contains a cycle")
        active_templates.add(identity)
        visited_nodes += 1
        if visited_nodes > _MAX_RESOURCE_NODES:
            active_templates.remove(identity)
            raise ValueError("ARM deployment resource count exceeds the inspection limit")
        try:
            target_scope = member(current, "targetScope")
            if target_scope is not missing and target_scope != "resourceGroup":
                raise ValueError("ARM deployment template targetScope must be resourceGroup")
            resources = member(current, "resources")
            visit_resource_collection(None if resources is missing else resources, depth)
        finally:
            active_templates.remove(identity)

    visit_template(template, 0)
