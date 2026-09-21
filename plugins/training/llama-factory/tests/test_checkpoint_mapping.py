"""Checkpoint argument mapping for the LLaMA Factory Plugin contract."""

from __future__ import annotations

import json

from llama_factory import LlamaFactoryTrainingPlugin


def test_compile_maps_resume_and_checkpoint_retention(tmp_path):
    dataset = tmp_path / "train.jsonl"
    dataset.write_text('{"instruction":"q","output":"a"}\n', encoding="utf-8")
    model = tmp_path / "base"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"llama"}', encoding="utf-8")
    spec = {
        "model": {"path": str(model)},
        "dataset": {"path": str(dataset)},
        "output_dir": str(tmp_path / "run"),
        "checkpoint": {
            "resume_from": str(tmp_path / "checkpoint-4"),
            "save_steps": 25,
            "save_total_limit": 2,
        },
        "lora": {"r": 8, "lora_alpha": 16},
        "hyperparams": {"learning_rate": 0.0002, "epochs": 1.0},
        "extra": {},
    }

    LlamaFactoryTrainingPlugin().compile(spec)
    arguments = json.loads((tmp_path / "run" / "train_config.json").read_text(encoding="utf-8"))

    assert arguments["resume_from_checkpoint"] == str(tmp_path / "checkpoint-4")
    assert arguments["save_strategy"] == "steps"
    assert arguments["save_steps"] == 25
    assert arguments["save_total_limit"] == 2
