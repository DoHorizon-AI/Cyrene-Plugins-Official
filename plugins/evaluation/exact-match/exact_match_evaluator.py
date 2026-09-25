"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 exact_match_evaluator.py                                       │
│  Module: exact_match_evaluator                                     │
│  Role: Canonical evaluation.runner.v1 exact-match implementation.  │
│                                                                     │
│  模块职责：精确匹配评估能力的唯一实现与标准 DirectPluginRuntime 入口。     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

CAPABILITY_ID = "evaluation.runner.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"
EVALUATOR_ID = "exact_match.v1"


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """Typed response consumed by DirectPluginRuntime.

        中文：由 DirectPluginRuntime 使用的有类型响应。"""

    value: bytes
    type_url: str


def _coerce_text(value: Any) -> str | None:
    """Project one JSON value to text without inventing a missing value.

        中文：将一个 JSON 值映射为文本，不虚构缺失值。"""

    if value is None:
        return None
    if isinstance(value, str | int | float | bool):
        return str(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _non_negative_integer(value: Any) -> int | None:
    """Return one provider-supplied counter when it is valid.

        中文：在提供方给出的计数器有效时返回该计数值。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage(record: dict[str, Any]) -> dict[str, Any] | None:
    """Project provider usage facts and never estimate missing counters.

        中文：映射提供方报告的用量事实，绝不估算缺失的计数值。"""

    value = record.get("usage")
    if not isinstance(value, dict):
        return None
    usage = {
        "prompt_tokens": _non_negative_integer(value.get("prompt_tokens")),
        "completion_tokens": _non_negative_integer(value.get("completion_tokens")),
        "total_tokens": _non_negative_integer(value.get("total_tokens")),
    }
    if all(item is None for item in usage.values()):
        return None
    source = value.get("source")
    usage["source"] = (
        source.strip() if isinstance(source, str) and source.strip() else "provider"
    )
    return usage


class ExactMatchEvaluationRunner:
    """Evaluate JSON records with strict field equality and return measurements.

        中文：使用严格字段相等规则评估 JSON 记录并返回测量结果。"""

    plugin_id = "cyrene.evaluation.exact-match"
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
        """Dispatch one typed JSON evaluation request.

            中文：分发一个有类型的 JSON 评估请求。"""

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
                raise TypeError("request must be an object")
            result = self.evaluate(
                records=request.get("records"),
                evaluator=request.get("evaluator"),
                expected_field=request.get("expected_field"),
                actual_field=request.get("actual_field"),
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

    def evaluate(
        self,
        records: list[dict[str, Any]],
        evaluator: str,
        expected_field: str,
        actual_field: str,
    ) -> dict[str, Any]:
        """Return exact-match measurements for all supplied records.

            中文：返回所有已提供记录的精确匹配测量结果。"""

        if evaluator != EVALUATOR_ID:
            raise ValueError(f"evaluator must be {EVALUATOR_ID}")
        for name, value in (
            ("expected_field", expected_field),
            ("actual_field", actual_field),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        if not isinstance(records, list) or not records:
            raise ValueError("records must be a non-empty array")

        samples = []
        for position, item in enumerate(records, start=1):
            if not isinstance(item, dict):
                raise TypeError(f"records[{position}] must be an object")
            sample_index = item.get("sample_index")
            record = item.get("record")
            if (
                isinstance(sample_index, bool)
                or not isinstance(sample_index, int)
                or sample_index < 1
            ):
                raise ValueError(
                    f"records[{position}].sample_index must be a positive integer"
                )
            if not isinstance(record, dict):
                raise TypeError(f"records[{position}].record must be an object")
            if expected_field not in record or actual_field not in record:
                raise ValueError(
                    f"record {sample_index} is missing an evaluation field"
                )
            expected = record[expected_field]
            actual = record[actual_field]
            passed = expected == actual
            samples.append(
                {
                    "sample_index": sample_index,
                    "input_record": record,
                    "expected": expected,
                    "actual": actual,
                    "raw_output": _coerce_text(record.get("output"))
                    or _coerce_text(actual),
                    "passed": passed,
                    "score": 1.0 if passed else 0.0,
                    "model_ref": _coerce_text(record.get("model")),
                    "endpoint_ref": _coerce_text(record.get("endpoint")),
                    "usage": _usage(record),
                    "judge_identity": None,
                }
            )

        passed_count = sum(1 for sample in samples if sample["passed"])
        record_count = len(samples)
        return {
            "evaluator": evaluator,
            "passed_count": passed_count,
            "record_count": record_count,
            "score": passed_count / record_count,
            "samples": samples,
        }


__all__ = [
    "CAPABILITY_ID",
    "EVALUATOR_ID",
    "INTERFACE_VERSION",
    "ExactMatchEvaluationRunner",
]
