"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 training_curation.py                                            │
│  Module: tools.dataset_preparation.training_curation                 │
│  Role: Stream, normalize, diagnose, and checkpoint training records. │
│                                                                      │
│  模块职责：流式整理训练记录，保留原始数据并生成可审核的规范记录。         │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "cyrene.training-record.v1"
SUPPORTED_FORMATS = {
    "auto",
    "alpaca",
    "promptCompletion",
    "messages",
    "sharegpt",
    "chatml",
}
SUPPORTED_ROLES = {"system", "user", "assistant"}
MAX_SOURCES = 20
MAX_SOURCE_BYTES = 64 * 1024 * 1024
CHECKPOINT_INTERVAL = 64
NEAR_DUPLICATE_THRESHOLD = 0.9
_LEARNED_MAPPING_KEYS = {
    "instruction",
    "input",
    "output",
    "prompt",
    "completion",
    "messages",
    "conversations",
    "chatml",
    "system",
    "history",
    "conversationRole",
    "conversationContent",
}
_RESERVED_RECORD_FIELDS = {
    "id",
    "recordid",
    "record_id",
    "sampleid",
    "sample_id",
    "sourcefamily",
    "source_family",
    "sourcefamilyid",
    "source_family_id",
    "sourcerevisionid",
    "source_revision_id",
    "conversationid",
    "conversation_id",
    "groupid",
    "group_id",
    "ordinal",
    "locator",
    "rawrecord",
    "raw_record",
    "rawline",
    "raw_line",
    "detectedformat",
    "detected_format",
    "normalized",
    "disposition",
    "issues",
    "contentdigest",
    "content_digest",
    "recipedigest",
    "recipe_digest",
    "processinghistory",
    "processing_history",
    "policy",
    "allowtraining",
    "allow_training",
    "allowedusepurposes",
    "allowed_use_purposes",
    "_acl",
    "acl",
    "_operatorannotation",
    "operatorannotation",
    "_operator_annotation",
    "operator_annotation",
    "_reviewnote",
    "reviewnote",
    "review_note",
    "_review_note",
    "allowedprincipalrefs",
    "allowed_principal_refs",
}
_CHATML_START = "<|im_start|>"
_CHATML_SEPARATOR = "<|im_sep|>"
_CHATML_ENDINGS = ("<|im_end|>", "<|fim_suffix|>", "<|ghissue|>")
_CHATML_MEDIA_MARKERS = ("<|image|>", "<|audio|>", "<|video|>", "<|vector3|>")
_WORD = re.compile(r"\w+", re.UNICODE)


class CurationCancelled(Exception):
    """Signal a cooperative cancellation after persisting a checkpoint."""


def curate_training_records(
    *,
    sources: list[dict[str, Any]],
    result_path: str | Path,
    checkpoint_path: str | Path,
    recipe_digest: str,
    recipe: dict[str, Any],
    cancellation: Any | None = None,
) -> dict[str, Any]:
    """Create a resumable, line-oriented normalized record artifact.

    Each input record is copied into an immutable envelope with its exact
    source locator and raw value. Invalid JSONL lines become issue records so
    one broken row cannot hide later rows. Checkpoints are committed only
    after output bytes are flushed, making retries safe from duplicate rows.

    Args:
        sources: At most twenty staged source descriptors with lineage and policy.
        result_path: Absolute output JSONL path for immutable record envelopes.
        checkpoint_path: Absolute JSON checkpoint path; a SQLite dedup index is
            kept beside it and can be reconstructed from committed output.
        recipe_digest: Stable digest of the versioned Catalyst recipe.
        recipe: Versioned normalization and format configuration.
        cancellation: Optional runtime cancellation token.
    Returns:
        Bounded receipt metadata and reconciled counts.
    """

    normalized_sources = _validate_sources(sources)
    output = _absolute_path(result_path, "result_path")
    checkpoint = _absolute_path(checkpoint_path, "checkpoint_path")
    input_paths = {Path(source["source_path"]) for source in normalized_sources}
    if output.is_symlink() or checkpoint.is_symlink():
        raise ValueError("result_path and checkpoint_path cannot be symlinks")
    if output in input_paths:
        raise ValueError("result_path cannot overwrite an input source")
    if checkpoint == output or checkpoint in input_paths:
        raise ValueError("checkpoint_path must be separate from input and result paths")
    recipe = _validate_recipe(recipe)
    recipe_digest = _required_text(recipe_digest, "recipe_digest")
    for source in normalized_sources:
        source["input_digest"] = _sha256_file(Path(source["source_path"]))

    batch_digest = _digest_json(
        {
            "recipeDigest": recipe_digest,
            "recipe": recipe,
            "sources": [
                {
                    "sourceRevisionId": source["source_revision_id"],
                    "sourceFamilyId": source["source_family_id"],
                    "inputDigest": source["input_digest"],
                    "filename": source["filename"],
                    "policy": source.get("policy"),
                    "format": source.get("format"),
                    "fieldMapping": source.get("field_mapping"),
                    "roleMapping": source.get("role_mapping"),
                }
                for source in normalized_sources
            ],
        }
    )
    initial = _initial_state(batch_digest, recipe_digest, output, normalized_sources)
    state, resumed = _load_or_reset_checkpoint(checkpoint, output, initial)
    output.parent.mkdir(parents=True, exist_ok=True)
    index_path = checkpoint.with_name(checkpoint.name + ".dedup.sqlite3")
    if index_path == output or index_path in input_paths:
        raise ValueError("dedup index path collides with an input or result path")
    with sqlite3.connect(index_path) as index:
        _create_index(index)
        if resumed:
            _rebuild_index(index, output, int(state["output_bytes"]))
        else:
            index.execute("DELETE FROM exact_records")
            index.execute("DELETE FROM prompt_tokens")
            index.execute("DELETE FROM prompts")

        output.touch(exist_ok=True)
        with output.open("r+b") as stream:
            stream.truncate(int(state["output_bytes"]))
            stream.seek(int(state["output_bytes"]))
            pending = 0
            committed_source_index = int(state["source_index"])
            for source_index in range(committed_source_index, len(normalized_sources)):
                source = normalized_sources[source_index]
                start_ordinal = (
                    int(state["source_ordinal"])
                    if source_index == committed_source_index
                    else 0
                )
                start_offset = (
                    int(state["source_offset"])
                    if source_index == committed_source_index
                    else 0
                )
                start_line = (
                    int(state["source_line"])
                    if source_index == committed_source_index
                    else 1
                )
                iterator = _iter_source_records(
                    Path(source["source_path"]),
                    str(source["filename"]),
                    source.get("format"),
                    start_ordinal=start_ordinal,
                    start_offset=start_offset,
                    start_line=start_line,
                )
                source_failure: str | None = None
                while True:
                    try:
                        item = next(iterator)
                    except StopIteration:
                        break
                    except (OSError, ValueError) as exc:
                        source_failure = str(exc).strip()[:2_000] or type(exc).__name__
                        break
                    if cancellation is not None and cancellation.is_cancelled():
                        stream.flush()
                        os.fsync(stream.fileno())
                        _save_checkpoint(checkpoint, state)
                        raise CurationCancelled
                    envelope = _normalize_item(
                        item=item,
                        source=source,
                        recipe=recipe,
                        recipe_digest=recipe_digest,
                        index=index,
                    )
                    encoded = _canonical_json(envelope) + b"\n"
                    stream.write(encoded)
                    state["output_chain_digest"] = _advance_output_chain(
                        state["output_chain_digest"], encoded
                    )
                    _record_counts(state, source_index, envelope)
                    state["output_bytes"] += len(encoded)
                    state["source_index"] = source_index
                    state["source_ordinal"] = item.ordinal + 1
                    state["source_offset"] = item.end_offset
                    state["source_line"] = item.next_line
                    state["total_processed"] += 1
                    pending += 1
                    if pending >= CHECKPOINT_INTERVAL:
                        stream.flush()
                        os.fsync(stream.fileno())
                        _save_checkpoint(checkpoint, state)
                        pending = 0
                if source_failure is not None:
                    source_result = state["sources"][source_index]
                    source_result["status"] = "FAILED"
                    source_result["failure"] = source_failure
                    source_result["diagnostic_counts"]["SOURCE_PARSE_FAILED"] = 1
                    state["issue_counts"]["SOURCE_PARSE_FAILED"] = (
                        state["issue_counts"].get("SOURCE_PARSE_FAILED", 0) + 1
                    )
                if _sha256_file(Path(source["source_path"])) != source["input_digest"]:
                    raise ValueError(
                        f"source changed while curation was reading {source['filename']}"
                    )
                # A completed source has a committed cursor at its boundary.
                state["source_index"] = source_index + 1
                state["source_ordinal"] = 0
                state["source_offset"] = 0
                state["source_line"] = 1
                stream.flush()
                os.fsync(stream.fileno())
                _save_checkpoint(checkpoint, state)
                pending = 0
                if state["sources"][source_index]["status"] == "PENDING":
                    source_result = state["sources"][source_index]
                    if source_result["counts"]["total"] == 0:
                        source_result["diagnostic_counts"]["EMPTY_SOURCE"] = 1
                        source_result["failure"] = "source contained no records"
                        state["issue_counts"]["EMPTY_SOURCE"] = (
                            state["issue_counts"].get("EMPTY_SOURCE", 0) + 1
                        )
                        source_result["status"] = "FAILED"
                    elif source_result["counts"]["recognized"] == 0:
                        source_result["status"] = "FAILED"
                        source_result["failure"] = (
                            "source contained no recognized training records"
                        )
                    else:
                        source_result["status"] = "SUCCEEDED"
                    _save_checkpoint(checkpoint, state)
                elif state["sources"][source_index]["counts"]["recognized"] == 0:
                    state["sources"][source_index]["status"] = "FAILED"
                    _save_checkpoint(checkpoint, state)
            state["complete"] = True
            stream.flush()
            os.fsync(stream.fileno())
            _save_checkpoint(checkpoint, state)

    index_path.unlink(missing_ok=True)
    counts = dict(state["counts"])
    if (
        counts["eligible"] + counts["pendingReview"] + counts["excluded"]
        != counts["total"]
    ):
        raise RuntimeError("curation disposition counts do not reconcile")
    return {
        "result_path": str(output),
        "digest": _sha256_file(output),
        "size_bytes": output.stat().st_size,
        "schema_version": SCHEMA_VERSION,
        "counts": counts,
        "sources": state["sources"],
        "recipe_digest": recipe_digest,
        "resumed_records": int(state["resumed_records"]),
    }


