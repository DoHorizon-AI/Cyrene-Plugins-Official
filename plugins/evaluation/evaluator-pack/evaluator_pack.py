"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 evaluator_pack.py                                              │
│  Module: evaluator_pack                                            │
│  Role: Deterministic evaluation.runner.v1 evaluator pack.          │
│                                                                     │
│  模块职责：evaluation.runner.v1 的确定性 evaluator 集合：contains /  │
│            regex / JSON structural / JSON Schema / numeric          │
│            tolerance。每个 evaluator 有自己的 owner-scoped schema     │
│            与稳定 evaluator id；Plugin 只回答"给我输入和规则，我算     │
│            评价结果"，不拥有 Product 状态。                            │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

CAPABILITY_ID = "evaluation.runner.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"

SUPPORTED_EVALUATORS = (
    "contains.v1",
    "regex.v1",
    "json_structural.v1",
    "json_schema.v1",
    "numeric_tolerance.v1",
)

_DETAIL_LIMIT = 300
_CHECK_LIMIT = 8


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """Typed response consumed by DirectPluginRuntime.

        中文：由 DirectPluginRuntime 使用的有类型响应。"""

    value: bytes
    type_url: str


class RequestError(ValueError):
    """Request-level contract violation; the whole evaluation fails closed.

        中文：请求级契约违规；整个评估会按失败即拒绝处理。"""


class EvaluatorPackPlugin:
    """Deterministic evaluators over Product-supplied JSON records.

        中文：针对 Product 提供的 JSON 记录运行的确定性评估器。"""

    plugin_id = "cyrene.evaluation.evaluator-pack"
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
        """Dispatch one typed evaluation request.

            中文：分发一个有类型的评估请求。"""

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action != "evaluate":
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        expected_type_url = f"{TYPE_PREFIX}.{action}.request"
        if request_type_url != expected_type_url:
            return (
                False,
                f"INVALID_REQUEST: request_type_url must be {expected_type_url}",
            )
        if stream_results:
            return False, "METHOD_NOT_SUPPORTED: evaluation is not streaming"
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"

        try:
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict):
                raise RequestError("request must be an object")
            response = self.evaluate(request)
        except RequestError as error:
            return False, f"INVALID_REQUEST: {error}"
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return False, f"INVALID_REQUEST: payload is not UTF-8 JSON: {error}"

        encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return True, TypedPayload(value=encoded, type_url=f"{TYPE_PREFIX}.evaluate.response")

    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        """Evaluate every record and return the typed response payload.

            中文：评估每条记录并返回有类型的响应载荷。"""

        evaluator = request.get("evaluator")
        if evaluator not in SUPPORTED_EVALUATORS:
            raise RequestError(f"unsupported evaluator {evaluator!r}")

        records = request.get("records")
        if not isinstance(records, list) or not records:
            raise RequestError("records must be a non-empty array")

        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RequestError("params must be an object")

        expected_field: str | None
        if evaluator == "json_schema.v1":
            expected_field = None
            actual_field = _required_text(request.get("actual_field"), "actual_field")
        else:
            expected_field = _required_text(request.get("expected_field"), "expected_field")
            actual_field = _required_text(request.get("actual_field"), "actual_field")

        check = _checks[evaluator](params)
        samples: list[dict[str, Any]] = []
        for entry in records:
            sample_index, record = _record_entry(entry)
            expected = None
            if expected_field is not None:
                if expected_field not in record:
                    raise RequestError(
                        f"record for sample {sample_index} is missing expected field "
                        f"{expected_field!r}"
                    )
                expected = record[expected_field]
            if actual_field not in record:
                raise RequestError(
                    f"record for sample {sample_index} is missing actual field {actual_field!r}"
                )
            actual = record[actual_field]
            passed, detail = check(expected, actual)
            samples.append(
                {
                    "sample_index": sample_index,
                    "passed": passed,
                    "score": 1.0 if passed else 0.0,
                    "detail": _bound(detail),
                }
            )

        passed_count = sum(1 for sample in samples if sample["passed"])
        return {
            "evaluator": evaluator,
            "record_count": len(samples),
            "passed_count": passed_count,
            "score": passed_count / len(samples),
            "samples": samples,
        }


# ── Shared helpers ─────────────────────────────────────────────────────
# 中文：── 共享辅助函数 ─────────────────────────────────────────────────────

def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RequestError(f"{field} must be a non-empty string")
    return value


def _optional_bool(params: dict[str, Any], field: str, default: bool) -> bool:
    value = params.get(field, default)
    if not isinstance(value, bool):
        raise RequestError(f"params.{field} must be a boolean")
    return value


def _optional_number(params: dict[str, Any], field: str, default: float) -> float:
    value = params.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(f"params.{field} must be a number")
    if value < 0 or not math.isfinite(value):
        raise RequestError(f"params.{field} must be a finite non-negative number")
    return float(value)


