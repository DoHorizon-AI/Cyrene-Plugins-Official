"""Verify and search immutable Cyrene knowledge bundle artifacts.

中文：校验外层摘要、内部校验和与来源策略，再对授权文本执行 BM25。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE = "CYRENE_KNOWLEDGE_BUNDLE_V1"
PROFILE_VERSION = 1
USE_PURPOSE = "knowledge_retrieval"
MAX_SEARCH_OFFSET = 2_000_000
_EXPECTED_MEMBERS = {
    "manifest.json",
    "chunks.jsonl",
    "sources.jsonl",
    "hierarchy.json",
    "checksums.json",
}
_PACKAGE_FILES = ("chunks.jsonl", "sources.jsonl", "hierarchy.json")
_VALID_PURPOSES = {"knowledge_retrieval", "model_training"}
_TOKEN_PATTERN = re.compile(r"[\w]+", flags=re.UNICODE)


class BundleError(ValueError):
    """A package, request, or record failed closed verification."""


@dataclass(frozen=True, slots=True)
class VerifiedBundle:
    """A bundle whose outer digest and every declared member hash match."""

    package_digest: str
    manifest_digest: str
    manifest: dict[str, Any]
    chunks: tuple[dict[str, Any], ...]
    sources: tuple[dict[str, Any], ...]


def resolve_bundle_path(artifact_root: str | Path, locator: str) -> Path:
    """Resolve a relative artifact locator without allowing escape from its root."""

    if (
        not isinstance(locator, str)
        or not locator
        or len(locator) > 2_048
        or "\x00" in locator
        or "\\" in locator
        or locator.startswith("/")
        or re.match(r"^[A-Za-z]:", locator)
    ):
        raise BundleError("bundleLocator must be a relative POSIX path")
    parts = locator.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BundleError("bundleLocator contains an unsafe path segment")
    root = Path(artifact_root).resolve(strict=True)
    if not root.is_dir():
        raise BundleError("configured artifact root is not a directory")
    try:
        path = (root / locator).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise BundleError(
            "bundleLocator is missing or outside the configured artifact root"
        ) from error
    if not path.is_file():
        raise BundleError("bundleLocator must identify a regular artifact file")
    return path


def verify_bundle(
    bundle_path: str | Path,
    *,
    package_digest: str,
    expected_manifest_digest: str | None = None,
    max_bundle_bytes: int = 256 * 1024 * 1024,
    max_member_bytes: int = 192 * 1024 * 1024,
    max_records: int = 500_000,
) -> VerifiedBundle:
    """Verify the exact archive, all checksums, and all record references."""

    expected_digest = _validate_digest(package_digest, "packageDigest")
    path = Path(bundle_path)
    try:
        file_size = path.stat().st_size
    except OSError as error:
        raise BundleError("bundle artifact could not be read") from error
    if not path.is_file() or file_size > max_bundle_bytes:
        raise BundleError("bundle is absent or exceeds the configured size limit")
    actual_digest = _digest_file(path)
    if actual_digest != expected_digest:
        raise BundleError("package digest does not match the published artifact reference")

    members = _read_archive(path, max_bundle_bytes, max_member_bytes)
    checksums = _loads_strict(members["checksums.json"])
    if (
        not isinstance(checksums, dict)
        or checksums.get("schemaVersion") != "cyrene.knowledge.checksums.v1"
    ):
        raise BundleError("checksums.json uses an unsupported schemaVersion")
    declared = checksums.get("files")
    if not isinstance(declared, dict) or set(declared) != _EXPECTED_MEMBERS - {"checksums.json"}:
        raise BundleError("checksums.json must list every other package member exactly once")
    for name, value in declared.items():
        if _digest_bytes(members[name]) != _validate_digest(value, f"checksums.files.{name}"):
            raise BundleError(f"package member digest mismatch: {name}")
    manifest_digest = _validate_digest(declared["manifest.json"], "checksums.files.manifest.json")
    if expected_manifest_digest is not None and (
        _validate_digest(expected_manifest_digest, "ArtifactRef.manifest_digest") != manifest_digest
    ):
        raise BundleError("manifest digest does not match the configured ArtifactRef")

    manifest = _loads_strict(members["manifest.json"])
    _validate_manifest(manifest, declared)
    chunks = _parse_jsonl(members["chunks.jsonl"], "chunks.jsonl", max_records)
    sources = _parse_jsonl(members["sources.jsonl"], "sources.jsonl", max_records)
    hierarchy = _loads_strict(members["hierarchy.json"])
    _validate_records(manifest, chunks, sources, hierarchy, max_records)
    return VerifiedBundle(actual_digest, manifest_digest, manifest, tuple(chunks), tuple(sources))


def search_bundle(
    bundle_path: str | Path,
    query: str,
    *,
    principal_refs: Sequence[str],
    use_purpose: str,
    package_digest: str,
    expected_manifest_digest: str | None = None,
    limit: int = 5,
    offset: int = 0,
    max_bundle_bytes: int = 256 * 1024 * 1024,
    max_member_bytes: int = 192 * 1024 * 1024,
    max_records: int = 500_000,
    max_limit: int = 20,
    max_query_chars: int = 4_096,
) -> dict[str, Any]:
    """Verify policy before ranking, and return provenance without stored ACLs."""

    _validate_search_request(
        query, principal_refs, use_purpose, limit, offset, max_limit, max_query_chars
    )
    bundle = verify_bundle(
        bundle_path,
        package_digest=package_digest,
        expected_manifest_digest=expected_manifest_digest,
        max_bundle_bytes=max_bundle_bytes,
        max_member_bytes=max_member_bytes,
        max_records=max_records,
    )
    principal_set = set(principal_refs)
    # Filter policy before text tokenization and BM25 scoring.
    authorized = [
        chunk
        for chunk in bundle.chunks
        if _is_authorized(chunk["policy"], principal_set, use_purpose)
    ]
    ranked = _bm25(authorized, _tokenize(query), offset + limit + 1)
    hits = ranked[offset : offset + limit]
    next_offset = offset + limit if len(ranked) > offset + limit else None
    return {
        "packageDigest": bundle.package_digest,
        "manifestDigest": bundle.manifest_digest,
        "contentRevisionId": bundle.manifest["contentRevisionId"],
        "usePurpose": use_purpose,
        "query": query,
        "nextOffset": next_offset,
        "hits": [{"score": round(score, 8), "chunk": _public_hit(chunk)} for score, chunk in hits],
    }


def _read_archive(path: Path, max_bundle_bytes: int, max_member_bytes: int) -> dict[str, bytes]:
    """Read a flat exact-member archive under compressed and expanded size bounds."""

    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or set(names) != _EXPECTED_MEMBERS:
                raise BundleError("archive members do not match CYRENE_KNOWLEDGE_BUNDLE_V1")
            expanded_size = 0
            for info in infos:
                if (
                    info.is_dir()
                    or "/" in info.filename
                    or "\\" in info.filename
                    or info.flag_bits & 0x1
                ):
                    raise BundleError("bundle members must be unencrypted flat files")
                if info.file_size > max_member_bytes:
                    raise BundleError(
                        f"bundle member exceeds configured size limit: {info.filename}"
                    )
                expanded_size += info.file_size
            if expanded_size > max_bundle_bytes:
                raise BundleError("bundle expanded size exceeds configured limit")
            return {name: archive.read(name) for name in names}
    except zipfile.BadZipFile as error:
        raise BundleError("bundle is not a valid ZIP archive") from error
    except OSError as error:
        raise BundleError("bundle archive could not be read") from error


def _validate_manifest(value: Any, declared: dict[str, Any]) -> None:
    """Require the versioned profile and exact agreement with member checksums."""

    if not isinstance(value, dict):
        raise BundleError("manifest.json must be an object")
    expected_keys = {
        "schemaVersion",
        "profile",
        "profileVersion",
        "datasetId",
        "contentRevisionId",
        "processingRunId",
        "usePurpose",
        "sourceRevisionIds",
        "files",
        "conversionReport",
    }
    if set(value) != expected_keys:
        raise BundleError("manifest.json has missing or unsupported fields")
    if (
        value.get("schemaVersion") != "cyrene.knowledge.bundle.v1"
        or value.get("profile") != PROFILE
        or value.get("profileVersion") != PROFILE_VERSION
        or value.get("usePurpose") != USE_PURPOSE
    ):
        raise BundleError("manifest profile or version is unsupported")
    for key in ("datasetId", "contentRevisionId", "processingRunId"):
        _validate_uuid(value.get(key), f"manifest.{key}")
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != set(_PACKAGE_FILES):
        raise BundleError("manifest.files must list chunks, sources, and hierarchy")
    for name in _PACKAGE_FILES:
        if _validate_digest(files[name], f"manifest.files.{name}") != declared[name]:
            raise BundleError(f"manifest and checksums disagree for {name}")
    source_ids = value.get("sourceRevisionIds")
    if not isinstance(source_ids, list) or any(not isinstance(item, str) for item in source_ids):
        raise BundleError("manifest.sourceRevisionIds must be a string array")
    for index, source_id in enumerate(source_ids, start=1):
        _validate_uuid(source_id, f"manifest.sourceRevisionIds[{index}]")
    if source_ids != sorted(set(source_ids)):
        raise BundleError("manifest.sourceRevisionIds must be unique and sorted")
    report = value.get("conversionReport")
    report_keys = {
        "inputBlockCount",
        "chunkCount",
        "sourceCount",
        "excludedBlockCount",
        "emptyTextBlockCount",
        "warningCount",
        "splitBlockCount",
        "skippedByPolicySourceCount",
        "assetRefCount",
    }
    if not isinstance(report, dict) or set(report) != report_keys:
        raise BundleError("manifest.conversionReport must be an object")
    if any(type(item) is not int or item < 0 for item in report.values()):
        raise BundleError("manifest.conversionReport contains invalid counts")
    _validate_digest(declared["manifest.json"], "checksums.files.manifest.json")


def _validate_records(
    manifest: dict[str, Any],
    chunks: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    hierarchy: Any,
    max_records: int,
) -> None:
    """Validate each record identity and fail closed on incomplete access policy."""

    if len(chunks) > max_records or len(sources) > max_records:
        raise BundleError("bundle contains too many JSONL records")
    source_by_revision: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(sources, start=1):
        revision_id = source.get("sourceRevisionId")
        source_id = source.get("sourceId")
        _validate_uuid(revision_id, f"sources.jsonl line {index} sourceRevisionId")
        _validate_uuid(source_id, f"sources.jsonl line {index} sourceId")
        if revision_id in source_by_revision:
            raise BundleError("sources.jsonl contains duplicate source revisions")
        if set(source) != {"sourceId", "sourceRevisionId", "revision", "digest", "artifact"}:
            raise BundleError(f"sources.jsonl line {index} has unsupported fields")
        digest = _validate_digest(source.get("digest"), f"sources.jsonl line {index} digest")
        artifact = source.get("artifact")
        if (
            not isinstance(artifact, dict)
            or set(artifact) - {"uri", "digest", "size_bytes", "kind", "manifest_digest"}
            or not {"uri", "digest", "size_bytes", "kind"}.issubset(artifact)
            or not isinstance(artifact.get("uri"), str)
            or re.fullmatch(r"artifact://sha256/[0-9a-f]{64}", artifact["uri"]) is None
            or artifact.get("digest") != digest
            or artifact["uri"].rsplit("/", 1)[-1] != digest.removeprefix("sha256:")
            or type(artifact.get("size_bytes")) is not int
            or artifact["size_bytes"] < 0
            or not isinstance(artifact.get("kind"), str)
            or not artifact["kind"]
        ):
            raise BundleError(f"sources.jsonl line {index} has an invalid ArtifactRef")
        if "manifest_digest" in artifact:
            _validate_digest(
                artifact["manifest_digest"],
                f"sources.jsonl line {index} manifest digest",
            )
        if type(source.get("revision")) is not int or source["revision"] < 1:
            raise BundleError(f"sources.jsonl line {index} has an invalid revision")
        source_by_revision[revision_id] = source
    if manifest["sourceRevisionIds"] != sorted(source_by_revision):
        raise BundleError("manifest sourceRevisionIds do not match sources.jsonl")

    chunk_ids: set[str] = set()
    for index, chunk in enumerate(chunks, start=1):
        chunk_id = chunk.get("chunkId")
        try:
            _validate_digest(chunk_id, f"chunks.jsonl line {index} chunkId")
        except BundleError as error:
            raise BundleError(f"chunks.jsonl line {index} has an invalid chunkId") from error
        if chunk_id in chunk_ids:
            raise BundleError(f"chunks.jsonl line {index} has a missing or duplicate chunkId")
        chunk_ids.add(chunk_id)
        expected_keys = {
            "chunkId",
            "text",
            "sourceId",
            "sourceRevisionId",
            "contentRevisionId",
            "blockId",
            "blockOrdinal",
            "blockKind",
            "origin",
            "locator",
            "textRange",
            "hierarchyPath",
            "policy",
            "assetRefs",
            "partIndex",
            "partCount",
        }
        if set(chunk) != expected_keys:
            raise BundleError(f"chunks.jsonl line {index} has unsupported fields")
        for key in ("sourceId", "sourceRevisionId", "contentRevisionId"):
            _validate_uuid(chunk.get(key), f"chunks.jsonl line {index} {key}")
        for key in ("blockId", "blockKind", "text"):
            if not isinstance(chunk.get(key), str) or not chunk[key]:
                raise BundleError(f"chunks.jsonl line {index} has invalid {key}")
        if type(chunk.get("blockOrdinal")) is not int or chunk["blockOrdinal"] < 0:
            raise BundleError(f"chunks.jsonl line {index} has invalid blockOrdinal")
        if chunk.get("origin") not in {"EXTRACTED", "NORMALIZED", "HUMAN_EDITED", "GENERATED"}:
            raise BundleError(f"chunks.jsonl line {index} has invalid origin")
        source = source_by_revision.get(chunk["sourceRevisionId"])
        if source is None or source["sourceId"] != chunk["sourceId"]:
            raise BundleError(f"chunks.jsonl line {index} references an unknown source")
        if chunk["contentRevisionId"] != manifest["contentRevisionId"]:
            raise BundleError(f"chunks.jsonl line {index} has a different content revision")
        if not isinstance(chunk.get("locator"), dict) or not isinstance(
            chunk.get("assetRefs"), list
        ):
            raise BundleError(f"chunks.jsonl line {index} has invalid locator or assetRefs")
        _validate_locator(chunk["locator"], f"chunks.jsonl line {index} locator")
        if any(not isinstance(item, dict) for item in chunk["assetRefs"]):
            raise BundleError(f"chunks.jsonl line {index} has invalid assetRefs")
        text_range = chunk.get("textRange")
        if (
            not isinstance(text_range, dict)
            or set(text_range) != {"start", "end"}
            or type(text_range.get("start")) is not int
            or type(text_range.get("end")) is not int
            or text_range["start"] < 0
            or text_range["end"] < max(1, text_range["start"])
        ):
            raise BundleError(f"chunks.jsonl line {index} has invalid textRange")
        hierarchy_path = chunk.get("hierarchyPath")
        if not isinstance(hierarchy_path, list) or any(
            not isinstance(part, str) for part in hierarchy_path
        ):
            raise BundleError(f"chunks.jsonl line {index} has invalid hierarchyPath")
        part_index = chunk.get("partIndex")
        part_count = chunk.get("partCount")
        if (
            type(part_index) is not int
            or type(part_count) is not int
            or part_index < 0
            or part_count < 1
            or part_index >= part_count
        ):
            raise BundleError(f"chunks.jsonl line {index} has invalid part index")
        _validate_stored_policy(chunk.get("policy"), f"chunks.jsonl line {index} policy")
    report = manifest["conversionReport"]
    if report.get("chunkCount") != len(chunks) or report.get("sourceCount") != len(sources):
        raise BundleError("manifest conversionReport counts do not match package records")
    if (
        not isinstance(hierarchy, dict)
        or hierarchy.get("schemaVersion") != "cyrene.knowledge.hierarchy.v1"
        or not isinstance(hierarchy.get("sources"), list)
    ):
        raise BundleError("hierarchy.json has an unsupported shape")


def _validate_stored_policy(value: Any, label: str) -> None:
    """Reject incomplete or unsupported stored policy fields before retrieval."""

    required = {"allowKnowledge", "allowTraining", "allowedPrincipalRefs", "allowedUsePurposes"}
    if not isinstance(value, dict) or set(value) != required:
        raise BundleError(f"{label} is missing or malformed")
    if type(value["allowKnowledge"]) is not bool or type(value["allowTraining"]) is not bool:
        raise BundleError(f"{label} contains invalid booleans")
    for key in ("allowedPrincipalRefs", "allowedUsePurposes"):
        values = value[key]
        if (
            not isinstance(values, list)
            or any(not isinstance(item, str) or not item.strip() for item in values)
            or len(values) != len(set(values))
        ):
            raise BundleError(f"{label}.{key} is malformed")
    if any(item not in _VALID_PURPOSES for item in value["allowedUsePurposes"]):
        raise BundleError(f"{label} contains an unsupported use purpose")


def _validate_locator(value: dict[str, Any], label: str) -> None:
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
    if set(value) - allowed:
        raise BundleError(f"{label} contains unsupported fields")
    if "page" in value and (type(value["page"]) is not int or value["page"] < 1):
        raise BundleError(f"{label}.page is invalid")
    if "sourcePages" in value and (
        not isinstance(value["sourcePages"], list)
        or any(type(item) is not int or item < 1 for item in value["sourcePages"])
        or len(value["sourcePages"]) != len(set(value["sourcePages"]))
    ):
        raise BundleError(f"{label}.sourcePages is invalid")
    if "sectionPath" in value and (
        not isinstance(value["sectionPath"], list)
        or any(not isinstance(item, str) for item in value["sectionPath"])
    ):
        raise BundleError(f"{label}.sectionPath is invalid")
    if "itemRef" in value and (not isinstance(value["itemRef"], str) or not value["itemRef"]):
        raise BundleError(f"{label}.itemRef is invalid")
    for key in ("treeLevel", "tableIndex", "startOffset", "endOffset"):
        if key in value and (type(value[key]) is not int or value[key] < 0):
            raise BundleError(f"{label}.{key} is invalid")
    if "provenance" in value and (
        not isinstance(value["provenance"], list)
        or any(not isinstance(item, dict) for item in value["provenance"])
    ):
        raise BundleError(f"{label}.provenance is invalid")
    if (
        "startOffset" in value
        and "endOffset" in value
        and value["endOffset"] < value["startOffset"]
    ):
        raise BundleError(f"{label}.endOffset precedes startOffset")


def _validate_search_request(
    query: str,
    principal_refs: Sequence[str],
    use_purpose: str,
    limit: int,
    offset: int,
    max_limit: int,
    max_query_chars: int,
) -> None:
    if not isinstance(query, str) or not query.strip() or len(query) > max_query_chars:
        raise BundleError("query must be non-empty text within the configured limit")
    if (
        isinstance(principal_refs, (str, bytes))
        or not isinstance(principal_refs, Sequence)
        or not principal_refs
        or any(not isinstance(item, str) or not item.strip() for item in principal_refs)
        or len(principal_refs) != len(set(principal_refs))
    ):
        raise BundleError("principalRefs must contain unique authenticated principal references")
    if use_purpose != USE_PURPOSE:
        raise BundleError(f"usePurpose must be {USE_PURPOSE!r}")
    if type(limit) is not int or not 1 <= limit <= min(100, max_limit):
        raise BundleError("limit exceeds the configured search bound")
    if type(offset) is not int or not 0 <= offset <= MAX_SEARCH_OFFSET:
        raise BundleError("chunkCursor exceeds the configured search bound")


def _is_authorized(policy: dict[str, Any], principal_refs: set[str], purpose: str) -> bool:
    """Require explicit knowledge permission, purpose, and principal intersection."""

    return (
        policy["allowKnowledge"] is True
        and purpose in policy["allowedUsePurposes"]
        and bool(principal_refs.intersection(policy["allowedPrincipalRefs"]))
    )


def _bm25(
    chunks: Sequence[dict[str, Any]], query_terms: list[str], limit: int
) -> list[tuple[float, dict[str, Any]]]:
    """Rank only already-authorized chunks with deterministic CPU BM25."""

    if not chunks or not query_terms:
        return []
    tokenized = [_tokenize(chunk["text"]) for chunk in chunks]
    lengths = [len(item) for item in tokenized]
    average_length = sum(lengths) / len(lengths)
    document_frequency: dict[str, int] = {}
    for tokens in tokenized:
        for term in set(tokens):
            document_frequency[term] = document_frequency.get(term, 0) + 1

    k1 = 1.2
    b = 0.75
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk, tokens, length in zip(chunks, tokenized, lengths, strict=True):
        if not tokens:
            continue
        frequencies: dict[str, int] = {}
        for term in tokens:
            frequencies[term] = frequencies.get(term, 0) + 1
        score = 0.0
        for term in dict.fromkeys(query_terms):
            count = frequencies.get(term, 0)
            if count == 0:
                continue
            doc_freq = document_frequency[term]
            inverse = math.log(1 + (len(chunks) - doc_freq + 0.5) / (doc_freq + 0.5))
            normalization = count + k1 * (1 - b + b * length / average_length)
            score += inverse * count * (k1 + 1) / normalization
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: (-item[0], item[1]["chunkId"]))
    return scored[:limit]


def _public_hit(chunk: dict[str, Any]) -> dict[str, Any]:
    """Remove stored authorization lists while preserving source locator facts."""

    return {key: value for key, value in chunk.items() if key != "policy"}


def _tokenize(text: str) -> list[str]:
    """Extract normalized word tokens and bigrams for common CJK scripts."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    terms: list[str] = []
    for match in _TOKEN_PATTERN.finditer(normalized):
        token = match.group(0)
        if any(_is_cjk(character) for character in token):
            if len(token) == 1:
                terms.append(token)
            else:
                terms.extend(token[index : index + 2] for index in range(len(token) - 1))
        else:
            terms.append(token)
    return terms


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _parse_jsonl(data: bytes, label: str, maximum: int) -> list[dict[str, Any]]:
    """Decode bounded UTF-8 JSONL and reject blank/non-object records."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BundleError(f"{label} must be UTF-8") from error
    if not text:
        return []
    records: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if index > maximum:
            raise BundleError(f"{label} exceeds the configured record limit")
        if not line.strip():
            raise BundleError(f"{label} line {index} is empty")
        item = _loads_strict(line)
        if not isinstance(item, dict):
            raise BundleError(f"{label} line {index} must be an object")
        records.append(item)
    return records


def _validate_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise BundleError(f"{label} must match sha256:<64 lowercase hex>")
    return value


def _validate_uuid(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            value,
        )
        is None
    ):
        raise BundleError(f"{label} must be a UUID")
    try:
        uuid.UUID(value)
    except ValueError as error:
        raise BundleError(f"{label} must be a UUID") from error
    return value


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise BundleError("bundle artifact could not be hashed") from error
    return "sha256:" + digest.hexdigest()


def _loads_strict(value: bytes | str) -> Any:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise BundleError(f"duplicate JSON key {key!r}")
            result[key] = item
        return result

    def invalid_constant(value: str) -> None:
        raise BundleError(f"invalid JSON constant {value}")

    try:
        return json.loads(value, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BundleError("bundle contains invalid JSON") from error