def remap_training_record(
    *,
    record: dict[str, Any],
    recipe: dict[str, Any],
    recipe_digest: str,
    format_hint: str | None = None,
    field_mapping: dict[str, str] | None = None,
    role_mapping: dict[str, str] | None = None,
    raw_record: Any | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Apply the same deterministic normalizer to one human-corrected record.

    The prior raw record and normalized content digest are retained in the
    processing history before the new mapping is applied.

    中文：审核者修正字段或角色映射时复用同一规范化逻辑，并保留旧值。
    """

    if not isinstance(record, dict) or record.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("record must be a cyrene.training-record.v1 envelope")
    recipe = _validate_recipe(recipe)
    recipe_digest = _required_text(recipe_digest, "recipe_digest")
    if recipe_digest != record.get("recipeDigest"):
        raise ValueError("recipe_digest cannot change the record's immutable recipeDigest")
    source_record = record.get("rawRecord") if raw_record is None else raw_record
    selected_format = format_hint or str(record.get("detectedFormat", "auto"))
    fields = (
        _string_mapping(field_mapping, "field_mapping")
        if field_mapping is not None
        else recipe.get("fieldMapping", {})
    )
    _reject_reserved_content_paths(fields, "field_mapping")
    roles = role_mapping if role_mapping is not None else recipe.get("roleMapping", {})
    format_name, messages, issues = _normalize_raw(
        source_record,
        selected_format,
        fields,
        roles,
        recipe,
        raw_line=record.get("rawLine"),
    )
    old_normalized = record.get("normalized")
    old_digest = _digest_json(old_normalized)
    original_raw = record.get("rawRecord")
    updated = dict(record)
    effective_policy, policy_issues = _row_policy(
        record.get("policy"), source_record, fields
    )
    issues.extend(policy_issues)
    issues.extend(_remap_lineage_issues(source_record, fields, record))
    if (
        not effective_policy["allowTraining"]
        or "model_training" not in effective_policy["allowedUsePurposes"]
    ):
        issues.append(
            _issue(
                "POLICY_BLOCKED",
                "record or source policy does not allow model training",
                "error",
            )
        )
    updated["rawRecord"] = source_record
    updated["rawLine"] = None
    updated["detectedFormat"] = format_name
    updated["normalized"] = {"messages": messages} if messages is not None else None
    updated["issues"] = _unique_issues(issues)
    updated["contentDigest"] = _digest_json(updated["normalized"])
    updated["disposition"] = _disposition(
        updated["issues"], effective_policy, messages
    )
    history = list(record.get("processingHistory", []))
    history.append(
        {
            "operation": "humanRemap",
            "mode": "human",
            "recipeDigest": recipe_digest,
            "recipeId": recipe["id"],
            "recipeVersion": recipe["version"],
            "inputDigest": _digest_json(
                {"rawRecord": original_raw, "rawLine": record.get("rawLine")}
            ),
            "previousNormalizedDigest": old_digest,
            "outputDigest": updated["contentDigest"],
            "rawRecordBefore": original_raw,
            "rawLineBefore": record.get("rawLine"),
            "note": note,
            "fieldMapping": fields,
            "roleMapping": roles,
        }
    )
    updated["processingHistory"] = history
    return updated


class _SourceItem:
    """One decoded row plus exact source position and recoverable raw form."""

    __slots__ = (
        "end_offset",
        "locator",
        "next_line",
        "ordinal",
        "parse_issue",
        "raw_line",
        "raw_record",
    )

    def __init__(
        self,
        ordinal: int,
        locator: str,
        raw_record: Any,
        raw_line: str | None,
        parse_issue: dict[str, str] | None,
        end_offset: int,
        next_line: int,
    ) -> None:
        self.ordinal = ordinal
        self.locator = locator
        self.raw_record = raw_record
        self.raw_line = raw_line
        self.parse_issue = parse_issue
        self.end_offset = end_offset
        self.next_line = next_line


def _iter_source_records(
    path: Path,
    filename: str,
    explicit_format: str | None,
    *,
    start_ordinal: int,
    start_offset: int,
    start_line: int,
) -> Iterator[_SourceItem]:
    """Yield JSONL, JSON-array, or single-JSON records without materializing rows."""

    container = _container_format(path, filename, explicit_format)
    if container == "JSONL":
        with path.open("rb") as stream:
            stream.seek(start_offset)
            ordinal = start_ordinal
            line_number = start_line
            while raw_bytes := stream.readline():
                end = stream.tell()
                try:
                    raw_line = raw_bytes.decode(
                        "utf-8-sig" if line_number == 1 else "utf-8"
                    )
                    value_text = raw_line.rstrip("\r\n")
                    if not value_text.strip():
                        issue = _issue(
                            "EMPTY_LINE", "blank JSONL line retained", "warning"
                        )
                        value = None
                    else:
                        try:
                            value = json.loads(value_text)
                            issue = None
                        except json.JSONDecodeError as exc:
                            if (
                                explicit_format == "chatml"
                                or value_text.lstrip().startswith(_CHATML_START)
                            ):
                                value = value_text
                                issue = None
                            else:
                                value = None
                                issue = _issue(
                                    "INVALID_JSON",
                                    f"invalid JSON at column {exc.colno}: {exc.msg}",
                                    "error",
                                )
                except UnicodeDecodeError as exc:
                    raw_line = raw_bytes.decode("utf-8", errors="replace").rstrip(
                        "\r\n"
                    )
                    value = None
                    issue = _issue(
                        "INVALID_ENCODING",
                        f"invalid UTF-8 at byte {exc.start}",
                        "error",
                    )
                if ordinal >= start_ordinal:
                    yield _SourceItem(
                        ordinal,
                        f"line:{line_number}",
                        value,
                        raw_line,
                        issue,
                        end,
                        line_number + 1,
                    )
                ordinal += 1
                line_number += 1
            return

    if container == "CHATML_TEXT":
        if start_ordinal > 0:
            return
        raw_bytes = path.read_bytes()
        try:
            raw_text = raw_bytes.decode("utf-8")
            issue = None
        except UnicodeDecodeError as exc:
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            issue = _issue(
                "INVALID_ENCODING", f"invalid UTF-8 at byte {exc.start}", "error"
            )
        yield _SourceItem(0, "text:1", raw_text, raw_text, issue, len(raw_bytes), 1)
        return

    if container == "JSON_ARRAY":
        yield from _iter_json_array(path, start_ordinal)
        return

    if start_ordinal > 0:
        return
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
        value = json.loads(raw_text)
        issue = None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        if isinstance(exc, json.JSONDecodeError):
            issue = _issue(
                "INVALID_JSON",
                f"invalid JSON at column {exc.colno}: {exc.msg}",
                "error",
            )
        else:
            issue = _issue(
                "INVALID_ENCODING", f"invalid UTF-8 at byte {exc.start}", "error"
            )
        value = None
    yield _SourceItem(0, "item:1", value, raw_text, issue, path.stat().st_size, 1)


def _iter_json_array(path: Path, start_ordinal: int) -> Iterator[_SourceItem]:
    """Incrementally decode a top-level JSON array using a bounded refill buffer."""

    decoder = json.JSONDecoder()
    with path.open("rb") as probe:
        has_bom = probe.read(3) == b"\xef\xbb\xbf"
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        buffer = ""
        base_bytes = 3 if has_bom else 0
        eof = False

        def refill() -> bool:
            nonlocal buffer, eof
            if eof:
                return False
            chunk = stream.read(64 * 1024)
            if chunk == "":
                eof = True
                return False
            buffer += chunk
            return True

        refill()
        leading = len(buffer) - len(buffer.lstrip())
        base_bytes += len(buffer[:leading].encode("utf-8"))
        buffer = buffer[leading:]
        if not buffer.startswith("["):
            raise ValueError("array:1: JSON array source must begin with '['")
        buffer = buffer[1:]
        base_bytes += 1
        ordinal = 0
        while True:
            leading = len(buffer) - len(buffer.lstrip())
            base_bytes += len(buffer[:leading].encode("utf-8"))
            buffer = buffer[leading:]
            while not buffer and refill():
                leading = len(buffer) - len(buffer.lstrip())
                base_bytes += len(buffer[:leading].encode("utf-8"))
                buffer = buffer[leading:]
            if not buffer:
                raise ValueError(
                    f"array:{ordinal + 1}: JSON array ended before closing ']'"
                )
            if buffer.startswith("]"):
                return
            if ordinal:
                if not buffer.startswith(","):
                    raise ValueError(
                        f"array:{ordinal + 1}: JSON array items must be comma-separated"
                    )
                buffer = buffer[1:]
                base_bytes += 1
                leading = len(buffer) - len(buffer.lstrip())
                base_bytes += len(buffer[:leading].encode("utf-8"))
                buffer = buffer[leading:]
                if not buffer:
                    while refill() and not buffer:
                        pass
                if buffer.startswith((",", "]")):
                    raise ValueError(f"array:{ordinal + 1}: JSON array has an empty item")
            elif buffer.startswith(","):
                raise ValueError("array:1: JSON array cannot begin with a comma")
            start_offset = base_bytes
            while True:
                try:
                    value, end = decoder.raw_decode(buffer)
                    break
                except json.JSONDecodeError:
                    if not refill():
                        raise ValueError(
                            f"array:{ordinal + 1}: malformed JSON array item"
                        )
            raw_prefix = buffer[:end]
            end_offset = start_offset + len(raw_prefix.encode("utf-8"))
            if ordinal >= start_ordinal:
                yield _SourceItem(
                    ordinal,
                    f"array:{ordinal + 1}",
                    value,
                    None,
                    None,
                    end_offset,
                    1,
                )
            ordinal += 1
            consumed_bytes = len(raw_prefix.encode("utf-8"))
            base_bytes += consumed_bytes
            buffer = buffer[end:]


def _container_format(path: Path, filename: str, explicit_format: str | None) -> str:
    """Choose the input container without confusing it with row format."""

    if Path(filename).suffix.lower() in {".txt", ".chatml"}:
        return "CHATML_TEXT"
    suffix = Path(filename).suffix.lower() or path.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        return "JSONL"
    if explicit_format == "chatml":
        return "CHATML_TEXT"
    if suffix == ".json":
        with path.open("rb") as stream:
            prefix = stream.read(4096).lstrip(b"\xef\xbb\xbf \t\r\n")
        return "JSON_ARRAY" if prefix.startswith(b"[") else "JSON_OBJECT"
    with path.open("rb") as stream:
        prefix = stream.read(4096).lstrip(b"\xef\xbb\xbf \t\r\n")
    if prefix.startswith(b"["):
        return "JSON_ARRAY"
    lines = [line for line in prefix.splitlines() if line.strip()]
    if len(lines) > 1 and all(line.lstrip().startswith(b"{") for line in lines[:2]):
        return "JSONL"
    if prefix.startswith((b"{", b'"')):
        return "JSON_OBJECT"
    return "CHATML_TEXT"


def _normalize_item(
    *,
    item: _SourceItem,
    source: dict[str, Any],
    recipe: dict[str, Any],
    recipe_digest: str,
    index: sqlite3.Connection,
) -> dict[str, Any]:
    """Create one raw-preserving training record envelope and issue set."""

    selected_format = source.get("format") or recipe["format"]
    field_mapping = source.get("field_mapping") or recipe.get("fieldMapping", {})
    role_mapping = source.get("role_mapping") or recipe.get("roleMapping", {})
    if item.parse_issue is not None:
        detected = "unknown"
        messages = None
        issues = [item.parse_issue]
    else:
        detected, messages, issues = _normalize_raw(
            item.raw_record,
            str(selected_format),
            field_mapping,
            role_mapping,
            recipe,
            raw_line=item.raw_line,
        )
    field_mapping = source.get("field_mapping") or recipe.get("fieldMapping", {})
    policy, policy_issues = _row_policy(
        source.get("policy"), item.raw_record, field_mapping
    )
    issues.extend(policy_issues)
    if (
        not policy["allowTraining"]
        or "model_training" not in policy["allowedUsePurposes"]
    ):
        issues.append(
            _issue(
                "POLICY_BLOCKED", "source policy does not allow model training", "error"
            )
        )
    issues.extend(_unsupported_structure_issues(item.raw_record))
    normalized = {"messages": messages} if messages is not None else None
    raw_digest = _digest_json(
        item.raw_record if item.raw_record is not None else item.raw_line
    )
    content_digest = _digest_json(normalized if normalized is not None else raw_digest)
    history = [
        {
            "operation": "normalize",
            "mode": "deterministic",
            "recipeDigest": recipe_digest,
            "inputDigest": raw_digest,
            "outputDigest": content_digest,
            "format": detected,
            "recipeId": recipe["id"],
            "recipeVersion": recipe["version"],
            "fieldMapping": field_mapping,
            "roleMapping": role_mapping,
            "sourceFormatOverride": source.get("format"),
        }
    ]
    provisional = {
        "schemaVersion": SCHEMA_VERSION,
        "id": f"record:{source['source_revision_id']}:{item.ordinal}",
        "sampleId": _sample_id(item.raw_record, field_mapping, source, item.ordinal),
        "sourceRevisionId": source["source_revision_id"],
        "sourceFamilyId": _training_family_id(
            item.raw_record, field_mapping, source["source_family_id"]
        ),
        "conversationId": _conversation_id(
            item.raw_record, field_mapping, item.ordinal
        ),
        "ordinal": item.ordinal,
        "locator": {"itemRef": item.locator},
        "rawRecord": item.raw_record,
        "rawLine": item.raw_line,
        "detectedFormat": detected,
        "normalized": normalized,
        "disposition": "review",
        "issues": issues,
        "contentDigest": content_digest,
        "recipeDigest": recipe_digest,
        "processingHistory": history,
        "policy": policy,
    }
    if messages is not None and _can_deduplicate(provisional, messages):
        duplicate_issue = _dedup_diagnostics(index, provisional, messages)
        if duplicate_issue is not None:
            issues.append(duplicate_issue)
            history.append(
                {
                    "operation": "deduplicate",
                    "mode": "deterministic",
                    "recipeDigest": recipe_digest,
                    "inputDigest": content_digest,
                    "outputDigest": content_digest,
                    "decision": duplicate_issue["code"],
                    "reference": duplicate_issue["message"],
                }
            )
    provisional["disposition"] = _disposition(issues, policy, messages)
    return provisional


def _normalize_raw(
    raw: Any,
    format_hint: str,
    field_mapping: Mapping[str, Any],
    role_mapping: Mapping[str, Any],
    recipe: Mapping[str, Any],
    *,
    raw_line: str | None,
) -> tuple[str, list[dict[str, Any]] | None, list[dict[str, str]]]:
    """Identify one row format and preserve its role-ordered message projection."""

    issues: list[dict[str, str]] = []
    format_name = _detect_row_format(raw, format_hint, field_mapping)
    if format_name == "unknown":
        return (
            format_name,
            None,
            [
                _issue(
                    "FORMAT_UNRECOGNIZED", "record format was not recognized", "error"
                )
            ],
        )
    allowed_fields = {
        "chatml": {"chatml", "text", "content", "system"},
        "alpaca": {"instruction", "input", "output", "system", "history"},
        "promptCompletion": {"prompt", "completion", "system"},
        "messages": {"messages", "system"},
        "sharegpt": {"conversations", "system"},
    }[format_name]
    issues.extend(
        _unmapped_semantic_field_issues(raw, allowed_fields, field_mapping)
    )
    messages: list[dict[str, Any]] | None = None
    if format_name == "chatml":
        text = (
            raw
            if isinstance(raw, str) and "chatml" not in field_mapping
            else _mapped_value(raw, field_mapping, "chatml")
        )
        if text is None and isinstance(raw, dict):
            text = raw.get("text", raw.get("content"))
        if not isinstance(text, str):
            issues.append(
                _issue("FIELD_TYPE_INVALID", "ChatML source must be text", "error")
            )
        else:
            messages, chat_issues = _parse_chatml(text)
            issues.extend(chat_issues)
        if messages is not None:
            for message in messages:
                role = str(message["role"])
                hard_issue = _unsupported_raw_role_issue(role)
                if hard_issue is not None:
                    issues.append(hard_issue)
                message["role"] = _map_role(role, role_mapping)
                if message["role"] not in SUPPORTED_ROLES:
                    issues.append(
                        _issue(
                            "UNSUPPORTED_ROLE",
                            f"role {role!r} requires an explicit role mapping",
                            "error",
                        )
                    )
                if isinstance(message["content"], str):
                    message["content"] = _normalize_text(message["content"], recipe)
    elif format_name == "alpaca":
        messages = _normalize_alpaca(raw, field_mapping, recipe, issues)
    elif format_name == "promptCompletion":
        prompt = _mapped_value(raw, field_mapping, "prompt", "prompt")
        completion = _mapped_value(raw, field_mapping, "completion", "completion")
        messages = None
        if not isinstance(prompt, str):
            issues.append(
                _issue(
                    "FIELD_MISSING" if prompt is None else "FIELD_TYPE_INVALID",
                    "prompt must be text",
                    "error",
                )
            )
        if not isinstance(completion, str):
            issues.append(
                _issue(
                    "EMPTY_RESPONSE"
                    if completion == ""
                    else "FIELD_MISSING"
                    if completion is None
                    else "FIELD_TYPE_INVALID",
                    "completion must be non-empty text",
                    "error",
                )
            )
        messages = []
        if isinstance(prompt, str) and isinstance(completion, str):
            messages.extend(
                [
                    {"role": "user", "content": _normalize_text(prompt, recipe)},
                    {
                        "role": "assistant",
                        "content": _normalize_text(completion, recipe),
                    },
                ]
            )
        messages = messages or None
    elif format_name == "messages":
        raw_messages = _mapped_value(raw, field_mapping, "messages", "messages")
        messages = _normalize_messages(raw_messages, role_mapping, recipe, issues)
    else:
        raw_conversations = _mapped_value(
            raw, field_mapping, "conversations", "conversations"
        )
        conversations = raw_conversations
        role_key = str(field_mapping.get("conversationRole", "from"))
        content_key = str(field_mapping.get("conversationContent", "value"))
        issues.extend(
            _unmapped_turn_field_issues(
                raw_conversations, {role_key, content_key, "tools", "tool_calls"}
            )
        )
        if (
            isinstance(raw, dict)
            and raw_conversations is not None
            and (role_key != "from" or content_key != "value")
        ):
            conversations = (
                [
                    {"from": item.get(role_key), "value": item.get(content_key)}
                    if isinstance(item, dict)
                    else item
                    for item in raw_conversations
                ]
                if isinstance(raw_conversations, list)
                else raw_conversations
            )
        messages = _normalize_sharegpt(conversations, role_mapping, recipe, issues)
    if format_name != "alpaca":
        messages = _prepend_system_message(raw, field_mapping, recipe, issues, messages)
    if messages is not None:
        _validate_messages(messages, recipe, issues)
    return format_name, messages, _unique_issues(issues)


def _normalize_alpaca(
    raw: Any,
    field_mapping: Mapping[str, Any],
    recipe: Mapping[str, Any],
    issues: list[dict[str, str]],
) -> list[dict[str, Any]] | None:
    """Preserve optional Alpaca system text and prior user/assistant pairs."""

    messages: list[dict[str, Any]] = []
    system = _mapped_value(raw, field_mapping, "system", "system")
    if system is not None:
        if isinstance(system, str):
            messages.append(
                {"role": "system", "content": _normalize_text(system, recipe)}
            )
        else:
            issues.append(
                _issue("FIELD_TYPE_INVALID", "Alpaca system must be text", "error")
            )

    history = _mapped_value(raw, field_mapping, "history", "history")
    if history is not None:
        if not isinstance(history, list):
            issues.append(
                _issue(
                    "MESSAGE_STRUCTURE_INVALID",
                    "Alpaca history must be an array of [user, assistant] pairs",
                    "error",
                )
            )
        else:
            for index, pair in enumerate(history):
                if not isinstance(pair, list) or len(pair) != 2:
                    issues.append(
                        _issue(
                            "MESSAGE_STRUCTURE_INVALID",
                            f"Alpaca history item {index} must be a two-text pair",
                            "error",
                        )
                    )
                    continue
                user_text, assistant_text = pair
                if not isinstance(user_text, str) or not isinstance(
                    assistant_text, str
                ):
                    issues.append(
                        _issue(
                            "FIELD_TYPE_INVALID",
                            f"Alpaca history item {index} must contain two text values",
                            "error",
                        )
                    )
                    continue
                messages.extend(
                    [
                        {
                            "role": "user",
                            "content": _normalize_text(user_text, recipe),
                        },
                        {
                            "role": "assistant",
                            "content": _normalize_text(assistant_text, recipe),
                        },
                    ]
                )

    instruction = _mapped_value(raw, field_mapping, "instruction", "instruction")
    input_text = _mapped_value(raw, field_mapping, "input", "input")
    output = _mapped_value(raw, field_mapping, "output", "output")
    if not isinstance(instruction, str):
        issues.append(
            _issue(
                "FIELD_MISSING" if instruction is None else "FIELD_TYPE_INVALID",
                "Alpaca instruction must be text",
                "error",
            )
        )
    if input_text is not None and not isinstance(input_text, str):
        issues.append(_issue("FIELD_TYPE_INVALID", "Alpaca input must be text", "error"))
    if not isinstance(output, str):
        issues.append(
            _issue(
                "EMPTY_RESPONSE"
                if output == ""
                else "FIELD_MISSING"
                if output is None
                else "FIELD_TYPE_INVALID",
                "Alpaca output must be non-empty text",
                "error",
            )
        )
    if (
        isinstance(instruction, str)
        and isinstance(output, str)
        and (input_text is None or isinstance(input_text, str))
    ):
        normalized_instruction = _normalize_text(instruction, recipe)
        normalized_input = (
            _normalize_text(input_text, recipe) if input_text is not None else ""
        )
        user_text = (
            normalized_instruction
            if not normalized_input
            else f"{normalized_instruction}\n{normalized_input}"
        )
        messages.extend(
            [
                {"role": "user", "content": _normalize_text(user_text, recipe)},
                {"role": "assistant", "content": _normalize_text(output, recipe)},
            ]
        )
    return messages or None


def _prepend_system_message(
    raw: Any,
    field_mapping: Mapping[str, Any],
    recipe: Mapping[str, Any],
    issues: list[dict[str, str]],
    messages: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Retain a top-level system field before other supported message turns."""

    system = _mapped_value(raw, field_mapping, "system", "system")
    if system is None:
        return messages
    if not isinstance(system, str):
        issues.append(_issue("FIELD_TYPE_INVALID", "system must be text", "error"))
        return messages
    return [
        {"role": "system", "content": _normalize_text(system, recipe)},
        *(messages or []),
    ]


def _unmapped_semantic_field_issues(
    raw: Any, allowed_fields: set[str], field_mapping: Mapping[str, Any]
) -> list[dict[str, str]]:
    """Report semantic row fields not understood by the selected format."""

    if not isinstance(raw, dict):
        return []
    mapped_roots = {
        path.split(".", 1)[0]
        for logical_key, path in field_mapping.items()
        if logical_key in _LEARNED_MAPPING_KEYS
    }
    known_fields = {field.casefold() for field in allowed_fields} | {
        field.casefold() for field in mapped_roots
    }
    issues = []
    for key in raw:
        folded = str(key).casefold()
        if folded in known_fields or folded in _RESERVED_RECORD_FIELDS:
            continue
        issues.append(
            _issue(
                "UNSUPPORTED_SEMANTIC_FIELD",
                f"record field {key!r} is not understood by the selected format",
                "error",
            )
        )
    return issues


def _unmapped_turn_field_issues(
    turns: Any, allowed_fields: set[str]
) -> list[dict[str, str]]:
    """Report extra ShareGPT turn fields without copying them into messages."""

    if not isinstance(turns, list):
        return []
    known_fields = {field.casefold() for field in allowed_fields} | {
        field.casefold() for field in _RESERVED_RECORD_FIELDS
    }
    issues = []
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        for key in turn:
            if str(key).casefold() in known_fields:
                continue
            issues.append(
                _issue(
                    "UNSUPPORTED_SEMANTIC_FIELD",
                    f"ShareGPT turn {index} field {key!r} is not understood",
                    "error",
                )
            )
    return issues


def _detect_row_format(raw: Any, hint: str, field_mapping: Mapping[str, Any]) -> str:
    """Detect Alpaca, prompt-completion, OpenAI, ShareGPT, or ChatML shape."""

    if hint not in SUPPORTED_FORMATS:
        return "unknown"
    if hint != "auto":
        return hint
    if isinstance(raw, str):
        return "chatml" if _CHATML_START in raw else "unknown"
    if not isinstance(raw, dict):
        return "unknown"
    messages_key = str(field_mapping.get("messages", "messages"))
    conversations_key = str(field_mapping.get("conversations", "conversations"))
    if _contains_path(raw, messages_key):
        return "messages"
    if _contains_path(raw, conversations_key):
        return "sharegpt"
    prompt_key = str(field_mapping.get("prompt", "prompt"))
    completion_key = str(field_mapping.get("completion", "completion"))
    if _contains_path(raw, prompt_key) or _contains_path(raw, completion_key):
        return "promptCompletion"
    instruction_key = str(field_mapping.get("instruction", "instruction"))
    output_key = str(field_mapping.get("output", "output"))
    if _contains_path(raw, instruction_key) or _contains_path(raw, output_key):
        return "alpaca"
    chatml_text = _mapped_value(raw, field_mapping, "chatml")
    if chatml_text is None and isinstance(raw, dict):
        chatml_text = raw.get("text", raw.get("content"))
    if isinstance(chatml_text, str) and _CHATML_START in chatml_text:
        return "chatml"
    return "unknown"


def _normalize_messages(
    raw_messages: Any,
    role_mapping: Mapping[str, Any],
    recipe: Mapping[str, Any],
    issues: list[dict[str, str]],
) -> list[dict[str, Any]] | None:
    """Normalize OpenAI/TRL messages while retaining unsupported values."""

    if not isinstance(raw_messages, list) or not raw_messages:
        issues.append(
            _issue(
                "MESSAGE_STRUCTURE_INVALID",
                "messages must be a non-empty array",
                "error",
            )
        )
        return None
    messages: list[dict[str, Any]] = []
    for index, item in enumerate(raw_messages):
        if not isinstance(item, dict):
            issues.append(
                _issue(
                    "MESSAGE_STRUCTURE_INVALID",
                    f"message {index} must be an object",
                    "error",
                )
            )
            continue
        recognized_fields = {"role", "content", "tool_calls", "function_call"}
        for key in item:
            if (
                key in recognized_fields
                or key.casefold() in _RESERVED_RECORD_FIELDS
            ):
                continue
            issues.append(
                _issue(
                    "UNSUPPORTED_SEMANTIC_FIELD",
                    f"message {index} field {key!r} is not understood",
                    "error",
                )
            )
        role = item.get("role")
        content = item.get("content")
        if not isinstance(role, str) or not role.strip():
            issues.append(
                _issue("UNSUPPORTED_ROLE", f"message {index} has no role", "error")
            )
            continue
        hard_issue = _unsupported_raw_role_issue(role)
        if hard_issue is not None:
            issues.append(hard_issue)
        canonical_role = _map_role(role, role_mapping)
        if canonical_role not in SUPPORTED_ROLES:
            issues.append(
                _issue(
                    "UNSUPPORTED_ROLE",
                    f"role {role!r} requires an explicit role mapping",
                    "error",
                )
            )
        if "tool_calls" in item or "function_call" in item or canonical_role == "tool":
            issues.append(
                _issue(
                    "UNSUPPORTED_TOOL_CALL",
                    f"message {index} contains tool-call data",
                    "error",
                )
            )
        if isinstance(content, str):
            content = _normalize_text(content, recipe)
            if any(marker in content for marker in _CHATML_MEDIA_MARKERS):
                issues.append(
                    _issue(
                        "UNSUPPORTED_MULTIMODAL",
                        f"message {index} contains multimodal markers",
                        "error",
                    )
                )
        elif content is None:
            issues.append(
                _issue(
                    "FIELD_MISSING",
                    f"message {index} must contain non-null text content",
                    "error",
                )
            )
        else:
            issues.append(
                _issue(
                    "UNSUPPORTED_MULTIMODAL",
                    f"message {index} content is not plain text",
                    "error",
                )
            )
        messages.append({"role": canonical_role, "content": content})
    if not messages:
        issues.append(
            _issue(
                "MESSAGE_STRUCTURE_INVALID",
                "messages did not contain any usable turns",
                "error",
            )
        )
        return None
    return messages


def _normalize_sharegpt(
    conversations: Any,
    role_mapping: Mapping[str, Any],
    recipe: Mapping[str, Any],
    issues: list[dict[str, str]],
) -> list[dict[str, Any]] | None:
    """Map ShareGPT from/value turns without flattening history."""

    if not isinstance(conversations, list) or not conversations:
        issues.append(
            _issue(
                "MESSAGE_STRUCTURE_INVALID",
                "conversations must be a non-empty array",
                "error",
            )
        )
        return None
    messages: list[dict[str, Any]] = []
    for index, turn in enumerate(conversations):
        if not isinstance(turn, dict):
            issues.append(
                _issue(
                    "MESSAGE_STRUCTURE_INVALID",
                    f"conversation {index} must be an object",
                    "error",
                )
            )
            continue
        role = turn.get("from")
        content = turn.get("value")
        if not isinstance(role, str):
            issues.append(
                _issue(
                    "UNSUPPORTED_ROLE", f"conversation {index} has no speaker", "error"
                )
            )
            continue
        hard_issue = _unsupported_raw_role_issue(role)
        if hard_issue is not None:
            issues.append(hard_issue)
        canonical_role = _map_role(role, role_mapping)
        if canonical_role not in SUPPORTED_ROLES:
            issues.append(
                _issue(
                    "UNSUPPORTED_ROLE",
                    f"role {role!r} requires an explicit role mapping",
                    "error",
                )
            )
        if not isinstance(content, str):
            issues.append(
                _issue(
                    "UNSUPPORTED_MULTIMODAL",
                    f"conversation {index} content is not plain text",
                    "error",
                )
            )
            normalized_content = content
        else:
            normalized_content = _normalize_text(content, recipe)
        if "tools" in turn or "tool_calls" in turn:
            issues.append(
                _issue(
                    "UNSUPPORTED_TOOL_CALL",
                    f"conversation {index} contains tool data",
                    "error",
                )
            )
        messages.append({"role": canonical_role, "content": normalized_content})
    return messages or None


def _parse_chatml(
    text: str,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, str]]]:
    """Parse common ChatML turn markers and retain unsafe tool/header cases."""

    issues: list[dict[str, str]] = []
    messages: list[dict[str, Any]] = []
    if not text.strip():
        return None, [
            _issue("MESSAGE_STRUCTURE_INVALID", "ChatML record is empty", "error")
        ]
    pieces = text.split(_CHATML_START)
    if pieces[0].strip():
        return None, [
            _issue(
                "CHATML_INVALID",
                "text before the first ChatML turn was retained",
                "error",
            )
        ]
    for index, piece in enumerate(pieces[1:]):
        separator_position = piece.find(_CHATML_SEPARATOR)
        newline_position = piece.find("\n")
        if separator_position >= 0 and (
            newline_position < 0 or separator_position < newline_position
        ):
            header = piece[:separator_position]
            remainder = piece[separator_position + len(_CHATML_SEPARATOR) :]
        elif newline_position >= 0:
            header = piece[:newline_position]
            remainder = piece[newline_position + 1 :]
        else:
            issues.append(
                _issue(
                    "CHATML_INVALID",
                    f"ChatML turn {index} has no message separator or header newline",
                    "error",
                )
            )
            continue
        header = header.strip()
        role = header.split(maxsplit=1)[0] if header else ""
        if not role:
            issues.append(
                _issue("UNSUPPORTED_ROLE", f"ChatML turn {index} has no role", "error")
            )
            continue
        end_positions = [
            (remainder.find(token), token)
            for token in _CHATML_ENDINGS
            if token in remainder
        ]
        if not end_positions:
            body = remainder
            issues.append(
                _issue(
                    "CHATML_UNTERMINATED",
                    f"ChatML turn {index} has no ending marker",
                    "error",
                )
            )
        else:
            end_position, ending = min(end_positions, key=lambda item: item[0])
            body = remainder[:end_position]
            if ending == "<|ghissue|>":
                issues.append(
                    _issue(
                        "UNSUPPORTED_TOOL_CALL",
                        f"ChatML turn {index} is directed to a tool",
                        "error",
                    )
                )
        if "to=" in header or "  code" in f" {header}":
            issues.append(
                _issue(
                    "UNSUPPORTED_TOOL_CALL",
                    f"ChatML turn {index} has a tool or constrained-format header",
                    "error",
                )
            )
        elif len(header.split()) > 1:
            issues.append(
                _issue(
                    "CHATML_METADATA_UNSUPPORTED",
                    f"ChatML turn {index} contains header metadata",
                    "warning",
                )
            )
        if any(marker in body for marker in _CHATML_MEDIA_MARKERS):
            issues.append(
                _issue(
                    "UNSUPPORTED_MULTIMODAL",
                    f"ChatML turn {index} contains multimodal markers",
                    "error",
                )
            )
        messages.append({"role": role, "content": body})
    return (messages or None), issues


