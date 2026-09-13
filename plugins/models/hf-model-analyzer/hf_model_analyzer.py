"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 hf_model_analyzer.py                                           │
│  Module: hf_model_analyzer                                         │
│  Role: Canonical model.analyzer.v1 implementation and endpoint.    │
│                                                                     │
│  模块职责：模型分析能力的唯一实现与标准 DirectPluginRuntime 入口。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CAPABILITY_ID = "model.analyzer.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"
_BYTES_PER_PARAMETER = {
    "fp32": 4.0,
    "fp16": 2.0,
    "bf16": 2.0,
    "fp8": 1.0,
    "int8": 1.0,
    "int4": 0.5,
}
_PARAMETER_PATTERN = re.compile(
    r"(?<![a-z0-9])(\d+(?:\.\d+)?)\s*([bkmg])(?![a-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """Typed result consumed by the standard direct runtime."""

    value: bytes
    type_url: str


@dataclass(frozen=True)
class VramEstimate:
    """Backward-compatible aggregate estimates in gigabytes."""

    train_gb: float
    infer_gb: float


def _parameter_count_from_name(model_id: str) -> int | None:
    """Read a parameter hint such as ``7B`` from a model identity."""

    match = _PARAMETER_PATTERN.search(model_id.lower())
    if match is None:
        return None
    multiplier = {
        "k": 1_000,
        "m": 1_000_000,
        "g": 1_000_000_000,
        "b": 1_000_000_000,
    }
    return int(float(match.group(1)) * multiplier[match.group(2).lower()])


def _model_family(model_id: str) -> str | None:
    """Return a stable best-effort model-family projection."""

    name = model_id.strip().split("/", 1)[-1]
    if not name:
        return None
    family = re.split(r"[-_\d]", name, maxsplit=1)[0].lower()
    return family or None


class HfModelAnalyzer:
    """Analyze model identity, memory bounds, and shard integrity."""

    plugin_id = "cyrene.models.hf-analyzer"
    version = "0.3.0"
    capabilities = (CAPABILITY_ID,)

    def __init__(self, default_context_length: int = 2048) -> None:
        self.default_context_length = default_context_length

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
        """Dispatch one typed JSON request through DirectPluginRuntime."""

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        handlers = {
            "analyze": self.analyze,
            "validate_shards": self.validate_shards,
            "compute_sha256": self.compute_sha256,
        }
        handler = handlers.get(action)
        if handler is None:
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        expected_type_url = f"{TYPE_PREFIX}.{action}.request"
        if request_type_url != expected_type_url:
            return (
                False,
                f"INVALID_REQUEST: request_type_url must be {expected_type_url}",
            )
        if stream_results:
            return (
                False,
                "METHOD_NOT_SUPPORTED: model analyzer methods are not streaming",
            )
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        try:
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict):
                return False, "INVALID_REQUEST: request must be an object"
            if action == "analyze":
                result = handler(
                    request.get("model_info", {}),
                    request.get("workload_intent", {}),
                )
            elif action == "validate_shards":
                result = handler(
                    request.get("model_directory"),
                    request.get("verify_checksums", False),
                )
            else:
                result = handler(
                    request.get("file_path"),
                    request.get("chunk_size", 65536),
                )
        except (
            OSError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            return False, f"INVALID_REQUEST: {exc}"
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        return True, TypedPayload(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            f"{TYPE_PREFIX}.{action}.response",
        )

    def estimate_vram(
        self,
        params_billion: float,
        context_length: int = 2048,
        weight_precision: str = "fp16",
        quantization: str | None = None,
        workload_type: str = "infer",
    ) -> VramEstimate:
        """Calculate backward-compatible aggregate memory estimates."""

        del workload_type
        num_params = max(params_billion, 0.1)
        ctx = max(context_length, 512)
        bytes_per_param = _BYTES_PER_PARAMETER.get(weight_precision.lower(), 2.0)
        if quantization in {"awq", "gptq", "bitsandbytes-4bit"}:
            bytes_per_param = min(bytes_per_param, 0.55)
        elif quantization in {"bitsandbytes-8bit", "smoothquant"}:
            bytes_per_param = min(bytes_per_param, 1.05)

        base_weights_gb = num_params * bytes_per_param
        kv_cache_gb = (ctx / 2048.0) * (num_params / 7.0) * 0.5
        cuda_overhead_gb = 0.8
        infer_gb = round(base_weights_gb + kv_cache_gb + cuda_overhead_gb, 2)
        train_gb = round(
            num_params * bytes_per_param * 2.0
            + num_params * 4.0 * 2.0
            + (ctx / 2048.0) * (num_params / 7.0) * 2.0
            + cuda_overhead_gb,
            2,
        )
        return VramEstimate(
            train_gb=max(train_gb, 1.0),
            infer_gb=max(infer_gb, 0.5),
        )

    def analyze(
        self,
        model_info: dict[str, Any],
        workload_intent: dict[str, Any],
    ) -> dict[str, Any]:
        """Return Product-neutral model facts and bounded memory evidence."""

        if not isinstance(model_info, dict) or not isinstance(workload_intent, dict):
            raise TypeError("model_info and workload_intent must be objects")
        model_id = str(model_info.get("id") or model_info.get("name") or "unknown")
        raw_parameters = model_info.get("parameter_count", model_info.get("params"))
        if raw_parameters is None and model_info.get("params_billion") is not None:
            raw_parameters = float(model_info["params_billion"]) * 1_000_000_000
        try:
            parameter_count = (
                int(raw_parameters) if raw_parameters is not None else None
            )
        except (TypeError, ValueError, OverflowError):
            parameter_count = None
        if parameter_count is None:
            parameter_count = _parameter_count_from_name(model_id)
        if parameter_count is not None and parameter_count <= 0:
            parameter_count = None

        precision = str(model_info.get("weight_precision") or "fp16").lower()
        execution_kind = str(workload_intent.get("kind") or "infer").lower()
        evidence = [f"model_id={model_id}", f"precision={precision}"]
        vram_estimate: dict[str, Any] | None = None
        tensor_parallelism: int | None = None
        estimated_vram_gb: float | None = None

        if parameter_count is not None and precision in _BYTES_PER_PARAMETER:
            weights = int(parameter_count * _BYTES_PER_PARAMETER[precision])
            activation = model_info.get("activation_memory_bytes")
            if (
                isinstance(activation, bool)
                or not isinstance(activation, int)
                or activation < 0
            ):
                activation = max(512 * 1024 * 1024, weights // 8)
                evidence.append("activation_memory=estimated")
            else:
                evidence.append("activation_memory=provided")
            if execution_kind == "train":
                lower = weights * 4 + activation
                upper = lower + max(1024 * 1024 * 1024, weights // 2)
                uncertainty = "activation, optimizer implementation, and parallelism can change the upper bound"
            else:
                lower = weights
                upper = weights + activation
                uncertainty = (
                    "runtime cache and request shape can change the upper bound"
                )
            accelerator_memory = model_info.get("accelerator_memory_bytes")
            if (
                not isinstance(accelerator_memory, bool)
                and isinstance(accelerator_memory, int)
                and accelerator_memory > 0
            ):
                tensor_parallelism = max(1, math.ceil(upper / accelerator_memory))
                evidence.append(
                    f"tensor_parallelism_recommendation={tensor_parallelism}"
                )
            context_length = model_info.get("context_length")
            if isinstance(context_length, bool) or not isinstance(context_length, int):
                context_length = self.default_context_length
            legacy_estimate = self.estimate_vram(
                parameter_count / 1_000_000_000,
                context_length=context_length,
                weight_precision=precision,
                quantization=model_info.get("quantization"),
                workload_type=execution_kind,
            )
            estimated_vram_gb = upper / (1024**3)
            vram_estimate = {
                "lower_bytes": lower,
                "upper_bytes": upper,
                "confidence": "estimated",
                "uncertainty": uncertainty,
                "train_gb": legacy_estimate.train_gb,
                "infer_gb": legacy_estimate.infer_gb,
            }
            evidence.append(f"parameter_count={parameter_count}")
        else:
            evidence.append("parameter_or_precision=unknown")

        return {
            "model_id": model_id,
            "model_family": _model_family(model_id),
            "parameter_count": parameter_count,
            "params_billion": (
                None if parameter_count is None else parameter_count / 1_000_000_000
            ),
            "weight_precision": precision,
            "estimated_vram_gb": estimated_vram_gb,
            "vram_estimate": vram_estimate,
            "tensor_parallelism_recommendation": tensor_parallelism,
            "evidence": evidence,
        }

    @staticmethod
    def compute_sha256(file_path: str, chunk_size: int = 65536) -> str:
        """Compute a prefixed SHA-256 digest."""

        if not isinstance(file_path, str) or not file_path:
            raise ValueError("file_path must be non-empty text")
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or chunk_size < 1
        ):
            raise ValueError("chunk_size must be a positive integer")
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                hasher.update(chunk)
        return f"sha256:{hasher.hexdigest()}"

    def validate_shards(
        self,
        model_directory: str,
        verify_checksums: bool = False,
    ) -> dict[str, Any]:
        """Validate a Hugging Face shard index and its referenced files."""

        if not isinstance(model_directory, str) or not model_directory:
            raise ValueError("model_directory must be non-empty text")
        if not isinstance(verify_checksums, bool):
            raise TypeError("verify_checksums must be boolean")
        model_path = Path(model_directory)
        if not model_path.is_dir():
            return {
                "valid": False,
                "error": f"Model directory not found: {model_directory}",
            }

        index_file = next(
            (
                path
                for path in (
                    model_path / "model.safetensors.index.json",
                    model_path / "pytorch_model.bin.index.json",
                )
                if path.is_file()
            ),
            None,
        )
        errors: list[str] = []
        checked_files: list[dict[str, Any]] = []
        if index_file is not None:
            try:
                index_data = json.loads(index_file.read_text(encoding="utf-8"))
                if not isinstance(index_data, dict):
                    raise TypeError("Shard index root must be an object")
                weight_map = index_data.get("weight_map", {})
                if not isinstance(weight_map, dict) or not all(
                    isinstance(filename, str) for filename in weight_map.values()
                ):
                    raise TypeError("Shard index weight_map must contain file names")
                required_files = sorted(set(weight_map.values()))
                for filename in required_files:
                    file_path = model_path / filename
                    if not file_path.is_file():
                        errors.append(f"Missing required shard: {filename}")
                        continue
                    file_info: dict[str, Any] = {
                        "filename": filename,
                        "size_bytes": file_path.stat().st_size,
                    }
                    if verify_checksums:
                        file_info["sha256"] = self.compute_sha256(str(file_path))
                    checked_files.append(file_info)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
                return {"valid": False, "error": f"Failed to parse shard index: {exc}"}
        else:
            single_weights = list(model_path.glob("*.safetensors")) + list(
                model_path.glob("*.bin")
            )
            if not single_weights:
                errors.append(
                    "No weight files (.safetensors or .bin) found in model directory"
                )
            for file_path in single_weights:
                file_info = {
                    "filename": file_path.name,
                    "size_bytes": file_path.stat().st_size,
                }
                if verify_checksums:
                    file_info["sha256"] = self.compute_sha256(str(file_path))
                checked_files.append(file_info)

        return {
            "valid": not errors,
            "index_present": index_file is not None,
            "shards_count": len(checked_files),
            "files": checked_files,
            "errors": errors,
        }


__all__ = ["CAPABILITY_ID", "INTERFACE_VERSION", "HfModelAnalyzer", "VramEstimate"]
