"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 llm_judge.py                                                    │
│  Module: llm_judge                                                  │
│  Role: Model-backed evaluation.runner.v1 judge (pointwise + pairwise).│
│                                                                     │
│  模块职责：evaluation.runner.v1 的 LLM judge 实现：pointwise 打分与      │
│            pairwise 比较。模型访问只经 model.provider.v1 编解码器与已    │
│            解析的模型端点点位；本插件不选择提供方、不管理凭据与路由。      │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cyrene_model_provider_contracts import (
    CAPABILITY_ID as MODEL_CAPABILITY_ID,
)
from cyrene_model_provider_contracts import (
    CHAT_COMPLETION_METHOD,
    CHAT_COMPLETION_REQUEST_TYPE_URL,
    ChatCompletionRequest,
    ChatMessage,
    ChatMessageRole,
    decode_chat_completion_response,
    encode_chat_completion_request,
)
from cyrene_model_provider_contracts import (
    INTERFACE_VERSION as MODEL_INTERFACE_VERSION,
)
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, DirectPluginError
from cyrene_plugin_runtime.configuration import read_environment_settings

CAPABILITY_ID = "evaluation.runner.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"

SUPPORTED_EVALUATORS = ("llm_judge.v1", "llm_pairwise.v1")

_DEFAULT_MAX_CHARS = 4000
_MIN_MAX_CHARS = 64
_MAX_MAX_CHARS = 20_000
_DETAIL_LIMIT = 300

_SYSTEM_PROMPT = (
    "You are a strict evaluation judge. Reply with a single JSON object and "
    "nothing else."
)
_DEFAULT_RUBRIC = "Score how well the actual answer matches the expected answer."


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """Typed response consumed by DirectPluginRuntime.

        中文：DirectPluginRuntime 使用的类型化响应。
    """

    value: bytes
    type_url: str


class RequestError(ValueError):
    """Request-level contract violation; the whole evaluation fails closed.

        中文：请求级 contract 违规会使整个评估失败关闭。
    """


class JudgeConfigError(ValueError):
    """The judge has no usable model provider binding.

        中文：judge 没有可用的模型 Provider binding。
    """


class JudgeUpstreamError(RuntimeError):
    """The model provider could not answer; the whole request fails closed.

        中文：模型 Provider 无法返回结果；整个请求失败关闭。
    """


class JudgeOutputError(ValueError):
    """One sample's judge reply was unusable; only that sample fails.

        中文：某个样本的 judge 回复不可用；只将该样本判为失败。
    """


@dataclass(frozen=True, slots=True)
class JudgeConfig:
    """One resolved model provider binding for the judge.

        中文：解析出的、供 judge 使用的一个模型 Provider binding。
    """

    binding_id: str
    model_endpoint: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 512
    timeout_seconds: float = 30.0
    pass_threshold: float = 0.5

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> JudgeConfig:
        """Build configuration from the standard plugin activation environment.

            中文：根据标准 Plugin activation 环境构造配置。
        """

        binding_id = settings.get("binding_id")
        if not binding_id:
            raise JudgeConfigError("binding_id is required")

        encoded = settings.get("config") or "{}"
        try:
            decoded = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise JudgeConfigError(f"config must contain valid JSON: {error}") from error
        if not isinstance(decoded, Mapping):
            raise JudgeConfigError("config must be a JSON object")

        return cls(
            binding_id=binding_id,
            model_endpoint=_required_config_text(decoded, "model_endpoint"),
            model=_required_config_text(decoded, "model"),
            temperature=_config_number(decoded, "temperature", 0.0, minimum=0.0),
            max_tokens=_config_integer(decoded, "max_tokens", 512, minimum=1),
            timeout_seconds=_config_number(
                decoded, "timeout_seconds", 30.0, minimum=0.001
            ),
            pass_threshold=_config_number(
                decoded, "pass_threshold", 0.5, minimum=0.0, maximum=1.0
            ),
        )