def _validate_messages(
    messages: list[dict[str, Any]],
    recipe: Mapping[str, Any],
    issues: list[dict[str, str]],
) -> None:
    """Flag structural, ordering, empty, and configured length problems."""

    if not messages:
        return
    if (
        len(messages) < 2
        or messages[-1].get("role") != "assistant"
        or not any(message.get("role") == "assistant" for message in messages)
    ):
        issues.append(
            _issue(
                "INCOMPLETE_PROMPT_COMPLETION",
                "record has no complete assistant response",
                "error",
            )
        )
    previous_role: str | None = None
    seen_non_system = False
    char_count = 0
    assistant_char_count = 0
    first_turn = next(
        (
            message.get("role")
            for message in messages
            if message.get("role") != "system"
        ),
        None,
    )
    if first_turn is not None and first_turn != "user":
        issues.append(
            _issue(
                "ROLE_ORDER_INVALID",
                "conversation must begin with a user turn",
                "warning",
            )
        )
    for position, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content")
        if role == "system" and seen_non_system:
            issues.append(
                _issue(
                    "ROLE_ORDER_INVALID",
                    f"system message appears after turn {position - 1}",
                    "warning",
                )
            )
        if role != "system":
            seen_non_system = True
        if role in {"user", "assistant"} and role == previous_role:
            issues.append(
                _issue(
                    "ROLE_ORDER_INVALID",
                    f"adjacent {role} messages at turn {position}",
                    "warning",
                )
            )
        if isinstance(content, str):
            char_count += len(content)
            if role == "assistant":
                assistant_char_count += len(content)
            if not content.strip():
                code = "EMPTY_RESPONSE" if role == "assistant" else "EMPTY_CONTENT"
                issues.append(
                    _issue(code, f"{role} message {position} is empty", "error")
                )
        previous_role = str(role)
    max_characters = int(recipe["maxCharacters"])
    min_characters = int(recipe["minCharacters"])
    if char_count > max_characters:
        issues.append(
            _issue(
                "SAMPLE_TOO_LONG",
                f"sample has {char_count} characters; maximum is {max_characters}",
                "warning",
            )
        )
    if char_count < min_characters or assistant_char_count < min_characters:
        issues.append(
            _issue(
                "SAMPLE_TOO_SHORT",
                f"sample has {char_count} characters and its assistant response has "
                f"{assistant_char_count}; minimum is {min_characters}",
                "warning",
            )
        )


