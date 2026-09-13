"""Contract and behavior tests for dataset.preparation.v1."""

from __future__ import annotations

import hashlib
import json

import duckdb
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from dataset_preparation import DatasetPreparationPlugin, run_pipeline


def test_instruction_preparation_is_deterministic_and_deduplicated(tmp_path) -> None:
    rows = [
        {"question": "  hello  ", "answer": "world", "group": "a"},
        {"question": "hello", "answer": "world", "group": "a"},
        {"question": "missing output", "answer": "   ", "group": "b"},
    ]
    mapping = {
        "mode": "instruction",
        "instruction": {"field": "question"},
        "output": {"field": "answer"},
        "group_by": "group",
    }
    normalization = {
        "trim_whitespace": True,
        "collapse_whitespace": True,
        "unicode_nfc": True,
    }

    first = run_pipeline(rows, mapping, normalization)
    second = run_pipeline(rows, mapping, normalization)

    assert first == second
    samples, errors, duplicates = first
    assert [sample.content for sample in samples] == [
        {"instruction": "hello", "output": "world"}
    ]
    assert errors[0]["reason_code"] == "EMPTY_FIELD"
    assert duplicates[0]["duplicateOfSampleIndex"] == 1


def test_direct_endpoint_writes_verified_exports(tmp_path) -> None:
    source = tmp_path / "source.jsonl"
    source.write_text(
        '{"speaker":"human","text":"hello","scene":"a"}\n'
        '{"speaker":"assistant","text":"world","scene":"a"}\n',
        encoding="utf-8",
    )
    result_path = tmp_path / "result.json"
    output_dir = tmp_path / "exports"
    plugin = DatasetPreparationPlugin()
    server, connection_ref = serve(plugin, "dataset.preparation.v1", "1", "127.0.0.1:0")
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        response = client.invoke(
            capability="dataset.preparation.v1",
            interface_version="1",
            method="prepare",
            request=DirectPayload(
                type_url="type.cyrene.io/dataset.preparation.v1.prepare.request",
                value=json.dumps(
                    {
                        "source_path": str(source),
                        "source_format": "JSONL",
                        "mapping": {
                            "mode": "conversation",
                            "conversation_value_field": "text",
                            "conversation_from_field": "speaker",
                            "group_by": "scene",
                        },
                        "normalization": {},
                        "split": {"train_ratio": 1.0},
                        "result_path": str(result_path),
                        "output_dir": str(output_dir),
                    }
                ).encode(),
            ),
            deadline_seconds=5,
        )
    finally:
        client.close()
        server.stop(grace=None).wait()

    payload = json.loads(response.value)
    assert response.type_url == "type.cyrene.io/dataset.preparation.v1.prepare.response"
    assert payload["unique_samples"] == 1
    assert payload["files"]["train.jsonl"]["row_count"] == 1
    assert payload["result_digest"] == (
        "sha256:" + hashlib.sha256(result_path.read_bytes()).hexdigest()
    )
    exported = json.loads((output_dir / "train.jsonl").read_text(encoding="utf-8"))
    assert exported["conversations"][1] == {"from": "assistant", "value": "world"}


def test_transform_writes_real_parquet(tmp_path) -> None:
    source = tmp_path / "source.jsonl"
    destination = tmp_path / "result.parquet"
    source.write_text('{"instruction":"a","output":"b"}\n', encoding="utf-8")

    result = DatasetPreparationPlugin().transform(source, destination)

    assert result["row_count"] == 1
    assert result["schema_fields"] == ["instruction", "output"]
    with duckdb.connect() as connection:
        assert connection.read_parquet(str(destination)).fetchall() == [("a", "b")]
