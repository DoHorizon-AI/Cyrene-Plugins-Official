"""Exercise signed conversation tools across the public schema and wire codec.

中文：验证真实签名、序列化及主体隔离，避免仅登记方法名却无法调用。
"""

from dataclasses import replace

import jsonschema
import pytest
from cyrene_integration_contracts import (
    TOOL_OPERATIONS,
    parse_tool_envelope,
    serialize_trusted_caller_context,
    sign_context,
    validate_schema,
    verify_context,
)

CONVERSATION_METHODS = tuple(method for method in TOOL_OPERATIONS if "." in method)


@pytest.mark.parametrize("method", CONVERSATION_METHODS)
def test_conversation_signature_roundtrip_and_scope_isolation(method: str) -> None:
    """Verify a wire envelope, then reject changed arguments and actor scope."""

    payload = {"request_ref": "request-1", "query": "authorized content"}
    context = sign_context(
        method=method,
        operation_id=method,
        payload=payload,
        caller_workload="navigator",
        audience="conversation-tools",
        issuer="https://identity.example",
        subject="actor-1",
        organization_id="org-1",
        workspace_id="workspace-1",
        binding_id="binding-1",
        request_id="request-1",
        key=b"c" * 32,
    )
    envelope = parse_tool_envelope(
        {
            "caller_context": serialize_trusted_caller_context(context),
            "arguments": payload,
        }
    )
    verification = {
        "method": method,
        "operation_id": method,
        "payload": payload,
        "context": envelope.caller_context,
        "key": b"c" * 32,
        "expected_caller_workload": "navigator",
        "expected_audience": "conversation-tools",
        "expected_binding_id": "binding-1",
        "expected_organization_id": "org-1",
        "expected_workspace_id": "workspace-1",
        "expected_issuer": "https://identity.example",
        "expected_subject": "actor-1",
    }
    assert verify_context(**verification)
    for field, changed in (
        ("payload", {**payload, "query": "private content"}),
        ("expected_subject", "actor-2"),
        ("expected_workspace_id", "workspace-2"),
        ("expected_binding_id", "binding-2"),
        ("key", b"d" * 32),
    ):
        assert not verify_context(**{**verification, field: changed})
    other_method = next(
        candidate for candidate in CONVERSATION_METHODS if candidate != method
    )
    forged = replace(context, operation_id=other_method)
    with pytest.raises(jsonschema.ValidationError):
        validate_schema(
            "TrustedCallerContext",
            {**context.as_dict(), "operation_id": other_method},
        )
    assert not verify_context(**{**verification, "context": forged})


def test_conversation_methods_are_explicitly_allowlisted() -> None:
    """An arbitrary dotted method never becomes a supported tool envelope."""

    assert len(CONVERSATION_METHODS) == 24
    with pytest.raises(ValueError, match="fixed tool pair"):
        sign_context(
            method="knowledge.arbitrary",
            operation_id="knowledge.arbitrary",
            payload={},
            caller_workload="navigator",
            audience="conversation-tools",
            issuer="https://identity.example",
            subject="actor-1",
            organization_id="org-1",
            workspace_id="workspace-1",
            binding_id="binding-1",
            request_id="request-1",
            key=b"c" * 32,
        )
