"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 compat_rules.py                                                │
│  Module: compat_rules                                              │
│  Role: Canonical compatibility.evaluator.v1 implementation.       │
│                                                                     │
│  模块职责：硬件、运行时、精度与内存兼容性的唯一插件实现及直连接口。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

CAPABILITY_ID = "compatibility.evaluator.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """Typed result consumed by the standard direct runtime.

        中文：标准 direct runtime 使用的类型化结果。
    """

    value: bytes
    type_url: str


@dataclass
class RuleDecision:
    """One Product-neutral compatibility decision.

        中文：一项不包含 Product 语义的兼容性决策。
    """

    subject: str
    verdict: str
    rationale: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON contract representation.

            中文：返回 JSON contract 表示。
        """

        return {
            "subject": self.subject,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "evidence": list(self.evidence),
        }


@dataclass
class EvaluationReport:
    """Consolidated compatibility report with stable issue evidence.

        中文：包含稳定 issue 证据的汇总兼容性报告。
    """

    compatible: bool
    summary: str
    decisions: list[RuleDecision] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON contract representation.

            中文：返回 JSON contract 表示。
        """

        return {
            "compatible": self.compatible,
            "summary": self.summary,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "issues": list(self.issues),
            "evidence": dict(self.evidence),
        }


def _version(value: str) -> tuple[int, int, int]:
    """Normalize a driver or runtime version for ordered comparison.

        中文：规范化驱动程序或 Runtime 版本，以便进行有序比较。
    """

    values = re.findall(r"\d+", value)
    return tuple(int(values[index]) if index < len(values) else 0 for index in range(3))


def _issue(
    code: str,
    severity: str,
    message: str,
    source: str,
    *,
    evidence: list[str] | None = None,
    remediation: str | None = None,
) -> dict[str, Any]:
    """Build one stable Product-neutral preflight issue.

        中文：构造一个稳定且不包含 Product 语义的 preflight issue。
    """

    return {
        "code": code,
        "severity": severity,
        "message": message,
        "source": source,
        "evidence": evidence or [],
        "remediation": remediation,
    }


class CompatibilityRuleEvaluator:
    """Evaluate hardware, model, environment, and workload compatibility.

        中文：评估硬件、模型、环境和工作负载的兼容性。
    """

    plugin_id = "cyrene.policy.compat-rules"
    version = "0.2.0"
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
        """Dispatch a typed evaluate request through DirectPluginRuntime.

            中文：通过 DirectPluginRuntime 分派类型化 evaluate 请求。
        """

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action != "evaluate":
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
                "METHOD_NOT_SUPPORTED: compatibility evaluation is not streaming",
            )
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        try:
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict):
                return False, "INVALID_REQUEST: request must be an object"
            hardware = request.get("hardware_facts")
            model = request.get("model_spec")
            workload = request.get("workload")
            if hardware is not None and not isinstance(hardware, dict):
                raise TypeError("hardware_facts must be an object or null")
            if not isinstance(model, dict) or not isinstance(workload, dict):
                raise TypeError("model_spec and workload must be objects")
            report = self.evaluate(hardware, model, workload)
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return False, f"INVALID_REQUEST: {exc}"
        if cancellation is not None and cancellation.is_cancelled():
            return False, "CANCELLED: operation cancelled"
        return True, TypedPayload(
            json.dumps(
                report.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            f"{TYPE_PREFIX}.{action}.response",
        )

    def evaluate(
        self,
        hardware_facts: dict[str, Any] | None,
        model_spec: dict[str, Any],
        workload: dict[str, Any],
    ) -> EvaluationReport:
        """Evaluate either the canonical inventory shape or the legacy GPU projection.

            中文：评估规范清单结构或旧版 GPU 投影。
        """

        if (
            hardware_facts is None
            or "accelerators" in hardware_facts
            or "vram_estimate" in model_spec
            or "environment" in workload
        ):
            return self._evaluate_inventory(hardware_facts, model_spec, workload)
        return self._evaluate_legacy(hardware_facts, model_spec, workload)

    def _evaluate_inventory(
        self,
        hardware: dict[str, Any] | None,
        model: dict[str, Any],
        workload: dict[str, Any],
    ) -> EvaluationReport:
        """Evaluate the canonical Node resource inventory projection.

            中文：评估规范 Node 资源清单投影。
        """

        issues: list[dict[str, Any]] = []
        decisions: list[RuleDecision] = []
        environment = workload.get("environment")
        if not isinstance(environment, dict):
            environment = {}
        evidence = {
            "model": model,
            "execution_kind": str(workload.get("execution_kind") or "unknown"),
        }
        if not environment.get("identity"):
            issues.append(
                _issue(
                    "environment.identity.missing",
                    "unknown",
                    "Resolved environment identity is unavailable.",
                    "environment",
                    remediation="Resolve a known-good EnvironmentLock before execution.",
                )
            )
        if hardware is None:
            issues.append(
                _issue(
                    "hardware.inventory.missing",
                    "unknown",
                    "Node resource inventory evidence is unavailable.",
                    "node-resource-inventory",
                    remediation="Request a current Node resource inventory snapshot.",
                )
            )
            return self._report(issues, decisions, evidence)
        evidence["hardware"] = hardware

        expected_runtime = environment.get("accelerator_runtime")
        actual_runtime = hardware.get("accelerator_runtime")
        if expected_runtime:
            if not actual_runtime:
                issues.append(
                    _issue(
                        "runtime.accelerator.unknown",
                        "unknown",
                        "Node accelerator runtime version is unavailable.",
                        "node-resource-inventory",
                        remediation="Refresh the Node resource inventory with runtime evidence.",
                    )
                )
            elif actual_runtime != expected_runtime:
                issues.append(
                    _issue(
                        "runtime.accelerator.mismatch",
                        "blocked",
                        "Resolved environment and Node accelerator runtimes are incompatible.",
                        "environment/node-resource-inventory",
                        evidence=[
                            f"environment_runtime={expected_runtime}",
                            f"node_runtime={actual_runtime}",
                        ],
                        remediation="Use an environment lock compatible with the selected Node runtime.",
                    )
                )

        minimum_driver = environment.get("minimum_driver")
        driver_version = hardware.get("driver_version")
        if minimum_driver:
            if not driver_version:
                issues.append(
                    _issue(
                        "runtime.driver.unknown",
                        "unknown",
                        "Node driver version is unavailable.",
                        "node-resource-inventory",
                        remediation="Refresh Node driver evidence before execution.",
                    )
                )
            elif _version(str(driver_version)) < _version(str(minimum_driver)):
                issues.append(
                    _issue(
                        "runtime.driver.too_old",
                        "blocked",
                        "Node driver version does not meet the environment minimum.",
                        "environment/node-resource-inventory",
                        evidence=[
                            f"minimum_driver={minimum_driver}",
                            f"node_driver={driver_version}",
                        ],
                        remediation="Upgrade the Node driver or choose a compatible EnvironmentLock.",
                    )
                )

        accelerators = hardware.get("accelerators")
        if not isinstance(accelerators, list):
            accelerators = []
        precision = str(
            model.get("precision") or model.get("weight_precision") or "fp16"
        )
        precision_token = f"precision:{precision}"
        feature_sets = [
            set(item.get("features", []))
            for item in accelerators
            if isinstance(item, dict)
        ]
        if not feature_sets:
            issues.append(
                _issue(
                    "accelerator.precision.unknown",
                    "unknown",
                    f"Node inventory does not prove {precision} support.",
                    "node-resource-inventory",
                    remediation="Refresh accelerator feature evidence or choose a proven precision.",
                )
            )
        elif not any(
            precision_token in features or precision in features
            for features in feature_sets
        ):
            issues.append(
                _issue(
                    "accelerator.precision.unsupported",
                    "blocked",
                    f"No selected accelerator supports {precision}.",
                    "node-resource-inventory",
                    evidence=[
                        f"precision={precision}",
                        f"source_ref={hardware.get('source_ref', 'unknown')}",
                    ],
                    remediation="Choose a supported precision or a compatible accelerator.",
                )
            )

        estimate = model.get("vram_estimate")
        if not isinstance(estimate, dict):
            issues.append(
                _issue(
                    "model.vram.unknown",
                    "unknown",
                    "Model memory estimate is unavailable.",
                    "model-analyzer",
                    remediation="Provide model evidence or run a bounded dry run when safe.",
                )
            )
        else:
            lower = estimate.get("lower_bytes")
            upper = estimate.get("upper_bytes")
            available = sum(
                int(item.get("allocatable_memory_bytes") or 0)
                for item in accelerators
                if isinstance(item, dict)
            )
            if not isinstance(lower, int) or not isinstance(upper, int):
                issues.append(
                    _issue(
                        "model.vram.unknown",
                        "unknown",
                        "Model memory estimate is unavailable.",
                        "model-analyzer",
                    )
                )
            elif available < lower:
                issues.append(
                    _issue(
                        "accelerator.memory.insufficient",
                        "blocked",
                        "Available accelerator memory is below the estimate lower bound.",
                        "model-analyzer/node-resource-inventory",
                        evidence=[
                            f"available_bytes={available}",
                            f"estimated_lower_bytes={lower}",
                            f"estimated_upper_bytes={upper}",
                        ],
                        remediation="Use a smaller model, lower-memory precision, or more accelerator memory.",
                    )
                )
            elif available < upper:
                issues.append(
                    _issue(
                        "accelerator.memory.range_overlap",
                        "warning",
                        "Available accelerator memory falls inside the estimate uncertainty range.",
                        "model-analyzer/node-resource-inventory",
                        evidence=[
                            f"available_bytes={available}",
                            f"estimated_lower_bytes={lower}",
                            f"estimated_upper_bytes={upper}",
                        ],
                        remediation="Use a bounded dry run to measure peak memory before a longer execution.",
                    )
                )

        for item in issues:
            severity = str(item["severity"])
            decisions.append(
                RuleDecision(
                    subject=str(item["code"]),
                    verdict="rejected" if severity == "blocked" else severity,
                    rationale=str(item["message"]),
                    evidence=list(item.get("evidence", [])),
                )
            )
        if not decisions:
            decisions.append(
                RuleDecision(
                    subject="compatibility",
                    verdict="chosen",
                    rationale="All compatibility rules passed.",
                )
            )
        return self._report(issues, decisions, evidence)

    def _evaluate_legacy(
        self,
        hardware_facts: dict[str, Any],
        model_spec: dict[str, Any],
        workload: dict[str, Any],
    ) -> EvaluationReport:
        """Preserve the previously published flat GPU request behavior.

            中文：保留先前发布的扁平 GPU 请求行为。
        """

        decisions: list[RuleDecision] = []
        gpus = hardware_facts.get("gpus", [])
        total_vram_gb = sum(
            float(gpu.get("vram_gb", 0)) * int(gpu.get("count", 1)) for gpu in gpus
        )
        required_vram_gb = float(
            model_spec.get("required_vram_gb")
            or model_spec.get("estimated_vram_gb", 14.0)
        )
        vram_ok = total_vram_gb >= required_vram_gb if total_vram_gb > 0 else True
        decisions.append(
            RuleDecision(
                subject="vram_sufficiency",
                verdict="chosen" if vram_ok else "rejected",
                rationale=(
                    f"Available VRAM ({total_vram_gb:.1f} GB) meets required headroom ({required_vram_gb:.1f} GB)."
                    if vram_ok
                    else f"Insufficient VRAM: {total_vram_gb:.1f} GB available < {required_vram_gb:.1f} GB required."
                ),
                evidence=[
                    f"total_vram_gb={total_vram_gb}",
                    f"required_vram_gb={required_vram_gb}",
                ],
            )
        )

        compute_capabilities = [
            float(gpu.get("compute_capability", 0))
            for gpu in gpus
            if gpu.get("compute_capability")
        ]
        max_compute_capability = (
            max(compute_capabilities) if compute_capabilities else 8.0
        )
        precision = str(model_spec.get("weight_precision", "fp16")).lower()
        if precision == "fp8":
            fp8_ok = max_compute_capability >= 8.9 or bool(
                hardware_facts.get("supports_fp8", False)
            )
            decisions.append(
                RuleDecision(
                    subject="fp8_compute_capability",
                    verdict="chosen" if fp8_ok else "rejected",
                    rationale=(
                        f"Compute capability {max_compute_capability} supports native FP8 Tensor Cores."
                        if fp8_ok
                        else f"FP8 requires compute capability >= 8.9 (detected: {max_compute_capability})."
                    ),
                    evidence=[f"max_cc={max_compute_capability}"],
                )
            )
        elif precision == "bf16":
            bf16_ok = max_compute_capability >= 8.0 or bool(
                hardware_facts.get("supports_bf16", False)
            )
            decisions.append(
                RuleDecision(
                    subject="bf16_compute_capability",
                    verdict="chosen" if bf16_ok else "rejected",
                    rationale=(
                        f"Compute capability {max_compute_capability} supports BF16 execution."
                        if bf16_ok
                        else f"BF16 requires compute capability >= 8.0 (detected: {max_compute_capability})."
                    ),
                    evidence=[f"max_cc={max_compute_capability}"],
                )
            )

        cuda_version = str(
            hardware_facts.get("cuda_version")
            or hardware_facts.get("cuda_max_supported", "12.4")
        )
        try:
            cuda_number = float(cuda_version.split()[0])
        except (ValueError, IndexError):
            cuda_number = 12.0
        if workload.get("engine") in {"vllm", "sglang"}:
            cuda_ok = cuda_number >= 11.8
            decisions.append(
                RuleDecision(
                    subject="engine_cuda_compatibility",
                    verdict="chosen" if cuda_ok else "rejected",
                    rationale=(
                        f"CUDA {cuda_version} meets minimum requirement (>= 11.8)."
                        if cuda_ok
                        else f"Inference engine requires CUDA >= 11.8 (detected: {cuda_version})."
                    ),
                    evidence=[f"cuda_version={cuda_version}"],
                )
            )
        return EvaluationReport(
            compatible=all(decision.verdict == "chosen" for decision in decisions),
            summary=(
                "All compatibility rules passed."
                if all(decision.verdict == "chosen" for decision in decisions)
                else "Compatibility checks encountered rejections."
            ),
            decisions=decisions,
            evidence={
                "hardware": hardware_facts,
                "model": model_spec,
                "workload": workload,
            },
        )

    @staticmethod
    def _report(
        issues: list[dict[str, Any]],
        decisions: list[RuleDecision],
        evidence: dict[str, Any],
    ) -> EvaluationReport:
        """Finalize one canonical inventory report.

            中文：完成一份规范资源清单报告。
        """

        compatible = not any(issue["severity"] == "blocked" for issue in issues)
        summary = (
            "Compatibility checks encountered blocking issues."
            if not compatible
            else "Compatibility checks completed without blocking issues."
        )
        return EvaluationReport(compatible, summary, decisions, issues, evidence)


__all__ = [
    "CAPABILITY_ID",
    "INTERFACE_VERSION",
    "CompatibilityRuleEvaluator",
    "EvaluationReport",
    "RuleDecision",
]
