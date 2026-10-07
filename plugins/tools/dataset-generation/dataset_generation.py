"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 dataset_generation.py                                           │
│  Module: dataset_generation                                         │
│  Role: SFT bundle builder and controlled grounded QA generator.    │
│                                                                     │
│  模块职责：构建带沿袭的 SFT bundle，并按配置生成 grounded QA 草稿。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import math
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cyrene_model_provider_contracts import (
    CAPABILITY_ID as MODEL_CAPABILITY_ID,
)
from cyrene_model_provider_contracts import (
    CHAT_COMPLETION_METHOD,
    CHAT_COMPLETION_REQUEST_TYPE_URL,
    ChatCompletionRequest,
    ChatMessage,
    ChatMessageRole,
    decode_chat_completion_response,
    encode_chat_completion_request,
)
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, DirectPluginError
from cyrene_plugin_runtime.configuration import read_environment_settings

CAPABILITY_ID = "dataset.generation.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"
SFT_PROFILE = "CYRENE_SFT_BUNDLE_V1"
SFT_PROFILE_VERSION = 1
SPLIT_ALGORITHM = "sha256-ranked-source-family-v1"
SAMPLE_ID_ALGORITHM = "sha256-grounded-qa-v1"
PROMPT_RECIPE_ID = "grounded-qa-v1"
PROMPT_RECIPE_VERSION = "1"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_SAMPLES = 50_000
MAX_BLOCKS = 10_000
MAX_BLOCK_TEXT_CHARS = 100_000
MAX_RESULT_DETAIL = 400
_ROW_METADATA_KEYS = {
    "sampleId",
    "sample_id",
    "sourceFamily",
    "source_family",
    "sourceFamilyId",
    "source_family_id",
    "conversationId",
    "conversation_id",
    "groupId",
    "group_id",
    "_acl",
    "acl",
    "_operatorAnnotation",
    "operatorAnnotation",
    "_operator_annotation",
    "_reviewNote",
    "reviewNote",
    "review_note",
    "_review_note",
    "allowedPrincipalRefs",
    "allowed_principal_refs",
}

_SYSTEM_PROMPT = (
    "Create one concise question and answer grounded only in the supplied source. "
    "Do not add facts that are absent from the source. Return exactly one JSON object "
    'with string keys "question" and "answer" and no other text.'
)


@dataclass(frozen=True, slots=True)
class TypedPayload:
    """One DirectPluginRuntime JSON response.

    中文:DirectPluginRuntime 使用的一份 JSON 类型化响应。
    """

    value: bytes
    type_url: str


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    """One activation-resolved model.provider.v1 endpoint.

    中文:一份在 capability activation 时解析的 model.provider.v1 endpoint。
    """

    binding_id: str
    model_endpoint: str
    model: str
    temperature: float = 0.0
    max_tokens_per_call: int = 512
    timeout_seconds: float = 30.0

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> GenerationConfig:
        """Read the provider endpoint from the standard Plugin binding config.

        中文:从标准 Plugin binding 配置中读取 Provider endpoint。
        """

        binding_id = settings.get("binding_id")
        if not binding_id:
            raise ValueError("binding_id is required for a provider binding")
        encoded = settings.get("config") or "{}"
        try:
            config = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ValueError(f"config must contain valid JSON: {error}") from error
        if not isinstance(config, Mapping):
            raise TypeError("config must be a JSON object")
        endpoint = _required_text(config.get("model_endpoint"), "config.model_endpoint")
        model = _required_text(config.get("model"), "config.model")
        temperature = _finite_number(
            config.get("temperature", 0.0),
            "config.temperature",
            minimum=0.0,
            maximum=2.0,
        )
        max_tokens = _integer(
            config.get("max_tokens_per_call", 512),
            "config.max_tokens_per_call",
            minimum=1,
            maximum=8192,
        )
        timeout = _finite_number(
            config.get("timeout_seconds", 30.0),
            "config.timeout_seconds",
            minimum=0.001,
            maximum=300.0,
        )
        return cls(
            binding_id=binding_id,
            model_endpoint=endpoint,
            model=model,
            temperature=temperature,
            max_tokens_per_call=max_tokens,
            timeout_seconds=timeout,
        )