def _required_config_text(config: Mapping[str, Any], field: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value:
        raise JudgeConfigError(f"config.{field} must be a non-empty string")
    return value


def _config_number(
    config: Mapping[str, Any],
    field: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    value = config.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JudgeConfigError(f"config.{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise JudgeConfigError(f"config.{field} must be a finite number >= {minimum}")
    if maximum is not None and number > maximum:
        raise JudgeConfigError(f"config.{field} must be <= {maximum}")
    return number


def _config_integer(
    config: Mapping[str, Any], field: str, default: int, *, minimum: int
) -> int:
    value = config.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise JudgeConfigError(f"config.{field} must be an integer >= {minimum}")
    return value


class LLMJudgePlugin:
    """Pointwise and pairwise LLM judging over Product-supplied records.

        中文：对 Product 提供的记录执行逐项和成对 LLM 判断。
    """

    plugin_id = "cyrene.evaluation.llm-judge"
    version = "0.1.0"
    capabilities = (CAPABILITY_ID,)

    def __init__(self, config: JudgeConfig | None = None) -> None:
        self._config = config
        if self._config is None:
            settings = read_environment_settings()
            if settings is not None:
                self._config = JudgeConfig.from_settings(settings)

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
        """Dispatch one typed judge request.

            中文：分派一个类型化 judge 请求。
        """

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

        if self._config is None:
            return (
                False,
                "UNAVAILABLE: llm judge requires a configured model provider endpoint",
            )

        try:
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict):
                raise RequestError("request must be an object")
            response = self.evaluate(request)
        except RequestError as error:
            return False, f"INVALID_REQUEST: {error}"
        except JudgeUpstreamError as error:
            return False, f"UNAVAILABLE: {error}"
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return False, f"INVALID_REQUEST: payload is not UTF-8 JSON: {error}"

        encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return True, TypedPayload(value=encoded, type_url=f"{TYPE_PREFIX}.evaluate.response")

    def evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        """Judge every record and return the typed response payload.

            中文：判断每条记录，并返回类型化响应负载。
        """

        config = self._config
        if config is None:
            raise JudgeConfigError("model provider is not configured")

        evaluator = request.get("evaluator")
        if evaluator not in SUPPORTED_EVALUATORS:
            raise RequestError(f"unsupported evaluator {evaluator!r}")

        records = request.get("records")
        if not isinstance(records, list) or not records:
            raise RequestError("records must be a non-empty array")

        params = request.get("params") or {}
        if not isinstance(params, dict):
            raise RequestError("params must be an object")

        max_chars = _bounded_integer(params, "max_chars", _DEFAULT_MAX_CHARS)
        rubric = _optional_text(params, "rubric") or _DEFAULT_RUBRIC

        client = DirectPluginClient.for_local_connection_ref(config.model_endpoint)
        try:
            if evaluator == "llm_pairwise.v1":
                response = self._evaluate_pairwise(
                    request, params, rubric, max_chars, client, config
                )
            else:
                response = self._evaluate_pointwise(
                    request, params, rubric, max_chars, client, config
                )
        finally:
            client.close()
        return response

    def _evaluate_pointwise(
        self,
        request: dict[str, Any],
        params: dict[str, Any],
        rubric: str,
        max_chars: int,
        client: DirectPluginClient,
        config: JudgeConfig,
    ) -> dict[str, Any]:
        expected_field = _required_text(request.get("expected_field"), "expected_field")
        actual_field = _required_text(request.get("actual_field"), "actual_field")
        threshold = params.get("pass_threshold", config.pass_threshold)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise RequestError("params.pass_threshold must be a number")
        threshold = float(threshold)
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise RequestError("params.pass_threshold must be between 0 and 1")

        samples: list[dict[str, Any]] = []
        for entry in request["records"]:
            sample_index, record = _record_entry(entry)
            expected = _field_value(record, expected_field, sample_index)
            actual = _field_value(record, actual_field, sample_index)
            prompt = (
                f"Rubric: {rubric}\n\n"
                f"Expected:\n{_bounded_text(expected, max_chars)}\n\n"
                f"Actual:\n{_bounded_text(actual, max_chars)}\n\n"
                'Reply exactly as {"score": <number between 0 and 1>, "reason": <short string>}.'
            )
            try:
                reply = self._complete(client, config, prompt)
                score = _parse_score(reply)
            except JudgeOutputError as error:
                samples.append(_pointwise_sample(sample_index, 0.0, False, str(error)))
                continue
            samples.append(_pointwise_sample(sample_index, score, score >= threshold, None))

        passed_count = sum(1 for sample in samples if sample["passed"])
        return {
            "evaluator": "llm_judge.v1",
            "judge": {"model": config.model, "plugin_binding_id": config.binding_id},
            "record_count": len(samples),
            "passed_count": passed_count,
            "score": passed_count / len(samples),
            "samples": samples,
        }

    def _evaluate_pairwise(
        self,
        request: dict[str, Any],
        params: dict[str, Any],
        rubric: str,
        max_chars: int,
        client: DirectPluginClient,
        config: JudgeConfig,
    ) -> dict[str, Any]:
        candidate_a_field = _required_text(
            request.get("candidate_a_field"), "candidate_a_field"
        )
        candidate_b_field = _required_text(
            request.get("candidate_b_field"), "candidate_b_field"
        )
        allow_tie = params.get("allow_tie", True)
        if not isinstance(allow_tie, bool):
            raise RequestError("params.allow_tie must be a boolean")

        samples: list[dict[str, Any]] = []
        for entry in request["records"]:
            sample_index, record = _record_entry(entry)
            candidate_a = _field_value(record, candidate_a_field, sample_index)
            candidate_b = _field_value(record, candidate_b_field, sample_index)
            prompt = (
                f"Rubric: {rubric}\n\n"
                f"Candidate A:\n{_bounded_text(candidate_a, max_chars)}\n\n"
                f"Candidate B:\n{_bounded_text(candidate_b, max_chars)}\n\n"
                'Reply exactly as {"winner": "a" or "b" or "tie", "reason": <short string>}.'
            )
            try:
                reply = self._complete(client, config, prompt)
                winner, reason = _parse_winner(reply)
            except JudgeOutputError as error:
                samples.append(_unjudged_sample(sample_index, str(error)))
                continue
            if winner == "tie" and not allow_tie:
                samples.append(
                    _unjudged_sample(
                        sample_index,
                        "judge returned a tie while ties are not allowed",
                    )
                )
                continue
            samples.append(_pairwise_sample(sample_index, winner, _winner_score(winner), reason))

        return {
            "evaluator": "llm_pairwise.v1",
            "judge": {"model": config.model, "plugin_binding_id": config.binding_id},
            "record_count": len(samples),
            "a_wins": sum(1 for sample in samples if sample["winner"] == "a"),
            "b_wins": sum(1 for sample in samples if sample["winner"] == "b"),
            "ties": sum(1 for sample in samples if sample["winner"] == "tie"),
            "unjudged": sum(1 for sample in samples if not sample["judged"]),
            "samples": samples,
        }

    def _complete(
        self, client: DirectPluginClient, config: JudgeConfig, user_prompt: str
    ) -> str:
        request = ChatCompletionRequest(
            messages=(
                ChatMessage(role=ChatMessageRole.SYSTEM, content=_SYSTEM_PROMPT),
                ChatMessage(role=ChatMessageRole.USER, content=user_prompt),
            ),
            model=config.model,
            stream=False,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        try:
            response = client.invoke(
                capability=MODEL_CAPABILITY_ID,
                interface_version=MODEL_INTERFACE_VERSION,
                method=CHAT_COMPLETION_METHOD,
                request=DirectPayload(
                    type_url=CHAT_COMPLETION_REQUEST_TYPE_URL,
                    value=encode_chat_completion_request(request),
                ),
                deadline_seconds=config.timeout_seconds,
            )
        except DirectPluginError as error:
            raise JudgeUpstreamError(str(error)) from error
        decoded = decode_chat_completion_response(response.value)
        return "".join(chunk.delta for chunk in decoded.chunks)


# ── Response builders ──────────────────────────────────────────────────
# 中文：# 中文：构造响应。

def _pointwise_sample(
    sample_index: int, score: float, passed: bool, detail: str | None
) -> dict[str, Any]:
    return {
        "sample_index": sample_index,
        "passed": passed,
        "score": score,
        "detail": _bound(detail),
    }


def _pairwise_sample(
    sample_index: int, winner: str, score: float, detail: str | None
) -> dict[str, Any]:
    return {
        "sample_index": sample_index,
        "winner": winner,
        "score": score,
        "detail": _bound(detail),
        "judged": True,
    }


def _unjudged_sample(sample_index: int, detail: str) -> dict[str, Any]:
    return {
        "sample_index": sample_index,
        "winner": None,
        "score": 0.0,
        "detail": _bound(detail),
        "judged": False,
    }


def _winner_score(winner: str) -> float:
    if winner == "a":
        return 1.0
    if winner == "b":
        return 0.0
    return 0.5


# ── Parsing helpers ────────────────────────────────────────────────────
# 中文：# 中文：解析辅助函数。

def _parse_score(reply: str) -> float:
    payload = _parse_judge_json(reply)
    score = payload.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise JudgeOutputError("judge reply score must be a number between 0 and 1")
    number = float(score)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise JudgeOutputError("judge reply score must be between 0 and 1")
    return number


def _parse_winner(reply: str) -> tuple[str, str | None]:
    payload = _parse_judge_json(reply)
    winner = payload.get("winner")
    if not isinstance(winner, str) or winner.strip().lower() not in {"a", "b", "tie"}:
        raise JudgeOutputError('judge reply winner must be "a", "b", or "tie"')
    reason = payload.get("reason")
    return winner.strip().lower(), reason if isinstance(reason, str) else None


def _parse_judge_json(reply: str) -> dict[str, Any]:
    """Return the first JSON object in a judge reply, fenced or embedded.

        中文：返回 judge 回复中的第一个 JSON 对象，无论该对象被代码围栏包裹还是嵌在文本中。
    """

    candidate = reply.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        first_newline = candidate.find("\n")
        if first_newline != -1 and " " not in candidate[:first_newline]:
            candidate = candidate[first_newline + 1 :]
    decoder = json.JSONDecoder()
    for index, character in enumerate(candidate):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise JudgeOutputError("judge reply contained no JSON object")


# ── Request helpers ────────────────────────────────────────────────────
# 中文：# 中文：请求辅助函数。

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


def _field_value(record: dict[str, Any], field: str, sample_index: int) -> Any:
    if field not in record:
        raise RequestError(
            f"record for sample {sample_index} is missing field {field!r}"
        )
    return record[field]


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RequestError(f"{field} must be a non-empty string")
    return value


def _optional_text(params: dict[str, Any], field: str) -> str | None:
    value = params.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RequestError(f"params.{field} must be a non-empty string")
    return value


def _bounded_integer(params: dict[str, Any], field: str, default: int) -> int:
    value = params.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError(f"params.{field} must be an integer")
    if not _MIN_MAX_CHARS <= value <= _MAX_MAX_CHARS:
        raise RequestError(
            f"params.{field} must be between {_MIN_MAX_CHARS} and {_MAX_MAX_CHARS}"
        )
    return value


def _bounded_text(value: Any, max_chars: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _bound(detail: str | None) -> str | None:
    if detail is None:
        return None
    if len(detail) <= _DETAIL_LIMIT:
        return detail
    return detail[: _DETAIL_LIMIT - 3] + "..."