def _dedup_diagnostics(
    index: sqlite3.Connection,
    envelope: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, str] | None:
    """Use a disk-backed exact and prompt-neighbor index with bounded queries."""

    content = _canonical_json(messages).decode("utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    prior = index.execute(
        "SELECT record_id FROM exact_records WHERE digest = ? LIMIT 1", (digest,)
    ).fetchone()
    if prior is not None:
        return _issue("DUPLICATE_EXACT", f"exact duplicate of {prior[0]}", "warning")
    user_text = "\n".join(
        message["content"]
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    )
    assistant_text = "\n".join(
        message["content"]
        for message in messages
        if message.get("role") == "assistant"
        and isinstance(message.get("content"), str)
    )
    prompt_digest = hashlib.sha256(user_text.encode("utf-8")).hexdigest()
    conflict = index.execute(
        "SELECT record_id FROM prompts WHERE prompt_digest = ? AND answer_digest != ? LIMIT 1",
        (prompt_digest, hashlib.sha256(assistant_text.encode("utf-8")).hexdigest()),
    ).fetchone()
    if conflict is not None:
        issue = _issue(
            "CONTENT_CONFLICT",
            f"same prompt has a different answer from {conflict[0]}",
            "warning",
        )
    else:
        issue = None
    tokens = sorted({token.casefold() for token in _WORD.findall(user_text)})[:128]
    if issue is None and len(tokens) >= 4:
        placeholders = ",".join("?" for _ in tokens)
        rare_tokens = [
            token
            for token, _ in index.execute(
                f"SELECT token, occurrence_count FROM token_counts WHERE token IN ({placeholders}) AND occurrence_count <= 64 ORDER BY occurrence_count, token LIMIT 16",
                tokens,
            ).fetchall()
        ]
        if not rare_tokens:
            rare_tokens = [token for token in tokens if len(token) >= 8][:16]
        rare_placeholders = ",".join("?" for _ in rare_tokens)
    else:
        rare_tokens = []
        rare_placeholders = ""
    if issue is None and rare_tokens:
        rows = index.execute(
            f"SELECT record_id, COUNT(*) AS overlap FROM prompt_tokens WHERE token IN ({rare_placeholders}) GROUP BY record_id ORDER BY overlap DESC LIMIT 64",
            rare_tokens,
        ).fetchall()
        current = set(tokens)
        for prior_id, _ in rows:
            prior_row = index.execute(
                "SELECT prompt_tokens FROM prompts WHERE record_id = ? LIMIT 1",
                (prior_id,),
            ).fetchone()
            if prior_row is None:
                continue
            prior_tokens = set(json.loads(prior_row[0]))
            union = len(current | prior_tokens)
            similarity = len(current & prior_tokens) / union if union else 0.0
            if similarity >= NEAR_DUPLICATE_THRESHOLD and current != prior_tokens:
                issue = _issue(
                    "SEMANTIC_DUPLICATE_CANDIDATE",
                    f"prompt is very similar to {prior_id}",
                    "warning",
                )
                break
    index.execute(
        "INSERT OR IGNORE INTO exact_records(digest, record_id) VALUES (?, ?)",
        (digest, str(envelope["id"])),
    )
    index.execute(
        "INSERT INTO prompts(record_id, prompt_digest, answer_digest, prompt_tokens) VALUES (?, ?, ?, ?)",
        (
            str(envelope["id"]),
            prompt_digest,
            hashlib.sha256(assistant_text.encode("utf-8")).hexdigest(),
            json.dumps(tokens),
        ),
    )
    index.executemany(
        "INSERT INTO prompt_tokens(record_id, token) VALUES (?, ?)",
        [(str(envelope["id"]), token) for token in tokens],
    )
    index.executemany(
        "INSERT INTO token_counts(token, occurrence_count) VALUES (?, 1) ON CONFLICT(token) DO UPDATE SET occurrence_count = occurrence_count + 1",
        [(token,) for token in tokens],
    )
    return issue


