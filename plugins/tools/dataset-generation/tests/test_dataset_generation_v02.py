"""Cross-output and provider-usage regression checks for Catalyst v0.2.

中文:验证 Catalyst v0.2 的 QA 草稿、人工审核后 SFT 导出和 Provider usage 边界。
"""

from __future__ import annotations

import json
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from cyrene_model_provider_contracts import CAPABILITY_ID as MODEL_CAPABILITY_ID
from cyrene_model_provider_contracts import (
    CHAT_COMPLETION_METHOD,
    CHAT_COMPLETION_REQUEST_TYPE_URL,
    CHAT_COMPLETION_RESPONSE_TYPE_URL,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    decode_chat_completion_request,
    encode_chat_completion_response,
)
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from dataset_generation import (
    CAPABILITY_ID,
    DatasetGenerationPlugin,
    GenerationConfig,
    RequestError,
)

DATASET_ID = "00000000-0000-4000-8000-000000000001"
CONTENT_REVISION_ID = "00000000-0000-4000-8000-000000000002"
APPROVED_REVISION_ID = "00000000-0000-4000-8000-000000000009"
PROCESSING_RUN_ID = "00000000-0000-4000-8000-000000000003"
SOURCE_REVISION_ID = "00000000-0000-4000-8000-000000000004"
SPLIT = {"train": 0.7, "validation": 0.2, "test": 0.1}
TRAINING_POLICY = {
    "allowedUsePurposes": ["knowledge_retrieval", "model_training"],
    "allowedPrincipalRefs": ["org:trial-alpha"],
}


class UsageReportingProvider:
    """A scripted provider that reports exact usage through the v1 contract.

    中文:通过 v1 contract 返回可控实际用量的 Scripted Provider。
    """

    plugin_id = "test.scripted-v02-provider"
    version = "0.1.0"
    capabilities = (MODEL_CAPABILITY_ID,)

    def __init__(self, usage: list[dict[str, int]]) -> None:
        self.usage = list(usage)
        self.requests: list[ChatCompletionRequest] = []

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        cancellation: Any | None = None,
        request_type_url: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, Any]:
        if capability != MODEL_CAPABILITY_ID or action != CHAT_COMPLETION_METHOD:
            return False, "METHOD_NOT_FOUND: unsupported provider method"
        if request_type_url != CHAT_COMPLETION_REQUEST_TYPE_URL:
            return False, "INVALID_REQUEST: wrong provider request type"
        self.requests.append(decode_chat_completion_request(payload))
        if not self.usage:
            return False, "INVALID_REQUEST: scripted usage is exhausted"
        usage = self.usage.pop(0)
        reply = json.dumps(
            {
                "question": f"Source question {len(self.requests)}?",
                "answer": f"Grounded answer {len(self.requests)}.",
            }
        )
        response = ChatCompletionResponse(
            chunks=(
                ChatCompletionChunk(
                    delta=reply,
                    finish_reason="stop",
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                ),
            )
        )
        return True, _TypedPayload(
            value=encode_chat_completion_response(response),
            type_url=CHAT_COMPLETION_RESPONSE_TYPE_URL,
        )


class _TypedPayload:
    def __init__(self, value: bytes, type_url: str) -> None:
        self.value = value
        self.type_url = type_url


def _content_block(
    block_id: str,
    family_id: str,
    text: str,
    *,
    ordinal: int,
    group_id: str | None = None,
    kind: str = "text",
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": block_id,
        "sourceRevisionId": SOURCE_REVISION_ID,
        "sourceFamilyId": family_id,
        "groupId": group_id,
        "ordinal": ordinal,
        "kind": kind,
        "text": text,
        "origin": "HUMAN_EDITED",
        "policy": policy or TRAINING_POLICY,
        "locator": {"page": ordinal + 1},
    }


