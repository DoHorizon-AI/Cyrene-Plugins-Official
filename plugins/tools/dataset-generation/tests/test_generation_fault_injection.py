"""Resilience and fault-injection tests for dataset generation (Phase 4).

Tests:
- Model provider timeout
- Model provider 429 / RESOURCE_EXHAUSTED
- Model provider 500 / INTERNAL
- Malformed output (invalid JSON)
- Malformed output (missing fields or unexpected keys)
- Empty output
- Budget exceeded (max_calls, max_examples, max_output_tokens, max_input_tokens)
- Cancellation
- Idempotency & retry
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cyrene_model_provider_contracts import (
    CAPABILITY_ID as MODEL_CAPABILITY_ID,
    CHAT_COMPLETION_METHOD,
    CHAT_COMPLETION_REQUEST_TYPE_URL,
    CHAT_COMPLETION_RESPONSE_TYPE_URL,
    ChatCompletionChunk,
    ChatCompletionResponse,
    decode_chat_completion_request,
    encode_chat_completion_response,
)
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, DirectPluginError, serve
from dataset_generation import (
    CAPABILITY_ID,
    DatasetGenerationPlugin,
    GenerationConfig,
    GenerationUpstreamError,
    RequestError,
)

DATASET_ID = "00000000-0000-4000-8000-000000000001"
CONTENT_REVISION_ID = "00000000-0000-4000-8000-000000000002"
PROCESSING_RUN_ID = "00000000-0000-4000-8000-000000000003"
SOURCE_REVISION_ID = "00000000-0000-4000-8000-000000000004"


class _Payload:
    def __init__(self, value: bytes, type_url: str) -> None:
        self.value = value
        self.type_url = type_url


class FaultyModelProvider:
    """Configurable provider that can inject timeouts, errors, and malformed outputs."""

    plugin_id = "test.faulty-generation-provider"
    version = "0.1.0"
    capabilities = (MODEL_CAPABILITY_ID,)

    def __init__(
        self,
        *,
        error_to_raise: str | None = None,
        raw_replies: list[str] | None = None,
    ) -> None:
        self.error_to_raise = error_to_raise
        self.replies = list(raw_replies or [])
        self.call_count = 0

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
        self.call_count += 1
        if self.error_to_raise:
            return False, self.error_to_raise
        reply = self.replies.pop(0) if self.replies else ""
        response = ChatCompletionResponse(
            chunks=(
                ChatCompletionChunk(
                    delta=reply,
                    finish_reason="stop",
                    prompt_tokens=10,
                    completion_tokens=8,
                ),
            )
        )
        return True, _Payload(
            value=encode_chat_completion_response(response),
            type_url=CHAT_COMPLETION_RESPONSE_TYPE_URL,
        )


def _content_block(block_id: str, family_id: str, text: str) -> dict[str, Any]:
    return {
        "id": block_id,
        "sourceRevisionId": SOURCE_REVISION_ID,
        "sourceFamilyId": family_id,
        "ordinal": 0,
        "kind": "paragraph",
        "text": text,
        "origin": "HUMAN_EDITED",
        "policy": {"allowedUsePurposes": ["model_training"]},
        "locator": {"page": 1},
    }


def _write_approved_blocks(path: Path, *blocks: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "cyrene.content.blocks.v1",
                "content_revision_id": CONTENT_REVISION_ID,
                "content_revision_state": "APPROVED",
                "blocks": list(blocks),
            }
        ),
        encoding="utf-8",
    )


def _setup_pipeline(tmp_path: Path, provider: FaultyModelProvider, *, budget: dict[str, Any] | None = None):
    provider_server, provider_ref = serve(provider, MODEL_CAPABILITY_ID, "1", "127.0.0.1:0")
    plugin = DatasetGenerationPlugin(
        GenerationConfig(
            binding_id="model.binding.fault",
            model_endpoint=provider_ref,
            model="fault-model",
            max_tokens_per_call=32,
            timeout_seconds=2,
        )
    )
    blocks_path = tmp_path / "source-blocks.json"
    _write_approved_blocks(
        blocks_path,
        _content_block("block-1", "family-1", "First paragraph content for training."),
        _content_block("block-2", "family-2", "Second paragraph content for training."),
    )
    request = {
        "blocks_path": str(blocks_path),
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "provenance_path": str(tmp_path / "provenance.jsonl"),
        "result_path": str(tmp_path / "result.json"),
        "dataset_id": DATASET_ID,
        "content_revision_id": CONTENT_REVISION_ID,
        "processing_run_id": PROCESSING_RUN_ID,
        "generation": budget or {"max_examples": 2, "max_calls": 2},
    }
    return provider_server, plugin, request


def test_provider_timeout_simulation(tmp_path: Path) -> None:
    """When provider times out, fail cleanly with FAILED_AMBIGUOUS_PROVIDER_RESULT checkpoint."""
    provider = FaultyModelProvider(error_to_raise="DEADLINE_EXCEEDED: model provider timed out after 2s")
    server, plugin, req = _setup_pipeline(tmp_path, provider)
    try:
        with pytest.raises(GenerationUpstreamError, match="timed out"):
            plugin.generate_qa(req)
        # Verify checkpoint
        res = json.loads(Path(req["result_path"]).read_text(encoding="utf-8"))
        assert res["status"] == "FAILED_AMBIGUOUS_PROVIDER_RESULT"
        assert res["budget"]["calls_used"] == 1
    finally:
        server.stop(grace=None).wait()


def test_provider_429_rate_limit_simulation(tmp_path: Path) -> None:
    """When provider returns 429/RESOURCE_EXHAUSTED, fail without silent retry."""
    provider = FaultyModelProvider(error_to_raise="RESOURCE_EXHAUSTED: rate limit exceeded (HTTP 429)")
    server, plugin, req = _setup_pipeline(tmp_path, provider)
    try:
        with pytest.raises(GenerationUpstreamError, match="rate limit exceeded"):
            plugin.generate_qa(req)
        res = json.loads(Path(req["result_path"]).read_text(encoding="utf-8"))
        assert res["status"] == "FAILED_AMBIGUOUS_PROVIDER_RESULT"
        # Must not retry silently
        assert provider.call_count == 1
    finally:
        server.stop(grace=None).wait()


def test_provider_500_internal_error_simulation(tmp_path: Path) -> None:
    """When provider returns 500/INTERNAL, fail cleanly and record checkpoint."""
    provider = FaultyModelProvider(error_to_raise="INTERNAL: upstream server error (HTTP 500)")
    server, plugin, req = _setup_pipeline(tmp_path, provider)
    try:
        with pytest.raises(GenerationUpstreamError, match="upstream server error"):
            plugin.generate_qa(req)
        res = json.loads(Path(req["result_path"]).read_text(encoding="utf-8"))
        assert res["status"] == "FAILED_AMBIGUOUS_PROVIDER_RESULT"
    finally:
        server.stop(grace=None).wait()


def test_malformed_output_not_json(tmp_path: Path) -> None:
    """When provider returns invalid JSON, fail with FAILED_PROVIDER_RESPONSE checkpoint."""
    provider = FaultyModelProvider(raw_replies=["I am an AI and here is your answer: {not json"])
    server, plugin, req = _setup_pipeline(tmp_path, provider)
    try:
        with pytest.raises(GenerationUpstreamError, match="not one JSON object"):
            plugin.generate_qa(req)
        res = json.loads(Path(req["result_path"]).read_text(encoding="utf-8"))
        assert res["status"] == "FAILED_PROVIDER_RESPONSE"
    finally:
        server.stop(grace=None).wait()


def test_malformed_output_missing_fields(tmp_path: Path) -> None:
    """When provider returns JSON missing question or answer, fail cleanly."""
    provider = FaultyModelProvider(raw_replies=['{"response": "missing question and answer keys"}'])
    server, plugin, req = _setup_pipeline(tmp_path, provider)
    try:
        with pytest.raises(GenerationUpstreamError, match="only question and answer"):
            plugin.generate_qa(req)
        res = json.loads(Path(req["result_path"]).read_text(encoding="utf-8"))
        assert res["status"] == "FAILED_PROVIDER_RESPONSE"
    finally:
        server.stop(grace=None).wait()


def test_empty_output(tmp_path: Path) -> None:
    """When provider returns empty string, fail cleanly."""
    provider = FaultyModelProvider(raw_replies=["   "])
    server, plugin, req = _setup_pipeline(tmp_path, provider)
    try:
        with pytest.raises(GenerationUpstreamError):
            plugin.generate_qa(req)
        res = json.loads(Path(req["result_path"]).read_text(encoding="utf-8"))
        assert res["status"] == "FAILED_PROVIDER_RESPONSE"
    finally:
        server.stop(grace=None).wait()


def test_budget_exceeded_max_calls(tmp_path: Path) -> None:
    """When budget max_calls is reached, generation stops cleanly with warning."""
    provider = FaultyModelProvider(
        raw_replies=[
            '{"question":"Q1?","answer":"A1"}',
            '{"question":"Q2?","answer":"A2"}',
        ]
    )
    budget = {"max_examples": 10, "max_calls": 1}
    server, plugin, req = _setup_pipeline(tmp_path, provider, budget=budget)
    try:
        res = plugin.generate_qa(req)
        assert res["status"] == "SUCCEEDED"
        assert res["budget"]["calls_used"] == 1
        assert res["draft_count"] == 1
        assert any(w["code"] == "MAX_CALLS_REACHED" for w in res["warnings"])
    finally:
        server.stop(grace=None).wait()


def test_budget_exceeded_output_tokens(tmp_path: Path) -> None:
    """When max_output_tokens is exhausted, generation stops with OUTPUT_BUDGET_EXHAUSTED."""
    provider = FaultyModelProvider(
        raw_replies=[
            '{"question":"Q1?","answer":"A1"}',
            '{"question":"Q2?","answer":"A2"}',
        ]
    )
    # Output budget 8 tokens (first reply consumes 8 tokens)
    budget = {"max_examples": 10, "max_calls": 10, "max_output_tokens": 8}
    server, plugin, req = _setup_pipeline(tmp_path, provider, budget=budget)
    try:
        res = plugin.generate_qa(req)
        assert res["status"] == "SUCCEEDED"
        assert res["draft_count"] == 1
        assert any(w["code"] == "OUTPUT_BUDGET_EXHAUSTED" for w in res["warnings"])
    finally:
        server.stop(grace=None).wait()


def test_cancellation(tmp_path: Path) -> None:
    """When cancellation is triggered, checkpoints with CANCELLED status."""
    class FakeCancellation:
        def is_cancelled(self) -> bool:
            return True

    provider = FaultyModelProvider(raw_replies=['{"question":"Q?","answer":"A"}'])
    server, plugin, req = _setup_pipeline(tmp_path, provider)
    try:
        with pytest.raises(RequestError, match="cancelled"):
            plugin.generate_qa(req, cancellation=FakeCancellation())
        res = json.loads(Path(req["result_path"]).read_text(encoding="utf-8"))
        assert res["status"] == "CANCELLED"
    finally:
        server.stop(grace=None).wait()


def test_idempotency_rerun(tmp_path: Path) -> None:
    """Re-running with identical inputs produces stable SFT bundle."""
    source_block = _content_block("b-1", "fam-1", '{"instruction":"Q","output":"A"}')
    source_block["kind"] = "code"
    blocks_path = tmp_path / "blocks.json"
    _write_approved_blocks(blocks_path, source_block)

    plugin = DatasetGenerationPlugin()
    req1 = {
        "blocks_path": str(blocks_path),
        "bundle_path": str(tmp_path / "bundle1.zip"),
        "result_path": str(tmp_path / "res1.json"),
        "dataset_id": DATASET_ID,
        "content_revision_id": CONTENT_REVISION_ID,
        "processing_run_id": PROCESSING_RUN_ID,
        "mode": "instruction",
        "split": {"train": 0.8, "validation": 0.1, "test": 0.1},
    }
    res1 = plugin.prepare_sft(req1)

    req2 = {
        **req1,
        "bundle_path": str(tmp_path / "bundle2.zip"),
        "result_path": str(tmp_path / "res2.json"),
    }
    res2 = plugin.prepare_sft(req2)

    assert res1["bundle_digest"] == res2["bundle_digest"]
    assert res1["split_stats"] == res2["split_stats"]