def _can_deduplicate(
    envelope: Mapping[str, Any], messages: list[dict[str, Any]]
) -> bool:
    """Keep prohibited or structurally invalid content out of candidate indexes."""

    policy = envelope.get("policy")
    if (
        not isinstance(policy, dict)
        or policy.get("allowTraining") is not True
        or "model_training" not in policy.get("allowedUsePurposes", [])
    ):
        return False
    if any(issue.get("severity") == "error" for issue in envelope.get("issues", [])):
        return False
    return all(
        message.get("role") in SUPPORTED_ROLES
        and isinstance(message.get("content"), str)
        for message in messages
    )


def _unsupported_structure_issues(raw: Any) -> list[dict[str, str]]:
    """Report nested tool and media structures without traversing unbounded data."""

    tool_keys = {"tool_call", "tool_calls", "function_call", "tools"}
    media_keys = {
        "image",
        "images",
        "image_url",
        "audio",
        "video",
        "media",
        "modalities",
    }
    issues: list[dict[str, str]] = []
    pending: list[tuple[Any, str, int]] = [(raw, "$", 0)]
    visited = 0
    scan_limited = False
    while pending and visited < 10_000:
        value, location, depth = pending.pop()
        visited += 1
        if depth >= 64:
            scan_limited = scan_limited or bool(value)
            continue
        if isinstance(value, dict):
            for key, child in value.items():
                key_name = str(key).casefold()
                field_path = f"{location}.{key}"
                if key_name in tool_keys:
                    issues.append(
                        _issue(
                            "UNSUPPORTED_TOOL_CALL",
                            f"tool data retained at {field_path}",
                            "error",
                        )
                    )
                if key_name in media_keys:
                    issues.append(
                        _issue(
                            "UNSUPPORTED_MULTIMODAL",
                            f"multimodal data retained at {field_path}",
                            "error",
                        )
                    )
                if isinstance(child, dict | list):
                    pending.append((child, field_path, depth + 1))
        elif isinstance(value, list):
            if len(value) > 1_000:
                scan_limited = True
            pending.extend(
                (child, f"{location}[{position}]", depth + 1)
                for position, child in enumerate(value[:1_000])
                if isinstance(child, dict | list)
            )
    if pending or scan_limited:
        issues.append(
            _issue(
                "UNSUPPORTED_STRUCTURE_SCAN_LIMIT",
                "nested structure exceeded diagnostic scan limit; raw record retained",
                "warning",
            )
        )
    return _unique_issues(issues)