def _jsonl_record(content: dict[str, Any], *, family: str, sample: str) -> str:
    row = {
        **content,
        "sourceFamily": family,
        "conversationId": f"{family}-{sample}",
        "sampleId": sample,
        "_acl": ["org:trial-alpha"],
        "_operatorAnnotation": "PRIVATE_MANAGEMENT_VALUE",
        "reviewNote": "PRIVATE_REVIEW_VALUE",
    }
    return json.dumps(row, ensure_ascii=False)


def _blocks_document(
    blocks: list[dict[str, Any]],
    *,
    state: str = "APPROVED",
    revision_id: str = CONTENT_REVISION_ID,
) -> dict[str, Any]:
    return {
        "schema_version": "cyrene.content.blocks.v1",
        "content_revision_id": revision_id,
        "content_revision_state": state,
        "blocks": blocks,
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _generate(
    tmp_path: Path,
    blocks: list[dict[str, Any]],
    usage: list[dict[str, int]],
    generation: dict[str, int],
) -> tuple[
    dict[str, Any], UsageReportingProvider, list[dict[str, Any]], list[dict[str, Any]]
]:
    provider = UsageReportingProvider(usage)
    provider_server, provider_ref = serve(
        provider, MODEL_CAPABILITY_ID, "1", "127.0.0.1:0"
    )
    generation_plugin = DatasetGenerationPlugin(
        GenerationConfig(
            binding_id="model.binding.v02-fixture",
            model_endpoint=provider_ref,
            model="fixture-qwen-compatible",
            max_tokens_per_call=128,
        )
    )
    generation_server, generation_ref = serve(
        generation_plugin, CAPABILITY_ID, "1", "127.0.0.1:0"
    )
    client = DirectPluginClient.for_local_connection_ref(generation_ref)
    blocks_path = tmp_path / "generation-blocks.json"
    drafts_path = tmp_path / "drafts.jsonl"
    provenance_path = tmp_path / "draft-provenance.jsonl"
    result_path = tmp_path / "generation-result.json"
    _write_json(blocks_path, _blocks_document(blocks))
    request = {
        "blocks_path": str(blocks_path),
        "drafts_path": str(drafts_path),
        "provenance_path": str(provenance_path),
        "result_path": str(result_path),
        "dataset_id": DATASET_ID,
        "content_revision_id": CONTENT_REVISION_ID,
        "processing_run_id": PROCESSING_RUN_ID,
        "split": SPLIT,
        "generation": generation,
    }
    try:
        response = client.invoke(
            capability=CAPABILITY_ID,
            interface_version="1",
            method="generate_qa",
            request=DirectPayload(
                type_url=f"type.cyrene.io/{CAPABILITY_ID}.generate_qa.request",
                value=json.dumps(request).encode("utf-8"),
            ),
            deadline_seconds=10,
        )
    finally:
        client.close()
        generation_server.stop(grace=None).wait()
        provider_server.stop(grace=None).wait()

    result = json.loads(response.value)
    drafts = [
        json.loads(line)
        for line in drafts_path.read_text(encoding="utf-8").splitlines()
    ]
    provenance = [
        json.loads(line)
        for line in provenance_path.read_text(encoding="utf-8").splitlines()
    ]
    return result, provider, drafts, provenance


def _prepare_request(
    tmp_path: Path,
    blocks_path: Path,
    *,
    mode: str,
    revision_id: str,
    stem: str,
) -> dict[str, Any]:
    return {
        "blocks_path": str(blocks_path),
        "bundle_path": str(tmp_path / f"{stem}.zip"),
        "result_path": str(tmp_path / f"{stem}.result.json"),
        "dataset_id": DATASET_ID,
        "content_revision_id": revision_id,
        "processing_run_id": PROCESSING_RUN_ID,
        "mode": mode,
        "split": SPLIT,
    }


def _generated_block(
    draft: dict[str, Any], provenance: dict[str, Any], ordinal: int
) -> dict[str, Any]:
    return {
        "id": f"approved-{provenance['block_id']}",
        "sourceRevisionId": SOURCE_REVISION_ID,
        "sourceFamilyId": provenance["source_family_id"],
        "groupId": provenance.get("group_id"),
        "sampleId": provenance["sample_id"],
        "ordinal": ordinal,
        "kind": "text",
        "text": json.dumps(draft, ensure_ascii=False),
        "origin": "GENERATED",
        "policy": provenance["policy"],
        "generationReceipt": provenance["generation_receipt"],
    }


def test_v02_mixed_sources_keep_conversation_family_lineage_and_review_receipts(
    tmp_path: Path,
) -> None:
    conversation_one = {
        "conversations": [
            {"from": "human", "value": "怎样申请恢复？"},
            {"from": "gpt", "value": "请使用恢复代码 ORCHID-42。"},
        ]
    }
    conversation_two = {
        "conversations": [
            {"from": "human", "value": "恢复码是什么？"},
            {"from": "gpt", "value": "恢复码为 ORCHID-42。"},
        ]
    }
    blocks = [
        _content_block(
            "conversation-a1",
            "family-a",
            _jsonl_record(conversation_one, family="family-a", sample="conv-1"),
            ordinal=0,
            group_id="family-a-conv-1",
        ),
        _content_block(
            "conversation-a2",
            "family-a",
            _jsonl_record(conversation_two, family="family-a", sample="conv-2"),
            ordinal=1,
            group_id="family-a-conv-2",
        ),
        _content_block(
            "prose-a",
            "family-a",
            "A plain-text source paragraph describes account recovery.",
            ordinal=2,
            group_id="family-a-prose",
        ),
        _content_block(
            "instruction-b",
            "family-b",
            _jsonl_record(
                {"instruction": "What is the code?", "input": "", "output": "B-7"},
                family="family-b",
                sample="sample-b",
            ),
            ordinal=3,
        ),
        _content_block(
            "instruction-c",
            "family-c",
            _jsonl_record(
                {"instruction": "Which release?", "output": "C-9"},
                family="family-c",
                sample="sample-c",
            ),
            ordinal=4,
        ),
        _content_block(
            "blocked-knowledge-only",
            "blocked-family-1",
            "BLOCKED_KNOWLEDGE_ONLY_SECRET",
            ordinal=5,
            policy={
                "allowedUsePurposes": ["knowledge_retrieval"],
                "allowKnowledge": True,
                "allowedPrincipalRefs": ["org:trial-alpha"],
            },
        ),
        _content_block(
            "blocked-no-purposes",
            "blocked-family-2",
            "BLOCKED_NO_PURPOSE_SECRET",
            ordinal=6,
            policy={
                "allowTraining": False,
                "allowedUsePurposes": [],
                "allowedPrincipalRefs": ["org:trial-beta"],
            },
        ),
    ]
    result, provider, drafts, provenance = _generate(
        tmp_path,
        blocks,
        [{"prompt_tokens": 12, "completion_tokens": 9} for _ in range(5)],
        {
            "max_examples": 10,
            "max_calls": 10,
            "max_input_tokens": 10_000,
            "max_output_tokens": 1_000,
        },
    )

    assert result["status"] == "SUCCEEDED"
    assert result["draft_count"] == 5
    assert result["budget"]["provider_prompt_tokens"] == 60
    assert result["budget"]["provider_completion_tokens"] == 45
    assert len(provider.requests) == len(drafts) == len(provenance) == 5
    assert result["split_stats"]["source_families"] == {
        "train": 1,
        "validation": 1,
        "test": 1,
    }

    family_splits: dict[str, set[str]] = defaultdict(set)
    family_a_conversations: list[str] = []
    for row, sidecar in zip(drafts, provenance, strict=True):
        assert set(row) == {"instruction", "output"}
        serialized_row = json.dumps(row, ensure_ascii=False)
        assert "PRIVATE_MANAGEMENT_VALUE" not in serialized_row
        assert "PRIVATE_REVIEW_VALUE" not in serialized_row
        assert "_acl" not in serialized_row
        assert sidecar["origin"] == "GENERATED"
        assert sidecar["review_state"] == "PENDING"
        family_splits[sidecar["source_family_id"]].add(sidecar["split"])
        receipt = sidecar["generation_receipt"]
        assert set(receipt) == {
            "recipeId",
            "recipeVersion",
            "recipeDigest",
            "bindingId",
            "model",
            "budget",
            "usage",
            "generatedAt",
            "sourceBlockIds",
        }
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", receipt["recipeDigest"])
        assert receipt["bindingId"] == "model.binding.v02-fixture"
        assert receipt["model"] == "fixture-qwen-compatible"
        assert receipt["budget"]["limits"]["max_calls"] == 10
        assert receipt["usage"]["providerPromptTokens"] == 12
        assert receipt["usage"]["providerCompletionTokens"] == 9
        assert receipt["sourceBlockIds"] == [sidecar["block_id"]]
        if sidecar["block_id"].startswith("conversation-"):
            family_a_conversations.append(sidecar["group_id"])
    assert set(family_splits) == {"family-a", "family-b", "family-c"}
    assert all(len(splits) == 1 for splits in family_splits.values())
    assert len(set(family_a_conversations)) == 2

    sent_contexts = [request.messages[1].content for request in provider.requests]
    joined_contexts = "\n".join(sent_contexts)
    assert "PRIVATE_MANAGEMENT_VALUE" not in joined_contexts
    assert "PRIVATE_REVIEW_VALUE" not in joined_contexts
    assert "BLOCKED_KNOWLEDGE_ONLY_SECRET" not in joined_contexts
    assert "BLOCKED_NO_PURPOSE_SECRET" not in joined_contexts

    original_blocks_path = tmp_path / "approved-original-and-generated.json"
    generated_blocks = [
        _generated_block(draft, sidecar, index + len(blocks))
        for index, (draft, sidecar) in enumerate(zip(drafts, provenance, strict=True))
    ]
    approved_snapshot = _blocks_document(
        [*blocks, *generated_blocks], revision_id=APPROVED_REVISION_ID
    )
    _write_json(
        original_blocks_path,
        {**approved_snapshot, "content_revision_state": "DRAFT"},
    )
    with pytest.raises(RequestError, match="APPROVED"):
        DatasetGenerationPlugin().prepare_sft(
            _prepare_request(
                tmp_path,
                original_blocks_path,
                mode="instruction",
                revision_id=APPROVED_REVISION_ID,
                stem="draft-must-not-export",
            )
        )

    _write_json(original_blocks_path, approved_snapshot)
    instruction_result = DatasetGenerationPlugin().prepare_sft(
        _prepare_request(
            tmp_path,
            original_blocks_path,
            mode="instruction",
            revision_id=APPROVED_REVISION_ID,
            stem="approved-instruction",
        )
    )
    with zipfile.ZipFile(tmp_path / "approved-instruction.zip") as archive:
        instruction_rows = [
            json.loads(line)
            for member in ("train.jsonl", "validation.jsonl", "test.jsonl")
            for line in archive.read(member).decode("utf-8").splitlines()
        ]
        instruction_sidecars = [
            json.loads(line)
            for line in archive.read("provenance.jsonl").decode("utf-8").splitlines()
        ]
    assert instruction_result["sample_count"] == 7
    assert len(instruction_rows) == 7
    assert instruction_result["files"]["train.jsonl"]["schema_fields"] == [
        "instruction",
        "input",
        "output",
    ]
    assert all(
        set(row) <= {"instruction", "input", "output"} for row in instruction_rows
    )
    assert all(
        "PRIVATE_MANAGEMENT_VALUE" not in json.dumps(row, ensure_ascii=False)
        and "PRIVATE_REVIEW_VALUE" not in json.dumps(row, ensure_ascii=False)
        for row in instruction_rows
    )
    instruction_family_splits: dict[str, set[str]] = defaultdict(set)
    for sidecar in instruction_sidecars:
        instruction_family_splits[sidecar["source_family_id"]].add(sidecar["split"])
    assert all(len(splits) == 1 for splits in instruction_family_splits.values())
    assert set(instruction_family_splits) == {"family-a", "family-b", "family-c"}
    generated_sidecars = [
        sidecar for sidecar in instruction_sidecars if sidecar["origin"] == "GENERATED"
    ]
    assert len(generated_sidecars) == 5
    source_blocks_by_sample = {
        item["sample_id"]: item["generation_receipt"]["sourceBlockIds"]
        for item in provenance
    }
    for sidecar in generated_sidecars:
        receipt = sidecar["generation"]
        assert receipt["recipe_id"] == "grounded-qa-v1"
        assert receipt["recipe_version"] == "1"
        assert receipt["provider_binding_id"] == "model.binding.v02-fixture"
        assert receipt["model"] == "fixture-qwen-compatible"
        assert receipt["budget"]["limits"]["max_calls"] == 10
        assert receipt["usage"]["providerPromptTokens"] == 12
        assert receipt["usage"]["providerCompletionTokens"] == 9
        assert receipt["source_block_ids"] == [
            source_blocks_by_sample[sidecar["sample_id"]][0]
        ]
        assert receipt["recipe_digest"].startswith("sha256:")
        assert receipt["generated_at"]
    assert any(
        warning["code"] == "SFT_BLOCK_NOT_STRUCTURED"
        for warning in instruction_result["warnings"]
    )

    conversation_result = DatasetGenerationPlugin().prepare_sft(
        _prepare_request(
            tmp_path,
            original_blocks_path,
            mode="conversation",
            revision_id=APPROVED_REVISION_ID,
            stem="approved-conversation",
        )
    )
    with zipfile.ZipFile(tmp_path / "approved-conversation.zip") as archive:
        conversation_rows = [
            json.loads(line)
            for line in archive.read("train.jsonl").decode("utf-8").splitlines()
        ]
        conversation_sidecars = [
            json.loads(line)
            for line in archive.read("provenance.jsonl").decode("utf-8").splitlines()
        ]
    assert conversation_result["sample_count"] == 2
    assert len(conversation_rows) == 2
    assert all(set(row) == {"conversations"} for row in conversation_rows)
    assert {sidecar["source_family_id"] for sidecar in conversation_sidecars} == {
        "family-a"
    }
    assert len({sidecar["group_id"] for sidecar in conversation_sidecars}) == 2
    assert len({sidecar["split"] for sidecar in conversation_sidecars}) == 1


def test_v02_provider_prompt_usage_overrun_warns_and_stops_later_calls(
    tmp_path: Path,
) -> None:
    blocks = [
        _content_block(
            f"input-{index}",
            "one-family",
            json.dumps(
                {"instruction": f"Question {index}?", "output": f"Answer {index}."}
            ),
            ordinal=index,
        )
        for index in range(3)
    ]
    result, provider, drafts, provenance = _generate(
        tmp_path,
        blocks,
        [
            {"prompt_tokens": 600, "completion_tokens": 3},
            {"prompt_tokens": 600, "completion_tokens": 3},
        ],
        {
            "max_examples": 3,
            "max_calls": 3,
            "max_input_tokens": 1_000,
            "max_output_tokens": 100,
        },
    )

    assert result["status"] == "SUCCEEDED"
    assert len(provider.requests) == len(drafts) == len(provenance) == 2
    assert result["budget"]["provider_prompt_tokens"] == 1_200
    assert result["budget"]["estimated_input_tokens"] < 1_000
    assert any(
        warning["code"] == "PROVIDER_INPUT_USAGE_EXCEEDED_BUDGET"
        for warning in result["warnings"]
    )


def test_v02_provider_completion_overrun_is_reported_in_result_and_receipt(
    tmp_path: Path,
) -> None:
    blocks = [
        _content_block(
            f"output-{index}",
            "one-family",
            json.dumps(
                {"instruction": f"Question {index}?", "output": f"Answer {index}."}
            ),
            ordinal=index,
        )
        for index in range(2)
    ]
    result, provider, drafts, provenance = _generate(
        tmp_path,
        blocks,
        [
            {"prompt_tokens": 5, "completion_tokens": 12},
            {"prompt_tokens": 5, "completion_tokens": 12},
        ],
        {
            "max_examples": 2,
            "max_calls": 2,
            "max_output_tokens": 20,
        },
    )

    assert result["status"] == "SUCCEEDED"
    assert len(provider.requests) == len(drafts) == len(provenance) == 2
    assert result["budget"]["provider_prompt_tokens"] == 10
    assert result["budget"]["provider_completion_tokens"] == 24
    warning_codes = {warning["code"] for warning in result["warnings"]}
    assert "PROVIDER_OUTPUT_USAGE_EXCEEDED_CALL_CAP" in warning_codes
    assert "PROVIDER_OUTPUT_USAGE_EXCEEDED_BUDGET" in warning_codes
    second_receipt = provenance[1]["generation_receipt"]
    assert second_receipt["budget"]["requestedOutputTokens"] == 8
    assert second_receipt["usage"]["providerPromptTokens"] == 5
    assert second_receipt["usage"]["providerCompletionTokens"] == 12


def test_v02_empty_non_training_blocks_are_skipped_before_text_validation(
    tmp_path: Path,
) -> None:
    eligible_text = "Eligible prose is the only training-approved context."
    blocks = [
        _content_block(
            "eligible-prose",
            "eligible-family",
            eligible_text,
            ordinal=0,
        ),
        _content_block(
            "empty-picture",
            "layout-family",
            "",
            ordinal=1,
            kind="picture",
            policy={
                "allowTraining": False,
                "allowKnowledge": True,
                "allowedUsePurposes": ["knowledge_retrieval"],
            },
        ),
        _content_block(
            "empty-layout",
            "blank-family",
            "",
            ordinal=2,
            kind="paragraph",
            policy={"allowTraining": False, "allowedUsePurposes": []},
        ),
    ]
    result, provider, drafts, provenance = _generate(
        tmp_path,
        blocks,
        [{"prompt_tokens": 18, "completion_tokens": 7}],
        {"max_examples": 1, "max_calls": 1, "max_input_tokens": 500},
    )

    assert result["status"] == "SUCCEEDED"
    assert result["budget"]["calls_used"] == 1
    assert result["draft_count"] == len(drafts) == len(provenance) == 1
    assert len(provider.requests) == 1
    user_context = provider.requests[0].messages[1].content
    assert eligible_text in user_context
    assert "empty-picture" not in user_context
    assert "empty-layout" not in user_context
    assert provenance[0]["source_family_id"] == "eligible-family"
    skipped = {
        warning["message"]
        for warning in result["warnings"]
        if warning["code"] == "BLOCK_SKIPPED_TRAINING_POLICY"
    }
    assert any(
        "empty-picture" in message and "layout-family" in message for message in skipped
    )
    assert any(
        "empty-layout" in message and "blank-family" in message for message in skipped
    )


def test_v02_empty_training_eligible_block_fails_before_provider_call(
    tmp_path: Path,
) -> None:
    block_path = tmp_path / "eligible-empty.json"
    _write_json(
        block_path,
        _blocks_document(
            [
                _content_block(
                    "eligible-empty",
                    "eligible-family",
                    "",
                    ordinal=0,
                )
            ]
        ),
    )
    plugin = DatasetGenerationPlugin(
        GenerationConfig(
            binding_id="model.binding.must-not-call",
            model_endpoint="grpc://127.0.0.1:1",
            model="fixture-qwen-compatible",
        )
    )
    request = {
        "blocks_path": str(block_path),
        "drafts_path": str(tmp_path / "eligible-empty-drafts.jsonl"),
        "provenance_path": str(tmp_path / "eligible-empty-provenance.jsonl"),
        "result_path": str(tmp_path / "eligible-empty-result.json"),
        "dataset_id": DATASET_ID,
        "content_revision_id": CONTENT_REVISION_ID,
        "processing_run_id": PROCESSING_RUN_ID,
        "generation": {"max_examples": 1, "max_calls": 1},
    }

    with pytest.raises(
        RequestError,
        match=r"blocks\[0\]\.text must be a non-empty string",
    ):
        plugin.generate_qa(request)
