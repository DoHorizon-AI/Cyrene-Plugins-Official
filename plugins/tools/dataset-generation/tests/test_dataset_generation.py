"""Focused contract and behavior checks for dataset.generation.v1.

中文:dataset.generation.v1 的重点契约与行为验证。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from cyrene_model_provider_contracts import (
    CAPABILITY_ID as MODEL_CAPABILITY_ID,
)
from cyrene_model_provider_contracts import (
    CHAT_COMPLETION_METHOD,
    CHAT_COMPLETION_REQUEST_TYPE_URL,
    CHAT_COMPLETION_RESPONSE_TYPE_URL,
    ChatCompletionChunk,
    ChatCompletionResponse,
    decode_chat_completion_request,
    encode_chat_completion_response,
)
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from dataset_generation import (
    CAPABILITY_ID,
    DatasetGenerationPlugin,
    GenerationConfig,
)

DATASET_ID = "00000000-0000-4000-8000-000000000001"
CONTENT_REVISION_ID = "00000000-0000-4000-8000-000000000002"
PROCESSING_RUN_ID = "00000000-0000-4000-8000-000000000003"
SOURCE_REVISION_ID = "00000000-0000-4000-8000-000000000004"
SPLIT = {"train": 0.7, "validation": 0.2, "test": 0.1}


class ScriptedModelProvider:
    """A deterministic provider fixture reached through DirectPluginRuntime.

    中文:通过 DirectPluginRuntime 调用的确定性 Provider fixture。
    """

    plugin_id = "test.scripted-generation-provider"
    version = "0.1.0"
    capabilities = (MODEL_CAPABILITY_ID,)

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.requests = []

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
        reply = self.replies.pop(0)
        response = ChatCompletionResponse(
            chunks=(
                ChatCompletionChunk(
                    delta=reply,
                    finish_reason="stop",
                    prompt_tokens=12,
                    completion_tokens=9,
                ),
            )
        )
        return True, _Payload(
            value=encode_chat_completion_response(response),
            type_url=CHAT_COMPLETION_RESPONSE_TYPE_URL,
        )


class _Payload:
    def __init__(self, value: bytes, type_url: str) -> None:
        self.value = value
        self.type_url = type_url


def _content_block(block_id: str, family_id: str, text: str) -> dict[str, Any]:
    return {
        "id": block_id,
        "sourceRevisionId": SOURCE_REVISION_ID,
        "sourceFamilyId": family_id,
        "ordinal": 0,
        "kind": "paragraph",
        "text": text,
        "origin": "HUMAN_EDITED",
        "policy": {"allowedUsePurposes": ["knowledge_retrieval", "model_training"]},
        "locator": {"page": 1},
    }


def _approved_blocks(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "cyrene.content.blocks.v1",
        "content_revision_id": CONTENT_REVISION_ID,
        "content_revision_state": "APPROVED",
        "blocks": list(blocks),
    }


def _write_blocks(path: Path, *blocks: dict[str, Any]) -> None:
    path.write_text(json.dumps(_approved_blocks(*blocks)), encoding="utf-8")


def _prepare_request(
    tmp_path: Path, blocks_path: Path, suffix: str = "one"
) -> dict[str, Any]:
    return {
        "blocks_path": str(blocks_path),
        "bundle_path": str(tmp_path / f"{suffix}.zip"),
        "result_path": str(tmp_path / f"{suffix}.result.json"),
        "dataset_id": DATASET_ID,
        "content_revision_id": CONTENT_REVISION_ID,
        "processing_run_id": PROCESSING_RUN_ID,
        "mode": "instruction",
        "split": SPLIT,
    }


def test_prepare_sft_bundle_is_stable_source_family_split_and_excludes_management_fields(
    tmp_path: Path,
) -> None:
    blocks = []
    for family_index in range(12):
        family = f"source-family-{family_index:02d}"
        for sample_index in range(2):
            row = {
                "instruction": f"Question {family_index}/{sample_index}",
                "input": "public context",
                "output": f"Answer {family_index}/{sample_index}",
            }
            blocks.append(
                _content_block(
                    f"block-{family_index:02d}-{sample_index}",
                    family,
                    json.dumps(row),
                )
            )
    source = tmp_path / "approved.json"
    _write_blocks(source, *blocks)

    plugin = DatasetGenerationPlugin()
    first = plugin.prepare_sft(_prepare_request(tmp_path, source, "first"))
    second = plugin.prepare_sft(_prepare_request(tmp_path, source, "second"))

    assert first["profile"] == "CYRENE_SFT_BUNDLE_V1"
    assert first["bundle_digest"] == second["bundle_digest"]
    assert first["sample_count"] == 24
    assert set(first["files"]) == {
        "train.jsonl",
        "validation.jsonl",
        "test.jsonl",
        "provenance.jsonl",
        "manifest.json",
    }
    with zipfile.ZipFile(tmp_path / "first.zip") as archive:
        assert set(archive.namelist()) == set(first["files"])
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["split_algorithm"] == "sha256-ranked-source-family-v1"
        sidecars = [
            json.loads(line)
            for line in archive.read("provenance.jsonl").decode("utf-8").splitlines()
        ]
        family_splits: dict[str, set[str]] = {}
        for entry in sidecars:
            family_splits.setdefault(entry["source_family_id"], set()).add(
                entry["split"]
            )
            assert entry["policy"] == {
                "allow_training": True,
                "allowed_use_purposes": ["knowledge_retrieval", "model_training"],
            }
        assert all(len(splits) == 1 for splits in family_splits.values())
        for member in ("train.jsonl", "validation.jsonl", "test.jsonl"):
            rows = [
                json.loads(line) for line in archive.read(member).decode().splitlines()
            ]
            for row in rows:
                assert set(row) == {"instruction", "input", "output"}
                assert "source_family_id" not in row
                assert "review_note" not in row
        assert manifest["files"]["train.jsonl"]["digest"].startswith("sha256:")


def test_prepare_sft_skips_prose_and_strips_known_import_management_fields(
    tmp_path: Path,
) -> None:
    source = tmp_path / "blocks.json"
    _write_blocks(
        source,
        _content_block("prose", "family-1", "A page of extracted PDF prose."),
        _content_block(
            "managed-row",
            "family-2",
            json.dumps(
                {
                    "instruction": "Question",
                    "output": "Answer",
                    "review_note": "do not train this metadata",
                }
            ),
        ),
    )

    result = DatasetGenerationPlugin().prepare_sft(_prepare_request(tmp_path, source))

    assert result["sample_count"] == 1
    assert [warning["code"] for warning in result["warnings"]] == [
        "SFT_BLOCK_NOT_STRUCTURED"
    ]
    with zipfile.ZipFile(tmp_path / "one.zip") as archive:
        rows = [
            json.loads(line)
            for line in archive.read("train.jsonl").decode().splitlines()
        ]
        sidecars = [
            json.loads(line)
            for line in archive.read("provenance.jsonl").decode().splitlines()
        ]
    assert len(rows) == 1
    assert rows[0] == {"instruction": "Question", "output": "Answer"}
    assert "review_note" not in rows[0]
    assert sidecars[0]["source_family_id"] == "family-2"
    assert sidecars[0]["policy"]["allow_training"] is True


def test_manual_sample_path_builds_sft_without_a_provider_binding(
    tmp_path: Path,
) -> None:
    manual = tmp_path / "manual-samples.json"
    manual.write_text(
        json.dumps(
            {
                "schema_version": "cyrene.dataset.sft-samples.v1",
                "content_revision_id": CONTENT_REVISION_ID,
                "content_revision_state": "APPROVED",
                "samples": [
                    {
                        "sample_id": "manual-sample-1",
                        "source_family_id": "manual-family-1",
                        "content": {
                            "instruction": "Question",
                            "input": "Context",
                            "output": "Answer",
                        },
                        "origin": "HUMAN_EDITED",
                        "policy": {"allowedUsePurposes": ["model_training"]},
                        "citations": [
                            {
                                "source_revision_id": SOURCE_REVISION_ID,
                                "block_id": "manual-block-1",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    request = _prepare_request(tmp_path, manual, "manual")
    del request["blocks_path"]
    request["samples_path"] = str(manual)

    result = DatasetGenerationPlugin().prepare_sft(request)

    assert result["sample_count"] == 1
    with zipfile.ZipFile(tmp_path / "manual.zip") as archive:
        rows = [
            json.loads(line)
            for line in archive.read("train.jsonl").decode().splitlines()
        ]
        provenance = [
            json.loads(line)
            for line in archive.read("provenance.jsonl").decode().splitlines()
        ]
    assert rows == [{"instruction": "Question", "input": "Context", "output": "Answer"}]
    assert provenance[0]["sample_id"] == "manual-sample-1"
    assert provenance[0]["source_family_id"] == "manual-family-1"


def test_fixed_jsonl_fixture_splits_ten_source_families_and_excludes_management_data(
    tmp_path: Path,
) -> None:
    fixture = (
        Path(__file__).resolve().parents[5]
        / "workspace/tests/fixtures/data-tools-trial/trial-training.jsonl"
    )
    source_lines = fixture.read_text(encoding="utf-8").splitlines()
    assert len(source_lines) == 20
    blocks = [
        {
            "id": f"fixture-block-{index:02d}",
            "sourceRevisionId": SOURCE_REVISION_ID,
            "ordinal": index,
            "kind": "text" if index % 2 else "paragraph",
            "text": line,
            "origin": "EXTRACTED",
            "policy": {
                "allowKnowledge": True,
                "allowTraining": True,
                "allowedPrincipalRefs": ["org:trial-alpha"],
                "allowedUsePurposes": ["knowledge_retrieval", "model_training"],
            },
        }
        for index, line in enumerate(source_lines)
    ]
    approved_path = tmp_path / "fixture-approved.json"
    _write_blocks(approved_path, *blocks)
    request = _prepare_request(tmp_path, approved_path, "fixture-first")
    request.pop("split")

    plugin = DatasetGenerationPlugin()
    first = plugin.prepare_sft(request)
    second_request = _prepare_request(tmp_path, approved_path, "fixture-second")
    second_request.pop("split")
    second = plugin.prepare_sft(second_request)

    assert first["sample_count"] == 20
    assert first["bundle_digest"] == second["bundle_digest"]
    assert first["split_stats"]["samples"] == {"train": 16, "validation": 2, "test": 2}
    with zipfile.ZipFile(tmp_path / "fixture-first.zip") as archive:
        family_splits: dict[str, set[str]] = {}
        sidecars = [
            json.loads(line)
            for line in archive.read("provenance.jsonl").decode("utf-8").splitlines()
        ]
        for entry in sidecars:
            family_splits.setdefault(entry["source_family_id"], set()).add(
                entry["split"]
            )
            assert entry["policy"]["allowed_principal_refs"] == ["org:trial-alpha"]
        assert len(family_splits) == 10
        assert all(len(splits) == 1 for splits in family_splits.values())
        for member in ("train.jsonl", "validation.jsonl", "test.jsonl"):
            rows = [
                json.loads(line)
                for line in archive.read(member).decode("utf-8").splitlines()
            ]
            for row in rows:
                assert set(row) == {"instruction", "input", "output"}
                serialized = json.dumps(row, ensure_ascii=False)
                assert "TRIAL_INTERNAL_REVIEW_NOTE" not in serialized
                assert "_acl" not in serialized
                assert "_operatorAnnotation" not in serialized


def test_generate_qa_uses_direct_model_provider_binding_and_checkpoints_pending_drafts(
    tmp_path: Path,
) -> None:
    provider = ScriptedModelProvider(
        [
            '{"question":"What is Cyrene?","answer":"A tools trial."}',
            '{"question":"What is the source?","answer":"An approved document."}',
        ]
    )
    provider_server, provider_ref = serve(
        provider, MODEL_CAPABILITY_ID, "1", "127.0.0.1:0"
    )
    config = GenerationConfig(
        binding_id="model.binding.trial",
        model_endpoint=provider_ref,
        model="scripted-model",
        max_tokens_per_call=32,
    )
    generation_plugin = DatasetGenerationPlugin(config)
    generation_server, generation_ref = serve(
        generation_plugin, CAPABILITY_ID, "1", "127.0.0.1:0"
    )
    client = DirectPluginClient.for_local_connection_ref(generation_ref)
    blocks_path = tmp_path / "blocks.json"
    _write_blocks(
        blocks_path,
        _content_block("block-a", "same-family", "Cyrene is a tools trial."),
        _content_block("block-b", "same-family", "The source is an approved document."),
    )
    request = {
        "blocks_path": str(blocks_path),
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "provenance_path": str(tmp_path / "draft-provenance.jsonl"),
        "result_path": str(tmp_path / "generation-result.json"),
        "dataset_id": DATASET_ID,
        "content_revision_id": CONTENT_REVISION_ID,
        "processing_run_id": PROCESSING_RUN_ID,
        "split": SPLIT,
        "generation": {
            "max_examples": 2,
            "max_calls": 2,
            "max_input_tokens": 1000,
            "max_output_tokens": 64,
        },
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
            deadline_seconds=5,
        )
    finally:
        client.close()
        generation_server.stop(grace=None).wait()
        provider_server.stop(grace=None).wait()

    result = json.loads(response.value)
    draft_rows = [
        json.loads(line)
        for line in (tmp_path / "drafts.jsonl").read_text().splitlines()
    ]
    provenance = [
        json.loads(line)
        for line in (tmp_path / "draft-provenance.jsonl").read_text().splitlines()
    ]
    assert response.type_url == f"type.cyrene.io/{CAPABILITY_ID}.generate_qa.response"
    assert result["status"] == "SUCCEEDED"
    assert result["draft_count"] == 2
    assert result["budget"]["calls_used"] == 2
    assert result["budget"]["provider_completion_tokens"] == 18
    assert result["provider"] == {
        "binding_id": "model.binding.trial",
        "model": "scripted-model",
    }
    assert len(provider.requests) == 2
    assert all(request.model == "scripted-model" for request in provider.requests)
    assert all(set(row) == {"instruction", "output"} for row in draft_rows)
    assert all(item["origin"] == "GENERATED" for item in provenance)
    assert all(item["review_state"] == "PENDING" for item in provenance)
    assert all(
        set(item["generation_receipt"])
        == {
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
        for item in provenance
    )
    assert all(
        item["generation_receipt"]["sourceBlockIds"] == [item["block_id"]]
        for item in provenance
    )
    assert all(item["split"] == provenance[0]["split"] for item in provenance)
    assert len({item["sample_id"] for item in provenance}) == 2


def test_generate_qa_without_binding_fails_closed_but_manual_sft_is_available(
    tmp_path: Path,
) -> None:
    plugin = DatasetGenerationPlugin()
    request = {
        "blocks_path": str(tmp_path / "missing.json"),
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "provenance_path": str(tmp_path / "provenance.jsonl"),
        "result_path": str(tmp_path / "result.json"),
        "dataset_id": DATASET_ID,
        "content_revision_id": CONTENT_REVISION_ID,
        "processing_run_id": PROCESSING_RUN_ID,
        "generation": {"max_examples": 1, "max_calls": 1},
    }
    ok, error = plugin.on_invoke(
        CAPABILITY_ID,
        "generate_qa",
        json.dumps(request).encode(),
        request_type_url=f"type.cyrene.io/{CAPABILITY_ID}.generate_qa.request",
    )
    assert ok is False
    assert "UNAVAILABLE" in error


def test_one_call_direct_runtime_generation_approval_and_sft_handoff(
    tmp_path: Path,
) -> None:
    provider = ScriptedModelProvider(
        ['{"question":"What is in the source?","answer":"A public trial handbook."}']
    )
    provider_server, provider_ref = serve(
        provider, MODEL_CAPABILITY_ID, "1", "127.0.0.1:0"
    )
    generation_plugin = DatasetGenerationPlugin(
        GenerationConfig(
            binding_id="model.binding.fixture",
            model_endpoint=provider_ref,
            model="scripted-model",
            max_tokens_per_call=32,
        )
    )
    generation_server, generation_ref = serve(
        generation_plugin, CAPABILITY_ID, "1", "127.0.0.1:0"
    )
    client = DirectPluginClient.for_local_connection_ref(generation_ref)
    source_path = tmp_path / "approved-source.json"
    _write_blocks(
        source_path,
        _content_block("source-block", "fixture-family", "A public trial handbook."),
    )
    generate_request = {
        "blocks_path": str(source_path),
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "provenance_path": str(tmp_path / "draft-provenance.jsonl"),
        "result_path": str(tmp_path / "generation-result.json"),
        "dataset_id": DATASET_ID,
        "content_revision_id": CONTENT_REVISION_ID,
        "processing_run_id": PROCESSING_RUN_ID,
        "generation": {"max_examples": 1, "max_calls": 1},
    }
    try:
        generated = client.invoke(
            capability=CAPABILITY_ID,
            interface_version="1",
            method="generate_qa",
            request=DirectPayload(
                type_url=f"type.cyrene.io/{CAPABILITY_ID}.generate_qa.request",
                value=json.dumps(generate_request).encode("utf-8"),
            ),
            deadline_seconds=5,
        )
    finally:
        client.close()
        generation_server.stop(grace=None).wait()
        provider_server.stop(grace=None).wait()

    generated_result = json.loads(generated.value)
    draft = json.loads((tmp_path / "drafts.jsonl").read_text(encoding="utf-8"))
    generated_sidecar = json.loads(
        (tmp_path / "draft-provenance.jsonl").read_text(encoding="utf-8")
    )
    assert generated_result["status"] == "SUCCEEDED"
    assert generated_result["budget"]["calls_used"] == 1
    assert draft == {
        "instruction": "What is in the source?",
        "output": "A public trial handbook.",
    }
    assert generated_sidecar["review_state"] == "PENDING"
    assert len(provider.requests) == 1

    # Simulate Catalyst's human approval creating a new approved revision and
    # carrying the private sidecar receipt into ContentBlock.generationReceipt.
    generated_block = {
        "id": "approved-generated-block",
        "sourceRevisionId": SOURCE_REVISION_ID,
        "sourceFamilyId": "fixture-family",
        "sampleId": generated_sidecar["sample_id"],
        "ordinal": 0,
        "kind": "paragraph",
        "text": json.dumps(draft, ensure_ascii=False),
        "origin": "GENERATED",
        "policy": {"allowedUsePurposes": ["model_training"]},
        "generationReceipt": generated_sidecar["generation_receipt"],
    }
    approved_generated_path = tmp_path / "approved-generated.json"
    _write_blocks(approved_generated_path, generated_block)
    sft = DatasetGenerationPlugin().prepare_sft(
        _prepare_request(tmp_path, approved_generated_path, "approved-generated")
    )
    with zipfile.ZipFile(tmp_path / "approved-generated.zip") as archive:
        train_rows = [
            json.loads(line)
            for line in archive.read("train.jsonl").decode("utf-8").splitlines()
        ]
        sidecars = [
            json.loads(line)
            for line in archive.read("provenance.jsonl").decode("utf-8").splitlines()
        ]
    assert sft["sample_count"] == 1
    assert train_rows == [draft]
    assert sidecars[0]["origin"] == "GENERATED"
    assert (
        sidecars[0]["generation"]["recipe_digest"]
        == generated_sidecar["generation_receipt"]["recipeDigest"]
    )