class DatasetGenerationPlugin:
    """Build reviewed SFT bundles and explicitly requested QA drafts.

    中文:构建经审核的 SFT bundle，并按显式请求生成 QA 草稿。
    """

    plugin_id = "cyrene.tools.dataset-generation"
    version = "0.1.0"
    capabilities = (CAPABILITY_ID,)

    def __init__(self, config: GenerationConfig | None = None) -> None:
        self._config = config
        if self._config is None:
            settings = read_environment_settings()
            if settings is not None:
                self._config = GenerationConfig.from_settings(settings)

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
        """Validate and dispatch a non-streaming capability request.

        中文:校验并分派一个非流式 capability 请求。
        """

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action not in {"prepare_sft", "generate_qa"}:
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        expected_type_url = f"{TYPE_PREFIX}.{action}.request"
        if request_type_url != expected_type_url:
            return (
                False,
                f"INVALID_REQUEST: request_type_url must be {expected_type_url}",
            )
        if stream_results:
            return False, "METHOD_NOT_SUPPORTED: generation methods are not streaming"
        if _cancelled(cancellation):
            return False, "CANCELLED: operation cancelled"
        try:
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict):
                raise RequestError("request must be an object")
            if action == "prepare_sft":
                response = self.prepare_sft(request)
            else:
                response = self.generate_qa(request, cancellation=cancellation)
        except RequestError as error:
            return False, f"INVALID_REQUEST: {error}"
        except GenerationUnavailable as error:
            return False, f"UNAVAILABLE: {error}"
        except GenerationUpstreamError as error:
            return False, f"UNAVAILABLE: {error}"
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return False, f"INVALID_REQUEST: payload is not UTF-8 JSON: {error}"
        except (OSError, ValueError) as error:
            return False, f"INVALID_INPUT: {error}"
        if _cancelled(cancellation):
            return False, "CANCELLED: operation cancelled"
        encoded = _canonical_json(response)
        return True, TypedPayload(
            value=encoded, type_url=f"{TYPE_PREFIX}.{action}.response"
        )

    def prepare_sft(self, request: dict[str, Any]) -> dict[str, Any]:
        """Build deterministic split files and a provenance-preserving ZIP.

        The input document must represent a reviewed ContentRevision. Learned
        rows are projected from an explicit allowlist; review and policy fields
        remain in the sidecar or are rejected when placed inside learned content.

        中文:构建确定性切分文件及保留沿袭的 ZIP。输入文档必须对应已审核的
        ContentRevision。训练行只从允许字段投影；审核/策略信息写入旁路文件，若混入
        学习内容则拒绝。
        """

        _keys(
            request,
            required={
                "bundle_path",
                "result_path",
                "dataset_id",
                "content_revision_id",
                "processing_run_id",
                "mode",
            },
            optional={"blocks_path", "samples_path", "split"},
            field="request",
        )
        if ("blocks_path" in request) == ("samples_path" in request):
            raise RequestError("provide exactly one of blocks_path or samples_path")
        path_key = "blocks_path" if "blocks_path" in request else "samples_path"
        input_path = _path(request[path_key], path_key)
        bundle_path = _path(request["bundle_path"], "bundle_path")
        result_path = _path(request["result_path"], "result_path")
        dataset_id = _uuid(request["dataset_id"], "dataset_id")
        content_revision_id = _uuid(
            request["content_revision_id"], "content_revision_id"
        )
        processing_run_id = _uuid(request["processing_run_id"], "processing_run_id")
        mode = _enum_text(request["mode"], "mode", {"instruction", "conversation"})
        split = _split_config(request.get("split"))
        document = _read_json(input_path)
        if isinstance(document, dict) and "blocks" in document:
            blocks = _approved_blocks(document, content_revision_id)
            samples, warnings = _samples_from_blocks(blocks, content_revision_id, mode)
        else:
            samples = _manual_samples(document, content_revision_id, mode)
            warnings = []
        if not samples:
            raise RequestError("no approved SFT samples were supplied")
        if len(samples) > MAX_SAMPLES:
            raise RequestError(f"samples exceeds {MAX_SAMPLES} records")

        assignments = _assign_families(
            [sample["split_group_id"] for sample in samples], split
        )
        learned: dict[str, list[bytes]] = {
            name: [] for name in ("train", "validation", "test")
        }
        provenance: list[bytes] = []
        for sample in samples:
            split_name = assignments[sample["split_group_id"]]
            learned[split_name].append(_canonical_json(sample["content"]) + b"\n")
            sidecar = {
                "sample_id": sample["sample_id"],
                "source_family_id": sample["source_family_id"],
                "group_id": sample.get("group_id"),
                "origin": sample["origin"],
                "policy": sample["policy"],
                "citations": sample["citations"],
                "split": split_name,
                "dataset_id": dataset_id,
                "content_revision_id": content_revision_id,
                "processing_run_id": processing_run_id,
            }
            if sample.get("generation") is not None:
                sidecar["generation"] = sample["generation"]
            provenance.append(_canonical_json(sidecar) + b"\n")

        schema_fields = (
            ["instruction", "output"] if mode == "instruction" else ["conversations"]
        )
        has_input = (
            ["input" in sample["content"] for sample in samples]
            if mode == "instruction"
            else []
        )
        if has_input and any(has_input) and not all(has_input):
            raise RequestError(
                "instruction samples must either all include input or all omit it"
            )
        if mode == "instruction" and has_input and all(has_input):
            schema_fields = ["instruction", "input", "output"]
        file_bytes: dict[str, bytes] = {
            "train.jsonl": b"".join(learned["train"]),
            "validation.jsonl": b"".join(learned["validation"]),
            "test.jsonl": b"".join(learned["test"]),
            "provenance.jsonl": b"".join(provenance),
        }
        row_counts = {
            "train.jsonl": len(learned["train"]),
            "validation.jsonl": len(learned["validation"]),
            "test.jsonl": len(learned["test"]),
            "provenance.jsonl": len(provenance),
        }
        files = {
            name: _file_receipt(
                data,
                row_counts[name],
                schema_fields
                if name.endswith(".jsonl") and name != "provenance.jsonl"
                else None,
            )
            for name, data in file_bytes.items()
        }
        split_stats = _split_stats(samples, assignments, split)
        manifest = {
            "schema_version": "cyrene.sft.bundle.v1",
            "profile": SFT_PROFILE,
            "profile_version": SFT_PROFILE_VERSION,
            "dataset_id": dataset_id,
            "content_revision_id": content_revision_id,
            "processing_run_id": processing_run_id,
            "mode": mode,
            "split_algorithm": SPLIT_ALGORITHM,
            "split_ratios": split,
            "split_stats": split_stats,
            "files": files,
            "recipe": {
                "capability": CAPABILITY_ID,
                "method": "prepare_sft",
                "version": "1",
            },
        }
        file_bytes["manifest.json"] = _canonical_json(manifest) + b"\n"
        all_file_receipts = {
            name: _file_receipt(
                data,
                row_counts.get(name, 1),
                schema_fields
                if name.endswith(".jsonl") and name != "provenance.jsonl"
                else None,
            )
            for name, data in file_bytes.items()
        }
        bundle_digest, bundle_size = _write_deterministic_zip(bundle_path, file_bytes)
        result = {
            "profile": SFT_PROFILE,
            "bundle_digest": bundle_digest,
            "bundle_size_bytes": bundle_size,
            "sample_count": len(samples),
            "files": all_file_receipts,
            "split_stats": split_stats,
            "warnings": warnings,
        }
        _write_json(result_path, result)
        return result

    def generate_qa(
        self, request: dict[str, Any], *, cancellation: Any | None = None
    ) -> dict[str, Any]:
        """Generate grounded, review-pending QA drafts under explicit budgets.

        Source families receive deterministic split assignments before the first
        provider call. Provider failures are surfaced without retry, and successful
        results are checkpointed after each call with a provenance receipt.

        中文:在显式预算下生成 grounded、待审核的 QA 草稿。首次 Provider 调用前先按
        source family 确定切分。Provider 失败后不重试；每次成功调用后都会检查点保存
        结果和沿袭回执。
        """

        _keys(
            request,
            required={
                "blocks_path",
                "drafts_path",
                "provenance_path",
                "result_path",
                "dataset_id",
                "content_revision_id",
                "processing_run_id",
                "generation",
            },
            optional={"split"},
            field="request",
        )
        if self._config is None:
            raise GenerationUnavailable(
                "dataset.generation.v1 has no configured model.provider.v1 binding"
            )
        blocks_path = _path(request["blocks_path"], "blocks_path")
        drafts_path = _path(request["drafts_path"], "drafts_path")
        provenance_path = _path(request["provenance_path"], "provenance_path")
        result_path = _path(request["result_path"], "result_path")
        dataset_id = _uuid(request["dataset_id"], "dataset_id")
        content_revision_id = _uuid(
            request["content_revision_id"], "content_revision_id"
        )
        processing_run_id = _uuid(request["processing_run_id"], "processing_run_id")
        split = _split_config(request.get("split"))
        budget = _generation_budget(request["generation"])
        blocks = _approved_blocks(_read_json(blocks_path), content_revision_id)
        if len(blocks) > MAX_BLOCKS:
            raise RequestError(f"blocks exceeds {MAX_BLOCKS} records")
        warnings: list[dict[str, str]] = []
        usable_blocks: list[dict[str, Any]] = []
        for block in blocks:
            context = _provider_context(block["text"])
            if context is None:
                _append_warning(
                    warnings,
                    "QA_BLOCK_NOT_SAFE_CONTEXT",
                    f"Block {block['id']} was skipped because it is not plain text or an approved SFT row.",
                )
                continue
            usable_blocks.append({**block, "generation_context": context})
        blocks = usable_blocks
        families = [block["split_group_id"] for block in blocks]
        assignments = _assign_families(families, split)
        split_stats = _block_split_stats(blocks, assignments, split)

        output_cap = budget.get("max_output_tokens")
        if output_cap is None:
            output_cap = min(
                100_000,
                self._config.max_tokens_per_call * budget["max_calls"],
            )
        remaining_output_tokens = output_cap
        estimated_input_tokens = 0
        reported_prompt_tokens = 0
        reported_completion_tokens = 0
        prompt_usage_known = True
        completion_usage_known = True
        calls_used = 0
        drafts: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        client = DirectPluginClient.for_local_connection_ref(
            self._config.model_endpoint
        )
        try:
            for block in blocks:
                if _cancelled(cancellation):
                    _persist_generation_checkpoint(
                        drafts_path,
                        provenance_path,
                        result_path,
                        drafts,
                        provenance,
                        status="CANCELLED",
                        config=self._config,
                        budget=budget,
                        calls_used=calls_used,
                        estimated_input_tokens=estimated_input_tokens,
                        reported_prompt_tokens=reported_prompt_tokens,
                        reported_completion_tokens=reported_completion_tokens,
                        split_stats=split_stats,
                        warnings=warnings,
                    )
                    raise RequestError(
                        "operation cancelled after partial results were checkpointed"
                    )
                if len(drafts) >= budget["max_examples"]:
                    warnings.append(
                        {
                            "code": "MAX_EXAMPLES_REACHED",
                            "message": "Generation stopped at max_examples.",
                        }
                    )
                    break
                if calls_used >= budget["max_calls"]:
                    warnings.append(
                        {
                            "code": "MAX_CALLS_REACHED",
                            "message": "Generation stopped at max_calls.",
                        }
                    )
                    break
                if remaining_output_tokens <= 0:
                    warnings.append(
                        {
                            "code": "OUTPUT_BUDGET_EXHAUSTED",
                            "message": "Generation stopped because the output token budget was consumed.",
                        }
                    )
                    break

                prompt = _grounded_prompt(block["generation_context"])
                prompt_estimate = _estimate_tokens(prompt)
                input_limit = budget.get("max_input_tokens")
                if (
                    input_limit is not None
                    and estimated_input_tokens + prompt_estimate > input_limit
                ):
                    warnings.append(
                        {
                            "code": "INPUT_BUDGET_EXHAUSTED",
                            "message": "Generation stopped before a prompt would exceed max_input_tokens.",
                        }
                    )
                    break

                requested_output_tokens = min(
                    remaining_output_tokens,
                    self._config.max_tokens_per_call,
                )
                calls_used += 1
                try:
                    reply, usage = self._complete(
                        client, prompt, max_tokens=requested_output_tokens
                    )
                except GenerationUpstreamError:
                    _persist_generation_checkpoint(
                        drafts_path,
                        provenance_path,
                        result_path,
                        drafts,
                        provenance,
                        status="FAILED_AMBIGUOUS_PROVIDER_RESULT",
                        config=self._config,
                        budget=budget,
                        calls_used=calls_used,
                        estimated_input_tokens=estimated_input_tokens,
                        reported_prompt_tokens=reported_prompt_tokens,
                        reported_completion_tokens=reported_completion_tokens,
                        split_stats=split_stats,
                        warnings=warnings,
                    )
                    raise

                estimated_input_tokens += prompt_estimate
                if usage["prompt_tokens"] is None:
                    prompt_usage_known = False
                else:
                    reported_prompt_tokens += usage["prompt_tokens"]
                if usage["completion_tokens"] is None:
                    completion_usage_known = False
                    # Reserve the entire request cap when the endpoint omits usage.
                    # This prevents another call from exceeding the declared budget.
                    remaining_output_tokens = 0
                else:
                    reported_completion_tokens += usage["completion_tokens"]
                    remaining_output_tokens = max(
                        0, remaining_output_tokens - usage["completion_tokens"]
                    )
                try:
                    question, answer = _parse_qa_reply(reply)
                except GenerationUpstreamError:
                    _persist_generation_checkpoint(
                        drafts_path,
                        provenance_path,
                        result_path,
                        drafts,
                        provenance,
                        status="FAILED_PROVIDER_RESPONSE",
                        config=self._config,
                        budget=budget,
                        calls_used=calls_used,
                        estimated_input_tokens=estimated_input_tokens,
                        reported_prompt_tokens=reported_prompt_tokens,
                        reported_completion_tokens=reported_completion_tokens,
                        split_stats=split_stats,
                        warnings=warnings,
                        prompt_usage_known=prompt_usage_known,
                        completion_usage_known=completion_usage_known,
                    )
                    raise

                sample_id = _generated_sample_id(
                    content_revision_id, block["id"], block.get("sample_id")
                )
                # Draft JSONL has exactly the learned SFT row fields so review can
                # persist this object as approved ContentBlock.text later.
                draft = {"instruction": question, "output": answer}
                source_citation = {
                    "block_id": block["id"],
                    "source_revision_id": block["source_revision_id"],
                }
                if block.get("locator") is not None:
                    source_citation["locator"] = block["locator"]
                sidecar = {
                    "sample_id": sample_id,
                    "origin": "GENERATED",
                    "review_state": "PENDING",
                    "policy": block["policy"],
                    "source_family_id": block["source_family_id"],
                    "block_id": block["id"],
                    "content_revision_id": content_revision_id,
                    "dataset_id": dataset_id,
                    "processing_run_id": processing_run_id,
                    "citations": [source_citation],
                    "split": assignments[block["split_group_id"]],
                    "group_id": block.get("group_id"),
                    "recipe": _recipe_receipt(),
                    "provider": {
                        "binding_id": self._config.binding_id,
                        "model": self._config.model,
                    },
                    "budget_usage": {
                        "requested_output_tokens": requested_output_tokens,
                        "estimated_input_tokens": prompt_estimate,
                        "provider_prompt_tokens": usage["prompt_tokens"],
                        "provider_completion_tokens": usage["completion_tokens"],
                        "provider_total_tokens": usage["total_tokens"],
                    },
                }
                sidecar["generation_receipt"] = {
                    "recipeId": PROMPT_RECIPE_ID,
                    "recipeVersion": PROMPT_RECIPE_VERSION,
                    "recipeDigest": _recipe_receipt()["prompt_digest"],
                    "bindingId": self._config.binding_id,
                    "model": self._config.model,
                    "budget": {
                        "limits": budget,
                        "requestedOutputTokens": requested_output_tokens,
                    },
                    "usage": {
                        "estimatedInputTokens": prompt_estimate,
                        "providerPromptTokens": usage["prompt_tokens"],
                        "providerCompletionTokens": usage["completion_tokens"],
                        "providerTotalTokens": usage["total_tokens"],
                    },
                    "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "sourceBlockIds": [block["id"]],
                }
                drafts.append(draft)
                provenance.append(sidecar)
                _persist_generation_checkpoint(
                    drafts_path,
                    provenance_path,
                    result_path,
                    drafts,
                    provenance,
                    status="RUNNING",
                    config=self._config,
                    budget=budget,
                    calls_used=calls_used,
                    estimated_input_tokens=estimated_input_tokens,
                    reported_prompt_tokens=reported_prompt_tokens,
                    reported_completion_tokens=reported_completion_tokens,
                    split_stats=split_stats,
                    warnings=warnings,
                    prompt_usage_known=prompt_usage_known,
                    completion_usage_known=completion_usage_known,
                )
        finally:
            client.close()

        final = _persist_generation_checkpoint(
            drafts_path,
            provenance_path,
            result_path,
            drafts,
            provenance,
            status="SUCCEEDED",
            config=self._config,
            budget=budget,
            calls_used=calls_used,
            estimated_input_tokens=estimated_input_tokens,
            reported_prompt_tokens=reported_prompt_tokens,
            reported_completion_tokens=reported_completion_tokens,
            split_stats=split_stats,
            warnings=warnings,
            prompt_usage_known=prompt_usage_known,
            completion_usage_known=completion_usage_known,
        )
        return final

    def _complete(
        self,
        client: DirectPluginClient,
        prompt: str,
        *,
        max_tokens: int,
    ) -> tuple[str, dict[str, int | None]]:
        """Call only the host-resolved model.provider.v1 binding once.

        中文:仅对宿主已解析的 model.provider.v1 binding 发起一次调用。
        """

        config = self._config
        if config is None:
            raise GenerationUnavailable("model.provider.v1 binding is not configured")
        request = ChatCompletionRequest(
            messages=(
                ChatMessage(role=ChatMessageRole.SYSTEM, content=_SYSTEM_PROMPT),
                ChatMessage(role=ChatMessageRole.USER, content=prompt),
            ),
            model=config.model,
            stream=False,
            temperature=config.temperature,
            max_tokens=max_tokens,
        )
        try:
            response = client.invoke(
                capability=MODEL_CAPABILITY_ID,
                interface_version="1",
                method=CHAT_COMPLETION_METHOD,
                request=DirectPayload(
                    type_url=CHAT_COMPLETION_REQUEST_TYPE_URL,
                    value=encode_chat_completion_request(request),
                ),
                deadline_seconds=config.timeout_seconds,
            )
        except DirectPluginError as error:
            # A timeout can mean the provider completed after the client stopped
            # waiting. Retrying here could incur a duplicate paid request.
            raise GenerationUpstreamError(str(error)) from error
        decoded = decode_chat_completion_response(response.value)
        text = "".join(chunk.delta for chunk in decoded.chunks)
        usage = {
            "prompt_tokens": _sum_optional(
                chunk.prompt_tokens for chunk in decoded.chunks
            ),
            "completion_tokens": _sum_optional(
                chunk.completion_tokens for chunk in decoded.chunks
            ),
            "total_tokens": _sum_optional(
                chunk.total_tokens for chunk in decoded.chunks
            ),
        }
        return text, usage


