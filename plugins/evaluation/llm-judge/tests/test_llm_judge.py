"""LLM judge tests: scripted model provider over real gRPC, parsing, fail-closed.

中文：LLM Judge 测试：通过真实 gRPC 调用脚本化模型提供方，验证解析和失败即拒绝行为。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cyrene_model_provider_contracts import (
    CAPABILITY_ID as MODEL_CAPABILITY_ID,
)
from cyrene_model_provider_contracts import (
    CHAT_COMPLETION_METHOD,
    CHAT_COMPLETION_REQUEST_TYPE_URL,
    CHAT_COMPLETION_RESPONSE_TYPE_URL,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    decode_chat_completion_request,
    encode_chat_completion_response,
)
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from jsonschema import Draft202012Validator
from llm_judge import CAPABILITY_ID, JudgeConfig, LLMJudgePlugin, RequestError

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "v1" / "schema.json").read_text(
        encoding="utf-8"
    )
)
REQUEST_VALIDATOR = Draft202012Validator(SCHEMA)
RESPONSE_VALIDATORS = {
    "llm_judge.v1": Draft202012Validator(
        {"$ref": "#/$defs/JudgeResponse", "$defs": SCHEMA["$defs"]}
    ),
    "llm_pairwise.v1": Draft202012Validator(
        {"$ref": "#/$defs/PairwiseResponse", "$defs": SCHEMA["$defs"]}
    ),
}
REQUEST_TYPE_URL = f"type.cyrene.io/{CAPABILITY_ID}.evaluate.request"


@dataclass(frozen=True, slots=True)
class _Payload:
    value: bytes
    type_url: str


class ScriptedModelProvider:
    """Answers model.provider.v1 chat_completion from a reply queue.

        中文：从回复队列中响应 model.provider.v1 chat_completion 请求。"""

    plugin_id = "test.scripted-model-provider"
    version = "0.1.0"
    capabilities = (MODEL_CAPABILITY_ID,)

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.requests: list[ChatCompletionRequest] = []

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: Any | None = None,
        request_type_url: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, _Payload | str]:
        if capability != MODEL_CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action != CHAT_COMPLETION_METHOD:
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        if request_type_url != CHAT_COMPLETION_REQUEST_TYPE_URL:
            return False, "INVALID_REQUEST: wrong request_type_url"
        self.requests.append(decode_chat_completion_request(payload))
        reply = self._replies.pop(0) if self._replies else '{"score": 0.0}'
        response = ChatCompletionResponse(
            chunks=(ChatCompletionChunk(delta=reply, finish_reason="stop"),)
        )
        return True, _Payload(
            value=encode_chat_completion_response(response),
            type_url=CHAT_COMPLETION_RESPONSE_TYPE_URL,
        )


class _ModelFixture:
    """One scripted model provider plus a judge configured against it.

        中文：一个脚本化模型提供方，以及针对该提供方配置的 Judge。"""

    def __init__(self, replies: list[str]) -> None:
        self.provider = ScriptedModelProvider(replies)
        self.server, connection_ref = serve(
            self.provider, MODEL_CAPABILITY_ID, "1", "127.0.0.1:0"
        )
        self.config = JudgeConfig(
            binding_id="judge.binding",
            model_endpoint=connection_ref,
            model="judge-model",
        )

    def close(self) -> None:
        self.server.stop(grace=None).wait()

    def judge(self) -> LLMJudgePlugin:
        return LLMJudgePlugin(self.config)


def pointwise_request(*records: dict[str, Any], **params: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "evaluator": "llm_judge.v1",
        "records": [
            {"sample_index": index, "record": record}
            for index, record in enumerate(records, start=1)
        ],
        "expected_field": "expected",
        "actual_field": "actual",
    }
    if params:
        payload["params"] = params
    return payload


def pairwise_request(*records: dict[str, Any], **params: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "evaluator": "llm_pairwise.v1",
        "records": [
            {"sample_index": index, "record": record}
            for index, record in enumerate(records, start=1)
        ],
        "candidate_a_field": "a",
        "candidate_b_field": "b",
    }
    if params:
        payload["params"] = params
    return payload


def test_pointwise_scores_against_the_threshold_and_records_judge_identity() -> None:
    fixture = _ModelFixture(['{"score": 0.9, "reason": "matches"}', '{"score": 0.2, "reason": "off"}'])
    try:
        response = fixture.judge().evaluate(
            pointwise_request(
                {"expected": "42", "actual": "42"},
                {"expected": "42", "actual": "41"},
            )
        )
    finally:
        fixture.close()

    REQUEST_VALIDATOR.validate(
        pointwise_request({"expected": "42", "actual": "42"})
    )
    RESPONSE_VALIDATORS["llm_judge.v1"].validate(response)
    assert response["record_count"] == 2
    assert response["passed_count"] == 1
    assert response["score"] == 0.5
    assert response["judge"] == {"model": "judge-model", "plugin_binding_id": "judge.binding"}

    assert len(fixture.provider.requests) == 2
    first = fixture.provider.requests[0]
    assert first.model == "judge-model"
    assert first.temperature == 0.0
    assert first.messages[0].role == 1  # system
                                        # 中文：系统
    assert "Expected" in first.messages[1].content
    assert "42" in first.messages[1].content


def test_pointwise_accepts_fenced_and_embedded_json() -> None:
    fixture = _ModelFixture(['```json\n{"score": 0.8}\n```', 'Here is my verdict: {"score": 0.6}'])
    try:
        response = fixture.judge().evaluate(
            pointwise_request(
                {"expected": "a", "actual": "a"},
                {"expected": "a", "actual": "b"},
            )
        )
    finally:
        fixture.close()

    assert response["passed_count"] == 2
    assert [sample["score"] for sample in response["samples"]] == [0.8, 0.6]


def test_pointwise_unparseable_reply_fails_only_that_sample() -> None:
    fixture = _ModelFixture(["this is not json", '{"score": 1.0}'])
    try:
        response = fixture.judge().evaluate(
            pointwise_request(
                {"expected": "a", "actual": "a"},
                {"expected": "b", "actual": "b"},
            )
        )
    finally:
        fixture.close()

    assert response["samples"][0]["passed"] is False
    assert response["samples"][0]["score"] == 0.0
    assert "JSON" in response["samples"][0]["detail"]
    assert response["samples"][1]["passed"] is True


def test_pairwise_reports_winners_ties_and_unjudged_samples() -> None:
    fixture = _ModelFixture(['{"winner": "A"}', '{"winner": "tie"}', '{"winner": "b"}'])
    try:
        response = fixture.judge().evaluate(
            pairwise_request(
                {"a": "first", "b": "second"},
                {"a": "first", "b": "second"},
                {"a": "first", "b": "second"},
            )
        )
    finally:
        fixture.close()

    RESPONSE_VALIDATORS["llm_pairwise.v1"].validate(response)
    assert (response["a_wins"], response["ties"], response["b_wins"]) == (1, 1, 1)
    assert response["unjudged"] == 0
    assert [sample["score"] for sample in response["samples"]] == [1.0, 0.5, 0.0]

    strict_fixture = _ModelFixture(['{"winner": "tie"}'])
    try:
        strict = strict_fixture.judge().evaluate(
            pairwise_request({"a": "x", "b": "y"}, allow_tie=False)
        )
    finally:
        strict_fixture.close()
    assert strict["unjudged"] == 1
    assert strict["samples"][0]["judged"] is False
    assert strict["samples"][0]["winner"] is None
    assert strict["samples"][0]["score"] == 0.0
    assert "ties are not allowed" in strict["samples"][0]["detail"]


def test_request_violations_fail_closed_without_calling_the_model() -> None:
    fixture = _ModelFixture([])
    try:
        judge = fixture.judge()
        with pytest.raises(RequestError):
            judge.evaluate({"evaluator": "nope.v1", "records": [{"sample_index": 1, "record": {}}]})
        with pytest.raises(RequestError):
            judge.evaluate(pointwise_request({"expected": "a"}, actual_field="actual"))
        with pytest.raises(RequestError):
            judge.evaluate(pointwise_request({"expected": "a", "actual": "a"}, pass_threshold=2))
        with pytest.raises(RequestError):
            judge.evaluate(
                pairwise_request({"a": "x", "b": "y"}, allow_tie="yes")
            )
    finally:
        fixture.close()
    assert fixture.provider.requests == []


def test_unconfigured_and_unreachable_models_fail_closed() -> None:
    unconfigured = LLMJudgePlugin()
    ok, message = unconfigured.on_invoke(
        CAPABILITY_ID, "evaluate", b"{}", request_type_url=REQUEST_TYPE_URL
    )
    assert ok is False
    assert message.startswith("UNAVAILABLE")

    dead = LLMJudgePlugin(
        JudgeConfig(
            binding_id="judge.binding",
            model_endpoint="127.0.0.1:1",
            model="judge-model",
            timeout_seconds=1.0,
        )
    )
    ok, message = dead.on_invoke(
        CAPABILITY_ID,
        "evaluate",
        json.dumps(pointwise_request({"expected": "a", "actual": "a"})).encode(),
        request_type_url=REQUEST_TYPE_URL,
    )
    assert ok is False
    assert message.startswith("UNAVAILABLE")


def test_activation_configuration_comes_from_the_standard_environment(monkeypatch) -> None:
    fixture = _ModelFixture(['{"score": 1.0}'])
    try:
        monkeypatch.setenv("CYRENE_CAPABILITY_BINDING_ID", "judge.binding")
        monkeypatch.setenv(
            "CYRENE_CAPABILITY_CONFIGURATION_JSON",
            json.dumps(
                {
                    "model_endpoint": fixture.config.model_endpoint,
                    "model": "judge-model",
                }
            ),
        )
        plugin = LLMJudgePlugin()
        ok, typed = plugin.on_invoke(
            CAPABILITY_ID,
            "evaluate",
            json.dumps(pointwise_request({"expected": "a", "actual": "a"})).encode(),
            request_type_url=REQUEST_TYPE_URL,
        )
    finally:
        fixture.close()

    assert ok is True
    assert json.loads(typed.value)["passed_count"] == 1


def test_judge_runs_through_direct_runtime() -> None:
    fixture = _ModelFixture(['{"score": 0.7}'])
    server, connection_ref = serve(fixture.judge(), CAPABILITY_ID, "1", "127.0.0.1:0")
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        response = client.invoke(
            capability=CAPABILITY_ID,
            interface_version="1",
            method="evaluate",
            request=DirectPayload(
                type_url=REQUEST_TYPE_URL,
                value=json.dumps(pointwise_request({"expected": "a", "actual": "a"})).encode(),
            ),
            deadline_seconds=5,
        )
    finally:
        client.close()
        server.stop(grace=None).wait()
        fixture.close()

    assert response.type_url == f"type.cyrene.io/{CAPABILITY_ID}.evaluate.response"
    decoded = json.loads(response.value)
    assert decoded["samples"][0]["score"] == 0.7
    RESPONSE_VALIDATORS["llm_judge.v1"].validate(decoded)
