"""Signed DirectPluginRuntime tests for the fixed knowledge search tool.

中文：验证宿主签名、服务器身份映射、固定 ArtifactRef 与查询游标。
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from cyrene_integration_contracts import (
    TOOL_OPERATIONS,
    serialize_trusted_caller_context,
    sign_context,
)
from cyrene_tool_provider_contracts import (
    CALL_TOOL_REQUEST_TYPE_URL,
    CALL_TOOL_RESPONSE_TYPE_URL,
    CAPABILITY_ID,
    LIST_TOOLS_REQUEST_TYPE_URL,
    LIST_TOOLS_RESPONSE_TYPE_URL,
    METHOD_CALL_TOOL,
    METHOD_LIST_TOOLS,
    TypedPayload,
    decode_response,
    encode_request,
    tool_provider_pb2,
)
from test_bundle import _build_bundle, _records

from knowledge_retrieval_provider import KnowledgeRetrievalProvider, RetrievalSettings
from knowledge_retrieval_provider.bundle import verify_bundle
from knowledge_retrieval_provider.plugin import TOOL_ID
from knowledge_retrieval_provider.settings import CONTEXT_KEY_REF

_KEY = b"\x37" * 32
_CALLER = {
    "caller_workload": "navigator.work",
    "audience": "knowledge-retrieval",
    "issuer": "https://identity.example.test",
    "subject": "subject:alice",
    "organization_id": "organization-1",
    "workspace_id": "workspace-1",
}


def _provider(
    tmp_path: Path,
    *,
    include_more_chunks: bool = False,
) -> tuple[KnowledgeRetrievalProvider, str]:
    root = tmp_path / "artifacts"
    root.mkdir()
    sources, chunks = _records()
    if include_more_chunks:
        for index in range(2):
            extra = copy.deepcopy(chunks[0])
            extra["chunkId"] = "sha256:" + hashlib.sha256(f"extra-{index}".encode()).hexdigest()
            extra["text"] = f"Cyrene memory note {index}"
            extra["blockId"] = f"block-{index + 2}"
            extra["textRange"] = {"start": 0, "end": len(extra["text"])}
            chunks.append(extra)
    path = root / "research.zip"
    digest = _build_bundle(path, sources, chunks)
    verified = verify_bundle(path, package_digest=digest)
    config = {
        "artifact_root": str(root),
        "trusted_caller": {
            "caller_workload": _CALLER["caller_workload"],
            "audience": _CALLER["audience"],
            "issuer": _CALLER["issuer"],
            "organization_id": _CALLER["organization_id"],
            "workspace_id": _CALLER["workspace_id"],
            "verification_key_ref": CONTEXT_KEY_REF,
        },
        "trusted_principals": [
            {
                "issuer": _CALLER["issuer"],
                "subject": _CALLER["subject"],
                "organization_id": _CALLER["organization_id"],
                "workspace_id": _CALLER["workspace_id"],
                "principal_refs": ["user:alice"],
            }
        ],
        "bundle_registry": [
            {
                "library_selector": "research",
                "bundle_locator": "research.zip",
                "artifact_ref": {
                    "uri": f"artifact://sha256/{digest.removeprefix('sha256:')}",
                    "digest": digest,
                    "size_bytes": path.stat().st_size,
                    "kind": "application/zip",
                    "manifest_digest": verified.manifest_digest,
                },
            }
        ],
        "hostSecretRefs": {CONTEXT_KEY_REF: "KNOWLEDGE_CALLER_CONTEXT_KEY"},
    }
    settings = RetrievalSettings.from_mapping(
        config,
        binding_id="knowledge-binding",
        environ={"KNOWLEDGE_CALLER_CONTEXT_KEY": _KEY.hex()},
    )
    return KnowledgeRetrievalProvider(settings=settings), digest


def _signed_request(
    arguments: dict[str, Any],
    *,
    subject: str = _CALLER["subject"],
    tamper_context: bool = False,
    tamper_arguments: bool = False,
) -> tool_provider_pb2.CallToolRequest:
    now = datetime.now(UTC)
    try:
        context = sign_context(
            method=TOOL_ID,
            operation_id=TOOL_OPERATIONS[TOOL_ID],
            payload=arguments,
            caller_workload=_CALLER["caller_workload"],
            audience=_CALLER["audience"],
            issuer=_CALLER["issuer"],
            subject=subject,
            organization_id=_CALLER["organization_id"],
            workspace_id=_CALLER["workspace_id"],
            binding_id="knowledge-binding",
            request_id=f"request-{now.timestamp()}",
            key=_KEY,
            issued_at=now,
            expires_at=now + timedelta(seconds=60),
        )
    except (KeyError, ValueError, jsonschema.ValidationError) as error:
        pytest.skip(
            "INTEGRATION_SUPPORT_SDK has not yet registered the knowledge.search context schema: "
            f"{error}"
        )
    caller_context = serialize_trusted_caller_context(context)
    if tamper_context:
        caller_context["workspace_id"] = "workspace-tampered"
    sent_arguments = dict(arguments)
    if tamper_arguments:
        sent_arguments["query"] = "tampered after signing"
    envelope = {"caller_context": caller_context, "arguments": sent_arguments}
    return tool_provider_pb2.CallToolRequest(
        binding_id="knowledge-binding",
        provider_tool_id=TOOL_ID,
        arguments_json=json.dumps(envelope, separators=(",", ":")),
    )


def _invoke(
    provider: KnowledgeRetrievalProvider,
    method: str,
    request: Any,
    request_type_url: str,
    response_type_url: str,
) -> tuple[bool, Any]:
    typed_request = encode_request(method, request)
    ok, response = provider.on_invoke(
        CAPABILITY_ID,
        method,
        typed_request.value,
        request_type_url=request_type_url,
    )
    if ok:
        assert isinstance(response, TypedPayload)
        assert response.type_url == response_type_url
    return ok, response


def _call(
    provider: KnowledgeRetrievalProvider,
    arguments: dict[str, Any],
    **options: Any,
) -> tool_provider_pb2.CallToolResponse:
    request = _signed_request(arguments, **options)
    ok, result = _invoke(
        provider,
        METHOD_CALL_TOOL,
        request,
        CALL_TOOL_REQUEST_TYPE_URL,
        CALL_TOOL_RESPONSE_TYPE_URL,
    )
    assert ok is True
    return decode_response(METHOD_CALL_TOOL, result)


def test_catalog_exposes_only_model_arguments_and_fixed_library_selectors(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    request = tool_provider_pb2.ListToolsRequest(binding_id="knowledge-binding")
    ok, output = _invoke(
        provider,
        METHOD_LIST_TOOLS,
        request,
        LIST_TOOLS_REQUEST_TYPE_URL,
        LIST_TOOLS_RESPONSE_TYPE_URL,
    )

    assert ok is True
    response = decode_response(METHOD_LIST_TOOLS, output)
    assert [tool.provider_tool_id for tool in response.catalog.tools] == [TOOL_ID]
    schema = json.loads(response.catalog.tools[0].input_schema_json)
    assert set(schema["required"]) == {"librarySelector", "query", "limit"}
    assert schema["properties"]["librarySelector"]["enum"] == ["research"]
    assert not {"principalRefs", "bundleLocator", "packageDigest", "usePurpose"} & set(
        schema["properties"]
    )


def test_plain_model_arguments_without_signed_context_are_rejected(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    request = tool_provider_pb2.CallToolRequest(
        binding_id="knowledge-binding",
        provider_tool_id=TOOL_ID,
        arguments_json=json.dumps(
            {
                "arguments": {
                    "librarySelector": "research",
                    "query": "cyrene",
                    "limit": 5,
                }
            }
        ),
    )
    ok, result = _invoke(
        provider,
        METHOD_CALL_TOOL,
        request,
        CALL_TOOL_REQUEST_TYPE_URL,
        CALL_TOOL_RESPONSE_TYPE_URL,
    )
    assert ok is True
    response = decode_response(METHOD_CALL_TOOL, result)
    assert response.WhichOneof("result") == "error"
    assert response.error.code == tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS


def test_signed_caller_context_resolves_principals_and_fixed_artifact(tmp_path: Path) -> None:
    provider, digest = _provider(tmp_path)
    response = _call(
        provider,
        {"librarySelector": "research", "query": "cyrene memory", "limit": 5},
    )
    assert response.WhichOneof("result") == "outcome"
    result = json.loads(response.outcome.content[0].json.json)
    assert result["packageDigest"] == digest
    assert result["librarySelector"] == "research"
    assert [hit["chunk"]["sourceId"] for hit in result["hits"]]
    assert all("policy" not in hit["chunk"] for hit in result["hits"])
    assert "principalRefs" not in result


def test_model_cannot_supply_principals_paths_or_digests(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    arguments = {
        "librarySelector": "research",
        "query": "cyrene",
        "limit": 5,
        "principalRefs": ["user:admin"],
        "bundleLocator": "../../secret.zip",
        "packageDigest": "sha256:" + "f" * 64,
    }
    request = tool_provider_pb2.CallToolRequest(
        binding_id="knowledge-binding",
        provider_tool_id=TOOL_ID,
        arguments_json=json.dumps({"caller_context": {}, "arguments": arguments}),
    )
    ok, result = _invoke(
        provider,
        METHOD_CALL_TOOL,
        request,
        CALL_TOOL_REQUEST_TYPE_URL,
        CALL_TOOL_RESPONSE_TYPE_URL,
    )
    assert ok is True
    response = decode_response(METHOD_CALL_TOOL, result)
    assert response.WhichOneof("result") == "error"
    assert response.error.code == tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS


def test_argument_tampering_after_signing_fails_closed(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    valid_arguments = {"librarySelector": "research", "query": "cyrene", "limit": 5}
    tampered = _call(provider, valid_arguments, tamper_arguments=True)
    assert tampered.WhichOneof("result") == "error"
    assert tampered.error.code == tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_PROVIDER_ERROR


def test_context_tampering_and_unmapped_subject_fail_closed(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    arguments = {"librarySelector": "research", "query": "cyrene", "limit": 5}
    tampered = _call(provider, arguments, tamper_context=True)
    assert tampered.WhichOneof("result") == "error"
    assert tampered.error.code == tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_PROVIDER_ERROR

    unmapped = _call(provider, arguments, subject="subject:unmapped")
    assert unmapped.WhichOneof("result") == "error"
    assert unmapped.error.code == tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_PROVIDER_ERROR


def test_provider_issued_chunk_cursor_is_bound_to_query_and_identity(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path, include_more_chunks=True)
    first = _call(
        provider,
        {"librarySelector": "research", "query": "cyrene memory", "limit": 1},
    )
    first_result = json.loads(first.outcome.content[0].json.json)
    cursor = first_result["nextCursor"]
    assert cursor

    second = _call(
        provider,
        {
            "librarySelector": "research",
            "query": "cyrene memory",
            "limit": 1,
            "chunkCursor": cursor,
        },
    )
    second_result = json.loads(second.outcome.content[0].json.json)
    assert (
        first_result["hits"][0]["chunk"]["chunkId"] != second_result["hits"][0]["chunk"]["chunkId"]
    )

    changed_query = _call(
        provider,
        {
            "librarySelector": "research",
            "query": "different query",
            "limit": 1,
            "chunkCursor": cursor,
        },
    )
    assert changed_query.WhichOneof("result") == "error"
    assert changed_query.error.code == tool_provider_pb2.TOOL_PROVIDER_ERROR_CODE_INVALID_ARGUMENTS
