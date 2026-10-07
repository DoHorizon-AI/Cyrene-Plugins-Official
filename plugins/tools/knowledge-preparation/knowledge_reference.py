"""Verify and search knowledge bundles without Cyrene services or databases.

This module uses only the Python standard library. Callers supply the package
digest and an already-authenticated principal set; stored ACL and purpose rules
are applied before any text is scored or returned.

模块职责：独立校验知识包并先执行 ACL / 用途过滤，再进行 CPU 文本检索。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE = "CYRENE_KNOWLEDGE_BUNDLE_V1"
PROFILE_VERSION = 1
SUPPORTED_USE_PURPOSE = "knowledge_retrieval"
VALID_USE_PURPOSES = {"knowledge_retrieval", "model_training"}
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 192 * 1024 * 1024
MAX_SEARCH_LIMIT = 100
EXPECTED_MEMBERS = {
    "manifest.json",
    "chunks.jsonl",
    "sources.jsonl",
    "hierarchy.json",
    "checksums.json",
}
PACKAGE_FILES = ("chunks.jsonl", "sources.jsonl", "hierarchy.json")
_TOKEN_PATTERN = re.compile(r"[\w]+", flags=re.UNICODE)


@dataclass(frozen=True, slots=True)
class VerifiedBundle:
    """A package whose outer digest and every declared member hash match."""

    package_digest: str
    manifest: dict[str, Any]
    chunks: tuple[dict[str, Any], ...]
    sources: tuple[dict[str, Any], ...]


def verify_bundle(
    bundle_path: str | Path,
    *,
    package_digest: str,
) -> dict[str, Any]:
    """Verify outer package digest, ZIP members and internal SHA-256 manifest.

    Args:
        bundle_path: Local path to the downloaded ZIP package.
        package_digest: Expected digest from the published ArtifactRef.
    Returns:
        A compact package identity and manifest summary.
    Raises:
        ValueError: If the expected digest, archive, manifest or any member fails.

    中文:要求调用者传入已发布 ArtifactRef 的 package digest，并校验每个内部文件。
    """

    bundle = _load_verified_bundle(bundle_path, package_digest=package_digest)
    return {
        "packageDigest": bundle.package_digest,
        "profile": bundle.manifest["profile"],
        "profileVersion": bundle.manifest["profileVersion"],
        "chunkCount": len(bundle.chunks),
        "sourceCount": len(bundle.sources),
        "contentRevisionId": bundle.manifest["contentRevisionId"],
    }


def search_bundle(
    bundle_path: str | Path,
    query: str,
    *,
    principal_refs: Sequence[str],
    use_purpose: str,
    package_digest: str,
    limit: int = 5,
) -> dict[str, Any]:
    """Verify, filter by actual stored policy, then rank matching chunks.

    Args:
        bundle_path: Local path to the downloaded ZIP package.
        query: Plain-text query used by the CPU BM25 scorer.
        principal_refs: Authenticated user and group refs supplied by the caller.
        use_purpose: Explicit purpose; v1 accepts only ``knowledge_retrieval``.
        package_digest: Expected package digest from the published ArtifactRef.
        limit: Maximum number of authorized hits to return, from 1 through 100.
    Returns:
        Authorized hits with exact source revision and locator metadata.

    中文:在 ACL 与用途策略过滤完成后，才对候选 chunk 计算词项相关性。
    """

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if use_purpose != SUPPORTED_USE_PURPOSE:
        raise ValueError(f"use_purpose must be {SUPPORTED_USE_PURPOSE!r}")
    if isinstance(principal_refs, (str, bytes)) or not isinstance(
        principal_refs, Sequence
    ):
        raise TypeError("principal_refs must be an array of principal refs")
    if any(not isinstance(item, str) or not item.strip() for item in principal_refs):
        raise ValueError("principal_refs must contain non-empty strings")
    if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError(f"limit must be an integer from 1 through {MAX_SEARCH_LIMIT}")

    bundle = _load_verified_bundle(bundle_path, package_digest=package_digest)
    caller_principals = set(principal_refs)
    # Enforce access policy before tokenization/scoring to avoid ranking denied text.
    authorized = [
        chunk
        for chunk in bundle.chunks
        if _is_authorized(chunk["policy"], caller_principals, use_purpose)
    ]
    query_terms = _tokenize(query)
    ranked = _bm25(authorized, query_terms, limit)
    return {
        "packageDigest": bundle.package_digest,
        "usePurpose": use_purpose,
        "query": query,
        "hits": [
            {
                "score": round(score, 8),
                "chunk": _public_hit(chunk),
            }
            for score, chunk in ranked
        ],
    }


def _load_verified_bundle(
    bundle_path: str | Path,
    *,
    package_digest: str,
) -> VerifiedBundle:
    """Load an archive only after outer and inner hashes have been verified."""

    expected_digest = _validate_digest(package_digest, "package_digest")
    path = Path(bundle_path)
    if not path.is_file():
        raise ValueError("bundle_path must identify a regular file")
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError(f"bundle exceeds {MAX_PACKAGE_BYTES} bytes")
    actual_digest = _digest_file(path)
    if actual_digest != expected_digest:
        raise ValueError("package digest does not match the published packageDigest")

    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise ValueError("bundle contains duplicate ZIP members")
            if set(names) != EXPECTED_MEMBERS:
                raise ValueError(
                    "bundle members do not match CYRENE_KNOWLEDGE_BUNDLE_V1"
                )
            total_uncompressed = 0
            for info in infos:
                if info.is_dir() or "/" in info.filename or "\\" in info.filename:
                    raise ValueError("bundle members must be flat files")
                if info.file_size > MAX_MEMBER_BYTES:
                    raise ValueError(
                        f"bundle member {info.filename} exceeds size limit"
                    )
                total_uncompressed += info.file_size
            if total_uncompressed > MAX_PACKAGE_BYTES:
                raise ValueError("bundle expanded size exceeds limit")
            members = {name: archive.read(name) for name in names}
    except zipfile.BadZipFile as exc:
        raise ValueError("bundle is not a valid ZIP archive") from exc

    checksums = _loads_strict(members["checksums.json"])
    if (
        not isinstance(checksums, dict)
        or checksums.get("schemaVersion") != "cyrene.knowledge.checksums.v1"
    ):
        raise ValueError("checksums.json has an unsupported schemaVersion")
    declared = checksums.get("files")
    expected_names = EXPECTED_MEMBERS - {"checksums.json"}
    if not isinstance(declared, dict) or set(declared) != expected_names:
        raise ValueError("checksums.json must list each package member except itself")
    for name in expected_names:
        digest = _validate_digest(declared[name], f"checksums.json.files.{name}")
        if _digest_bytes(members[name]) != digest:
            raise ValueError(f"package member digest mismatch: {name}")

    manifest = _loads_strict(members["manifest.json"])
    if not isinstance(manifest, dict):
        raise TypeError("manifest.json must be an object")
    if (
        manifest.get("schemaVersion") != "cyrene.knowledge.bundle.v1"
        or manifest.get("profile") != PROFILE
        or manifest.get("profileVersion") != PROFILE_VERSION
        or manifest.get("usePurpose") != SUPPORTED_USE_PURPOSE
    ):
        raise ValueError("manifest profile or version is unsupported")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict) or set(manifest_files) != set(
        PACKAGE_FILES
    ):
        raise ValueError("manifest.files must list chunks, sources and hierarchy")
    for name in PACKAGE_FILES:
        declared_digest = _validate_digest(
            manifest_files[name], f"manifest.files.{name}"
        )
        if declared_digest != declared[name]:
            raise ValueError(f"manifest and checksums disagree for {name}")
    if declared["manifest.json"] != _digest_bytes(members["manifest.json"]):
        raise ValueError("manifest digest mismatch")

    chunks = _parse_jsonl(members["chunks.jsonl"], "chunks.jsonl")
    sources = _parse_jsonl(members["sources.jsonl"], "sources.jsonl")
    hierarchy = _loads_strict(members["hierarchy.json"])
    _validate_package_records(manifest, chunks, sources, hierarchy)
    return VerifiedBundle(
        package_digest=actual_digest,
        manifest=manifest,
        chunks=tuple(chunks),
        sources=tuple(sources),
    )


def _validate_package_records(
    manifest: dict[str, Any],
    chunks: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    hierarchy: Any,
) -> None:
    """Validate record identity and fail closed for malformed policies."""

    for key in ("datasetId", "contentRevisionId", "processingRunId"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise ValueError(f"manifest.{key} must be a non-empty string")
    source_ids: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise TypeError(f"sources.jsonl line {index + 1} must be an object")
        source_revision_id = source.get("sourceRevisionId")
        if not isinstance(source_revision_id, str) or not source_revision_id:
            raise ValueError(f"sources.jsonl line {index + 1} has no sourceRevisionId")
        if source_revision_id in source_ids:
            raise ValueError("sources.jsonl contains duplicate sourceRevisionId values")
        source_ids.add(source_revision_id)
        _validate_digest(source.get("digest"), f"sources.jsonl line {index + 1} digest")
        artifact = source.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("digest") != source["digest"]:
            raise ValueError(f"sources.jsonl line {index + 1} has invalid ArtifactRef")

    chunk_ids: set[str] = set()
    for index, chunk in enumerate(chunks):
        line = index + 1
        if not isinstance(chunk, dict):
            raise TypeError(f"chunks.jsonl line {line} must be an object")
        chunk_id = chunk.get("chunkId")
        if not isinstance(chunk_id, str) or not chunk_id:
            raise ValueError(f"chunks.jsonl line {line} has no chunkId")
        if chunk_id in chunk_ids:
            raise ValueError("chunks.jsonl contains duplicate chunkId values")
        chunk_ids.add(chunk_id)
        for key in ("sourceRevisionId", "contentRevisionId", "blockId", "text"):
            if not isinstance(chunk.get(key), str) or not chunk[key]:
                raise ValueError(f"chunks.jsonl line {line} has invalid {key}")
        if chunk["sourceRevisionId"] not in source_ids:
            raise ValueError(f"chunks.jsonl line {line} references an unknown source")
        if chunk["contentRevisionId"] != manifest["contentRevisionId"]:
            raise ValueError(
                f"chunks.jsonl line {line} belongs to another content revision"
            )
        if not isinstance(chunk.get("locator"), dict):
            raise TypeError(f"chunks.jsonl line {line} has invalid locator")
        if not isinstance(chunk.get("assetRefs"), list):
            raise TypeError(f"chunks.jsonl line {line} has invalid assetRefs")
        policy = chunk.get("policy")
        _validate_stored_policy(policy, f"chunks.jsonl line {line} policy")

    if manifest.get("sourceRevisionIds") != sorted(source_ids):
        raise ValueError("manifest sourceRevisionIds do not match sources.jsonl")
    report = manifest.get("conversionReport")
    if not isinstance(report, dict):
        raise TypeError("manifest.conversionReport must be an object")
    if report.get("chunkCount") != len(chunks) or report.get("sourceCount") != len(
        sources
    ):
        raise ValueError(
            "manifest conversionReport counts do not match package contents"
        )
    if (
        not isinstance(hierarchy, dict)
        or hierarchy.get("schemaVersion") != "cyrene.knowledge.hierarchy.v1"
        or not isinstance(hierarchy.get("sources"), list)
    ):
        raise ValueError("hierarchy.json has an unsupported shape")


def _validate_stored_policy(policy: Any, label: str) -> None:
    """Reject incomplete or unsupported stored policy before retrieval."""

    if not isinstance(policy, dict):
        raise TypeError(f"{label} is missing or malformed")
    expected = {
        "allowKnowledge",
        "allowTraining",
        "allowedPrincipalRefs",
        "allowedUsePurposes",
    }
    if set(policy) != expected:
        raise ValueError(f"{label} is missing or malformed")
    if (
        type(policy["allowKnowledge"]) is not bool
        or type(policy["allowTraining"]) is not bool
    ):
        raise ValueError(f"{label} has malformed booleans")
    for key in ("allowedPrincipalRefs", "allowedUsePurposes"):
        values = policy[key]
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not item.strip() for item in values
        ):
            raise ValueError(f"{label} has malformed {key}")
        if len(set(values)) != len(values):
            raise ValueError(f"{label} has duplicate {key}")
    if any(value not in VALID_USE_PURPOSES for value in policy["allowedUsePurposes"]):
        raise ValueError(f"{label} contains an unsupported use purpose")


def _is_authorized(policy: dict[str, Any], principals: set[str], purpose: str) -> bool:
    """Apply exact stored ACL and purpose checks; missing data never grants access."""

    return (
        policy.get("allowKnowledge") is True
        and purpose in policy.get("allowedUsePurposes", [])
        and bool(principals.intersection(policy.get("allowedPrincipalRefs", [])))
    )


def _bm25(
    chunks: list[dict[str, Any]], query_terms: list[str], limit: int
) -> list[tuple[float, dict[str, Any]]]:
    """Rank authorized chunks with a compact, deterministic BM25 implementation."""

    if not chunks or not query_terms:
        return []
    tokenized = [_tokenize(chunk["text"]) for chunk in chunks]
    lengths = [len(tokens) for tokens in tokenized]
    average_length = sum(lengths) / len(lengths) if lengths else 0.0
    document_frequency: dict[str, int] = {}
    for tokens in tokenized:
        for term in set(tokens):
            document_frequency[term] = document_frequency.get(term, 0) + 1

    k1 = 1.2
    b = 0.75
    unique_query_terms = list(dict.fromkeys(query_terms))
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk, tokens, length in zip(chunks, tokenized, lengths, strict=True):
        if not tokens:
            continue
        frequency: dict[str, int] = {}
        for term in tokens:
            frequency[term] = frequency.get(term, 0) + 1
        score = 0.0
        for term in unique_query_terms:
            count = frequency.get(term, 0)
            if count == 0:
                continue
            doc_freq = document_frequency.get(term, 0)
            inverse = math.log(1 + (len(chunks) - doc_freq + 0.5) / (doc_freq + 0.5))
            normalization = count + k1 * (
                1 - b + b * length / average_length if average_length else 1 - b
            )
            score += inverse * count * (k1 + 1) / normalization
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: (-item[0], item[1]["chunkId"]))
    return scored[:limit]


def _public_hit(chunk: dict[str, Any]) -> dict[str, Any]:
    """Omit ACL membership details while returning provenance and locator fields."""

    return {key: value for key, value in chunk.items() if key != "policy"}


def _tokenize(text: str) -> list[str]:
    """Extract Latin word tokens and CJK bigrams for CPU text search."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    terms: list[str] = []
    for match in _TOKEN_PATTERN.finditer(normalized):
        token = match.group(0)
        if any(_is_cjk_character(character) for character in token):
            if len(token) == 1:
                terms.append(token)
            else:
                terms.extend(
                    token[index : index + 2] for index in range(len(token) - 1)
                )
        else:
            terms.append(token)
    return terms


