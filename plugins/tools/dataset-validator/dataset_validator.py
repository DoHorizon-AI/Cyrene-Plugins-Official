"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 dataset_validator.py                                           │
│  Module: dataset_validator                                         │
│  Role: Canonical tool.dataset.validator.v1 implementation.         │
│                                                                     │
│  模块职责：训练数据集深度校验的唯一实现与 DirectPluginRuntime 入口。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CAPABILITY_ID = "tool.dataset.validator.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"
SUPPORTED_FORMATS = frozenset({"jsonl", "json", "parquet", "csv"})
SUPPORTED_SCHEMAS = frozenset({"instruction", "conversation", "sharegpt"})


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """Typed response consumed by DirectPluginRuntime."""

    value: bytes
    type_url: str


class DatasetValidatorPlugin:
    """Validate reusable dataset structure without owning Product state."""

    plugin_id = "cyrene.tools.dataset-validator"
    version = "0.2.0"
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
        """Dispatch one typed dataset-validation request."""

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action != "validate":
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        expected_type_url = f"{TYPE_PREFIX}.{action}.request"
        if request_type_url != expected_type_url:
            return (
                False,
                f"INVALID_REQUEST: request_type_url must be {expected_type_url}",
            )
        if stream_results:
            return False, "METHOD_NOT_SUPPORTED: dataset validation is not streaming"
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        try:
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict):
                raise TypeError("request must be an object")
            result = self.validate(
                file_path=_required_text(request.get("file_path"), "file_path"),
                format_type=_optional_text(request.get("format"), "format") or "auto",
                sample_size=_bounded_integer(
                    request.get("sample_size", 5), "sample_size", 0, 100
                ),
                schema=_optional_text(request.get("schema"), "schema"),
                max_errors=_bounded_integer(
                    request.get("max_errors", 100), "max_errors", 1, 1000
                ),
                max_sequence_length=_bounded_integer(
                    request.get("max_sequence_length", 2048),
                    "max_sequence_length",
                    1,
                    1_000_000,
                ),
            )
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return False, f"INVALID_REQUEST: {exc}"
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        return True, TypedPayload(
            value=json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
            type_url=f"{TYPE_PREFIX}.{action}.response",
        )

    def validate(
        self,
        file_path: str,
        *,
        format_type: str = "auto",
        sample_size: int = 5,
        schema: str | None = None,
        max_errors: int = 100,
        max_sequence_length: int = 2048,
    ) -> dict[str, Any]:
        """Load and validate one local dataset staged for this Plugin process."""

        path = Path(file_path)
        if not path.is_file():
            return _result(
                valid=False,
                errors=[_error(None, None, "File does not exist")],
                detected_schema=schema,
            )

        normalized_format = format_type.strip().lower()
        if normalized_format == "auto":
            normalized_format = _detect_format(path)
        if normalized_format not in SUPPORTED_FORMATS:
            return _result(
                valid=False,
                errors=[_error(None, None, f"Unsupported format: {normalized_format}")],
                detected_format=normalized_format,
                detected_schema=schema,
            )
        if schema is not None and schema not in SUPPORTED_SCHEMAS:
            return _result(
                valid=False,
                errors=[_error(None, "schema", f"Unsupported schema: {schema}")],
                detected_format=normalized_format,
                detected_schema=schema,
            )

        try:
            rows, columns = _load_data(path, normalized_format)
        except (OSError, UnicodeError, TypeError, ValueError, csv.Error) as exc:
            return _result(
                valid=False,
                errors=[_error(None, None, f"Failed to load data: {exc}")],
                detected_format=normalized_format,
                detected_schema=schema,
            )

        detected_schema = schema or _detect_schema(columns)
        errors: list[dict[str, Any]] = []
        warnings: list[str] = []
        required = {
            "instruction": {"instruction", "output"},
            "conversation": {"conversations"},
            "sharegpt": {"conversations"},
        }.get(detected_schema or "", set())
        missing = required - set(columns)
        if missing:
            errors.append(
                _error(
                    None,
                    None,
                    f"Missing required columns: {', '.join(sorted(missing))}",
                )
            )

        empty_prompt_count = 0
        empty_response_count = 0
        truncation_risks = 0
        total_characters = 0
        for row_index, row in enumerate(rows):
            if len(errors) >= max_errors:
                warnings.append(
                    f"Maximum error count ({max_errors}) reached; validation stopped"
                )
                break
            row_errors = _validate_row(row, detected_schema, row_index)
            errors.extend(row_errors[: max_errors - len(errors)])
            prompt, response, row_characters = _text_measurements(row)
            empty_prompt_count += int(not prompt.strip())
            empty_response_count += int(not response.strip())
            total_characters += row_characters
            truncation_risks += int(row_characters / 3.5 > max_sequence_length)

        if empty_prompt_count:
            warnings.append(f"{empty_prompt_count} rows have empty prompts")
        if empty_response_count:
            warnings.append(f"{empty_response_count} rows have empty responses")
        if truncation_risks:
            warnings.append(
                f"{truncation_risks} rows risk token truncation under "
                f"max_sequence_length={max_sequence_length}"
            )

        return _result(
            valid=bool(rows) and not errors,
            row_count=len(rows),
            columns=columns,
            errors=errors,
            warnings=warnings,
            sample_rows=rows[:sample_size],
            detected_format=normalized_format,
            detected_schema=detected_schema,
            metrics={
                "average_characters": round(total_characters / len(rows), 1)
                if rows
                else 0,
                "empty_prompts": empty_prompt_count,
                "empty_responses": empty_response_count,
                "truncation_risks": truncation_risks,
            },
        )


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _bounded_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jsonl", ".json", ".parquet", ".csv"}:
        return suffix.removeprefix(".")
    try:
        prefix = path.read_bytes()[:64].lstrip()
    except OSError:
        return "jsonl"
    return "json" if prefix.startswith((b"{", b"[")) else "jsonl"