def _record_entry(entry: Any) -> tuple[int, dict[str, Any]]:
    if not isinstance(entry, dict):
        raise RequestError("each record entry must be an object")
    sample_index = entry.get("sample_index")
    if isinstance(sample_index, bool) or not isinstance(sample_index, int) or sample_index < 1:
        raise RequestError("each record entry needs a positive integer sample_index")
    record = entry.get("record")
    if not isinstance(record, dict):
        raise RequestError("each record entry needs an object record")
    return sample_index, record


def _bound(detail: str | None) -> str | None:
    if detail is None:
        return None
    if len(detail) <= _DETAIL_LIMIT:
        return detail
    return detail[: _DETAIL_LIMIT - 3] + "..."


# ── Evaluators ─────────────────────────────────────────────────────────
# 中文：── 评估器 ─────────────────────────────────────────────────────────────

def _contains_check(params: dict[str, Any]) -> Callable[[Any, Any], tuple[bool, str | None]]:
    case_sensitive = _optional_bool(params, "case_sensitive", True)

    def check(expected: Any, actual: Any) -> tuple[bool, str | None]:
        if not isinstance(expected, str) or not isinstance(actual, str):
            return False, "contains.v1 requires string expected and actual values"
        needle, haystack = expected, actual
        if not case_sensitive:
            needle, haystack = needle.lower(), haystack.lower()
        if needle in haystack:
            return True, None
        return False, "expected substring not found in actual value"

    return check


def _regex_check(params: dict[str, Any]) -> Callable[[Any, Any], tuple[bool, str | None]]:
    pattern = params.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise RequestError("params.pattern must be a non-empty string")
    mode = params.get("mode", "search")
    if mode not in ("search", "fullmatch"):
        raise RequestError("params.mode must be 'search' or 'fullmatch'")
    flags = params.get("flags", [])
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        raise RequestError("params.flags must be an array of strings")

    compiled_flags = 0
    for flag in flags:
        if flag == "ignorecase":
            compiled_flags |= re.IGNORECASE
        elif flag == "multiline":
            compiled_flags |= re.MULTILINE
        elif flag == "dotall":
            compiled_flags |= re.DOTALL
        else:
            raise RequestError(f"unsupported regex flag {flag!r}")
    try:
        compiled = re.compile(pattern, compiled_flags)
    except re.error as error:
        raise RequestError(f"params.pattern is not a valid regex: {error}") from error

    def check(expected: Any, actual: Any) -> tuple[bool, str | None]:
        if not isinstance(actual, str):
            return False, "regex.v1 requires a string actual value"
        matched = compiled.search(actual) if mode == "search" else compiled.fullmatch(actual)
        if matched:
            return True, None
        return False, f"actual value does not match regex ({mode})"

    return check


def _as_json_value(value: Any) -> Any:
    """Parse JSON encoded strings once so structural comparison is stable.

        中文：仅解析一次 JSON 编码字符串，以保证结构比较稳定。"""

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _json_structural_check(
    params: dict[str, Any],
) -> Callable[[Any, Any], tuple[bool, str | None]]:
    if params:
        raise RequestError("json_structural.v1 accepts no params")

    def check(expected: Any, actual: Any) -> tuple[bool, str | None]:
        if _as_json_value(expected) == _as_json_value(actual):
            return True, None
        return False, "expected and actual JSON structures differ"

    return check


def _json_schema_check(params: dict[str, Any]) -> Callable[[Any, Any], tuple[bool, str | None]]:
    schema = params.get("schema")
    if not isinstance(schema, dict):
        raise RequestError("params.schema must be a JSON Schema object")
    try:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
    except SchemaError as error:
        raise RequestError(f"params.schema is not a valid JSON Schema: {error.message}") from error

    def check(_expected: Any, actual: Any) -> tuple[bool, str | None]:
        errors = sorted(
            validator.iter_errors(_as_json_value(actual)),
            key=lambda error: list(error.path),
        )
        if not errors:
            return True, None
        rendered = "; ".join(
            f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
            for error in errors[:_CHECK_LIMIT]
        )
        return False, rendered

    return check


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _numeric_tolerance_check(
    params: dict[str, Any],
) -> Callable[[Any, Any], tuple[bool, str | None]]:
    abs_tolerance = _optional_number(params, "abs_tolerance", 0.0)
    rel_tolerance = _optional_number(params, "rel_tolerance", 0.0)

    def check(expected: Any, actual: Any) -> tuple[bool, str | None]:
        expected_number = _as_number(expected)
        actual_number = _as_number(actual)
        if expected_number is None or actual_number is None:
            return False, "numeric_tolerance.v1 requires finite numeric values"
        allowed = abs_tolerance + rel_tolerance * abs(expected_number)
        if abs(expected_number - actual_number) <= allowed:
            return True, None
        return False, (
            f"|{expected_number} - {actual_number}| exceeds tolerance {allowed}"
        )

    return check


_checks: dict[str, Callable[[dict[str, Any]], Callable[[Any, Any], tuple[bool, str | None]]]] = {
    "contains.v1": _contains_check,
    "regex.v1": _regex_check,
    "json_structural.v1": _json_structural_check,
    "json_schema.v1": _json_schema_check,
    "numeric_tolerance.v1": _numeric_tolerance_check,
}