def _create_index(index: sqlite3.Connection) -> None:
    """Create small job-local indexes; no dataset state leaves the plugin job."""

    index.execute(
        "CREATE TABLE IF NOT EXISTS exact_records (digest TEXT PRIMARY KEY, record_id TEXT NOT NULL)"
    )
    index.execute(
        "CREATE TABLE IF NOT EXISTS prompts (record_id TEXT PRIMARY KEY, prompt_digest TEXT NOT NULL, answer_digest TEXT NOT NULL, prompt_tokens TEXT NOT NULL)"
    )
    index.execute(
        "CREATE TABLE IF NOT EXISTS prompt_tokens (record_id TEXT NOT NULL, token TEXT NOT NULL, PRIMARY KEY(record_id, token))"
    )
    index.execute(
        "CREATE INDEX IF NOT EXISTS idx_prompt_tokens_token ON prompt_tokens(token)"
    )
    index.execute(
        "CREATE TABLE IF NOT EXISTS token_counts (token TEXT PRIMARY KEY, occurrence_count INTEGER NOT NULL)"
    )


def _rebuild_index(index: sqlite3.Connection, output: Path, output_bytes: int) -> None:
    """Reconstruct dedup state from exactly the checkpointed output prefix."""

    index.execute("DELETE FROM exact_records")
    index.execute("DELETE FROM prompt_tokens")
    index.execute("DELETE FROM prompts")
    index.execute("DELETE FROM token_counts")
    consumed = 0
    with output.open("rb") as stream:
        while consumed < output_bytes:
            line = stream.readline()
            if not line:
                raise ValueError(
                    "checkpoint output is shorter than its committed byte count"
                )
            consumed += len(line)
            document = json.loads(line)
            messages = document.get("normalized", {}).get("messages")
            if messages and _can_deduplicate(document, messages):
                _dedup_diagnostics(index, document, messages)


