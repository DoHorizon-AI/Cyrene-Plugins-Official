"""training.llama-factory.v1 conformance tests. | 训练能力契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from llama_factory import LlamaFactoryTrainingPlugin

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads(
    (
        REPOSITORY_ROOT
        / "plugins/training/llama-factory/contracts/v1/schema.json"
    ).read_text(encoding="utf-8")
)


def _validate(definition: str, payload: dict) -> None:
    Draft202012Validator(
        {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]}
    ).validate(payload)


def _spec(tmp_path: Path) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
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


def test_inspect_compile_and_parse_event_match_the_contract(tmp_path: Path) -> None:
    plugin = LlamaFactoryTrainingPlugin()

    _validate("InspectResponse", plugin.inspect())

    compiled = plugin.compile(_spec(tmp_path))
    _validate("CompileResponse", compiled)
    assert "CUDA_VISIBLE_DEVICES" not in compiled["launch"].get("env", {})
    run_dir = tmp_path / "run"
    assert (run_dir / "train_config.json").is_file()
    assert (run_dir / "dataset_info.json").is_file()
    assert compiled["launch"]["argv"][-2:] == ["train", str(run_dir / "train_config.json")]

    progress = plugin.parse_event('{"loss": 0.25, "step": 1}')
    _validate("ParseEventResponse", progress)
    _validate("ParseEventResponse", plugin.parse_event("Loading model"))


def test_compile_rejects_device_and_identity_authority(tmp_path: Path) -> None:
    plugin = LlamaFactoryTrainingPlugin()

    device_spec = _spec(tmp_path / "device")
    device_spec["environment"] = {"env": {"CUDA_VISIBLE_DEVICES": "0"}}
    try:
        plugin.compile(device_spec)
    except ValueError as exc:
        assert "machine devices" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("device assignment must be rejected")

    identity_spec = _spec(tmp_path / "identity")
    identity_spec["extra"]["llamafactory_args"] = {"output_dir": "/tmp/elsewhere"}
    try:
        plugin.compile(identity_spec)
    except ValueError as exc:
        assert "must not override" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("identity overrides must be rejected")