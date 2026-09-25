"""Deterministic evaluator pack tests: semantics, fail-closed requests, schema.

中文：确定性评估器包测试：语义、失败即拒绝请求和架构。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from evaluator_pack import CAPABILITY_ID, EvaluatorPackPlugin, RequestError
from jsonschema import Draft202012Validator

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "v1" / "schema.json").read_text(
        encoding="utf-8"
    )
)
REQUEST_VALIDATOR = Draft202012Validator(SCHEMA)
RESPONSE_VALIDATOR = Draft202012Validator(
    {"$ref": "#/$defs/EvaluateResponse", "$defs": SCHEMA["$defs"]}
)


def request(evaluator: str, records: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"evaluator": evaluator, "records": records}
    payload.update(fields)
    return payload


def records(*pairs: tuple[Any, Any]) -> list[dict[str, Any]]:
    return [
        {"sample_index": index, "record": {"expected": expected, "actual": actual}}
        for index, (expected, actual) in enumerate(pairs, start=1)
    ]


def test_contains_reports_substring_matches_and_case_sensitivity() -> None:
    plugin = EvaluatorPackPlugin()
    response = plugin.evaluate(
        request(
            "contains.v1",
            records(("answer", "the answer is 42"), ("ANSWER", "the answer is 42")),
            expected_field="expected",
            actual_field="actual",
        )
    )
    assert response["record_count"] == 2
    assert response["passed_count"] == 1
    assert response["score"] == 0.5
    assert response["samples"][1]["detail"] is not None

    insensitive = plugin.evaluate(
        request(
            "contains.v1",
            records(("ANSWER", "the answer is 42")),
            expected_field="expected",
            actual_field="actual",
            params={"case_sensitive": False},
        )
    )
    assert insensitive["passed_count"] == 1

    non_string = plugin.evaluate(
        request("contains.v1", records((1, "text")), expected_field="expected", actual_field="actual")
    )
    assert non_string["passed_count"] == 0


def test_regex_supports_modes_flags_and_rejects_invalid_patterns() -> None:
    plugin = EvaluatorPackPlugin()
    search = plugin.evaluate(
        request(
            "regex.v1",
            records((None, "abc-123"), (None, "no digits")),
            expected_field="expected",
            actual_field="actual",
            params={"pattern": r"\d{3}"},
        )
    )
    assert search["passed_count"] == 1

    fullmatch = plugin.evaluate(
        request(
            "regex.v1",
            records((None, "abc-123")),
            expected_field="expected",
            actual_field="actual",
            params={"pattern": r"[a-z]+-\d{3}", "mode": "fullmatch"},
        )
    )
    assert fullmatch["passed_count"] == 1

    insensitive = plugin.evaluate(
        request(
            "regex.v1",
            records((None, "HELLO")),
            expected_field="expected",
            actual_field="actual",
            params={"pattern": "hello", "flags": ["ignorecase"]},
        )
    )
    assert insensitive["passed_count"] == 1

    with pytest.raises(RequestError):
        plugin.evaluate(
            request(
                "regex.v1",
                records((None, "x")),
                expected_field="expected",
                actual_field="actual",
                params={"pattern": "("},
            )
        )
    with pytest.raises(RequestError):
        plugin.evaluate(
            request(
                "regex.v1",
                records((None, "x")),
                expected_field="expected",
                actual_field="actual",
                params={"pattern": "x", "flags": ["verbose"]},
            )
        )


def test_json_structural_ignores_key_order_but_not_array_order() -> None:
    plugin = EvaluatorPackPlugin()
    key_order = plugin.evaluate(
        request(
            "json_structural.v1",
            records(({"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1})),
            expected_field="expected",
            actual_field="actual",
        )
    )
    assert key_order["passed_count"] == 1

    array_order = plugin.evaluate(
        request(
            "json_structural.v1",
            records(({"a": [1, 2]}, {"a": [2, 1]})),
            expected_field="expected",
            actual_field="actual",
        )
    )
    assert array_order["passed_count"] == 0

    encoded = plugin.evaluate(
        request(
            "json_structural.v1",
            records(('{"a": 1}', {"a": 1})),
            expected_field="expected",
            actual_field="actual",
        )
    )
    assert encoded["passed_count"] == 1


def test_json_schema_validates_actual_values() -> None:
    plugin = EvaluatorPackPlugin()
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
    }
    response = plugin.evaluate(
        request(
            "json_schema.v1",
            records((None, {"name": "cyrene"}), (None, {"name": 7})),
            actual_field="actual",
            params={"schema": schema},
        )
    )
    assert response["passed_count"] == 1
    assert "name" in response["samples"][1]["detail"]

    with pytest.raises(RequestError):
        plugin.evaluate(
            request(
                "json_schema.v1",
                records((None, {"name": "cyrene"})),
                actual_field="actual",
                params={"schema": {"type": "not-a-type"}},
            )
        )


def test_numeric_tolerance_uses_absolute_and_relative_bounds() -> None:
    plugin = EvaluatorPackPlugin()
    absolute = plugin.evaluate(
        request(
            "numeric_tolerance.v1",
            records((1.0, 1.05), (1.0, 1.2), ("2.5", "2.55")),
            expected_field="expected",
            actual_field="actual",
            params={"abs_tolerance": 0.1},
        )
    )
    assert absolute["passed_count"] == 2

    relative = plugin.evaluate(
        request(
            "numeric_tolerance.v1",
            records((1000.0, 1005.0), (1000.0, 1100.0)),
            expected_field="expected",
            actual_field="actual",
            params={"rel_tolerance": 0.01},
        )
    )
    assert relative["passed_count"] == 1

    non_numeric = plugin.evaluate(
        request(
            "numeric_tolerance.v1",
            records(("abc", "abc")),
            expected_field="expected",
            actual_field="actual",
        )
    )
    assert non_numeric["passed_count"] == 0


def test_request_violations_fail_closed() -> None:
    plugin = EvaluatorPackPlugin()
    with pytest.raises(RequestError):
        plugin.evaluate(request("unknown.v1", records((1, 1)), expected_field="expected", actual_field="actual"))
    with pytest.raises(RequestError):
        plugin.evaluate(request("contains.v1", [], expected_field="expected", actual_field="actual"))
    with pytest.raises(RequestError):
        plugin.evaluate(
            request(
                "contains.v1",
                [{"sample_index": 1, "record": {"actual": "text"}}],
                expected_field="expected",
                actual_field="actual",
            )
        )
    with pytest.raises(RequestError):
        plugin.evaluate(
            request(
                "numeric_tolerance.v1",
                records((1, 1)),
                expected_field="expected",
                actual_field="actual",
                params={"abs_tolerance": -1},
            )
        )

    ok, message = plugin.on_invoke(
        CAPABILITY_ID,
        "evaluate",
        json.dumps(
            request("regex.v1", records((None, "x")), expected_field="expected", actual_field="actual")
        ).encode(),
        request_type_url=f"type.cyrene.io/{CAPABILITY_ID}.evaluate.request",
    )
    assert ok is False
    assert message.startswith("INVALID_REQUEST")


def test_responses_validate_against_the_schema_and_are_deterministic() -> None:
    plugin = EvaluatorPackPlugin()
    payload = request(
        "json_structural.v1",
        records(({"a": 1}, {"a": 1}), ({"a": 1}, {"a": 2})),
        expected_field="expected",
        actual_field="actual",
    )
    first = plugin.evaluate(payload)
    second = plugin.evaluate(payload)
    assert first == second
    RESPONSE_VALIDATOR.validate(first)
    for sample in first["samples"]:
        assert 0.0 <= sample["score"] <= 1.0

    ok, typed = plugin.on_invoke(
        CAPABILITY_ID,
        "evaluate",
        json.dumps(payload).encode(),
        request_type_url=f"type.cyrene.io/{CAPABILITY_ID}.evaluate.request",
    )
    assert ok is True
    assert typed.type_url == f"type.cyrene.io/{CAPABILITY_ID}.evaluate.response"
    assert json.loads(typed.value) == first


def test_on_invoke_rejects_wrong_type_url_streaming_and_cancellation() -> None:
    plugin = EvaluatorPackPlugin()
    ok, message = plugin.on_invoke(
        CAPABILITY_ID, "evaluate", b"{}", request_type_url="type.cyrene.io/wrong.request"
    )
    assert ok is False and message.startswith("INVALID_REQUEST")

    ok, message = plugin.on_invoke(
        CAPABILITY_ID,
        "evaluate",
        b"{}",
        request_type_url=f"type.cyrene.io/{CAPABILITY_ID}.evaluate.request",
        stream_results=True,
    )
    assert ok is False and message.startswith("METHOD_NOT_SUPPORTED")

    class _Cancelled:
        def is_cancelled(self) -> bool:
            return True

    ok, message = plugin.on_invoke(
        CAPABILITY_ID,
        "evaluate",
        b"{}",
        request_type_url=f"type.cyrene.io/{CAPABILITY_ID}.evaluate.request",
        cancellation=_Cancelled(),
    )
    assert ok is False and message.startswith("CANCELLED")

    ok, message = plugin.on_invoke("other.capability", "evaluate", b"{}")
    assert ok is False and message.startswith("INVALID_REQUEST")


def test_pack_runs_through_direct_runtime() -> None:
    plugin = EvaluatorPackPlugin()
    payload = request(
        "numeric_tolerance.v1",
        records((1.0, 1.05)),
        expected_field="expected",
        actual_field="actual",
        params={"abs_tolerance": 0.1},
    )
    server, connection_ref = serve(plugin, CAPABILITY_ID, "1", "127.0.0.1:0")
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        response = client.invoke(
            capability=CAPABILITY_ID,
            interface_version="1",
            method="evaluate",
            request=DirectPayload(
                type_url=f"type.cyrene.io/{CAPABILITY_ID}.evaluate.request",
                value=json.dumps(payload).encode(),
            ),
            deadline_seconds=2,
        )
    finally:
        client.close()
        server.stop(grace=None).wait()

    assert response.type_url == f"type.cyrene.io/{CAPABILITY_ID}.evaluate.response"
    decoded = json.loads(response.value)
    assert decoded["passed_count"] == 1
    RESPONSE_VALIDATOR.validate(decoded)
