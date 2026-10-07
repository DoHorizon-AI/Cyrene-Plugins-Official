"""Build the stateless `dataset.knowledge.v1` package from approved blocks.

The module consumes Catalyst-owned approved JSONL and writes a self-contained
knowledge ZIP plus a bounded receipt. It does not access Catalyst state.

模块职责：从已审核 blocks 构建无状态知识包，不访问 Catalyst 数据库。
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knowledge_reference import PROFILE, verify_bundle

CAPABILITY_ID = "dataset.knowledge.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"
PLUGIN_ID = "cyrene.tools.knowledge-preparation"
PLUGIN_VERSION = "0.1.0"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_BLOCKS = 200_000
MAX_CHUNK_CHARACTERS = 2_400
SUPPORTED_USE_PURPOSE = "knowledge_retrieval"
VALID_USE_PURPOSES = {"knowledge_retrieval", "model_training"}
PROFILE_VERSION = 1
MANIFEST_NAME = "manifest.json"
PACKAGE_FILES = (
    "chunks.jsonl",
    "sources.jsonl",
    "hierarchy.json",
)


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """A typed response understood by DirectPluginRuntime."""

    value: bytes
    type_url: str


class KnowledgePreparationPlugin:
    """Build verified knowledge bundles from approved ContentBlocks."""

    plugin_id = PLUGIN_ID
    version = PLUGIN_VERSION
    capabilities = (CAPABILITY_ID,)

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: Any | None = None,
        request_type_url: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, TypedPayload | str]:
        """Validate and dispatch one typed build request."""

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action != "build":
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        expected_type_url = f"{TYPE_PREFIX}.build.request"
        if request_type_url != expected_type_url:
            return (
                False,
                f"INVALID_REQUEST: request_type_url must be {expected_type_url}",
            )
        if stream_results:
            return False, "METHOD_NOT_SUPPORTED: build is not streaming"
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"

        try:
            request = _loads_strict(payload.decode("utf-8"))
            if not isinstance(request, dict):
                raise TypeError("request must be an object")
            expected_fields = {
                "blocks_path",
                "bundle_path",
                "result_path",
                "dataset_id",
                "content_revision_id",
                "processing_run_id",
                "source_revisions",
                "policy",
            }
            if request.keys() != expected_fields:
                raise ValueError(
                    "request fields must be exactly "
                    f"{sorted(expected_fields)}; got {sorted(request)}"
                )
            result = self.build(
                blocks_path=_absolute_path(request.get("blocks_path"), "blocks_path"),
                bundle_path=_absolute_path(request.get("bundle_path"), "bundle_path"),
                result_path=_absolute_path(request.get("result_path"), "result_path"),
                dataset_id=_required_text(request.get("dataset_id"), "dataset_id"),
                content_revision_id=_required_text(
                    request.get("content_revision_id"), "content_revision_id"
                ),
                processing_run_id=_required_text(
                    request.get("processing_run_id"), "processing_run_id"
                ),
                source_revisions=request.get("source_revisions"),
                policy=request.get("policy"),
            )
        except (
            TypeError,
            ValueError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
        ) as exc:
            return False, f"INVALID_INPUT: {exc}"

        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        return True, TypedPayload(
            value=_canonical_json(result),
            type_url=f"{TYPE_PREFIX}.build.response",
        )

    def build(
        self,
        *,
        blocks_path: Path,
        bundle_path: Path,
        result_path: Path,
        dataset_id: str,
        content_revision_id: str,
        processing_run_id: str,
        source_revisions: Any,
        policy: Any,
    ) -> dict[str, Any]:
        """Create a deterministic ZIP bundle from approved block JSONL.

        Args:
            blocks_path: Executor-private JSONL file of approved ContentBlocks.
            bundle_path: Executor-private destination for the ZIP artifact.
            result_path: Executor-private destination for the conversion receipt.
            dataset_id: Product dataset UUID used only as package lineage.
            content_revision_id: Approved immutable Product revision UUID.
            processing_run_id: Product processing run UUID for provenance.
            source_revisions: SourceRevision metadata supplied by Catalyst.
            policy: Build-purpose selection; it cannot widen block ACLs.
        Returns:
            A DirectPluginRuntime receipt with artifact digests and report counts.

        中文:只读取 Catalyst 提供的已审核 JSONL，并生成可验证的知识 ZIP。
        """

        _validate_distinct_paths(blocks_path, bundle_path, result_path)
        _validate_policy_request(policy)
        source_records = _validate_source_revisions(source_revisions)
        source_by_revision = {item["sourceRevisionId"]: item for item in source_records}
        blocks = _read_blocks(blocks_path)

        chunks: list[dict[str, Any]] = []
        hierarchy = _new_hierarchy()
        source_block_counts: dict[str, int] = {}
        skipped_not_knowledge = 0
        skipped_empty_text = 0
        warning_count = 0
        seen_block_ids: set[tuple[str, str]] = set()

        for block_line, block in enumerate(blocks, start=1):
            _validate_block(block, block_line, source_by_revision)
            source_revision_id = block["sourceRevisionId"]
            identity = (source_revision_id, block["id"])
            if identity in seen_block_ids:
                raise ValueError(
                    f"blocks_path line {block_line} duplicates a block ID in its source revision"
                )
            seen_block_ids.add(identity)
            if (
                not block["policy"]["allowKnowledge"]
                or SUPPORTED_USE_PURPOSE not in block["policy"]["allowedUsePurposes"]
            ):
                skipped_not_knowledge += 1
                continue

            warning_count += len(block.get("warnings", []))
            text = block["text"]
            if not text.strip():
                skipped_empty_text += 1
                continue

            parts = _split_text(text, MAX_CHUNK_CHARACTERS)
            source_block_counts[source_revision_id] = (
                source_block_counts.get(source_revision_id, 0) + 1
            )
            for part_index, (part_text, start, end) in enumerate(parts):
                chunk_id = _stable_chunk_id(
                    content_revision_id,
                    source_revision_id,
                    block["id"],
                    part_index,
                )
                locator = _normalize_locator(block.get("locator", {}))
                chunk = {
                    "chunkId": chunk_id,
                    "text": part_text,
                    "sourceId": source_by_revision[source_revision_id]["sourceId"],
                    "sourceRevisionId": source_revision_id,
                    "contentRevisionId": content_revision_id,
                    "blockId": block["id"],
                    "blockOrdinal": block["ordinal"],
                    "blockKind": block["kind"],
                    "origin": block["origin"],
                    "locator": locator,
                    "textRange": {"start": start, "end": end},
                    "hierarchyPath": _section_path(locator),
                    "policy": _copy_content_policy(block["policy"]),
                    "assetRefs": block.get("assetRefs", []),
                    "partIndex": part_index,
                    "partCount": len(parts),
                }
                chunks.append(chunk)
                _add_hierarchy_chunk(hierarchy, source_revision_id, chunk)

        chunks.sort(
            key=lambda item: (
                item["sourceRevisionId"],
                item["blockOrdinal"],
                item["blockId"],
                item["partIndex"],
            )
        )
        source_records_used = [
            source_by_revision[source_revision_id]
            for source_revision_id in sorted(source_block_counts)
        ]
        source_lines = [_public_source_record(source) for source in source_records_used]

        payloads = {
            "chunks.jsonl": _jsonl_bytes(chunks),
            "sources.jsonl": _jsonl_bytes(source_lines),
            "hierarchy.json": _canonical_json(_finalize_hierarchy(hierarchy)),
        }
        per_file_digests = {
            name: _digest_bytes(data) for name, data in sorted(payloads.items())
        }
        conversion_report = {
            "inputBlockCount": len(blocks),
            "chunkCount": len(chunks),
            "sourceCount": len(source_records_used),
            "excludedBlockCount": skipped_not_knowledge,
            "emptyTextBlockCount": skipped_empty_text,
            "warningCount": warning_count,
            "splitBlockCount": sum(
                1 for count in _part_counts(chunks).values() if count > 1
            ),
            "skippedByPolicySourceCount": sum(
                1
                for source_id in source_by_revision
                if any(block["sourceRevisionId"] == source_id for block in blocks)
                and all(
                    (
                        not block["policy"]["allowKnowledge"]
                        or SUPPORTED_USE_PURPOSE
                        not in block["policy"]["allowedUsePurposes"]
                    )
                    for block in blocks
                    if block["sourceRevisionId"] == source_id
                )
            ),
            "assetRefCount": sum(len(chunk["assetRefs"]) for chunk in chunks),
        }
        manifest = {
            "schemaVersion": "cyrene.knowledge.bundle.v1",
            "profile": PROFILE,
            "profileVersion": PROFILE_VERSION,
            "datasetId": dataset_id,
            "contentRevisionId": content_revision_id,
            "processingRunId": processing_run_id,
            "usePurpose": SUPPORTED_USE_PURPOSE,
            "sourceRevisionIds": [
                item["sourceRevisionId"] for item in source_records_used
            ],
            "files": per_file_digests,
            "conversionReport": conversion_report,
        }
        manifest_bytes = _canonical_json(manifest)
        all_digests = {**per_file_digests, MANIFEST_NAME: _digest_bytes(manifest_bytes)}
        checksums_bytes = _canonical_json(
            {"schemaVersion": "cyrene.knowledge.checksums.v1", "files": all_digests}
        )
        archive_files = {
            MANIFEST_NAME: manifest_bytes,
            **payloads,
            "checksums.json": checksums_bytes,
        }
        _write_deterministic_zip(bundle_path, archive_files)
        verified = verify_bundle(bundle_path, package_digest=_digest_file(bundle_path))
        bundle_digest = verified["packageDigest"]
        bundle_size = bundle_path.stat().st_size
        manifest_digest = all_digests[MANIFEST_NAME]
        receipt_document = {
            "profile": PROFILE,
            "bundleDigest": bundle_digest,
            "bundleSize": bundle_size,
            "manifestDigest": manifest_digest,
            "chunkCount": len(chunks),
            "sourceCount": len(source_records_used),
            "conversionReport": conversion_report,
        }
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_bytes(_canonical_json(receipt_document))
        result_size = result_path.stat().st_size
        return {
            "bundle_digest": bundle_digest,
            "bundle_size": bundle_size,
            "manifest_digest": manifest_digest,
            "result_digest": _digest_file(result_path),
            "result_size": result_size,
            "chunk_count": len(chunks),
            "source_count": len(source_records_used),
            "conversion_report": conversion_report,
        }


def _read_blocks(path: Path) -> list[dict[str, Any]]:
    """Load bounded JSONL and reject empty lines or malformed records."""

    _validate_input_file(path, "blocks_path")
    data = path.read_bytes()
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError(f"blocks_path exceeds {MAX_INPUT_BYTES} bytes")
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("blocks_path must be UTF-8 JSONL") from exc
    if source and not source.endswith("\n"):
        # Accept a final line without a newline, as standard JSONL readers do.
        source += "\n"
    records: list[dict[str, Any]] = []
    for index, line in enumerate(source.splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"blocks_path line {index} is empty")
        record = _loads_strict(line)
        if not isinstance(record, dict):
            raise TypeError(f"blocks_path line {index} must be an object")
        records.append(record)
        if len(records) > MAX_BLOCKS:
            raise ValueError(f"blocks_path exceeds {MAX_BLOCKS} blocks")
    return records


def _validate_block(
    block: dict[str, Any], line: int, source_by_revision: dict[str, dict[str, Any]]
) -> None:
    """Check approved ContentBlock identity, text, locator and fail-closed ACL."""

    required = {
        "id",
        "sourceRevisionId",
        "ordinal",
        "kind",
        "text",
        "locator",
        "origin",
        "policy",
    }
    missing = required - block.keys()
    if missing:
        raise ValueError(f"blocks_path line {line} missing fields: {sorted(missing)}")
    _required_text(block["id"], f"blocks_path line {line} id")
    revision_id = _required_text(
        block["sourceRevisionId"], f"blocks_path line {line} sourceRevisionId"
    )
    if revision_id not in source_by_revision:
        raise ValueError(
            f"blocks_path line {line} references an unknown source revision"
        )
    if type(block["ordinal"]) is not int or block["ordinal"] < 0:
        raise ValueError(
            f"blocks_path line {line} ordinal must be a non-negative integer"
        )
    for key in ("kind", "origin"):
        _required_text(block[key], f"blocks_path line {line} {key}")
    if not isinstance(block["text"], str):
        raise TypeError(f"blocks_path line {line} text must be a string")
    if not isinstance(block["locator"], dict):
        raise TypeError(f"blocks_path line {line} locator must be an object")
    if "assetRefs" in block and not isinstance(block["assetRefs"], list):
        raise ValueError(f"blocks_path line {line} assetRefs must be an array")
    if "warnings" in block and (
        not isinstance(block["warnings"], list)
        or any(not isinstance(warning, str) for warning in block["warnings"])
    ):
        raise ValueError(
            f"blocks_path line {line} warnings must be an array of strings"
        )
    _validate_content_policy(block["policy"], f"blocks_path line {line} policy")


def _validate_content_policy(policy: Any, label: str) -> None:
    """Reject incomplete, invalid or permissive-by-default policy records."""

    if not isinstance(policy, dict):
        raise TypeError(f"{label} must be an object")
    required = {
        "allowKnowledge",
        "allowTraining",
        "allowedPrincipalRefs",
        "allowedUsePurposes",
    }
    missing = required - policy.keys()
    unknown = policy.keys() - required
    if missing or unknown:
        raise ValueError(
            f"{label} fields invalid; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    if (
        type(policy["allowKnowledge"]) is not bool
        or type(policy["allowTraining"]) is not bool
    ):
        raise ValueError(f"{label} allowKnowledge and allowTraining must be booleans")
    for key in ("allowedPrincipalRefs", "allowedUsePurposes"):
        value = policy[key]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(f"{label} {key} must be an array of non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError(f"{label} {key} must not contain duplicates")
    if any(
        purpose not in VALID_USE_PURPOSES for purpose in policy["allowedUsePurposes"]
    ):
        raise ValueError(f"{label} contains an unsupported use purpose")


def _validate_source_revisions(value: Any) -> list[dict[str, Any]]:
    """Validate SourceRevision lineage and retain the canonical ArtifactRef."""

    if not isinstance(value, list):
        raise TypeError("source_revisions must be an array")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, source in enumerate(value):
        label = f"source_revisions[{index}]"
        if not isinstance(source, dict):
            raise TypeError(f"{label} must be an object")
        required = {"source_id", "source_revision_id", "revision", "digest", "artifact"}
        if source.keys() != required:
            raise ValueError(f"{label} fields must be exactly {sorted(required)}")
        source_id = _required_text(source["source_id"], f"{label}.source_id")
        revision_id = _required_text(
            source["source_revision_id"], f"{label}.source_revision_id"
        )
        if revision_id in seen:
            raise ValueError(
                "source_revisions must have unique source_revision_id values"
            )
        seen.add(revision_id)
        if type(source["revision"]) is not int or source["revision"] < 1:
            raise ValueError(f"{label}.revision must be a positive integer")
        digest = _required_digest(source["digest"], f"{label}.digest")
        if source_id == digest:
            raise ValueError(
                f"{label}.source_id must be a logical identity, not a digest"
            )
        artifact = source["artifact"]
        if not isinstance(artifact, dict):
            raise TypeError(f"{label}.artifact must be an ArtifactRef object")
        artifact_fields = {"uri", "digest", "size_bytes", "kind", "manifest_digest"}
        if (
            artifact.keys() - artifact_fields
            or not {"uri", "digest", "size_bytes", "kind"} <= artifact.keys()
        ):
            raise ValueError(f"{label}.artifact has invalid ArtifactRef fields")
        artifact_digest = _required_digest(
            artifact.get("digest"), f"{label}.artifact.digest"
        )
        if artifact_digest != digest:
            raise ValueError(f"{label} digest must match artifact.digest")
        expected_uri = f"artifact://sha256/{digest.removeprefix('sha256:')}"
        if artifact.get("uri") != expected_uri:
            raise ValueError(f"{label}.artifact.uri must identify its content digest")
        if type(artifact.get("size_bytes")) is not int or artifact["size_bytes"] < 0:
            raise ValueError(
                f"{label}.artifact.size_bytes must be a non-negative integer"
            )
        _required_text(artifact.get("kind"), f"{label}.artifact.kind")
        if "manifest_digest" in artifact:
            _required_digest(
                artifact["manifest_digest"], f"{label}.artifact.manifest_digest"
            )
        records.append(
            {
                "sourceId": source_id,
                "sourceRevisionId": revision_id,
                "revision": source["revision"],
                "digest": digest,
                "artifact": artifact,
            }
        )
    return sorted(records, key=lambda item: item["sourceRevisionId"])


def _public_source_record(source: dict[str, Any]) -> dict[str, Any]:
    """Project SourceRevision lineage into the public package shape."""

    return {
        "sourceId": source["sourceId"],
        "sourceRevisionId": source["sourceRevisionId"],
        "revision": source["revision"],
        "digest": source["digest"],
        "artifact": source["artifact"],
    }


def _validate_policy_request(policy: Any) -> None:
    """Ensure this run cannot claim a purpose outside the frozen capability."""

    if policy != {"use_purpose": SUPPORTED_USE_PURPOSE}:
        raise ValueError(
            f"policy must equal {{'use_purpose': {SUPPORTED_USE_PURPOSE!r}}}"
        )


def _split_text(text: str, maximum: int) -> list[tuple[str, int, int]]:
    """Split long blocks on whitespace while retaining block-local offsets."""

    if len(text) <= maximum:
        stripped = text.strip()
        if not stripped:
            return []
        start = text.index(stripped)
        return [(stripped, start, start + len(stripped))]

    parts: list[tuple[str, int, int]] = []
    cursor = 0
    text_length = len(text)
    while cursor < text_length:
        while cursor < text_length and text[cursor].isspace():
            cursor += 1
        if cursor >= text_length:
            break
        end = min(cursor + maximum, text_length)
        if end < text_length:
            boundary = text.rfind(" ", cursor, end + 1)
            newline = text.rfind("\n", cursor, end + 1)
            end = max(boundary, newline)
            if end <= cursor:
                end = min(cursor + maximum, text_length)
        raw_end = end
        while raw_end > cursor and text[raw_end - 1].isspace():
            raw_end -= 1
        if raw_end > cursor:
            parts.append((text[cursor:raw_end], cursor, raw_end))
        cursor = max(end, cursor + 1)
    return parts


def _section_path(locator: dict[str, Any]) -> list[str]:
    """Read the canonical section path from a normalized public locator."""

    value = locator.get("sectionPath", [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return []
    return value


def _normalize_locator(locator: dict[str, Any]) -> dict[str, Any]:
    """Map parser snake_case location keys to the public camelCase package shape."""

    aliases = {
        "source_pages": "sourcePages",
        "section_path": "sectionPath",
        "item_ref": "itemRef",
        "tree_level": "treeLevel",
        "table_index": "tableIndex",
    }
    allowed = {
        "page",
        "sourcePages",
        "sectionPath",
        "itemRef",
        "treeLevel",
        "tableIndex",
        "startOffset",
        "endOffset",
        "provenance",
    }
    normalized: dict[str, Any] = {}
    for key, value in locator.items():
        public_key = aliases.get(key, key)
        if public_key not in allowed:
            raise ValueError(f"locator contains unsupported field {key!r}")
        if public_key in normalized:
            raise ValueError(f"locator supplies duplicate aliases for {public_key!r}")
        normalized[public_key] = value

    for key in ("page", "treeLevel", "tableIndex", "startOffset", "endOffset"):
        if key in normalized:
            minimum = 1 if key == "page" else 0
            if type(normalized[key]) is not int or normalized[key] < minimum:
                raise ValueError(f"locator.{key} must be an integer >= {minimum}")
    if "sourcePages" in normalized:
        pages = normalized["sourcePages"]
        if (
            not isinstance(pages, list)
            or any(type(page) is not int or page < 1 for page in pages)
            or len(set(pages)) != len(pages)
        ):
            raise ValueError(
                "locator.sourcePages must contain unique positive integers"
            )
    if "sectionPath" in normalized and (
        not isinstance(normalized["sectionPath"], list)
        or any(not isinstance(item, str) for item in normalized["sectionPath"])
    ):
        raise ValueError("locator.sectionPath must be an array of strings")
    if "itemRef" in normalized:
        _required_text(normalized["itemRef"], "locator.itemRef")
    if "provenance" in normalized and (
        not isinstance(normalized["provenance"], list)
        or any(not isinstance(item, dict) for item in normalized["provenance"])
    ):
        raise ValueError("locator.provenance must be an array of objects")
    if (
        "startOffset" in normalized
        and "endOffset" in normalized
        and normalized["endOffset"] < normalized["startOffset"]
    ):
        raise ValueError(
            "locator.endOffset must be greater than or equal to startOffset"
        )
    return normalized


def _new_hierarchy() -> dict[str, Any]:
    """Initialize source-scoped hierarchy nodes."""

    return {"schemaVersion": "cyrene.knowledge.hierarchy.v1", "sources": {}}


def _add_hierarchy_chunk(
    hierarchy: dict[str, Any], source_revision_id: str, chunk: dict[str, Any]
) -> None:
    """Attach one chunk to the tree for its SourceRevision and section path."""

    source = hierarchy["sources"].setdefault(
        source_revision_id, {"rootChunkIds": [], "sections": {}}
    )
    path = chunk["hierarchyPath"]
    if not path:
        source["rootChunkIds"].append(chunk["chunkId"])
        return
    cursor = source["sections"]
    accumulated: list[str] = []
    for item in path:
        accumulated.append(item)
        key = "/".join(accumulated)
        node = cursor.setdefault(
            key,
            {"name": item, "path": list(accumulated), "chunkIds": [], "children": {}},
        )
        cursor = node["children"]
    node["chunkIds"].append(chunk["chunkId"])


def _finalize_hierarchy(hierarchy: dict[str, Any]) -> dict[str, Any]:
    """Convert internal section maps into deterministically ordered arrays."""

    def nodes(mapping: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for key in sorted(mapping):
            node = mapping[key]
            result.append(
                {
                    "name": node["name"],
                    "path": node["path"],
                    "chunkIds": sorted(node["chunkIds"]),
                    "children": nodes(node["children"]),
                }
            )
        return result

    sources = []
    for source_revision_id in sorted(hierarchy["sources"]):
        source = hierarchy["sources"][source_revision_id]
        sources.append(
            {
                "sourceRevisionId": source_revision_id,
                "rootChunkIds": sorted(source["rootChunkIds"]),
                "sections": nodes(source["sections"]),
            }
        )
    return {"schemaVersion": hierarchy["schemaVersion"], "sources": sources}


def _part_counts(chunks: list[dict[str, Any]]) -> dict[str, int]:
    """Return unique block part counts for conversion reporting."""

    counts: dict[str, int] = {}
    for chunk in chunks:
        identity = f"{chunk['sourceRevisionId']}\0{chunk['blockId']}"
        counts[identity] = chunk["partCount"]
    return counts


def _copy_content_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical policy copy suitable for public chunk records."""

    return {
        "allowKnowledge": policy["allowKnowledge"],
        "allowTraining": policy["allowTraining"],
        "allowedPrincipalRefs": sorted(policy["allowedPrincipalRefs"]),
        "allowedUsePurposes": sorted(policy["allowedUsePurposes"]),
    }


