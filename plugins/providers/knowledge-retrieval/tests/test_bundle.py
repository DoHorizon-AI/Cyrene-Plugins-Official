"""Verification and retrieval tests for CYRENE_KNOWLEDGE_BUNDLE_V1.

中文：覆盖包摘要、成员校验、授权过滤、路径约束及检索排序。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from knowledge_retrieval_provider.bundle import (
    BundleError,
    resolve_bundle_path,
    search_bundle,
    verify_bundle,
)

_DIGEST = "sha256:" + "a" * 64
_SOURCE_ID = "00000000-0000-4000-8000-000000000001"
_SOURCE_REVISION_ID = "00000000-0000-4000-8000-000000000002"
_CONTENT_REVISION_ID = "00000000-0000-4000-8000-000000000003"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _records(
    *,
    denied_text: str | None = None,
    allow_knowledge: bool = True,
    allowed_principals: list[str] | None = None,
    allowed_purposes: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = {
        "sourceId": _SOURCE_ID,
        "sourceRevisionId": _SOURCE_REVISION_ID,
        "revision": 1,
        "digest": _DIGEST,
        "artifact": {
            "uri": "artifact://sha256/" + "a" * 64,
            "digest": _DIGEST,
            "size_bytes": 12,
            "kind": "text/plain",
        },
    }
    chunks = [
        _chunk(
            "chunk-authorized",
            "Cyrene memory stores Alice's preference.",
            allow_knowledge=allow_knowledge,
            principals=allowed_principals or ["user:alice"],
            purposes=allowed_purposes or ["knowledge_retrieval"],
        )
    ]
    if denied_text is not None:
        chunks.append(
            _chunk(
                "chunk-denied",
                denied_text,
                allow_knowledge=False,
                principals=["user:other"],
                purposes=["knowledge_retrieval"],
            )
        )
    return [source], chunks


def _chunk(
    chunk_id: str,
    text: str,
    *,
    allow_knowledge: bool = True,
    principals: list[str] | None = None,
    purposes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "chunkId": _sha(chunk_id.encode()),
        "text": text,
        "sourceId": _SOURCE_ID,
        "sourceRevisionId": _SOURCE_REVISION_ID,
        "contentRevisionId": _CONTENT_REVISION_ID,
        "blockId": "block-1",
        "blockOrdinal": 0,
        "blockKind": "paragraph",
        "origin": "EXTRACTED",
        "locator": {"page": 1},
        "textRange": {"start": 0, "end": len(text)},
        "hierarchyPath": ["Root"],
        "policy": {
            "allowKnowledge": allow_knowledge,
            "allowTraining": False,
            "allowedPrincipalRefs": principals or ["user:alice"],
            "allowedUsePurposes": purposes or ["knowledge_retrieval"],
        },
        "assetRefs": [],
        "partIndex": 0,
        "partCount": 1,
    }


def _build_bundle(path: Path, sources: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> str:
    source_bytes = _jsonl(sources)
    chunk_bytes = _jsonl(chunks)
    hierarchy_bytes = json.dumps(
        {"schemaVersion": "cyrene.knowledge.hierarchy.v1", "sources": []},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    files = {
        "chunks.jsonl": _sha(chunk_bytes),
        "sources.jsonl": _sha(source_bytes),
        "hierarchy.json": _sha(hierarchy_bytes),
    }
    manifest = {
        "schemaVersion": "cyrene.knowledge.bundle.v1",
        "profile": "CYRENE_KNOWLEDGE_BUNDLE_V1",
        "profileVersion": 1,
        "datasetId": "00000000-0000-4000-8000-000000000004",
        "contentRevisionId": _CONTENT_REVISION_ID,
        "processingRunId": "00000000-0000-4000-8000-000000000005",
        "usePurpose": "knowledge_retrieval",
        "sourceRevisionIds": [_SOURCE_REVISION_ID],
        "files": files,
        "conversionReport": {
            "inputBlockCount": len(chunks),
            "chunkCount": len(chunks),
            "sourceCount": len(sources),
            "excludedBlockCount": 0,
            "emptyTextBlockCount": 0,
            "warningCount": 0,
            "splitBlockCount": 0,
            "skippedByPolicySourceCount": 0,
            "assetRefCount": 0,
        },
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    checksums = {
        "schemaVersion": "cyrene.knowledge.checksums.v1",
        "files": {"manifest.json": _sha(manifest_bytes), **files},
    }
    members = {
        "manifest.json": manifest_bytes,
        "chunks.jsonl": chunk_bytes,
        "sources.jsonl": source_bytes,
        "hierarchy.json": hierarchy_bytes,
        "checksums.json": json.dumps(checksums, sort_keys=True, separators=(",", ":")).encode(),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return _sha(path.read_bytes())


def _jsonl(records: list[dict[str, Any]]) -> bytes:
    content = "".join(
        json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in records
    )
    return content.encode()


def test_search_verifies_bundle_then_filters_before_ranking(tmp_path: Path) -> None:
    sources, chunks = _records(denied_text="Cyrene Cyrene Cyrene secret record")
    bundle = tmp_path / "knowledge.zip"
    digest = _build_bundle(bundle, sources, chunks)

    result = search_bundle(
        bundle,
        "cyrene memory",
        principal_refs=["user:alice"],
        use_purpose="knowledge_retrieval",
        package_digest=digest,
        limit=5,
    )
    assert result["packageDigest"] == digest
    assert [item["chunk"]["chunkId"] for item in result["hits"]] == [_sha(b"chunk-authorized")]
    assert "policy" not in result["hits"][0]["chunk"]

    only_authorized = tmp_path / "only-authorized.zip"
    sources, authorized_chunks = _records()
    digest2 = _build_bundle(only_authorized, sources, authorized_chunks)
    baseline = search_bundle(
        only_authorized,
        "cyrene memory",
        principal_refs=["user:alice"],
        use_purpose="knowledge_retrieval",
        package_digest=digest2,
    )
    assert result["hits"][0]["score"] == baseline["hits"][0]["score"]


@pytest.mark.parametrize(
    ("allow_knowledge", "principals", "purposes"),
    [
        (False, ["user:alice"], ["knowledge_retrieval"]),
        (True, ["user:bob"], ["knowledge_retrieval"]),
        (True, ["user:alice"], ["model_training"]),
    ],
)
def test_content_policy_requires_all_three_grants(
    tmp_path: Path,
    allow_knowledge: bool,
    principals: list[str],
    purposes: list[str],
) -> None:
    sources, chunks = _records(
        allow_knowledge=allow_knowledge,
        allowed_principals=principals,
        allowed_purposes=purposes,
    )
    bundle = tmp_path / "knowledge.zip"
    digest = _build_bundle(bundle, sources, chunks)
    result = search_bundle(
        bundle,
        "cyrene",
        principal_refs=["user:alice"],
        use_purpose="knowledge_retrieval",
        package_digest=digest,
    )
    assert result["hits"] == []


def test_digest_checks_and_malformed_package_fail_closed(tmp_path: Path) -> None:
    sources, chunks = _records()
    bundle = tmp_path / "knowledge.zip"
    _build_bundle(bundle, sources, chunks)
    with pytest.raises(BundleError, match="package digest"):
        verify_bundle(bundle, package_digest=_DIGEST)

    # Repackage with a new outer digest but retain the old member checksums.
    with zipfile.ZipFile(bundle, "r") as source_archive:
        members = {name: source_archive.read(name) for name in source_archive.namelist()}
    members["chunks.jsonl"] = members["chunks.jsonl"].replace(b"Cyrene", b"Altered")
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(tampered, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    with pytest.raises(BundleError, match="member digest mismatch"):
        verify_bundle(tampered, package_digest=_sha(tampered.read_bytes()))


def test_authenticated_principal_and_purpose_are_required(tmp_path: Path) -> None:
    sources, chunks = _records()
    bundle = tmp_path / "knowledge.zip"
    digest = _build_bundle(bundle, sources, chunks)
    common = {
        "bundle_path": bundle,
        "query": "cyrene",
        "use_purpose": "knowledge_retrieval",
        "package_digest": digest,
    }
    with pytest.raises(BundleError, match="principalRefs"):
        search_bundle(**common, principal_refs=[])
    with pytest.raises(BundleError, match="usePurpose"):
        search_bundle(**{**common, "use_purpose": "model_training"}, principal_refs=["user:alice"])


def test_locator_rejects_traversal_absolute_paths_and_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"not a bundle")
    (root / "safe.zip").write_bytes(b"within root")
    assert resolve_bundle_path(root, "safe.zip") == (root / "safe.zip").resolve()
    for unsafe in ("../outside.zip", "/etc/passwd", "C:/outside.zip", "nested\\file.zip"):
        with pytest.raises(BundleError):
            resolve_bundle_path(root, unsafe)
    try:
        (root / "escape.zip").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(BundleError, match="outside"):
        resolve_bundle_path(root, "escape.zip")