class RequestError(ValueError):
    """A caller payload or staged input violates this capability contract.

    中文:调用负载或 staging 输入违反本 capability contract。
    """


class GenerationUnavailable(RuntimeError):
    """Explicit QA generation was requested without a configured provider.

    中文:显式请求 QA 生成，但当前没有配置 Provider。
    """


class GenerationUpstreamError(RuntimeError):
    """One model.provider.v1 call failed or returned an ambiguous result.

    中文:一次 model.provider.v1 调用失败或结果不明确。
    """


def _manual_samples(
    document: Any, content_revision_id: str, mode: str
) -> list[dict[str, Any]]:
    """Validate approved manual samples and project learned versus sidecar data.

    中文:校验已批准的手工样本，并分离学习内容和 provenance sidecar 数据。
    """

    if not isinstance(document, dict):
        raise RequestError("samples document must be an object")
    _keys(
        document,
        required={
            "schema_version",
            "content_revision_id",
            "content_revision_state",
            "samples",
        },
        optional=set(),
        field="samples document",
    )
    if document["schema_version"] != "cyrene.dataset.sft-samples.v1":
        raise RequestError("samples document schema_version is unsupported")
    if (
        _uuid(document["content_revision_id"], "samples.content_revision_id")
        != content_revision_id
    ):
        raise RequestError(
            "samples document content_revision_id does not match the request"
        )
    if document["content_revision_state"] != "APPROVED":
        raise RequestError("SFT preparation requires an APPROVED content revision")
    raw_samples = document["samples"]
    if not isinstance(raw_samples, list):
        raise RequestError("samples must be an array")
    samples: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_samples):
        field = f"samples[{index}]"
        if not isinstance(raw, dict):
            raise RequestError(f"{field} must be an object")
        _keys(
            raw,
            required={
                "sample_id",
                "source_family_id",
                "content",
                "origin",
                "policy",
                "citations",
            },
            optional={"generation", "group_id"},
            field=field,
        )
        sample_id = _required_text(raw["sample_id"], f"{field}.sample_id")
        if sample_id in seen_ids:
            raise RequestError(f"{field}.sample_id must be unique")
        seen_ids.add(sample_id)
        source_family_id = _required_text(
            raw["source_family_id"], f"{field}.source_family_id"
        )
        group_id = _optional_text(raw.get("group_id"), f"{field}.group_id")
        origin = _enum_text(
            raw["origin"],
            f"{field}.origin",
            {"EXTRACTED", "NORMALIZED", "HUMAN_EDITED", "GENERATED"},
        )
        policy = raw["policy"]
        if not isinstance(policy, dict):
            raise RequestError(f"{field}.policy must be an object")
        if not _training_policy(policy, f"{field}.policy"):
            raise RequestError(f"{field}.policy.allow_training must be true")
        content = _learned_content(raw["content"], mode, f"{field}.content")
        citations = _citations(raw["citations"], f"{field}.citations")
        generation = raw.get("generation")
        if origin == "GENERATED" or generation is not None:
            generation = _generation_provenance(generation, f"{field}.generation")
        samples.append(
            {
                "sample_id": sample_id,
                "source_family_id": source_family_id,
                "group_id": group_id,
                # An explicit groupId can represent a conversation group. Without
                # one, the stable source family remains the split isolation key.
                "split_group_id": source_family_id or group_id,
                "content": content,
                "origin": origin,
                "policy": _normalized_training_policy(policy, f"{field}.policy"),
                "citations": citations,
                "generation": generation,
            }
        )
    return samples