def _is_cjk_character(character: str) -> bool:
    """Recognize common Han, Hiragana, Katakana and Hangul code points."""

    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _parse_jsonl(data: bytes, label: str) -> list[dict[str, Any]]:
    """Decode package JSONL strictly, rejecting blank and non-object records."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be UTF-8") from exc
    if not text:
        return []
    records = []
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"{label} line {index} is empty")
        value = _loads_strict(line)
        if not isinstance(value, dict):
            raise TypeError(f"{label} line {index} must be an object")
        records.append(value)
    return records


def _validate_digest(value: Any, label: str) -> str:
    """Require canonical lower-case SHA-256 digest spelling."""

    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise ValueError(f"{label} must match sha256:<64 lowercase hex>")
    return value


def _digest_bytes(value: bytes) -> str:
    """Hash a byte sequence in canonical receipt form."""

    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_file(path: Path) -> str:
    """Hash a package incrementally."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _loads_strict(value: bytes | str) -> Any:
    """Decode JSON while rejecting ambiguous duplicate keys."""

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = item
        return result

    return json.loads(value, object_pairs_hook=unique_pairs)


def _main(argv: list[str] | None = None) -> int:
    """Run the standalone verify/search CLI without importing Cyrene code."""

    parser = argparse.ArgumentParser(
        description="Verify and search a Cyrene knowledge bundle"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser(
        "verify", help="verify package digest and manifest"
    )
    verify_parser.add_argument("--bundle", required=True)
    verify_parser.add_argument("--package-digest", required=True)
    search_parser = subparsers.add_parser(
        "search", help="search authorized text chunks"
    )
    search_parser.add_argument("--bundle", required=True)
    search_parser.add_argument("--package-digest", required=True)
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--principal-ref", action="append", default=[])
    search_parser.add_argument(
        "--use-purpose", required=True, choices=[SUPPORTED_USE_PURPOSE]
    )
    search_parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_bundle(args.bundle, package_digest=args.package_digest)
        else:
            result = search_bundle(
                args.bundle,
                args.query,
                principal_refs=args.principal_ref,
                use_purpose=args.use_purpose,
                package_digest=args.package_digest,
                limit=args.limit,
            )
    except (OSError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
