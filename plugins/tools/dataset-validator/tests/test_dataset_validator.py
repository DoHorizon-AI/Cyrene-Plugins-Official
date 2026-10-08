import json

import dataset_validator
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from dataset_validator import DatasetValidatorPlugin


def test_csv_claim_has_a_real_parser(tmp_path) -> None:
    dataset = tmp_path / "dataset.csv"
    dataset.write_text("instruction,output\nhello,world\n", encoding="utf-8")

    result = DatasetValidatorPlugin().validate(str(dataset), format_type="csv")

    assert result["valid"] is True
    assert result["row_count"] == 1
    assert result["metrics"]["empty_prompts"] == 0


def test_json_array_rejects_non_object_rows(tmp_path) -> None:
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps([{"instruction": "a", "output": "b"}, "bad"]), encoding="utf-8"
    )

    result = DatasetValidatorPlugin().validate(str(dataset), format_type="json")

    assert result["valid"] is False
    assert result["row_count"] == 0
    assert "array of objects" in result["errors"][0]["message"]


def test_messages_schema_preserves_system_and_complete_multi_turn_rows(
    tmp_path,
) -> None:
    dataset = tmp_path / "messages.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "Answer in Chinese."},
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "你好。"},
                    {"role": "user", "content": "再见"},
                    {"role": "assistant", "content": "再见。"},
                ]
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    result = DatasetValidatorPlugin().validate(
        str(dataset), format_type="jsonl", schema="messages"
    )

    assert result["valid"] is True
    assert result["row_count"] == 1
    assert result["sample_rows"][0]["messages"][0]["role"] == "system"


def test_messages_schema_rejects_tool_roles_and_incomplete_turns(tmp_path) -> None:
    dataset = tmp_path / "messages.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "question"},
                    {"role": "tool", "content": "result"},
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = DatasetValidatorPlugin().validate(
        str(dataset), format_type="jsonl", schema="messages"
    )

    assert result["valid"] is False
    assert any(
        "unsupported message role" in error["message"] for error in result["errors"]
    )


def test_messages_schema_rejects_unmodeled_message_fields(tmp_path) -> None:
    dataset = tmp_path / "messages.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer", "tool_calls": []},
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = DatasetValidatorPlugin().validate(
        str(dataset), format_type="jsonl", schema="messages"
    )

    assert result["valid"] is False
    assert any(
        "only role and content" in error["message"] for error in result["errors"]
    )


def test_instruction_history_schema_requires_roundtrip_fields(tmp_path) -> None:
    dataset = tmp_path / "sft.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "instruction": "current question",
                "input": "",
                "output": "current answer",
                "system": "be concise",
                "history": [["earlier question", "earlier answer"]],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = DatasetValidatorPlugin().validate(
        str(dataset), format_type="jsonl", schema="instruction_history"
    )

    assert result["valid"] is True
    assert result["detected_schema"] == "instruction_history"


def test_jsonl_validation_continues_after_a_corrupt_line(tmp_path) -> None:
    dataset = tmp_path / "mixed.jsonl"
    dataset.write_text(
        '{"prompt":"q1","completion":"a1"}\nnot-json\n'
        '{"prompt":"q2","completion":"a2"}\n',
        encoding="utf-8",
    )

    result = DatasetValidatorPlugin().validate(str(dataset), format_type="jsonl")

    assert result["valid"] is False
    assert result["row_count"] == 3
    assert result["detected_schema"] == "prompt_completion"
    assert len(result["sample_rows"]) == 2
    assert len(result["errors"]) == 1


def test_jsonl_rejects_duplicate_keys_and_nonstandard_numbers(tmp_path) -> None:
    dataset = tmp_path / "strict.jsonl"
    dataset.write_text(
        '{"prompt":"q","prompt":"overridden","completion":"a"}\n'
        '{"prompt":"q","completion":NaN}\n'
        '{"prompt":"ok","completion":"answer"}\n',
        encoding="utf-8",
    )

    result = DatasetValidatorPlugin().validate(str(dataset), format_type="jsonl")

    assert result["valid"] is False
    assert result["row_count"] == 3
    assert len(result["errors"]) == 2
    assert result["sample_rows"] == [{"prompt": "ok", "completion": "answer"}]


def test_jsonl_oversized_line_is_bounded_and_later_rows_are_validated(
    tmp_path, monkeypatch
) -> None:
    dataset = tmp_path / "bounded.jsonl"
    row = json.dumps({"prompt": "q", "completion": "a"})
    monkeypatch.setattr(dataset_validator, "_MAX_JSONL_ROW_BYTES", len(row) + 10)
    dataset.write_text(
        "x" * 1000 + "\n" + row + "\n",
        encoding="utf-8",
    )

    result = DatasetValidatorPlugin().validate(str(dataset), format_type="jsonl")

    assert result["valid"] is False
    assert result["row_count"] == 2
    assert result["sample_rows"] == [{"prompt": "q", "completion": "a"}]
    assert "byte limit" in result["errors"][0]["message"]


def test_dataset_validator_runs_through_direct_runtime(tmp_path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text('{"instruction":"hello","output":"world"}\n', encoding="utf-8")
    plugin = DatasetValidatorPlugin()
    server, connection_ref = serve(
        plugin, "tool.dataset.validator.v1", "1", "127.0.0.1:0"
    )
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        response = client.invoke(
            capability="tool.dataset.validator.v1",
            interface_version="1",
            method="validate",
            request=DirectPayload(
                type_url="type.cyrene.io/tool.dataset.validator.v1.validate.request",
                value=json.dumps(
                    {
                        "file_path": str(dataset),
                        "format": "jsonl",
                        "schema": "instruction",
                    }
                ).encode(),
            ),
            deadline_seconds=2,
        )
    finally:
        client.close()
        server.stop(grace=None).wait()

    assert (
        response.type_url
        == "type.cyrene.io/tool.dataset.validator.v1.validate.response"
    )
    assert json.loads(response.value)["valid"] is True