def _samples_from_blocks(
    blocks: list[dict[str, Any]], content_revision_id: str, mode: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Extract SFT rows only from approved blocks with the frozen JSON shape.

    Plain prose is retained by Catalyst but skipped here; this method never
    invents an answer from a source paragraph.

    中文:只从符合冻结 JSON 形状的已批准 block 中提取 SFT 行。普通 prose 由
    Catalyst 保留但在此跳过；本方法不会从来源段落编造答案。
    """

    samples: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for block in blocks:
        try:
            decoded = json.loads(block["text"])
        except json.JSONDecodeError:
            _append_warning(
                warnings,
                "SFT_BLOCK_NOT_STRUCTURED",
                f"Block {block['id']} is not a JSON SFT record.",
            )
            continue
        try:
            content, row_metadata = _project_import_record(
                decoded, mode, f"block[{block['id']}].text"
            )
        except RequestError as error:
            _append_warning(
                warnings,
                "SFT_BLOCK_UNSUPPORTED",
                f"Block {block['id']} was skipped: {error}",
            )
            continue
        generation = block.get("generation")
        sample_id = (
            _optional_text(
                row_metadata.get("sampleId", row_metadata.get("sample_id")), "sampleId"
            )
            or block["sample_id"]
        )
        source_family_id = (
            _optional_text(
                row_metadata.get(
                    "sourceFamily",
                    row_metadata.get(
                        "source_family",
                        row_metadata.get(
                            "sourceFamilyId", row_metadata.get("source_family_id")
                        ),
                    ),
                ),
                "sourceFamily",
            )
            or block["source_family_id"]
        )
        group_id = _optional_text(
            row_metadata.get(
                "conversationId",
                row_metadata.get(
                    "conversation_id",
                    row_metadata.get("groupId", row_metadata.get("group_id")),
                ),
            ),
            "conversationId",
        ) or block.get("group_id")
        row_family_id = _optional_text(
            row_metadata.get(
                "sourceFamily",
                row_metadata.get(
                    "source_family",
                    row_metadata.get(
                        "sourceFamilyId", row_metadata.get("source_family_id")
                    ),
                ),
            ),
            "sourceFamily",
        )
        split_group_id = row_family_id or block["split_group_id"]
        if block["origin"] == "GENERATED" and generation is None:
            _append_warning(
                warnings,
                "GENERATED_PROVENANCE_MISSING",
                f"Block {block['id']} has no generation receipt and was skipped.",
            )
            continue
        citation = {
            "source_revision_id": block["source_revision_id"],
            "block_id": block["id"],
        }
        if block.get("locator") is not None:
            citation["locator"] = block["locator"]
        samples.append(
            {
                "sample_id": sample_id,
                "source_family_id": source_family_id,
                "group_id": group_id,
                "split_group_id": split_group_id,
                "content": content,
                "origin": block["origin"],
                "policy": block["policy"],
                "citations": [citation],
                "generation": generation,
            }
        )
    return samples, warnings


def _project_import_record(
    value: Any, mode: str, field: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Strip recognized import metadata and return only learned row fields.

    中文:剔除已识别的导入管理字段，只返回学习行字段及 sidecar metadata。
    """

    if not isinstance(value, dict):
        raise RequestError(f"{field} must be a JSON object")
    content_keys = (
        {"instruction", "input", "output"}
        if mode == "instruction"
        else {"conversations"}
    )
    unknown = value.keys() - content_keys - _ROW_METADATA_KEYS
    if unknown:
        raise RequestError(f"{field} has unsupported fields {sorted(unknown)}")
    learned = {key: value[key] for key in content_keys if key in value}
    metadata = {key: value[key] for key in _ROW_METADATA_KEYS if key in value}
    return _learned_content(learned, mode, field), metadata


def _provider_context(value: str) -> str | None:
    """Remove import-only fields before any source row is sent to a Provider.

    Plain prose is safe context. Structured JSON must match the frozen SFT row
    shape; unknown fields make it ineligible for model calls.

    中文:发送给 Provider 前剔除导入管理字段。普通 prose 可作上下文；结构化 JSON 必须
    符合冻结的 SFT 行格式，未知字段会使其不进入模型调用。
    """

    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not isinstance(decoded, dict):
        return None
    if "conversations" in decoded:
        mode = "conversation"
    elif {"instruction", "output"}.issubset(decoded):
        mode = "instruction"
    else:
        return None
    try:
        content, _ = _project_import_record(decoded, mode, "block.text")
    except RequestError:
        return None
    return _canonical_json(content).decode("utf-8")


def _import_metadata_from_text(value: Any) -> dict[str, Any]:
    """Read only recognized import sidecar keys embedded in an SFT JSON row.

    中文:从 SFT JSON 行中仅读取已识别的导入 sidecar 字段。
    """

    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {key: decoded[key] for key in _ROW_METADATA_KEYS if key in decoded}


def _learned_content(value: Any, mode: str, field: str) -> dict[str, Any]:
    """Allowlist legacy SFT row fields and reject embedded management data.

    中文:只允许旧版 SFT 行字段，并拒绝混入的管理数据。
    """

    if not isinstance(value, dict):
        raise RequestError(f"{field} must be an object")
    if mode == "instruction":
        _keys(
            value, required={"instruction", "output"}, optional={"input"}, field=field
        )
        row: dict[str, str] = {}
        for key in ("instruction", "input", "output"):
            if key not in value:
                continue
            if key == "input":
                if not isinstance(value[key], str):
                    raise RequestError(f"{field}.input must be a string")
                row[key] = value[key]
            else:
                row[key] = _required_text(value[key], f"{field}.{key}")
        if not row["instruction"].strip() or not row["output"].strip():
            raise RequestError(
                f"{field}.instruction and {field}.output must be non-empty"
            )
        return row
    _keys(value, required={"conversations"}, optional=set(), field=field)
    conversations = value["conversations"]
    if not isinstance(conversations, list) or len(conversations) < 2:
        raise RequestError(f"{field}.conversations must contain at least two turns")
    projected = []
    for index, turn in enumerate(conversations):
        if not isinstance(turn, dict):
            raise RequestError(f"{field}.conversations[{index}] must be an object")
        _keys(
            turn,
            required={"from", "value"},
            optional=set(),
            field=f"{field}.conversations[{index}]",
        )
        speaker = _enum_text(
            turn["from"],
            f"{field}.conversations[{index}].from",
            {"human", "assistant", "gpt"},
        )
        text = _required_text(turn["value"], f"{field}.conversations[{index}].value")
        projected.append({"from": speaker, "value": text})
    if projected[0]["from"] != "human" or projected[-1]["from"] not in {
        "assistant",
        "gpt",
    }:
        raise RequestError(
            f"{field}.conversations must start with human and end with assistant"
        )
    return {"conversations": projected}


def _citations(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise RequestError(f"{field} must be a non-empty array")
    result = []
    for index, item in enumerate(value):
        item_field = f"{field}[{index}]"
        if not isinstance(item, dict):
            raise RequestError(f"{item_field} must be an object")
        _keys(
            item,
            required={"source_revision_id", "block_id"},
            optional={"locator"},
            field=item_field,
        )
        citation = {
            "source_revision_id": _uuid(
                item["source_revision_id"], f"{item_field}.source_revision_id"
            ),
            "block_id": _required_text(item["block_id"], f"{item_field}.block_id"),
        }
        if "locator" in item:
            if not isinstance(item["locator"], dict):
                raise RequestError(f"{item_field}.locator must be an object")
            citation["locator"] = item["locator"]
        result.append(citation)
    return result


def _training_policy(policy: dict[str, Any], field: str) -> bool:
    """Resolve legacy allowTraining and additive allowed-use purpose policies.

    中文:解析旧 allowTraining 标志和新增的 allowedUsePurposes 策略。
    """

    allowed_keys = {
        "allow_training",
        "allowTraining",
        "allowed_use_purposes",
        "allowedUsePurposes",
        "allow_knowledge",
        "allowKnowledge",
        "allowed_principal_refs",
        "allowedPrincipalRefs",
    }
    unknown = policy.keys() - allowed_keys
    if unknown:
        raise RequestError(f"{field} has unknown fields {sorted(unknown)}")
    flag = policy.get("allow_training", policy.get("allowTraining"))
    purposes = policy.get("allowed_use_purposes", policy.get("allowedUsePurposes"))
    if flag is not None and not isinstance(flag, bool):
        raise RequestError(f"{field}.allowTraining must be a boolean")
    if purposes is not None:
        if not isinstance(purposes, list) or any(
            purpose not in {"knowledge_retrieval", "model_training"}
            for purpose in purposes
        ):
            raise RequestError(f"{field}.allowedUsePurposes has unsupported values")
        purpose_allows = "model_training" in purposes
        if flag is not None and flag != purpose_allows:
            raise RequestError(
                f"{field}.allowTraining conflicts with allowedUsePurposes"
            )
        return purpose_allows
    if flag is None:
        raise RequestError(f"{field} needs allowTraining or allowedUsePurposes")
    return flag


def _normalized_training_policy(policy: dict[str, Any], field: str) -> dict[str, Any]:
    allowed = _training_policy(policy, field)
    purposes = policy.get("allowed_use_purposes", policy.get("allowedUsePurposes"))
    if purposes is None:
        normalized_purposes = ["model_training"] if allowed else []
    else:
        normalized_purposes = sorted(set(purposes))
    result: dict[str, Any] = {
        "allow_training": allowed,
        "allowed_use_purposes": normalized_purposes,
    }
    knowledge = policy.get("allow_knowledge", policy.get("allowKnowledge"))
    if knowledge is not None:
        if not isinstance(knowledge, bool):
            raise RequestError(f"{field}.allowKnowledge must be a boolean")
        result["allow_knowledge"] = knowledge
    principal_refs = policy.get(
        "allowed_principal_refs", policy.get("allowedPrincipalRefs")
    )
    if principal_refs is not None:
        if not isinstance(principal_refs, list) or any(
            not isinstance(principal, str) or not principal
            for principal in principal_refs
        ):
            raise RequestError(f"{field}.allowedPrincipalRefs must be a string array")
        result["allowed_principal_refs"] = list(principal_refs)
    return result


def _generation_provenance(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RequestError(f"{field} is required for GENERATED samples")
    allowed = {
        "recipe_id",
        "recipeId",
        "recipe_version",
        "recipeVersion",
        "recipe_digest",
        "recipeDigest",
        "provider_binding_id",
        "binding_id",
        "bindingId",
        "model",
        "budget",
        "usage",
        "generated_at",
        "generatedAt",
        "source_block_ids",
        "sourceBlockIds",
    }
    unknown = value.keys() - allowed
    if unknown:
        raise RequestError(f"{field} has unknown fields {sorted(unknown)}")

    def pick(*keys: str) -> Any:
        return next((value[key] for key in keys if key in value), None)

    result = {
        "recipe_id": _required_text(pick("recipe_id", "recipeId"), f"{field}.recipeId"),
        "recipe_version": _required_text(
            pick("recipe_version", "recipeVersion"), f"{field}.recipeVersion"
        ),
        "recipe_digest": _required_text(
            pick("recipe_digest", "recipeDigest"), f"{field}.recipeDigest"
        ),
        "provider_binding_id": _required_text(
            pick("provider_binding_id", "bindingId", "binding_id"), f"{field}.bindingId"
        ),
        "model": _required_text(value.get("model"), f"{field}.model"),
        "budget": value.get("budget"),
        "usage": value.get("usage"),
    }
    if not isinstance(result["budget"], dict) or not isinstance(result["usage"], dict):
        raise RequestError(f"{field}.budget and {field}.usage must be objects")
    generated_at = pick("generated_at", "generatedAt")
    source_block_ids = pick("source_block_ids", "sourceBlockIds")
    if generated_at is not None:
        result["generated_at"] = _required_text(generated_at, f"{field}.generatedAt")
    if source_block_ids is not None:
        if not isinstance(source_block_ids, list) or any(
            not isinstance(item, str) for item in source_block_ids
        ):
            raise RequestError(f"{field}.sourceBlockIds must be a string array")
        result["source_block_ids"] = source_block_ids
    return result


def _approved_blocks(document: Any, content_revision_id: str) -> list[dict[str, Any]]:
    """Validate approved, training-permitted block records for QA context.

    中文:校验已批准且允许训练的 block records，作为 QA 生成上下文。
    """

    if not isinstance(document, dict):
        raise RequestError("blocks document must be an object")
    schema_version = document.get("schema_version", document.get("schemaVersion"))
    if schema_version is not None and schema_version not in {
        "cyrene.content.blocks.v1",
        "cyrene.document.blocks.v1",
    }:
        raise RequestError("blocks document schema_version is unsupported")
    revision_value = document.get(
        "content_revision_id", document.get("contentRevisionId", document.get("id"))
    )
    if _uuid(revision_value, "blocks.content_revision_id") != content_revision_id:
        raise RequestError(
            "blocks document content_revision_id does not match the request"
        )
    revision_state = document.get(
        "content_revision_state",
        document.get("contentRevisionState", document.get("state")),
    )
    if revision_state != "APPROVED":
        raise RequestError("QA generation requires an APPROVED content revision")
    raw_blocks = document["blocks"]
    if not isinstance(raw_blocks, list):
        raise RequestError("blocks must be an array")
    blocks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_blocks):
        field = f"blocks[{index}]"
        if not isinstance(raw, dict):
            raise RequestError(f"{field} must be an object")
        block_id = _required_text(raw.get("id"), f"{field}.id")
        if block_id in seen_ids:
            raise RequestError(f"{field}.id must be unique")
        seen_ids.add(block_id)
        source_revision_id = _uuid(
            raw.get("source_revision_id", raw.get("sourceRevisionId")),
            f"{field}.sourceRevisionId",
        )
        family_value = raw.get(
            "source_family_id",
            raw.get("sourceFamilyId", raw.get("source_id", raw.get("sourceId"))),
        )
        row_metadata = _import_metadata_from_text(raw.get("text"))
        row_family_value = row_metadata.get(
            "sourceFamily",
            row_metadata.get(
                "source_family",
                row_metadata.get(
                    "sourceFamilyId", row_metadata.get("source_family_id")
                ),
            ),
        )
        if family_value and row_family_value and family_value != row_family_value:
            raise RequestError(f"{field} source family conflicts with its JSONL row")
        source_family_explicit = bool(family_value or row_family_value)
        source_family_id = _required_text(
            family_value or row_family_value or source_revision_id,
            f"{field}.sourceFamilyId, groupId, or sourceRevisionId",
        )
        group_value = raw.get(
            "group_id",
            raw.get("groupId", raw.get("conversation_id", raw.get("conversationId"))),
        )
        row_group_value = row_metadata.get(
            "conversationId",
            row_metadata.get(
                "conversation_id",
                row_metadata.get("groupId", row_metadata.get("group_id")),
            ),
        )
        if group_value and row_group_value and group_value != row_group_value:
            raise RequestError(f"{field} group id conflicts with its JSONL row")
        group_id = _optional_text(
            group_value or row_group_value,
            f"{field}.groupId",
        )
        ordinal = _integer(raw.get("ordinal"), f"{field}.ordinal", minimum=0)
        kind = _required_text(raw.get("kind"), f"{field}.kind")
        text = _required_text(raw.get("text"), f"{field}.text")
        if len(text) > MAX_BLOCK_TEXT_CHARS:
            raise RequestError(
                f"{field}.text exceeds {MAX_BLOCK_TEXT_CHARS} characters"
            )
        origin = _enum_text(
            raw.get("origin"),
            f"{field}.origin",
            {"EXTRACTED", "NORMALIZED", "HUMAN_EDITED", "GENERATED"},
        )
        policy = raw.get("policy")
        if not isinstance(policy, dict):
            raise RequestError(f"{field}.policy must be an object")
        if not _training_policy(policy, f"{field}.policy"):
            continue
        block = {
            "id": block_id,
            "sample_id": _optional_text(
                raw.get("sample_id", raw.get("sampleId")), f"{field}.sampleId"
            )
            or _optional_text(
                row_metadata.get("sampleId", row_metadata.get("sample_id")),
                f"{field}.text.sampleId",
            )
            or _block_sample_id(block_id),
            "source_revision_id": source_revision_id,
            "source_family_id": source_family_id,
            "source_family_explicit": source_family_explicit,
            "group_id": group_id,
            "split_group_id": (family_value or row_family_value)
            or group_id
            or source_revision_id,
            "ordinal": ordinal,
            "kind": kind,
            "text": text,
            "origin": origin,
            "policy": _normalized_training_policy(policy, f"{field}.policy"),
        }
        locator = raw.get("locator")
        if locator is not None:
            if not isinstance(locator, dict):
                raise RequestError(f"{field}.locator must be an object")
            block["locator"] = locator
        generation = raw.get(
            "generation", raw.get("generationProvenance", raw.get("generationReceipt"))
        )
        if generation is not None:
            block["generation"] = _generation_provenance(
                generation, f"{field}.generation"
            )
        blocks.append(block)
    blocks.sort(
        key=lambda block: (
            block["source_family_id"],
            block["source_revision_id"],
            block["ordinal"],
            block["id"],
        )
    )
    return blocks


def _split_config(value: Any) -> dict[str, float]:
    if value is None:
        return {"train": 0.8, "validation": 0.1, "test": 0.1}
    if not isinstance(value, dict):
        raise RequestError("split must be an object")
    _keys(
        value, required={"train", "validation", "test"}, optional=set(), field="split"
    )
    ratios = {
        key: _finite_number(value[key], f"split.{key}", minimum=0.0, maximum=1.0)
        for key in ("train", "validation", "test")
    }
    if not math.isclose(sum(ratios.values()), 1.0, abs_tol=1e-9):
        raise RequestError("split ratios must sum to 1")
    return ratios


def _assign_families(family_ids: list[str], ratios: dict[str, float]) -> dict[str, str]:
    unique_families = set(family_ids)
    if not unique_families:
        return {}
    split_names = ("train", "validation", "test")
    active_splits = [name for name in split_names if ratios[name] > 0]
    ranked_families = sorted(
        unique_families,
        key=lambda family_id: (
            hashlib.sha256(f"{SPLIT_ALGORITHM}:{family_id}".encode()).digest(),
            family_id,
        ),
    )
    counts = {
        name: math.floor(len(ranked_families) * ratios[name]) for name in split_names
    }
    remainder = len(ranked_families) - sum(counts.values())
    fractional = sorted(
        split_names,
        key=lambda name: (
            -(len(ranked_families) * ratios[name] - counts[name]),
            split_names.index(name),
        ),
    )
    for name in fractional[:remainder]:
        counts[name] += 1

    # Small datasets still populate every requested split when they have enough
    # source families. Transfer from the largest split while preserving at least
    # one family for that donor.
    if len(ranked_families) >= len(active_splits):
        for name in active_splits:
            if counts[name] == 0:
                donors = [
                    candidate for candidate in split_names if counts[candidate] > 1
                ]
                if not donors:
                    break
                donor = max(
                    donors,
                    key=lambda candidate: (
                        counts[candidate],
                        -split_names.index(candidate),
                    ),
                )
                counts[donor] -= 1
                counts[name] = 1

    assignments: dict[str, str] = {}
    offset = 0
    for name in split_names:
        for family_id in ranked_families[offset : offset + counts[name]]:
            assignments[family_id] = name
        offset += counts[name]
    return assignments


def _split_stats(
    samples: list[dict[str, Any]], assignments: dict[str, str], ratios: dict[str, float]
) -> dict[str, Any]:
    return {
        "algorithm": SPLIT_ALGORITHM,
        "ratios": ratios,
        "samples": {
            split_name: sum(
                1
                for sample in samples
                if assignments[sample["split_group_id"]] == split_name
            )
            for split_name in ("train", "validation", "test")
        },
        "source_families": {
            split_name: len(
                {
                    sample["source_family_id"]
                    for sample in samples
                    if assignments[sample["split_group_id"]] == split_name
                }
            )
            for split_name in ("train", "validation", "test")
        },
    }


def _block_split_stats(
    blocks: list[dict[str, Any]], assignments: dict[str, str], ratios: dict[str, float]
) -> dict[str, Any]:
    return {
        "algorithm": SPLIT_ALGORITHM,
        "ratios": ratios,
        "blocks": {
            split_name: sum(
                1
                for block in blocks
                if assignments[block["split_group_id"]] == split_name
            )
            for split_name in ("train", "validation", "test")
        },
        "source_families": {
            split_name: sum(
                1
                for family in {block["source_family_id"] for block in blocks}
                if any(
                    item["source_family_id"] == family
                    and assignments[item["split_group_id"]] == split_name
                    for item in blocks
                )
            )
            for split_name in ("train", "validation", "test")
        },
        "groups": {
            split_name: sum(1 for group in assignments.values() if group == split_name)
            for split_name in ("train", "validation", "test")
        },
    }


def _generation_budget(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RequestError("generation must be an object")
    _keys(
        value,
        required={"max_examples", "max_calls"},
        optional={"max_input_tokens", "max_output_tokens"},
        field="generation",
    )
    return {
        "max_examples": _integer(
            value["max_examples"], "generation.max_examples", minimum=1, maximum=1000
        ),
        "max_calls": _integer(
            value["max_calls"], "generation.max_calls", minimum=1, maximum=1000
        ),
        **(
            {
                "max_input_tokens": _integer(
                    value["max_input_tokens"],
                    "generation.max_input_tokens",
                    minimum=1,
                    maximum=1_000_000,
                )
            }
            if "max_input_tokens" in value
            else {}
        ),
        **(
            {
                "max_output_tokens": _integer(
                    value["max_output_tokens"],
                    "generation.max_output_tokens",
                    minimum=1,
                    maximum=100_000,
                )
            }
            if "max_output_tokens" in value
            else {}
        ),
    }


def _grounded_prompt(source_text: str) -> str:
    return (
        "Source material:\n"
        f"{source_text}\n\n"
        "Write one useful question about this material and its directly supported answer. "
        'Return JSON: {"question":"...","answer":"..."}.'
    )


def _parse_qa_reply(reply: str) -> tuple[str, str]:
    try:
        document = json.loads(reply.strip())
    except json.JSONDecodeError as error:
        raise GenerationUpstreamError(
            f"provider reply was not one JSON object: {error}"
        ) from error
    if not isinstance(document, dict) or set(document) != {"question", "answer"}:
        raise GenerationUpstreamError(
            "provider reply must contain only question and answer strings"
        )
    question = document["question"]
    answer = document["answer"]
    if not isinstance(question, str) or not question.strip():
        raise GenerationUpstreamError("provider question must be non-empty text")
    if not isinstance(answer, str) or not answer.strip():
        raise GenerationUpstreamError("provider answer must be non-empty text")
    return question.strip(), answer.strip()


def _generated_sample_id(
    content_revision_id: str, block_id: str, source_sample_id: str | None = None
) -> str:
    digest = hashlib.sha256(
        f"{SAMPLE_ID_ALGORITHM}:{content_revision_id}:{source_sample_id or block_id}:{PROMPT_RECIPE_VERSION}".encode()
    ).hexdigest()
    return f"generated-{digest[:32]}"


def _block_sample_id(block_id: str) -> str:
    """Derive one stable SFT sample identity from a stable ContentBlock id.

    中文:根据稳定的 ContentBlock id 派生稳定的 SFT sample id。
    """

    digest = hashlib.sha256(
        f"cyrene-sft-block-sample-v1:{block_id}".encode()
    ).hexdigest()
    return f"sample-{digest[:32]}"


def _append_warning(warnings: list[dict[str, str]], code: str, message: str) -> None:
    if len(warnings) < 100:
        warnings.append({"code": code, "message": message[:MAX_RESULT_DETAIL]})
    elif len(warnings) == 100:
        warnings.append(
            {
                "code": "WARNINGS_TRUNCATED",
                "message": "Additional warnings were omitted.",
            }
        )


def _recipe_receipt() -> dict[str, str]:
    return {
        "id": PROMPT_RECIPE_ID,
        "version": PROMPT_RECIPE_VERSION,
        "prompt_digest": _digest(
            _SYSTEM_PROMPT + "\n" + _grounded_prompt("{source_text}")
        ),
    }


def _persist_generation_checkpoint(
    drafts_path: Path,
    provenance_path: Path,
    result_path: Path,
    drafts: list[dict[str, Any]],
    provenance: list[dict[str, Any]],
    *,
    status: str,
    config: GenerationConfig,
    budget: dict[str, int],
    calls_used: int,
    estimated_input_tokens: int,
    reported_prompt_tokens: int,
    reported_completion_tokens: int,
    split_stats: dict[str, Any],
    warnings: list[dict[str, str]],
    prompt_usage_known: bool = True,
    completion_usage_known: bool = True,
) -> dict[str, Any]:
    draft_bytes = b"".join(_canonical_json(item) + b"\n" for item in drafts)
    provenance_bytes = b"".join(_canonical_json(item) + b"\n" for item in provenance)
    _write_bytes(drafts_path, draft_bytes)
    _write_bytes(provenance_path, provenance_bytes)
    budget_result: dict[str, Any] = {
        "limits": budget,
        "calls_used": calls_used,
        "examples_generated": len(drafts),
        "estimated_input_tokens": estimated_input_tokens,
        "provider_prompt_tokens": reported_prompt_tokens
        if prompt_usage_known
        else None,
        "provider_completion_tokens": reported_completion_tokens
        if completion_usage_known
        else None,
    }
    result = {
        "status": status,
        "drafts_digest": _digest_bytes(draft_bytes),
        "drafts_size_bytes": len(draft_bytes),
        "draft_count": len(drafts),
        "provenance_digest": _digest_bytes(provenance_bytes),
        "provenance_size_bytes": len(provenance_bytes),
        "provider": {"binding_id": config.binding_id, "model": config.model},
        "recipe": _recipe_receipt(),
        "budget": budget_result,
        "split_stats": split_stats,
        "warnings": warnings,
    }
    _write_json(result_path, result)
    return result


def _estimate_tokens(text: str) -> int:
    """Estimate prompt cost conservatively from UTF-8 bytes, without inventing usage.

    中文:根据 UTF-8 字节数保守估计 prompt 成本，不伪造 Provider usage。
    """

    return max(1, (len(text.encode("utf-8")) + 2) // 3)


def _sum_optional(values: Any) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _file_receipt(
    data: bytes, row_count: int, schema_fields: list[str] | None = None
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "digest": _digest_bytes(data),
        "size_bytes": len(data),
        "row_count": row_count,
    }
    if schema_fields is not None:
        receipt["schema_fields"] = schema_fields
    return receipt


def _write_deterministic_zip(path: Path, files: dict[str, bytes]) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(files):
            if Path(name).name != name:
                raise ValueError("bundle member names must be simple file names")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            archive.writestr(info, files[name])
    return f"sha256:{_sha256_file(path)}", path.stat().st_size


def _read_json(path: Path) -> Any:
    _validate_input(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_input(path: Path) -> None:
    if not path.is_file():
        raise RequestError(f"input file does not exist: {path.name}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise RequestError(f"input file exceeds {MAX_INPUT_BYTES} bytes")


def _path(value: Any, field: str) -> Path:
    text = _required_text(value, field)
    path = Path(text)
    if not path.is_absolute():
        raise RequestError(f"{field} must be an absolute path")
    return path


def _uuid(value: Any, field: str) -> str:
    text = _required_text(value, field)
    import uuid

    try:
        parsed = uuid.UUID(text)
    except ValueError as error:
        raise RequestError(f"{field} must be a UUID") from error
    if str(parsed) != text.lower():
        raise RequestError(f"{field} must use canonical UUID spelling")
    return str(parsed)


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RequestError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RequestError(f"{field} must be a non-empty string when provided")
    return value


def _enum_text(value: Any, field: str, allowed: set[str]) -> str:
    text = _required_text(value, field)
    if text not in allowed:
        raise RequestError(f"{field} must be one of {sorted(allowed)}")
    return text


def _integer(
    value: Any, field: str, *, minimum: int, maximum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RequestError(f"{field} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise RequestError(f"{field} must be <= {maximum}")
    return value


def _finite_number(
    value: Any,
    field: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise RequestError(f"{field} must be finite and >= {minimum}")
    if maximum is not None and number > maximum:
        raise RequestError(f"{field} must be <= {maximum}")
    return number


def _keys(
    value: dict[str, Any], *, required: set[str], optional: set[str], field: str
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise RequestError(f"{field} is missing fields {sorted(missing)}")
    if unknown:
        raise RequestError(f"{field} has unknown fields {sorted(unknown)}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(path, _canonical_json(value) + b"\n")


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _digest_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cancelled(cancellation: Any | None) -> bool:
    return cancellation is not None and cancellation.is_cancelled()