def _load_data(path: Path, format_type: str) -> tuple[list[dict[str, Any]], list[str]]:
    if format_type == "jsonl":
        rows = []
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError(f"JSONL row {line_number} must be an object")
                rows.append(value)
        return rows, _columns(rows)
    if format_type == "json":
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        items = value if isinstance(value, list) else [value]
        if not all(isinstance(item, dict) for item in items):
            raise ValueError("JSON root must be an object or an array of objects")
        rows = [dict(item) for item in items]
        return rows, _columns(rows)
    if format_type == "csv":
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                raise ValueError("CSV header is required")
            return [dict(row) for row in reader], list(reader.fieldnames)
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise ValueError("duckdb is required for Parquet validation") from exc
    connection = duckdb.connect()
    try:
        relation = connection.read_parquet(str(path))
        rows = [
            dict(zip(relation.columns, values, strict=True))
            for values in relation.fetchall()
        ]
        return rows, list(relation.columns)
    except duckdb.Error as exc:
        raise ValueError(f"Parquet could not be read: {exc}") from exc
    finally:
        connection.close()


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({key for row in rows for key in row})


def _detect_schema(columns: list[str]) -> str | None:
    values = set(columns)
    if "conversations" in values:
        return "conversation"
    if {"instruction", "output"}.issubset(values):
        return "instruction"
    return None


def _validate_row(
    row: dict[str, Any], schema: str | None, row_index: int
) -> list[dict[str, Any]]:
    if schema == "instruction":
        return _validate_instruction_row(row, row_index)
    if schema in {"conversation", "sharegpt"}:
        return _validate_conversation_row(row, row_index)
    return []


def _validate_instruction_row(
    row: dict[str, Any], row_index: int
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for field in ("instruction", "output"):
        if field not in row:
            errors.append(_error(row_index, field, f"Missing required field: {field}"))
        elif not isinstance(row[field], str) or not row[field].strip():
            errors.append(
                _error(
                    row_index,
                    field,
                    f"{field} must be a non-empty string",
                    row.get(field),
                )
            )
    if "input" in row and not isinstance(row["input"], str):
        errors.append(
            _error(
                row_index,
                "input",
                "input must be a string",
                type(row["input"]).__name__,
            )
        )
    return errors


def _validate_conversation_row(
    row: dict[str, Any], row_index: int
) -> list[dict[str, Any]]:
    conversations = row.get("conversations")
    if not isinstance(conversations, list) or not conversations:
        return [
            _error(
                row_index,
                "conversations",
                "conversations must be a non-empty array",
                type(conversations).__name__,
            )
        ]
    errors = []
    for item_index, item in enumerate(conversations):
        field = f"conversations[{item_index}]"
        if not isinstance(item, dict):
            errors.append(
                _error(row_index, field, "Conversation item must be an object")
            )
            continue
        if "from" not in item or "value" not in item:
            errors.append(
                _error(
                    row_index,
                    field,
                    "Conversation item must contain 'from' and 'value'",
                )
            )
        elif not isinstance(item["value"], str) or not item["value"].strip():
            errors.append(
                _error(row_index, f"{field}.value", "value must be a non-empty string")
            )
    return errors


def _text_measurements(row: dict[str, Any]) -> tuple[str, str, int]:
    conversations = row.get("conversations")
    if isinstance(conversations, list):
        values = [
            str(item.get("value", ""))
            for item in conversations
            if isinstance(item, dict)
        ]
        text = "".join(values)
        return text, text, len(text)
    prompt = str(row.get("instruction") or row.get("prompt") or "")
    response = str(row.get("output") or row.get("response") or "")
    return prompt, response, len(prompt) + len(response)


def _error(
    row_index: int | None,
    field: str | None,
    message: str,
    value: Any | None = None,
) -> dict[str, Any]:
    return {"row_index": row_index, "field": field, "message": message, "value": value}


def _result(
    *,
    valid: bool,
    row_count: int = 0,
    columns: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    sample_rows: list[dict[str, Any]] | None = None,
    detected_format: str | None = None,
    detected_schema: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "valid": valid,
        "row_count": row_count,
        "columns": columns or [],
        "errors": errors or [],
        "warnings": warnings or [],
        "sample_rows": sample_rows or [],
        "detected_format": detected_format,
        "detected_schema": detected_schema,
        "metrics": metrics
        or {
            "average_characters": 0,
            "empty_prompts": 0,
            "empty_responses": 0,
            "truncation_risks": 0,
        },
    }


__all__ = ["CAPABILITY_ID", "INTERFACE_VERSION", "DatasetValidatorPlugin"]
