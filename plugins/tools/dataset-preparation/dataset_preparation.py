"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 dataset_preparation.py                                         │
│  Module: dataset_preparation                                       │
│  Role: Canonical dataset.preparation.v1 implementation.            │
│                                                                     │
│  模块职责：数据导入、映射、规范化、去重、切分与格式转换的唯一实现。          │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import datetime
import decimal
import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

CAPABILITY_ID = "dataset.preparation.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_EXCERPT_LENGTH = 200


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """Typed response consumed by DirectPluginRuntime."""

    value: bytes
    type_url: str


@dataclass(frozen=True, slots=True)
class PreparedSample:
    """One unique normalized sample."""

    index: int
    group_key: str
    content: dict[str, Any]
    source_row_indexes: list[int]


class DatasetPreparationPlugin:
    """Run stateless deterministic dataset preparation over staged paths."""

    plugin_id = "cyrene.tools.dataset-preparation"
    version = "0.1.0"
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
        """Dispatch one typed preparation request."""

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action not in {"inspect", "prepare", "transform"}:
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        expected_type_url = f"{TYPE_PREFIX}.{action}.request"
        if request_type_url != expected_type_url:
            return (
                False,
                f"INVALID_REQUEST: request_type_url must be {expected_type_url}",
            )
        if stream_results:
            return False, "METHOD_NOT_SUPPORTED: preparation methods are not streaming"
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        try:
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict):
                raise TypeError("request must be an object")
            if action == "inspect":
                result = self.inspect(
                    _path(request.get("source_path"), "source_path"),
                    _path(request.get("result_path"), "result_path"),
                )
            elif action == "prepare":
                result = self.prepare(
                    source_path=request.get("source_path"),
                    source_format=request.get("source_format"),
                    mapping=request.get("mapping"),
                    normalization=request.get("normalization"),
                    result_path=request.get("result_path"),
                    split=request.get("split"),
                    output_dir=request.get("output_dir"),
                )
            else:
                result = self.transform(
                    _path(request.get("source_path"), "source_path"),
                    _path(request.get("destination_path"), "destination_path"),
                )
        except (
            TypeError,
            ValueError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            duckdb.Error,
        ) as exc:
            return False, f"INVALID_INPUT: {exc}"
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        return True, TypedPayload(
            value=_canonical_json(result),
            type_url=f"{TYPE_PREFIX}.{action}.response",
        )

    def inspect(self, source_path: Path, result_path: Path) -> dict[str, Any]:
        """Parse a staged source and persist its generic row projection."""

        _validate_source(source_path)
        source_format = detect_format(source_path.read_bytes())
        rows = parse_rows(source_path, source_format)
        result = {"format": source_format, "rows": rows}
        receipt = _write_json_result(result_path, result)
        return {
            **receipt,
            "format": source_format,
            "row_count": len(rows),
            "detected_fields": sorted({key for row in rows for key in row})[:500],
        }

    def prepare(
        self,
        source_path: str | Path,
        source_format: str,
        mapping: dict[str, Any],
        normalization: dict[str, Any],
        result_path: str | Path,
        split: dict[str, Any] | None = None,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Normalize and split rows, persisting bounded-path result artifacts."""

        source_path = _path(source_path, "source_path")
        result_path = _path(result_path, "result_path")
        _validate_source(source_path)
        source_format = _enum_text(
            source_format, "source_format", {"JSONL", "JSON", "TEXT"}
        )
        mapping = _mapping(mapping, "mapping")
        normalization = _mapping(normalization, "normalization")
        if split is not None:
            split = _mapping(split, "split")
        rows = parse_rows(source_path, source_format)
        samples, errors, duplicates = run_pipeline(rows, mapping, normalization)
        assignment, split_stats = assign_split(samples, split)
        result = {
            "samples": [_sample_document(sample) for sample in samples],
            "errors": errors,
            "duplicates": duplicates,
            "assignment": {str(key): value for key, value in assignment.items()},
            "split_stats": split_stats,
        }
        receipt = _write_json_result(result_path, result)
        files: dict[str, dict[str, Any]] = {}
        if output_dir is not None:
            if split is None:
                raise ValueError("split is required when output_dir is provided")
            output_dir = _path(output_dir, "output_dir")
            output_dir.mkdir(parents=True, exist_ok=True)
            files = _write_exports(output_dir, samples, errors, assignment)
        return {
            **receipt,
            "row_count": len(rows),
            "unique_samples": len(samples),
            "error_samples": len(errors),
            "duplicate_samples": len(duplicates),
            "group_count": len({sample.group_key for sample in samples}),
            "files": files,
        }

    def transform(self, source_path: Path, destination_path: Path) -> dict[str, Any]:
        """Convert newline-delimited JSON to compressed Parquet."""

        _validate_source(source_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect()
        try:
            relation = connection.read_json(
                str(source_path), format="newline_delimited"
            )
            row = relation.aggregate("count(*) AS row_count").fetchone()
            relation.write_parquet(str(destination_path), compression="zstd")
            return {
                "row_count": int(row[0]) if row is not None else 0,
                "schema_fields": list(relation.columns),
                "output_digest": f"sha256:{_sha256_file(destination_path)}",
                "output_size": destination_path.stat().st_size,
            }
        finally:
            connection.close()


def detect_format(data: bytes) -> str:
    """Detect supported text formats from content rather than a filename."""

    if not data.strip():
        raise ValueError("source is empty")
    for magic, label in (
        (b"%PDF", "PDF"),
        (b"\xd0\xcf\x11\xe0", "Legacy Office"),
        (b"PK\x03\x04", "OOXML"),
    ):
        if data.startswith(magic):
            raise ValueError(f"{label} is not supported by this implementation")
    text = data.decode("utf-8")
    stripped = text.lstrip("﻿").lstrip()
    if stripped.startswith("["):
        return "JSON"
    if stripped.startswith("{"):
        return "JSONL"
    return "TEXT"


def _json_native(value: Any) -> Any:
    """Project one DuckDB cell onto a JSON-native value. | 归一为 JSON 原生值。

    DuckDB infers JSON scalars into rich SQL types, so an ISO timestamp becomes
    a ``datetime``. The capability wire type is JSON, therefore every cell is
    projected back onto values ``json`` can serialize without a fallback.
    """

    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, dict):
        return {str(key): _json_native(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_native(item) for item in value]
    return str(value)


def parse_rows(source: Path, source_format: str) -> list[dict[str, Any]]:
    """Parse JSON/JSONL with DuckDB or non-empty plain-text lines."""

    if source_format == "TEXT":
        rows = [
            {"text": line.rstrip("\n\r")}
            for line in source.read_text(encoding="utf-8").splitlines(keepends=True)
            if line.strip()
        ]
        if not rows:
            raise ValueError("text source contains no non-empty lines")
        return rows
    connection = duckdb.connect()
    try:
        relation = connection.read_json(
            str(source),
            format="newline_delimited" if source_format == "JSONL" else "array",
        )
        columns = list(relation.columns)
        return [
            {key: _json_native(value) for key, value in zip(columns, values, strict=True)}
            for values in relation.fetchall()
        ]
    finally:
        connection.close()


def run_pipeline(
    rows: list[dict[str, Any]],
    mapping: dict[str, Any],
    normalization: dict[str, Any],
) -> tuple[list[PreparedSample], list[dict[str, Any]], list[dict[str, Any]]]:
    """Map, normalize, quality-check, and deduplicate rows deterministically."""

    mode = _enum_text(
        mapping.get("mode"), "mapping.mode", {"instruction", "conversation"}
    )
    errors: list[dict[str, Any]] = []
    candidates: list[PreparedSample] = []
    if mode == "instruction":
        _collect_instruction(rows, mapping, normalization, errors, candidates)
    else:
        _collect_conversations(rows, mapping, normalization, errors, candidates)
    samples: list[PreparedSample] = []
    duplicates: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for candidate in candidates:
        digest = hashlib.sha256(_canonical_json(candidate.content)).hexdigest()
        original = seen.get(digest)
        if original is None:
            seen[digest] = candidate.index
            samples.append(candidate)
        else:
            duplicates.append(
                {
                    "sampleIndex": candidate.index,
                    "duplicateOfSampleIndex": original,
                    "groupKey": candidate.group_key,
                    "sourceRowIndexes": candidate.source_row_indexes,
                }
            )
    return samples, errors, duplicates


def _collect_instruction(
    rows: list[dict[str, Any]],
    mapping: dict[str, Any],
    normalization: dict[str, Any],
    errors: list[dict[str, Any]],
    candidates: list[PreparedSample],
) -> None:
    specs = []
    for role, optional in (("instruction", False), ("input", True), ("output", False)):
        source = mapping.get(role)
        if source is not None:
            specs.append((role, _mapping(source, f"mapping.{role}"), optional))
    if not {role for role, _, _ in specs}.issuperset({"instruction", "output"}):
        raise ValueError("instruction mapping requires instruction and output sources")
    for row_index, row in enumerate(rows, start=1):
        content: dict[str, Any] = {}
        before = len(errors)
        for role, source, optional in specs:
            value = _resolve_source(
                row, row_index, source, normalization, errors, optional
            )
            if value is not None:
                content[role] = value
        group_key = _group_key(row, row_index, mapping)
        if group_key is None:
            errors.append(
                _sample_error(row_index, "MISSING_GROUP_FIELD", mapping.get("group_by"))
            )
        if len(errors) == before and group_key is not None:
            candidates.append(
                PreparedSample(len(candidates) + 1, group_key, content, [row_index])
            )


def _collect_conversations(
    rows: list[dict[str, Any]],
    mapping: dict[str, Any],
    normalization: dict[str, Any],
    errors: list[dict[str, Any]],
    candidates: list[PreparedSample],
) -> None:
    value_field = _required_text(
        mapping.get("conversation_value_field"), "mapping.conversation_value_field"
    )
    from_field = mapping.get("conversation_from_field")
    group_by = mapping.get("group_by")
    grouped: list[tuple[str, list[tuple[int, dict[str, Any]]]]] = []
    for row_index, row in enumerate(rows, start=1):
        key = _group_key(row, row_index, mapping)
        if key is None:
            errors.append(_sample_error(row_index, "MISSING_GROUP_FIELD", group_by))
            continue
        if grouped and grouped[-1][0] == key:
            grouped[-1][1].append((row_index, row))
        else:
            grouped.append((key, [(row_index, row)]))
    for key, group_rows in grouped:
        conversations = []
        source_indexes = []
        for row_index, row in group_rows:
            raw = row.get(value_field)
            text = _stringify(raw)
            if text is None or not _normalize(text, normalization):
                code = "MISSING_FIELD" if raw is None else "EMPTY_FIELD"
                errors.append(_sample_error(row_index, code, value_field, raw))
                continue
            speaker = "human"
            if from_field is not None:
                raw_speaker = _stringify(row.get(str(from_field)))
                if raw_speaker is None or not raw_speaker.strip():
                    errors.append(
                        _sample_error(row_index, "MISSING_FIELD", str(from_field))
                    )
                    continue
                speaker = _normalize(raw_speaker, normalization)
            conversations.append(
                {"from": speaker, "value": _normalize(text, normalization)}
            )
            source_indexes.append(row_index)
        if conversations:
            candidates.append(
                PreparedSample(
                    len(candidates) + 1,
                    key,
                    {"conversations": conversations},
                    source_indexes,
                )
            )


def _resolve_source(
    row: dict[str, Any],
    row_index: int,
    source: dict[str, Any],
    normalization: dict[str, Any],
    errors: list[dict[str, Any]],
    optional: bool,
) -> str | None:
    field = source.get("field")
    raw = (
        source.get("literal")
        if source.get("literal") is not None
        else row.get(str(field))
    )
    if raw is None:
        if not optional:
            errors.append(_sample_error(row_index, "MISSING_FIELD", field))
        return None
    text = _stringify(raw)
    if text is None:
        errors.append(_sample_error(row_index, "FIELD_TYPE_INVALID", field, raw))
        return None
    value = _normalize(text, normalization)
    if not value and not optional:
        errors.append(_sample_error(row_index, "EMPTY_FIELD", field, raw))
        return None
    return value


def assign_split(
    samples: list[PreparedSample], split: dict[str, Any] | None
) -> tuple[dict[int, str], dict[str, Any] | None]:
    """Assign whole groups to train or validation using a stable hash."""

    if split is None:
        return {}, None
    ratio = split.get("train_ratio")
    if (
        isinstance(ratio, bool)
        or not isinstance(ratio, int | float)
        or not 0 <= ratio <= 1
    ):
        raise ValueError("split.train_ratio must be between 0 and 1")
    threshold = round(float(ratio) * 10_000)
    assignment = {
        sample.index: (
            "train"
            if int.from_bytes(
                hashlib.sha256(
                    f"catalyst-split-v1:{sample.group_key}".encode()
                ).digest()[:8],
                "big",
            )
            % 10_000
            < threshold
            else "val"
        )
        for sample in samples
    }
    train = [sample for sample in samples if assignment[sample.index] == "train"]
    val = [sample for sample in samples if assignment[sample.index] == "val"]
    return assignment, {
        "train_ratio": float(ratio),
        "train_samples": len(train),
        "val_samples": len(val),
        "train_groups": len({sample.group_key for sample in train}),
        "val_groups": len({sample.group_key for sample in val}),
    }


def _write_exports(
    output_dir: Path,
    samples: list[PreparedSample],
    errors: list[dict[str, Any]],
    assignment: dict[int, str],
) -> dict[str, dict[str, Any]]:
    bundles = {
        "train.jsonl": [
            sample.content for sample in samples if assignment[sample.index] == "train"
        ],
        "val.jsonl": [
            sample.content for sample in samples if assignment[sample.index] == "val"
        ],
        "errors.jsonl": errors,
    }
    receipts = {}
    for name, rows in bundles.items():
        path = output_dir / name
        payload = b"".join(_canonical_json(row) + b"\n" for row in rows)
        path.write_bytes(payload)
        receipts[name] = {
            "row_count": len(rows),
            "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            "size": len(payload),
        }
    return receipts


def _write_json_result(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    payload = _canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "result_digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "result_size": len(payload),
    }


def _sample_document(sample: PreparedSample) -> dict[str, Any]:
    return {
        "index": sample.index,
        "group_key": sample.group_key,
        "content": sample.content,
        "source_row_indexes": sample.source_row_indexes,
    }


def _group_key(
    row: dict[str, Any], row_index: int, mapping: dict[str, Any]
) -> str | None:
    field = mapping.get("group_by")
    return f"row:{row_index}" if field is None else _stringify(row.get(str(field)))


def _normalize(value: str, config: dict[str, Any]) -> str:
    text = (
        unicodedata.normalize("NFC", value)
        if config.get("unicode_nfc", True)
        else value
    )
    if config.get("collapse_whitespace", True):
        return " ".join(text.split())
    return text.strip() if config.get("trim_whitespace", True) else text


def _stringify(value: Any) -> str | None:
    if value is None or isinstance(value, list | dict):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _sample_error(
    row_index: int, reason_code: str, field: Any, value: Any | None = None
) -> dict[str, Any]:
    normalized_field = field if isinstance(field, str) else None
    return {
        "row_index": row_index,
        "reason_code": reason_code,
        "message": f"{reason_code} at row {row_index}",
        "field": normalized_field,
        "excerpt": repr(value)[:MAX_EXCERPT_LENGTH],
    }


def _path(value: Any, field: str) -> Path:
    text = _required_text(value, field)
    path = Path(text)
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute staged path")
    return path


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _enum_text(value: Any, field: str, allowed: set[str]) -> str:
    text = _required_text(value, field)
    if text not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}")
    return text


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return value


def _validate_source(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError("source_path must be a regular file")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("source exceeds 64 MiB")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


__all__ = ["CAPABILITY_ID", "INTERFACE_VERSION", "DatasetPreparationPlugin"]