def _record_counts(
    state: dict[str, Any], source_index: int, envelope: dict[str, Any]
) -> None:
    """Update reconciled disposition counts and overlapping diagnostics."""

    disposition = envelope["disposition"]
    bucket = (
        "eligible"
        if disposition == "eligible"
        else "pendingReview"
        if disposition == "review"
        else "excluded"
    )
    state["counts"]["total"] += 1
    state["counts"][bucket] += 1
    if envelope["detectedFormat"] != "unknown":
        state["counts"]["recognized"] += 1
    codes = {issue["code"] for issue in envelope["issues"]}
    if codes & {
        "INVALID_JSON",
        "INVALID_ENCODING",
        "FORMAT_UNRECOGNIZED",
        "MESSAGE_STRUCTURE_INVALID",
        "FIELD_MISSING",
        "FIELD_TYPE_INVALID",
    }:
        state["counts"]["formatErrors"] += 1
    if "DUPLICATE_EXACT" in codes:
        state["counts"]["duplicateCandidates"] += 1
    source = state["sources"][source_index]
    source["counts"][bucket] += 1
    source["counts"]["total"] += 1
    if envelope["detectedFormat"] != "unknown":
        source["counts"]["recognized"] += 1
    if codes & {
        "INVALID_JSON",
        "INVALID_ENCODING",
        "FORMAT_UNRECOGNIZED",
        "MESSAGE_STRUCTURE_INVALID",
        "FIELD_MISSING",
        "FIELD_TYPE_INVALID",
    }:
        source["counts"]["formatErrors"] += 1
    if "DUPLICATE_EXACT" in codes:
        source["counts"]["duplicateCandidates"] += 1
    for code in codes:
        state["issue_counts"][code] = state["issue_counts"].get(code, 0) + 1
        source["diagnostic_counts"][code] = source["diagnostic_counts"].get(code, 0) + 1
    for issue in envelope["issues"]:
        if issue["code"].startswith("UNSUPPORTED_"):
            source["unsupported_counts"][issue["code"]] = (
                source["unsupported_counts"].get(issue["code"], 0) + 1
            )
    source["status"] = (
        "SUCCEEDED_WITH_WARNINGS" if source["diagnostic_counts"] else "SUCCEEDED"
    )


def _initial_state(
    batch_digest: str,
    recipe_digest: str,
    output: Path,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Construct a serializable checkpoint with source-level reconciliations."""

    zero_counts = {
        "total": 0,
        "recognized": 0,
        "formatErrors": 0,
        "duplicateCandidates": 0,
        "pendingReview": 0,
        "excluded": 0,
        "eligible": 0,
    }
    return {
        "schemaVersion": 1,
        "batch_digest": batch_digest,
        "recipe_digest": recipe_digest,
        "output_path": str(output),
        "output_bytes": 0,
        "output_chain_digest": _empty_output_chain(),
        "source_index": 0,
        "source_ordinal": 0,
        "source_offset": 0,
        "source_line": 1,
        "complete": False,
        "counts": dict(zero_counts),
        "issue_counts": {},
        "total_processed": 0,
        "resumed_records": 0,
        "sources": [
            {
                "source_revision_id": source["source_revision_id"],
                "counts": dict(zero_counts),
                "diagnostic_counts": {},
                "unsupported_counts": {},
                "status": "PENDING",
            }
            for source in sources
        ],
    }


def _load_or_reset_checkpoint(
    checkpoint: Path, output: Path, initial: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Reuse matching complete or partial work, otherwise start cleanly."""

    try:
        current = json.loads(checkpoint.read_text(encoding="utf-8"))
        matches = (
            current.get("schemaVersion") == 1
            and current.get("batch_digest") == initial["batch_digest"]
            and current.get("recipe_digest") == initial["recipe_digest"]
            and current.get("output_path") == str(output)
            and output.is_file()
            and output.stat().st_size >= int(current.get("output_bytes", -1))
            and _verify_output_chain(
                output,
                int(current.get("output_bytes", -1)),
                current.get("output_chain_digest"),
            )
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        matches = False
        current = initial
    if matches:
        current["resumed_records"] = int(current.get("total_processed", 0))
        return current, True
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"")
    _save_checkpoint(checkpoint, initial)
    return initial, False


def _save_checkpoint(checkpoint: Path, state: dict[str, Any]) -> None:
    """Atomically replace the job checkpoint after its output prefix is durable."""

    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(state)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{checkpoint.name}.", dir=checkpoint.parent
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, checkpoint)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _advance_output_chain(previous: str, encoded: bytes) -> str:
    """Hash one output record into a resumable integrity chain."""

    digest = hashlib.sha256()
    digest.update(previous.encode("ascii"))
    digest.update(b"\0")
    digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def _empty_output_chain() -> str:
    """Return the seed digest for a checkpoint with no output records."""

    return f"sha256:{hashlib.sha256(b'').hexdigest()}"


def _verify_output_chain(path: Path, byte_count: int, expected: Any) -> bool:
    """Verify that the checkpointed output prefix has not been altered."""

    if not isinstance(expected, str) or byte_count < 0:
        return False
    chain = _empty_output_chain()
    consumed = 0
    try:
        with path.open("rb") as stream:
            while consumed < byte_count:
                encoded = stream.readline()
                if not encoded:
                    return False
                consumed += len(encoded)
                if consumed > byte_count:
                    return False
                chain = _advance_output_chain(chain, encoded)
    except OSError:
        return False
    return consumed == byte_count and chain == expected


def _validate_sources(sources: Any) -> list[dict[str, Any]]:
    """Validate staged source paths and stable Catalyst lineage."""

    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise ValueError(f"sources must contain 1 to {MAX_SOURCES} items")
    normalized: list[dict[str, Any]] = []
    for index, value in enumerate(sources):
        if not isinstance(value, dict):
            raise TypeError(f"sources[{index}] must be an object")
        source_path = _absolute_path(
            value.get("source_path"), f"sources[{index}].source_path"
        )
        if not source_path.is_file() or source_path.is_symlink():
            raise ValueError(f"sources[{index}].source_path must be a regular file")
        if source_path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError(f"sources[{index}] exceeds 64 MiB")
        policy = value.get("policy")
        if policy is not None and not isinstance(policy, dict):
            raise TypeError(f"sources[{index}].policy must be an object")
        source = {
            "source_path": str(source_path),
            "source_revision_id": _required_text(
                value.get("source_revision_id"), f"sources[{index}].source_revision_id"
            ),
            "source_family_id": _required_text(
                value.get("source_family_id"), f"sources[{index}].source_family_id"
            ),
            "filename": _required_text(
                value.get("filename", source_path.name), f"sources[{index}].filename"
            ),
            "policy": policy,
        }
        for source_key, allowed in (("format", SUPPORTED_FORMATS),):
            if value.get(source_key) is not None:
                source[source_key] = _enum(
                    value[source_key], f"sources[{index}].{source_key}", allowed
                )
        for key in ("field_mapping", "role_mapping"):
            if value.get(key) is not None:
                mapping = _string_mapping(value[key], f"sources[{index}].{key}")
                if key == "field_mapping":
                    _reject_reserved_content_paths(
                        mapping, f"sources[{index}].{key}"
                    )
                source[key] = mapping
        normalized.append(source)
    return normalized


def _validate_recipe(recipe: Any) -> dict[str, Any]:
    """Validate immutable recipe settings and fill deterministic defaults."""

    if not isinstance(recipe, dict):
        raise TypeError("recipe must be an object")
    result = dict(recipe)
    _required_text(result.get("id"), "recipe.id")
    _required_text(result.get("version"), "recipe.version")
    result["format"] = _enum(
        result.get("format", "auto"), "recipe.format", SUPPORTED_FORMATS
    )
    result["fieldMapping"] = _string_mapping(
        result.get("fieldMapping", {}), "recipe.fieldMapping"
    )
    _reject_reserved_content_paths(result["fieldMapping"], "recipe.fieldMapping")
    result["roleMapping"] = _string_mapping(
        result.get("roleMapping", {}), "recipe.roleMapping"
    )
    result["maxCharacters"] = _bounded_int(
        result.get("maxCharacters", 100_000), "recipe.maxCharacters", 1, 10_000_000
    )
    result["minCharacters"] = _bounded_int(
        result.get("minCharacters", 2), "recipe.minCharacters", 0, 10_000_000
    )
    result["unicodeNormalization"] = _enum(
        result.get("unicodeNormalization", "NFC"),
        "recipe.unicodeNormalization",
        {"NFC", "NFD", "NFKC", "NFKD", "none"},
    )
    return result


