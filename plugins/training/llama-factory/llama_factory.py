"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 llama_factory.py                                                │
│  Module: llama_factory                                              │
│  Role: Canonical training.llama-factory.v1 implementation.          │
│                                                                     │
│  模块职责：LLaMA-Factory 训练能力的唯一实现：检视、编译与事件解析。        │
└─────────────────────────────────────────────────────────────────────┘

The Plugin owns the trainer launch contract and never owns Product job state.
``compile`` materializes a complete LLaMA-Factory run directory (trainer config
plus dataset index) and returns an executor-agnostic launch description; the
Kernel executor remains responsible for devices, mounts, and process lifetime.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from pathlib import Path
from typing import Any

CAPABILITY_ID = "training.llama-factory.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"
DATASET_NAME = "cyrene"
DEFAULT_ENTRYPOINT = "llamafactory-cli"
FORBIDDEN_ENV = frozenset(
    {
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
        "HIP_VISIBLE_DEVICES",
        "ROCR_VISIBLE_DEVICES",
    }
)


class TypedPayload:
    """Typed response consumed by DirectPluginRuntime.

        中文:由 DirectPluginRuntime 使用的有类型响应。"""

    def __init__(self, value: bytes, type_url: str) -> None:
        self.value = value
        self.type_url = type_url


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _entrypoint() -> list[str]:
    """Resolve the trainer entrypoint the operator installed in the runtime.

        中文:解析操作者在运行时中安装的训练器入口。"""

    configured = os.environ.get("CYRENE_LLAMA_FACTORY_ENTRYPOINT", "").strip()
    if configured:
        return shlex.split(configured)
    python = os.environ.get("CYRENE_LLAMA_FACTORY_PYTHON", "").strip()
    if python:
        return [python, "-m", "llamafactory.cli"]
    return [DEFAULT_ENTRYPOINT]


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _positive_int(value: Any, name: str, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _text(value: Any, name: str, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _trainer_arguments(spec: dict[str, Any], work_dir: Path, dataset_path: str) -> dict[str, Any]:
    """Project the Product training intent onto LLaMA-Factory arguments.

    The Plugin writes the dataset index and the trainer config into the run
    directory so the launch stays a single deterministic command with no hidden
    environment authority.

        中文:将 Product 训练意图映射为 LLaMA-Factory 参数。

        中文:Plugin 会将数据集索引和训练器配置写入运行目录,使启动保持为一条确定性命令,不依赖隐藏的环境权限。
    """

    model = _mapping(spec.get("model"), "model")
    lora = _mapping(spec.get("lora") or {}, "lora")
    hyperparams = _mapping(spec.get("hyperparams") or {}, "hyperparams")
    extras = _mapping(spec.get("extra") or {}, "extra")
    checkpoint = _mapping(spec.get("checkpoint") or {}, "checkpoint")
    output_dir = _text(spec.get("output_dir"), "output_dir")
    if output_dir is None:
        raise ValueError("output_dir is required")

    dataset_info = {
        DATASET_NAME: {
            "file_name": dataset_path,
            "formatting": "alpaca",
            "columns": {"prompt": "instruction", "query": "input", "response": "output"},
        }
    }
    (work_dir / "dataset_info.json").write_text(
        json.dumps(dataset_info, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    arguments: dict[str, Any] = {
        "model_name_or_path": _text(model.get("path"), "model.path"),
        "dataset_dir": str(work_dir),
        "dataset": DATASET_NAME,
        "stage": _text(spec.get("stage"), "stage", "sft"),
        "finetuning_type": _text(spec.get("finetuning_type"), "finetuning_type", "lora"),
        "output_dir": output_dir,
        "overwrite_output_dir": True,
        "do_train": True,
        "report_to": "none",
        "template": _text(extras.get("template"), "extra.template", "default"),
        "cutoff_len": _positive_int(extras.get("max_length"), "extra.max_length", 512),
        "per_device_train_batch_size": _positive_int(
            extras.get("per_device_batch_size"), "extra.per_device_batch_size", 1
        ),
        "gradient_accumulation_steps": _positive_int(
            extras.get("gradient_accumulation_steps"), "extra.gradient_accumulation_steps", 1
        ),
        "learning_rate": hyperparams.get("learning_rate", 2e-4),
        "num_train_epochs": hyperparams.get("epochs", 1.0),
        "lr_scheduler_type": "cosine",
        "logging_steps": 1,
        "save_strategy": "no",
        "bf16": True,
    }
    if arguments["finetuning_type"] == "lora":
        arguments["lora_rank"] = _positive_int(lora.get("r"), "lora.r", 64)
        arguments["lora_alpha"] = _positive_int(lora.get("lora_alpha"), "lora.lora_alpha", 16)
        arguments["lora_dropout"] = lora.get("lora_dropout", 0.05)
        if lora.get("target_modules"):
            arguments["lora_target"] = ",".join(str(item) for item in lora["target_modules"])
    max_steps = _positive_int(extras.get("max_steps"), "extra.max_steps")
    if max_steps is not None:
        arguments["max_steps"] = max_steps
    max_train_samples = _positive_int(extras.get("max_train_samples"), "extra.max_train_samples")
    if max_train_samples is not None:
        arguments["max_samples"] = max_train_samples
    resume_from = _text(checkpoint.get("resume_from"), "checkpoint.resume_from")
    if resume_from is not None:
        arguments["resume_from_checkpoint"] = resume_from
    save_steps = _positive_int(checkpoint.get("save_steps"), "checkpoint.save_steps")
    if save_steps is not None:
        arguments["save_strategy"] = "steps"
        arguments["save_steps"] = save_steps
    save_total_limit = _positive_int(checkpoint.get("save_total_limit"), "checkpoint.save_total_limit")
    if save_total_limit is not None:
        arguments["save_total_limit"] = save_total_limit
    overrides = _mapping(extras.get("llamafactory_args") or {}, "extra.llamafactory_args")
    for key, value in overrides.items():
        if key in {"model_name_or_path", "dataset_dir", "dataset", "output_dir"}:
            raise ValueError(f"extra.llamafactory_args must not override {key}")
        arguments[key] = value
    if FORBIDDEN_ENV.intersection({str(key) for key in arguments}):
        raise ValueError("trainer arguments must not assign machine devices")
    return arguments


class LlamaFactoryTrainingPlugin:
    """Stateless LLaMA-Factory launch-contract implementation.

        中文:无状态的 LLaMA-Factory 启动契约实现。"""

    plugin_id = "cyrene.training.llama-factory"
    version = "0.1.0"
    capabilities = (CAPABILITY_ID,)

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
        """Dispatch one typed trainer request.

            中文:分发一个有类型的训练器请求。"""

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action not in {"inspect", "compile", "parse_event"}:
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        expected_type_url = f"{TYPE_PREFIX}.{action}.request"
        if request_type_url != expected_type_url:
            return False, f"INVALID_REQUEST: request_type_url must be {expected_type_url}"
        if stream_results:
            return False, "METHOD_NOT_SUPPORTED: trainer methods are not streaming"
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        try:
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict):
                raise TypeError("request must be an object")
            if action == "inspect":
                result = self.inspect()
            elif action == "compile":
                result = self.compile(_mapping(request.get("spec"), "spec"))
            else:
                result = self.parse_event(_text(request.get("line"), "line"))
        except (TypeError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
            return False, f"INVALID_REQUEST: {exc}"
        return True, TypedPayload(
            value=_canonical_json(result),
            type_url=f"{TYPE_PREFIX}.{action}.response",
        )

    def inspect(self) -> dict[str, Any]:
        """Report the declared trainer surface without probing devices.

            中文:报告已声明的训练器接口,不探测设备。"""

        entrypoint = _entrypoint()
        return {
            "engine": "llamafactory",
            "available": bool(entrypoint),
            "version": os.environ.get("CYRENE_LLAMA_FACTORY_VERSION"),
            "supported_strategies": ["single", "ddp", "fsdp", "deepspeed", "torchrun"],
            "supported_finetune_types": ["lora", "full", "freeze", "oft"],
            "notes": ["launch contract compiled by the Plugins-owned trainer"],
        }

    def compile(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Materialize the run directory and return the launch description.

            中文:创建运行目录并返回启动说明。"""

        output_dir = _text(spec.get("output_dir"), "output_dir")
        if output_dir is None:
            raise ValueError("output_dir is required")
        work_dir = Path(output_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        dataset = _mapping(spec.get("dataset"), "dataset")
        dataset_path = _text(dataset.get("path"), "dataset.path")
        if dataset_path is None:
            raise ValueError("dataset.path is required")
        arguments = _trainer_arguments(spec, work_dir, dataset_path)
        config_path = work_dir / "train_config.json"
        config_path.write_text(
            json.dumps(arguments, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        distributed = _mapping(spec.get("distributed") or {}, "distributed")
        checkpoint = _mapping(spec.get("checkpoint") or {}, "checkpoint")
        environment = _mapping(spec.get("environment") or {}, "environment")
        launch_env = {
            str(key): str(value)
            for key, value in _mapping(environment.get("env") or {}, "environment.env").items()
        }
        if FORBIDDEN_ENV.intersection(launch_env):
            raise ValueError("environment.env must not assign machine devices")
        gpu_count = _positive_int(distributed.get("gpu_count"), "distributed.gpu_count", 1) or 1
        world_size = _positive_int(distributed.get("world_size"), "distributed.world_size", gpu_count)
        return {
            "launch": {
                "engine": "llamafactory",
                "argv": [*_entrypoint(), "train", str(config_path)],
                "work_dir": str(work_dir),
                "cwd": str(work_dir),
                "distributed": dict(distributed) or {"gpu_count": 1, "world_size": 1},
                "checkpoint": dict(checkpoint),
                "launch_kind": "direct",
                "env": launch_env,
                "resources": {
                    "gpu_count": gpu_count,
                    "world_size": world_size,
                    "nnodes": _positive_int(distributed.get("nnodes"), "distributed.nnodes", 1),
                    "nproc_per_node": _positive_int(
                        distributed.get("nproc_per_node"), "distributed.nproc_per_node", gpu_count
                    ),
                    "gpu_memory_gb": 0.0,
                },
                "mounts": [
                    {
                        "source": dataset_path,
                        "target": "/input/dataset",
                        "kind": "bind",
                        "read_only": True,
                    },
                    {
                        "source": str(work_dir),
                        "target": "/output",
                        "kind": "bind",
                        "read_only": False,
                    },
                ],
                "output_layout": {"root": ""},
                "spec_artifact_path": str(config_path),
                "extra": {
                    "config_digest": "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest()
                },
            }
        }

    def parse_event(self, line: str) -> dict[str, Any]:
        """Classify one trainer output line without owning run state.

            中文:对一行训练器输出进行分类,不持有运行状态。"""

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and ("loss" in payload or "eval_loss" in payload):
            return {"kind": "progress", "message": line, "payload": payload, "raw": line}
        return {"kind": "log", "message": line, "raw": line}
