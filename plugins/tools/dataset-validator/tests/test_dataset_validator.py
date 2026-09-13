import json

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
