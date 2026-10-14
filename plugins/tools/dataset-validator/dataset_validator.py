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
_MAX_JSONL_ROW_BYTES = 16 * 1024 * 1024
SUPPORTED_FORMATS = frozenset({"jsonl", "json", "parquet", "csv"})
SUPPORTED_SCHEMAS = frozenset(
    {
        "instruction",
        "instruction_history",
        "conversation",
        "sharegpt",
        "messages",
        "prompt_completion",
    }
)


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """Typed response consumed by DirectPluginRuntime.

    中文:由 DirectPluginRuntime 使用的有类型响应。"""

    value: bytes
    type_url: str


class DatasetValidatorPlugin:
    """Validate reusable dataset structure without owning Product state.

    中文:验证可复用数据集结构,不持有 Product 状态。"""

    plugin_id = "cyrene.tools.dataset-validator"
    version = "0.3.0"
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
        """Dispatch one typed dataset-validation request.

        中文:分发一个有类型的数据集验证请求。"""

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
        """Load and validate one local dataset staged for this Plugin process.

        中文:加载并验证为此 Plugin 进程暂存的一个本地数据集。"""

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
            rows, columns = _iter_data(path, normalized_format)
        except (
            OSError,
            UnicodeError,
            TypeError,
            ValueError,
            RecursionError,
            csv.Error,
        ) as exc:
            return _result(
                valid=False,
                errors=[_error(None, None, f"Failed to load data: {exc}")],
                detected_format=normalized_format,
                detected_schema=schema,
            )

        detected_schema = schema or _detect_schema(columns)
        errors: list[dict[str, Any]] = []
        warnings: list[str] = []
        empty_prompt_count = 0
        empty_response_count = 0
        truncation_risks = 0
        total_characters = 0
        row_count = 0
        sampled_rows: list[dict[str, Any]] = []
        observed_columns = set(columns)
        for row_index, row, parse_error in rows:
            row_count += 1
            if parse_error is not None:
                if len(errors) < max_errors:
                    errors.append(_error(row_index, None, parse_error))
                continue
            assert row is not None
            observed_columns.update(row)
            if schema is None and detected_schema is None:
                detected_schema = _detect_schema(list(row))
            if len(sampled_rows) < sample_size:
                sampled_rows.append(row)
            if len(errors) >= max_errors:
                continue
            row_errors = _validate_row(row, detected_schema, row_index)
            errors.extend(row_errors[: max_errors - len(errors)])
            prompt, response, row_characters = _text_measurements(row)
            empty_prompt_count += int(not prompt.strip())
            empty_response_count += int(not response.strip())
            total_characters += row_characters
            truncation_risks += int(row_characters / 3.5 > max_sequence_length)

        if row_count and len(errors) >= max_errors:
            warnings.append(
                f"Maximum error count ({max_errors}) reached; additional errors were not recorded"
            )
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
            valid=row_count > 0 and not errors,
            row_count=row_count,
            columns=sorted(observed_columns),
            errors=errors,
            warnings=warnings,
            sample_rows=sampled_rows,
            detected_format=normalized_format,
            detected_schema=detected_schema,
            metrics={
                "average_characters": round(total_characters / row_count, 1)
                if row_count
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
        with path.open("rb") as stream:
            prefix = stream.read(64).lstrip()
    except OSError:
        return "jsonl"
    return "json" if prefix.startswith((b"{", b"[")) else "jsonl"


def _iter_data(path: Path, format_type: str) -> tuple[Any, list[str]]:
    if format_type == "jsonl":

        def jsonl_rows() -> Any:
            with path.open("rb") as stream:
                line_number = 0
                while line := stream.readline(_MAX_JSONL_ROW_BYTES + 1):
                    line_number += 1
                    if len(line) > _MAX_JSONL_ROW_BYTES:
                        while not line.endswith(b"\n"):
                            line = stream.readline(_MAX_JSONL_ROW_BYTES + 1)
                            if not line:
                                break
                        yield (
                            line_number - 1,
                            None,
                            f"JSONL row exceeds {_MAX_JSONL_ROW_BYTES} byte limit",
                        )
                        continue
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(
                            line,
                            object_pairs_hook=_unique_json_object,
                            parse_constant=_reject_json_constant,
                        )
                    except (
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        RecursionError,
                        ValueError,
                    ) as exc:
                        yield line_number - 1, None, f"Invalid JSONL: {exc}"
                        continue
                    if not isinstance(value, dict):
                        yield line_number - 1, None, "JSONL row must be an object"
                        continue
                    yield line_number - 1, value, None

        return jsonl_rows(), []
    if format_type == "json":
        with path.open(encoding="utf-8") as stream:
            value = json.load(
                stream,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        items = value if isinstance(value, list) else [value]
        if not all(isinstance(item, dict) for item in items):
            raise ValueError("JSON root must be an object or an array of objects")
        return (
            iter((index, dict(item), None) for index, item in enumerate(items)),
            _columns(items),
        )
    if format_type == "csv":
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                raise ValueError("CSV header is required")
            columns = list(reader.fieldnames)

        def csv_rows() -> Any:
            with path.open(encoding="utf-8", newline="") as csv_stream:
                reader = csv.DictReader(csv_stream)
                for index, row in enumerate(reader):
                    yield index, dict(row), None

        return csv_rows(), columns
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise ValueError("duckdb is required for Parquet validation") from exc
    connection = duckdb.connect()
    try:
        relation = connection.read_parquet(str(path))
        columns = list(relation.columns)

        rows = [
            (index, dict(zip(columns, values, strict=True)), None)
            for index, values in enumerate(relation.fetchall())
        ]
        return iter(rows), columns
    except duckdb.Error as exc:
        raise ValueError(f"Parquet could not be read: {exc}") from exc
    finally:
        connection.close()


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({key for row in rows for key in row})


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number is not allowed: {value}")


def _detect_schema(columns: list[str]) -> str | None:
    values = set(columns)
    if "messages" in values:
        return "messages"
    if {"prompt", "completion"}.issubset(values):
        return "prompt_completion"
    if "conversations" in values:
        return "conversation"
    if {"instruction", "input", "output", "system", "history"}.issubset(values):
        return "instruction_history"
    if {"instruction", "output"}.issubset(values):
        return "instruction"
    return None


def _validate_row(
    row: dict[str, Any], schema: str | None, row_index: int
) -> list[dict[str, Any]]:
    if schema == "instruction":
        return _validate_instruction_row(row, row_index) + _unexpected_fields(
            row, {"instruction", "input", "output", "system", "history"}, row_index
        )
    if schema == "instruction_history":
        return _validate_instruction_history_row(row, row_index) + _unexpected_fields(
            row, {"instruction", "input", "output", "system", "history"}, row_index
        )
    if schema == "messages":
        return _validate_messages_row(row, row_index) + _unexpected_fields(
            row, {"messages"}, row_index
        )
    if schema == "prompt_completion":
        return _validate_prompt_completion_row(row, row_index) + _unexpected_fields(
            row, {"prompt", "completion"}, row_index
        )
    if schema in {"conversation", "sharegpt"}:
        return _validate_conversation_row(row, row_index)
    return []


def _unexpected_fields(
    row: dict[str, Any], allowed: set[str], row_index: int
) -> list[dict[str, Any]]:
    return [
        _error(row_index, field, "Unexpected field for the declared target schema")
        for field in sorted(row.keys() - allowed)
    ]


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
    if "system" in row and not isinstance(row["system"], str):
        errors.append(
            _error(row_index, "system", "system must be a string", row["system"])
        )
    if "history" in row:
        history = row["history"]
        if not isinstance(history, list):
            errors.append(
                _error(row_index, "history", "history must be an array", history)
            )
        else:
            for history_index, pair in enumerate(history):
                if (
                    not isinstance(pair, list)
                    or len(pair) != 2
                    or any(
                        not isinstance(value, str) or not value.strip()
                        for value in pair
                    )
                ):
                    errors.append(
                        _error(
                            row_index,
                            f"history[{history_index}]",
                            "history entries must be [user, assistant] non-empty string pairs",
                            pair,
                        )
                    )
    return errors


def _validate_instruction_history_row(
    row: dict[str, Any], row_index: int
) -> list[dict[str, Any]]:
    errors = _validate_instruction_row(row, row_index)
    if "system" not in row:
        errors.append(_error(row_index, "system", "Missing required field: system"))
    if "history" not in row:
        errors.append(_error(row_index, "history", "Missing required field: history"))
    return errors


def _validate_messages_row(row: dict[str, Any], row_index: int) -> list[dict[str, Any]]:
    messages = row.get("messages")
    if not isinstance(messages, list) or not messages:
        return [
            _error(
                row_index, "messages", "messages must be a non-empty array", messages
            )
        ]
    errors: list[dict[str, Any]] = []
    expected = "user"
    saw_user = False
    saw_assistant = False
    for index, message in enumerate(messages):
        field = f"messages[{index}]"
        if not isinstance(message, dict):
            errors.append(
                _error(row_index, field, "message must be an object", message)
            )
            continue
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant"}:
            errors.append(
                _error(row_index, f"{field}.role", "unsupported message role", role)
            )
            continue
        if message.keys() != {"role", "content"}:
            errors.append(
                _error(
                    row_index,
                    field,
                    "message objects may contain only role and content fields",
                )
            )
        if not isinstance(content, str) or not content.strip():
            errors.append(
                _error(
                    row_index,
                    f"{field}.content",
                    "content must be non-empty text",
                    content,
                )
            )
            continue
        if role == "system":
            if index != 0 or saw_user:
                errors.append(
                    _error(
                        row_index,
                        f"{field}.role",
                        "system is only supported as the first message",
                        role,
                    )
                )
        elif role != expected:
            errors.append(
                _error(row_index, f"{field}.role", f"expected {expected} role", role)
            )
        else:
            if role == "user":
                saw_user = True
                expected = "assistant"
            else:
                saw_assistant = True
                expected = "user"
    if not saw_user or not saw_assistant or expected != "user":
        errors.append(
            _error(
                row_index,
                "messages",
                "messages must contain complete user/assistant turns and end with assistant",
            )
        )
    return errors


def _validate_prompt_completion_row(
    row: dict[str, Any], row_index: int
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for field in ("prompt", "completion"):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(
                _error(row_index, field, f"{field} must be non-empty text", value)
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
    messages = row.get("messages")
    if isinstance(messages, list):
        prompt = "".join(
            str(item.get("content", ""))
            for item in messages
            if isinstance(item, dict) and item.get("role") in {"system", "user"}
        )
        response = "".join(
            str(item.get("content", ""))
            for item in messages
            if isinstance(item, dict) and item.get("role") == "assistant"
        )
        return prompt, response, len(prompt) + len(response)
    prompt_completion = "completion" in row
    if prompt_completion:
        prompt = str(row.get("prompt") or "")
        response = str(row.get("completion") or "")
        return prompt, response, len(prompt) + len(response)
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
    history = row.get("history")
    history_characters = (
        sum(
            len(value)
            for pair in history
            if isinstance(pair, list)
            for value in pair
            if isinstance(value, str)
        )
        if isinstance(history, list)
        else 0
    )
    system_characters = (
        len(row.get("system", "")) if isinstance(row.get("system", ""), str) else 0
    )
    return (
        prompt,
        response,
        len(prompt) + len(response) + history_characters + system_characters,
    )


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