def _stable_chunk_id(
    content_revision_id: str, source_revision_id: str, block_id: str, part_index: int
) -> str:
    """Derive identity from logical lineage rather than content hash."""

    identity = "\0".join(
        (content_revision_id, source_revision_id, block_id, str(part_index))
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(identity).hexdigest()


def _write_deterministic_zip(path: Path, files: dict[str, bytes]) -> None:
    """Write stable ZIP metadata so identical inputs produce identical bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(
                info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
            )


def _validate_input_file(path: Path, label: str) -> None:
    """Require a readable, regular staged file under the input size bound."""

    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute executor-local path")
    if not path.is_file():
        raise ValueError(f"{label} must identify a regular file")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"{label} exceeds {MAX_INPUT_BYTES} bytes")


def _validate_distinct_paths(*paths: Path) -> None:
    """Prevent staged input and output aliases from overwriting each other."""

    resolved = [path.resolve(strict=False) for path in paths]
    if len(resolved) != len(set(resolved)):
        raise ValueError("blocks_path, bundle_path and result_path must be distinct")


def _absolute_path(value: Any, label: str) -> Path:
    """Parse the private executor-local absolute path field."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute executor-local path")
    return path


def _required_text(value: Any, label: str) -> str:
    """Require stable non-empty public identity text."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _required_digest(value: Any, label: str) -> str:
    """Require a canonical SHA-256 digest string."""

    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise ValueError(f"{label} must match sha256:<64 lowercase hex>")
    return value


def _loads_strict(value: str) -> Any:
    """Decode JSON while rejecting duplicate object keys."""

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = item
        return result

    return json.loads(value, object_pairs_hook=unique_pairs)


def _canonical_json(value: Any) -> bytes:
    """Serialize JSON deterministically as UTF-8 without a trailing newline."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    """Serialize JSONL records in a stable order."""

    return b"".join(_canonical_json(record) + b"\n" for record in records)


def _digest_bytes(value: bytes) -> str:
    """Return a prefixed SHA-256 digest for bytes."""

    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_file(path: Path) -> str:
    """Hash a file incrementally without loading the artifact into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
