"""Contract and behavior tests for training.llama-factory.v1."""

from __future__ import annotations

import json

from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from llama_factory import LlamaFactoryTrainingPlugin


def _spec(tmp_path) -> dict:
    dataset = tmp_path / "train.jsonl"
    dataset.write_text('{"instruction":"q","input":"","output":"a"}\n', encoding="utf-8")
    model = tmp_path / "base"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"llama"}', encoding="utf-8")
    return {
        "engine": "llamafactory",
        "model": {"path": str(model)},
        "dataset": {"path": str(dataset)},
        "output_dir": str(tmp_path / "run"),
        "stage": "sft",
        "finetuning_type": "lora",
        "lora": {"r": 8, "lora_alpha": 16},
        "hyperparams": {"learning_rate": 0.0002, "epochs": 1.0},
        "distributed": {"gpu_count": 1, "world_size": 1, "nnodes": 1, "nproc_per_node": 1},
        "checkpoint": {},
        "extra": {"template": "qwen", "max_steps": 1, "max_length": 512},
    }


def test_compile_materializes_a_real_run_directory(tmp_path) -> None:
    result = LlamaFactoryTrainingPlugin().compile(_spec(tmp_path))
    launch = result["launch"]

    run_dir = tmp_path / "run"
    arguments = json.loads((run_dir / "train_config.json").read_text(encoding="utf-8"))
    dataset_info = json.loads((run_dir / "dataset_info.json").read_text(encoding="utf-8"))

    assert arguments["model_name_or_path"] == str(tmp_path / "base")
    assert arguments["output_dir"] == str(run_dir)
    assert arguments["max_steps"] == 1
    assert arguments["lora_rank"] == 8
    assert arguments["template"] == "qwen"
    assert arguments["do_train"] is True
    assert dataset_info["cyrene"]["file_name"] == str(tmp_path / "train.jsonl")
    assert launch["argv"][-2:] == ["train", str(run_dir / "train_config.json")]
    assert launch["spec_artifact_path"] == str(run_dir / "train_config.json")
    assert launch["mounts"][0]["read_only"] is True
    assert launch["resources"]["gpu_count"] == 1
    assert launch["launch_kind"] == "direct"


def test_compile_rejects_machine_device_authority(tmp_path) -> None:
    spec = _spec(tmp_path)
    spec["environment"] = {"env": {"CUDA_VISIBLE_DEVICES": "0"}}

    try:
        LlamaFactoryTrainingPlugin().compile(spec)
    except ValueError as exc:
        assert "machine devices" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("device assignment must be rejected")


def test_compile_ignores_unknown_overrides_but_blocks_identity(tmp_path) -> None:
    spec = _spec(tmp_path)
    spec["extra"]["llamafactory_args"] = {"output_dir": "/tmp/elsewhere"}

    try:
        LlamaFactoryTrainingPlugin().compile(spec)
    except ValueError as exc:
        assert "must not override" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("identity overrides must be rejected")


def test_parse_event_classifies_progress_lines() -> None:
    plugin = LlamaFactoryTrainingPlugin()

    progress = plugin.parse_event('{"loss": 0.25, "step": 1}')
    log = plugin.parse_event("Loading model")

    assert progress["kind"] == "progress"
    assert progress["payload"]["loss"] == 0.25
    assert log["kind"] == "log"


def test_direct_endpoint_compiles_over_the_real_runtime(tmp_path) -> None:
    plugin = LlamaFactoryTrainingPlugin()
    server, connection_ref = serve(plugin, "training.llama-factory.v1", "1", "127.0.0.1:0")
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        response = client.invoke(
            capability="training.llama-factory.v1",
            interface_version="1",
            method="compile",
            request=DirectPayload(
                type_url="type.cyrene.io/training.llama-factory.v1.compile.request",
                value=json.dumps({"spec": _spec(tmp_path)}).encode(),
            ),
            deadline_seconds=5,
        )
    finally:
        client.close()
        server.stop(grace=None).wait()

    payload = json.loads(response.value)
    assert response.type_url == "type.cyrene.io/training.llama-factory.v1.compile.response"
    assert payload["launch"]["argv"][-2] == "train"
    assert (tmp_path / "run" / "train_config.json").is_file()