def _normalize_text(value: str, recipe: Mapping[str, Any]) -> str:
    """Normalize line endings and Unicode without trimming meaningful content."""

    text = value.replace("\r\n", "\n").replace("\r", "\n")
    form = str(recipe.get("unicodeNormalization", "NFC"))
    return text if form == "none" else unicodedata.normalize(form, text)


def _training_family_id(
    raw: Any, fields: Mapping[str, Any], default: str
) -> str:
    """Use an explicit record family for cross-file split isolation when present."""

    value = _mapped_value(raw, fields, "sourceFamilyId")
    if value is None and isinstance(raw, dict):
        value = raw.get("source_family_id", raw.get("sourceFamilyId"))
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return default


def _mapped_value(
    raw: Any, mapping: Mapping[str, Any], logical_name: str, default: str | None = None
) -> Any:
    """Read a mapped dotted path or the format's standard field name."""

    path = mapping.get(logical_name, default if default is not None else logical_name)
    if not isinstance(path, str) or not path:
        return None
    return _nested(raw, path)


def _nested(value: Any, path: str) -> Any:
    """Resolve dot-separated object paths without mutating source objects."""

    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _contains_path(value: Any, path: str) -> bool:
    """Distinguish a present null field from an absent format field."""

    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False
    return True


def _map_role(role: str, mapping: Mapping[str, Any]) -> str:
    """Apply explicit aliases, then only the established ShareGPT aliases."""

    if role in mapping and isinstance(mapping[role], str):
        return str(mapping[role])
    return {"human": "user", "gpt": "assistant"}.get(role, role)


def _unsupported_raw_role_issue(role: str) -> dict[str, str] | None:
    """Keep tool and observation roles blocked even if an alias maps them."""

    source_role = role.casefold()
    if source_role in {"tool", "function"}:
        return _issue(
            "UNSUPPORTED_TOOL_CALL",
            f"source role {role!r} cannot be mapped to a conversational role",
            "error",
        )
    if source_role == "observation":
        return _issue(
            "UNSUPPORTED_ROLE",
            "observation role requires raw-record correction or exclusion",
            "error",
        )
    return None


def _sample_id(
    raw: Any, fields: Mapping[str, Any], source: Mapping[str, Any], ordinal: int
) -> str:
    """Preserve an external sample ID or derive a stable source-local ID."""

    value = _mapped_value(raw, fields, "sampleId")
    if value is None and isinstance(raw, dict):
        value = raw.get("sample_id", raw.get("id"))
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return f"{source['source_revision_id']}:{ordinal}"


def _conversation_id(raw: Any, fields: Mapping[str, Any], ordinal: int) -> str | None:
    """Retain a supplied conversation lineage identifier when present."""

    value = _mapped_value(raw, fields, "conversationId")
    if value is None and isinstance(raw, dict):
        value = raw.get("conversation_id")
    return (
        str(value)
        if isinstance(value, str | int) and not isinstance(value, bool)
        else None
    )


def _remap_lineage_issues(
    raw: Any, fields: Mapping[str, Any], envelope: Mapping[str, Any]
) -> list[dict[str, str]]:
    """Report corrected raw lineage aliases without rewriting envelope lineage."""

    issues: list[dict[str, str]] = []
    aliases = {
        "sourceRevisionId": ("sourceRevisionId", "source_revision_id"),
        "sourceFamilyId": ("sourceFamilyId", "source_family_id"),
        "conversationId": ("conversationId", "conversation_id"),
    }
    for field, names in aliases.items():
        value = _mapped_value(raw, fields, field)
        if value is None and isinstance(raw, dict):
            value = next((raw[name] for name in names if name in raw), None)
        if value is None:
            continue
        authoritative = envelope.get(field)
        if str(value) != (str(authoritative) if authoritative is not None else ""):
            issues.append(
                _issue(
                    "LINEAGE_METADATA_CONFLICT",
                    f"raw {field} conflicts with immutable source lineage",
                    "error",
                )
            )

    sample_path = fields.get("sampleId", "sampleId")
    raw_sample_id = _nested(raw, sample_path) if isinstance(sample_path, str) else None
    if raw_sample_id is None and isinstance(raw, dict):
        raw_sample_id = raw.get("sample_id", raw.get("id"))
    if raw_sample_id is not None and str(raw_sample_id) != envelope.get("sampleId"):
        issues.append(
            _issue(
                "SAMPLE_ID_METADATA_IGNORED",
                "corrected raw sample ID cannot replace the immutable sample ID",
                "warning",
            )
        )
    return issues


def _normalize_policy(value: Any) -> dict[str, Any]:
    """Fail closed when explicit training permission is absent."""

    if not isinstance(value, dict):
        return {
            "allowTraining": False,
            "allowedUsePurposes": [],
            "reason": "missing_policy",
        }
    purposes = value.get("allowedUsePurposes", [])
    if not isinstance(purposes, list) or any(
        not isinstance(item, str) for item in purposes
    ):
        purposes = []
    return {
        **value,
        "allowTraining": value.get("allowTraining") is True,
        "allowedUsePurposes": purposes,
    }


def _effective_policy(source_policy: Any, record_policy: Any) -> dict[str, Any]:
    """Apply row restrictions without granting rights absent at source level.

    中文：记录级禁用策略优先；来源策略不能为记录补授训练权限。
    """

    source = _normalize_policy(source_policy)
    effective = dict(source)
    record = (
        _normalize_policy(record_policy) if isinstance(record_policy, dict) else None
    )
    source_allows = (
        source["allowTraining"] and "model_training" in source["allowedUsePurposes"]
    )
    if record is not None:
        effective["recordPolicy"] = record
        source_allows = (
            source_allows
            and record["allowTraining"]
            and "model_training" in record["allowedUsePurposes"]
        )
        effective["allowedUsePurposes"] = sorted(
            set(source["allowedUsePurposes"]) & set(record["allowedUsePurposes"])
        )
    effective["allowTraining"] = source_allows
    return effective


def _row_policy(
    source_policy: Any, raw: Any, fields: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Apply row policy restrictions and fail closed on malformed declarations."""

    policy_path = fields.get("policy", "policy")
    if not isinstance(policy_path, str) or not policy_path:
        return _effective_policy(source_policy, None), []
    if not _contains_path(raw, policy_path):
        return _effective_policy(source_policy, None), []
    record_policy = _nested(raw, policy_path)
    if not isinstance(record_policy, dict):
        return (
            _effective_policy(
                source_policy, {"allowTraining": False, "allowedUsePurposes": []}
            ),
            [_issue("POLICY_INVALID", "record policy must be an object", "error")],
        )
    return _effective_policy(source_policy, record_policy), []


def _disposition(issues: list[dict[str, str]], policy: Any, messages: Any) -> str:
    """Assign only disposition; Catalyst still owns review and approval state."""

    if (
        not isinstance(policy, dict)
        or policy.get("allowTraining") is not True
        or "model_training" not in policy.get("allowedUsePurposes", [])
    ):
        return "excluded"
    if any(issue["code"] == "DUPLICATE_EXACT" for issue in issues):
        return "excluded"
    if issues:
        return "review"
    if not isinstance(messages, list) or not messages:
        return "excluded"
    return "eligible"


def _issue(code: str, message: str, severity: str) -> dict[str, str]:
    """Build a stable machine-filterable diagnostic item."""

    return {"code": code, "message": message, "severity": severity}


def _unique_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    """Retain the first occurrence of each identical diagnostic."""

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (issue["code"], issue["message"], issue["severity"])
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def _canonical_json(value: Any) -> bytes:
    """Serialize stable compact UTF-8 JSON used for digests and records."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest_json(value: Any) -> str:
    """Return a stable `sha256:` digest over a JSON-native value."""

    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _sha256_file(path: Path) -> str:
    """Hash a file in bounded chunks."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(128 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _absolute_path(value: Any, field: str) -> Path:
    """Require a nonempty absolute staging path."""

    if not isinstance(value, str | Path) or not str(value).strip():
        raise TypeError(f"{field} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute staged path")
    return path


def _required_text(value: Any, field: str) -> str:
    """Validate an identifier without coercing arbitrary values."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    """Validate an exact case-sensitive enum value."""

    text = _required_text(value, field)
    if text not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}")
    return text


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    """Validate a bounded integer while excluding booleans."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _string_mapping(value: Any, field: str) -> dict[str, str]:
    """Validate field or role mappings as string-to-string objects."""

    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise TypeError(f"{field} must be an object with string keys and values")
    return dict(value)


def _reject_reserved_content_paths(mapping: Mapping[str, str], field: str) -> None:
    """Prevent internal management fields from becoming learned message text."""

    for logical_key, path in mapping.items():
        if logical_key not in _LEARNED_MAPPING_KEYS:
            continue
        reserved = next(
            (
                component
                for component in path.split(".")
                if component.casefold() in _RESERVED_RECORD_FIELDS
            ),
            None,
        )
        if reserved is not None:
            raise ValueError(
                f"{field}.{logical_key} cannot map from reserved management field "
                f"{reserved!r}"
            )
