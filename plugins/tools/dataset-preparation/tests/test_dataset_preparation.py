"""Contract and behavior tests for dataset.preparation.v1.

中文：dataset.preparation.v1 的契约和行为测试。"""

from __future__ import annotations

import hashlib
import json

import duckdb
import pytest
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


def test_inspect_projects_duckdb_temporal_cells_back_to_json(tmp_path) -> None:
    source = tmp_path / "source.jsonl"
    source.write_text(
        '{"instruction":"q","output":"a","annotation":{"createdAt":"2026-09-20T18:39:12.541157Z"}}\n',
        encoding="utf-8",
    )
    result_path = tmp_path / "result.json"

    result = DatasetPreparationPlugin().inspect(source, result_path)

    assert result["row_count"] == 1
    rows = json.loads(result_path.read_text(encoding="utf-8"))["rows"]
    projected = rows[0]["annotation"]["createdAt"]
    assert isinstance(projected, str)
    assert projected.startswith("2026-09-20T18:39:12.541157")


def test_inspect_and_prepare_csv_with_explicit_format_hint(tmp_path) -> None:
    source = tmp_path / "source.bin"
    source.write_text("question,answer,group\nhello,world,a\n", encoding="utf-8")
    inspect_result = tmp_path / "inspect.json"

    receipt = DatasetPreparationPlugin().inspect(
        source, inspect_result, format_hint="CSV"
    )

    assert receipt["format"] == "CSV"
    assert receipt["detected_fields"] == ["answer", "group", "question"]
    assert json.loads(inspect_result.read_text(encoding="utf-8"))["rows"] == [
        {"answer": "world", "group": "a", "question": "hello"}
    ]

    prepare_result = tmp_path / "prepare.json"
    prepared = DatasetPreparationPlugin().prepare(
        source_path=source,
        source_format="CSV",
        mapping={
            "mode": "instruction",
            "instruction": {"field": "question"},
            "output": {"field": "answer"},
            "group_by": "group",
        },
        normalization={},
        result_path=prepare_result,
    )
    assert prepared["unique_samples"] == 1
    assert json.loads(prepare_result.read_text(encoding="utf-8"))["samples"][0][
        "content"
    ] == {"instruction": "hello", "output": "world"}


def test_inspect_rejects_ragged_csv_rows(tmp_path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("question,answer\nhello,world,unexpected\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown or missing columns"):
        DatasetPreparationPlugin().inspect(
            source, tmp_path / "result.json", format_hint="CSV"
        )


def test_inspect_parquet_from_content_or_hint(tmp_path) -> None:
    source = tmp_path / "source.bin"
    with duckdb.connect() as connection:
        connection.execute(
            "COPY (SELECT 'q' AS question, 'a' AS answer) TO ? (FORMAT PARQUET)",
            [str(source)],
        )

    plugin = DatasetPreparationPlugin()
    detected_result = tmp_path / "detected.json"
    detected = plugin.inspect(source, detected_result)
    hinted_result = tmp_path / "hinted.json"
    hinted = plugin.inspect(source, hinted_result, format_hint="PARQUET")

    assert detected["format"] == hinted["format"] == "PARQUET"
    assert json.loads(detected_result.read_text(encoding="utf-8"))["rows"] == [
        {"answer": "a", "question": "q"}
    ]


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
    assert set(payload["files"]) == {
        f"{bundle}.{suffix}"
        for bundle in ("train", "val", "errors")
        for suffix in ("jsonl", "csv", "parquet")
    }
    assert payload["result_digest"] == (
        "sha256:" + hashlib.sha256(result_path.read_bytes()).hexdigest()
    )
    exported = json.loads((output_dir / "train.jsonl").read_text(encoding="utf-8"))
    assert exported["conversations"][1] == {"from": "assistant", "value": "world"}
    assert (output_dir / "train.csv").read_text(encoding="utf-8").splitlines()[0] == (
        "conversations"
    )
    with duckdb.connect() as connection:
        parquet_rows = connection.read_parquet(str(output_dir / "train.parquet")).fetchall()
    assert len(parquet_rows) == 1


def test_direct_endpoint_inspects_csv_with_format_hint(tmp_path) -> None:
    source = tmp_path / "opaque-source"
    source.write_text("question,answer\nhello,world\n", encoding="utf-8")
    result_path = tmp_path / "inspect.json"
    server, connection_ref = serve(
        DatasetPreparationPlugin(), "dataset.preparation.v1", "1", "127.0.0.1:0"
    )
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        response = client.invoke(
            capability="dataset.preparation.v1",
            interface_version="1",
            method="inspect",
            request=DirectPayload(
                type_url="type.cyrene.io/dataset.preparation.v1.inspect.request",
                value=json.dumps(
                    {
                        "source_path": str(source),
                        "result_path": str(result_path),
                        "format_hint": "CSV",
                    }
                ).encode(),
            ),
            deadline_seconds=5,
        )
    finally:
        client.close()
        server.stop(grace=None).wait()

    receipt = json.loads(response.value)
    assert receipt["format"] == "CSV"
    assert receipt["row_count"] == 1
    assert json.loads(result_path.read_text(encoding="utf-8"))["rows"] == [
        {"answer": "world", "question": "hello"}
    ]


def test_transform_writes_real_parquet(tmp_path) -> None:
    source = tmp_path / "source.jsonl"
    destination = tmp_path / "result.parquet"
    source.write_text('{"instruction":"a","output":"b"}\n', encoding="utf-8")

    result = DatasetPreparationPlugin().transform(source, destination)

    assert result["row_count"] == 1
    assert result["schema_fields"] == ["instruction", "output"]
    with duckdb.connect() as connection:
        assert connection.read_parquet(str(destination)).fetchall() == [("a", "b")]


def test_transform_accepts_csv_source_format(tmp_path) -> None:
    source = tmp_path / "opaque-source"
    destination = tmp_path / "result.parquet"
    source.write_text("instruction,output\na,b\n", encoding="utf-8")

    result = DatasetPreparationPlugin().transform(
        source, destination, source_format="CSV"
    )

    assert result["row_count"] == 1
    assert result["schema_fields"] == ["instruction", "output"]
    with duckdb.connect() as connection:
        assert connection.read_parquet(str(destination)).fetchall() == [("a", "b")]
