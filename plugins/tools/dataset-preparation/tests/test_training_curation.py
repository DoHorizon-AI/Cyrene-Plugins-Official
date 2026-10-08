"""Streaming and provenance tests for Catalyst training-data curation.

中文：验证训练数据识别、诊断、来源保留和断点恢复。
"""

from __future__ import annotations

import hashlib
import json
import tracemalloc
from pathlib import Path
from typing import Any

import pytest
from dataset_preparation import DatasetPreparationPlugin
from training_curation import (
    CurationCancelled,
    curate_training_records,
    remap_training_record,
)

TRAINING_POLICY = {
    "allowTraining": True,
    "allowedUsePurposes": ["model_training"],
}
RECIPE = {
    "id": "training-curation-v1",
    "version": "1",
    "format": "auto",
    "fieldMapping": {},
    "roleMapping": {},
    "maxCharacters": 256,
    "minCharacters": 2,
    "unicodeNormalization": "NFC",
}
RECIPE_DIGEST = "sha256:" + "a" * 64


def _source(
    path: Path, revision_id: str, family_id: str = "family-a"
) -> dict[str, Any]:
    return {
        "source_path": str(path),
        "source_revision_id": revision_id,
        "source_family_id": family_id,
        "filename": path.name,
        "policy": TRAINING_POLICY,
    }


def _read_envelopes(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_mixed_sources(tmp_path: Path) -> tuple[Path, Path]:
    """Write stable mixed-format fixtures without depending on other repositories."""

    chatml = (
        "<|im_start|>system<|im_sep|>Be concise.<|im_end|>"
        "<|im_start|>user<|im_sep|>你好<|im_end|>"
        "<|im_start|>assistant<|im_sep|>Hello!<|fim_suffix|>"
    )
    rows = [
        {
            "sample_id": "alpaca-1",
            "instruction": "Cafe\u0301",
            "input": "friend",
            "output": "Bonjour",
        },
        {"prompt": "What is 2 + 2?", "completion": "4"},
        {
            "conversation_id": "dialog-1",
            "messages": [
                {"role": "system", "content": "Use short answers."},
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"},
                {"role": "assistant", "content": "Second answer"},
            ],
        },
        {
            "conversations": [
                {"from": "human", "value": "你好"},
                {"from": "gpt", "value": "Hello"},
            ]
        },
        json.dumps(chatml, ensure_ascii=False),
        "{broken-json",
        "",
        {
            "messages": [
                {"role": "critic", "content": "thinking"},
                {"role": "assistant", "content": "answer"},
            ]
        },
        {"prompt": "Empty response?", "completion": ""},
        {"prompt": "Long sample", "completion": "x" * 300},
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": "run code",
                    "tool_calls": [{"name": "python"}],
                }
            ]
        },
        {
            "messages": [
                {"role": "user", "content": [{"type": "image", "url": "image-ref"}]},
                {"role": "assistant", "content": "image"},
            ]
        },
        {
            "instruction": "Do not train this row",
            "output": "Restricted answer",
            "policy": {"allowTraining": False, "allowedUsePurposes": []},
        },
        {"instruction": "Missing answer"},
        {"prompt": "same prompt", "completion": "first answer"},
        {"prompt": "same prompt", "completion": "different answer"},
    ]
    jsonl = tmp_path / "mixed.jsonl"
    # Leave malformed and blank entries as literal source lines.
    lines: list[str] = []
    for row in rows:
        if row == "":
            lines.append("")
        elif row == "{broken-json":
            lines.append(row)
        else:
            lines.append(json.dumps(row, ensure_ascii=False))
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    array_json = tmp_path / "second.json"
    array_json.write_text(
        json.dumps(
            [
                {"instruction": "Cafe\u0301", "input": "friend", "output": "Bonjour"},
                {
                    "messages": [
                        {"role": "user", "content": "Another"},
                        {"role": "assistant", "content": "row"},
                    ]
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return jsonl, array_json


def _curate(
    sources: list[dict[str, Any]],
    output: Path,
    checkpoint: Path,
    recipe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return curate_training_records(
        sources=sources,
        result_path=output,
        checkpoint_path=checkpoint,
        recipe_digest=RECIPE_DIGEST,
        recipe=RECIPE if recipe is None else recipe,
    )


def test_mixed_formats_preserve_raw_lineage_and_report_issues(tmp_path: Path) -> None:
    jsonl, array_json = _write_mixed_sources(tmp_path)
    output = tmp_path / "records.jsonl"
    receipt = _curate(
        [
            _source(jsonl, "revision-one"),
            _source(array_json, "revision-two", "family-b"),
        ],
        output,
        tmp_path / "checkpoint.json",
    )

    envelopes = _read_envelopes(output)
    by_format = {record["detectedFormat"] for record in envelopes}
    all_codes = {issue["code"] for record in envelopes for issue in record["issues"]}
    first = next(record for record in envelopes if record["sampleId"] == "alpaca-1")
    multi_turn = next(
        record for record in envelopes if record.get("conversationId") == "dialog-1"
    )
    prohibited = next(
        record
        for record in envelopes
        if isinstance(record["rawRecord"], dict)
        and record["rawRecord"].get("policy", {}).get("allowTraining") is False
    )

    assert by_format >= {
        "alpaca",
        "promptCompletion",
        "messages",
        "sharegpt",
        "chatml",
        "unknown",
    }
    assert first["normalized"]["messages"][0]["content"] == "Café\nfriend"
    assert first["rawRecord"]["instruction"] == "Cafe\u0301"
    assert multi_turn["normalized"]["messages"] == [
        {"role": "system", "content": "Use short answers."},
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Second question"},
        {"role": "assistant", "content": "Second answer"},
    ]
    assert multi_turn["conversationId"] == "dialog-1"
    assert prohibited["disposition"] == "excluded"
    assert "POLICY_BLOCKED" in {issue["code"] for issue in prohibited["issues"]}
    assert {
        "INVALID_JSON",
        "EMPTY_LINE",
        "DUPLICATE_EXACT",
        "UNSUPPORTED_ROLE",
        "UNSUPPORTED_TOOL_CALL",
        "UNSUPPORTED_MULTIMODAL",
        "EMPTY_RESPONSE",
        "SAMPLE_TOO_LONG",
        "FIELD_MISSING",
        "CONTENT_CONFLICT",
    } <= all_codes
    assert (
        receipt["counts"]["total"]
        == len(envelopes)
        == len(_write_mixed_sources_count(jsonl)) + 2
    )
    counts = receipt["counts"]
    assert (
        counts["eligible"] + counts["pendingReview"] + counts["excluded"]
        == counts["total"]
    )
    assert counts["duplicateCandidates"] == 1
    exact_duplicate = next(
        record
        for record in envelopes
        if "DUPLICATE_EXACT" in {issue["code"] for issue in record["issues"]}
    )
    assert exact_duplicate["processingHistory"][-1]["operation"] == "deduplicate"
    assert receipt["sources"][0]["diagnostic_counts"]["INVALID_JSON"] == 1
    assert receipt["sources"][0]["diagnostic_counts"]["EMPTY_LINE"] == 1
    malformed = next(
        record
        for record in envelopes
        if "INVALID_JSON" in {issue["code"] for issue in record["issues"]}
    )
    assert malformed["disposition"] == "review"
    assert malformed["normalized"] is None
    assert malformed["rawLine"] == "{broken-json\n"
    assert malformed["locator"]["itemRef"].startswith("line:")
    assert (
        receipt["digest"] == "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest()
    )


def _write_mixed_sources_count(path: Path) -> list[str]:
    """Return physical JSONL lines, including the explicit blank row."""

    return path.read_text(encoding="utf-8").splitlines()


def test_manual_field_and_role_mapping_use_same_normalizer(tmp_path: Path) -> None:
    custom = tmp_path / "custom.jsonl"
    custom.write_text(
        json.dumps(
            {
                "task": "  say hello\r\n",
                "context": "friend",
                "answer": "hi",
                "record_key": "external-7",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recipe = {
        **RECIPE,
        "format": "alpaca",
        "fieldMapping": {
            "instruction": "task",
            "input": "context",
            "output": "answer",
            "sampleId": "record_key",
        },
    }
    output = tmp_path / "custom-records.jsonl"
    _curate(
        [_source(custom, "custom-rev")],
        output,
        tmp_path / "custom-checkpoint.json",
        recipe,
    )
    record = _read_envelopes(output)[0]

    assert record["sampleId"] == "external-7"
    assert record["normalized"]["messages"] == [
        {"role": "user", "content": "say hello\nfriend"},
        {"role": "assistant", "content": "hi"},
    ]

    role_record = {
        **record,
        "rawRecord": {
            "messages": [
                {"role": "coach", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ]
        },
        "detectedFormat": "messages",
        "normalized": None,
    }
    remapped = remap_training_record(
        record=role_record,
        recipe={**RECIPE, "roleMapping": {"coach": "user"}},
        recipe_digest=RECIPE_DIGEST,
        format_hint="messages",
        role_mapping={"coach": "user"},
        note="mapped the local speaker role",
    )
    assert remapped["normalized"]["messages"][0]["role"] == "user"
    assert remapped["disposition"] == "eligible"
    assert remapped["processingHistory"][-1]["operation"] == "humanRemap"
    assert (
        remapped["processingHistory"][-1]["rawRecordBefore"] == role_record["rawRecord"]
    )
    assert remapped["processingHistory"][-1]["note"] == "mapped the local speaker role"


def test_chatml_text_field_is_detected_and_missing_message_content_is_blocked(
    tmp_path: Path,
) -> None:
    source = tmp_path / "embedded-chatml.jsonl"
    chatml = (
        "<|im_start|>user\nQuestion<|im_end|>"
        "<|im_start|>assistant\nAnswer<|fim_suffix|>"
    )
    source.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": None},
                    {"role": "assistant", "content": "answer"},
                ],
                "sample_id": "",
            }
        )
        + "\n"
        + json.dumps({"text": chatml})
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "embedded-chatml-records.jsonl"
    receipt = _curate(
        [_source(source, "embedded-chatml-rev")],
        output,
        tmp_path / "embedded-chatml-checkpoint.json",
    )
    envelopes = _read_envelopes(output)

    assert envelopes[0]["disposition"] == "review"
    assert envelopes[0]["sampleId"] == "embedded-chatml-rev:0"
    missing = next(
        issue for issue in envelopes[0]["issues"] if issue["code"] == "FIELD_MISSING"
    )
    assert missing["severity"] == "error"
    assert envelopes[1]["detectedFormat"] == "chatml"
    assert envelopes[1]["normalized"]["messages"] == [
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": "Answer"},
    ]
    assert receipt["counts"]["eligible"] == 1
    assert receipt["counts"]["pendingReview"] == 1


def test_mapping_cannot_promote_reserved_management_fields_to_learned_content(
    tmp_path: Path,
) -> None:
    source = tmp_path / "management-fields.jsonl"
    source.write_text(
        json.dumps(
            {
                "_source_text": "actual user prompt",
                "answer": "actual assistant answer",
                "sampleId": "external-11",
                "reviewNote": "operator-only note",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    safe_recipe = {
        **RECIPE,
        "format": "alpaca",
        "fieldMapping": {
            "instruction": "_source_text",
            "output": "answer",
            "sampleId": "sampleId",
        },
    }
    safe_output = tmp_path / "safe-management-record.jsonl"
    _curate(
        [_source(source, "management-rev")],
        safe_output,
        tmp_path / "safe-management-checkpoint.json",
        safe_recipe,
    )
    record = _read_envelopes(safe_output)[0]
    assert record["sampleId"] == "external-11"
    assert record["normalized"]["messages"] == [
        {"role": "user", "content": "actual user prompt"},
        {"role": "assistant", "content": "actual assistant answer"},
    ]
    assert "operator-only note" not in json.dumps(record["normalized"])

    with pytest.raises(ValueError, match="reserved management field"):
        _curate(
            [_source(source, "management-rev")],
            tmp_path / "blocked-record.jsonl",
            tmp_path / "blocked-checkpoint.json",
            {
                **safe_recipe,
                "fieldMapping": {
                    **safe_recipe["fieldMapping"],
                    "instruction": "sampleId",
                },
            },
        )

    with pytest.raises(ValueError, match="reserved management field"):
        remap_training_record(
            record=record,
            recipe=RECIPE,
            recipe_digest=RECIPE_DIGEST,
            format_hint="alpaca",
            field_mapping={"instruction": "reviewNote", "output": "answer"},
        )


def test_unknown_format_is_reviewable_and_can_be_human_mapped(tmp_path: Path) -> None:
    source = tmp_path / "manual-format.jsonl"
    source.write_text(
        json.dumps({"task": "question", "answer": "answer"}) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "manual-format-record.jsonl"
    _curate(
        [_source(source, "manual-format-rev")],
        output,
        tmp_path / "manual-format-checkpoint.json",
    )
    unresolved = _read_envelopes(output)[0]

    assert unresolved["disposition"] == "review"
    assert unresolved["normalized"] is None
    corrected = remap_training_record(
        record=unresolved,
        recipe=RECIPE,
        recipe_digest=RECIPE_DIGEST,
        format_hint="alpaca",
        field_mapping={"instruction": "task", "output": "answer"},
    )

    assert corrected["disposition"] == "eligible"
    assert corrected["normalized"]["messages"] == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert corrected["processingHistory"][-1]["operation"] == "humanRemap"


def test_human_remap_cannot_change_authoritative_lineage_or_policy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "immutable-lineage.jsonl"
    source.write_text(
        json.dumps(
            {
                "sample_id": "sample-1",
                "conversation_id": "conversation-1",
                "prompt": "Question",
                "completion": "Answer",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "immutable-lineage-records.jsonl"
    _curate(
        [_source(source, "lineage-rev", "lineage-family")],
        output,
        tmp_path / "immutable-lineage-checkpoint.json",
    )
    original = _read_envelopes(output)[0]
    lineage_fields = (
        "id",
        "sourceRevisionId",
        "sourceFamilyId",
        "sampleId",
        "conversationId",
        "ordinal",
        "locator",
        "recipeDigest",
        "policy",
    )
    original_lineage = {field: original[field] for field in lineage_fields}
    edited_raw = {
        **original["rawRecord"],
        "sample_id": "changed-sample",
        "conversation_id": "changed-conversation",
        "source_revision_id": "changed-revision",
        "source_family_id": "changed-family",
        "policy": {"allowTraining": False, "allowedUsePurposes": []},
    }

    remapped = remap_training_record(
        record=original,
        recipe=RECIPE,
        recipe_digest=RECIPE_DIGEST,
        format_hint="promptCompletion",
        raw_record=edited_raw,
    )

    assert {field: remapped[field] for field in lineage_fields} == original_lineage
    codes = {issue["code"] for issue in remapped["issues"]}
    assert "LINEAGE_METADATA_CONFLICT" in codes
    assert "SAMPLE_ID_METADATA_IGNORED" in codes
    assert "POLICY_BLOCKED" in codes
    assert remapped["disposition"] == "excluded"
    assert remapped["processingHistory"][:-1] == original["processingHistory"]
    assert remapped["processingHistory"][-1]["rawRecordBefore"] == original[
        "rawRecord"
    ]

    with pytest.raises(ValueError, match="immutable recipeDigest"):
        remap_training_record(
            record=original,
            recipe=RECIPE,
            recipe_digest="sha256:" + "b" * 64,
            format_hint="promptCompletion",
        )


def test_malformed_record_policy_is_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "malformed-policy.jsonl"
    source.write_text(
        json.dumps(
            {
                "prompt": "Question",
                "completion": "Answer",
                "policy": "allow everything",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "malformed-policy-records.jsonl"
    _curate(
        [_source(source, "malformed-policy-rev")],
        output,
        tmp_path / "malformed-policy-checkpoint.json",
    )
    record = _read_envelopes(output)[0]

    assert record["policy"]["allowTraining"] is False
    assert record["disposition"] == "excluded"
    assert "POLICY_INVALID" in {issue["code"] for issue in record["issues"]}


def test_alpaca_system_and_history_turns_are_preserved_and_unknown_fields_reported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "alpaca-history.jsonl"
    source.write_text(
        json.dumps(
            {
                "system": "Follow the policy.",
                "history": [["Earlier question", "Earlier answer"]],
                "instruction": "Current question",
                "input": "Current context",
                "output": "Current answer",
                "_reviewNote": "operator-only note",
                "score": 0.9,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "alpaca-history-record.jsonl"
    _curate(
        [_source(source, "alpaca-history-rev")],
        output,
        tmp_path / "alpaca-history-checkpoint.json",
        {**RECIPE, "format": "alpaca"},
    )
    record = _read_envelopes(output)[0]

    assert record["normalized"]["messages"] == [
        {"role": "system", "content": "Follow the policy."},
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "Current question\nCurrent context"},
        {"role": "assistant", "content": "Current answer"},
    ]
    assert record["disposition"] == "review"
    assert "UNSUPPORTED_SEMANTIC_FIELD" in {
        issue["code"] for issue in record["issues"]
    }
    assert "operator-only note" not in json.dumps(record["normalized"])


def test_role_mapping_cannot_legalize_tool_function_or_observation_roles(
    tmp_path: Path,
) -> None:
    source = tmp_path / "hard-unsupported-roles.jsonl"
    records = [
        {
            "messages": [
                {
                    "role": "tool",
                    "content": "result",
                    "id": "message-id-is-management",
                    "analysis": "unmapped semantic content",
                },
                {"role": "assistant", "content": "answer"},
            ]
        },
        {
            "conversations": [
                {"from": "function", "value": "result"},
                {"from": "gpt", "value": "answer"},
            ]
        },
        (
            "<|im_start|>observation\nresult<|im_end|>"
            "<|im_start|>assistant\nanswer<|im_end|>"
        ),
    ]
    source.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "hard-unsupported-records.jsonl"
    recipe = {
        **RECIPE,
        "roleMapping": {
            "tool": "assistant",
            "function": "assistant",
            "observation": "assistant",
        },
    }
    _curate(
        [_source(source, "hard-unsupported-rev")],
        output,
        tmp_path / "hard-unsupported-checkpoint.json",
        recipe,
    )
    envelopes = _read_envelopes(output)

    assert all(record["disposition"] == "review" for record in envelopes)
    assert "UNSUPPORTED_TOOL_CALL" in {
        issue["code"] for issue in envelopes[0]["issues"]
    }
    assert "UNSUPPORTED_SEMANTIC_FIELD" in {
        issue["code"] for issue in envelopes[0]["issues"]
    }
    assert "UNSUPPORTED_TOOL_CALL" in {
        issue["code"] for issue in envelopes[1]["issues"]
    }
    assert "UNSUPPORTED_ROLE" in {
        issue["code"] for issue in envelopes[2]["issues"]
    }


def test_retry_resumes_committed_prefix_and_completed_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large.jsonl"
    with source.open("w", encoding="utf-8") as stream:
        for index in range(150):
            stream.write(
                json.dumps(
                    {
                        "prompt": f"Question number {index} please?",
                        "completion": f"Answer number {index}.",
                    }
                )
                + "\n"
            )
    output = tmp_path / "large-records.jsonl"
    checkpoint = tmp_path / "large-checkpoint.json"

    class CancelAfter:
        calls = 0

        def is_cancelled(self) -> bool:
            self.calls += 1
            return self.calls >= 75

    request = {
        "sources": [_source(source, "large-rev")],
        "result_path": output,
        "checkpoint_path": checkpoint,
        "recipe_digest": RECIPE_DIGEST,
        "recipe": RECIPE,
    }
    with pytest.raises(CurationCancelled):
        curate_training_records(**request, cancellation=CancelAfter())

    resumed = curate_training_records(**request)
    original_bytes = output.read_bytes()
    repeated = curate_training_records(**request)

    assert resumed["resumed_records"] >= 64
    assert resumed["counts"]["total"] == 150
    assert resumed["counts"]["eligible"] == 150
    assert repeated["counts"] == resumed["counts"]
    assert repeated["digest"] == resumed["digest"]
    assert output.read_bytes() == original_bytes
    assert len(_read_envelopes(output)) == 150

    tampered = output.read_bytes().replace(b"Question number 0", b"QuestioX number 0", 1)
    assert tampered != original_bytes
    output.write_bytes(tampered)
    repaired = curate_training_records(**request)
    assert repaired["resumed_records"] == 0
    assert repaired["digest"] == resumed["digest"]
    assert output.read_bytes() == original_bytes


def test_malformed_json_array_fails_only_its_source_and_continues_batch(
    tmp_path: Path,
) -> None:
    malformed_array = tmp_path / "malformed-array.json"
    malformed_array.write_text(
        '[{"prompt":"first question","completion":"first answer"}, '
        '{"prompt":"unfinished"',
        encoding="utf-8",
    )
    later_source = tmp_path / "later-source.jsonl"
    later_source.write_text(
        json.dumps({"prompt": "later question", "completion": "later answer"})
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "partial-batch-records.jsonl"
    request = {
        "sources": [
            _source(malformed_array, "malformed-array-rev"),
            _source(later_source, "later-source-rev", "family-later"),
        ],
        "result_path": output,
        "checkpoint_path": tmp_path / "partial-batch-checkpoint.json",
        "recipe_digest": RECIPE_DIGEST,
        "recipe": RECIPE,
    }

    receipt = curate_training_records(**request)
    records = _read_envelopes(output)

    assert receipt["counts"]["total"] == 2
    assert [record["sampleId"] for record in records] == [
        "malformed-array-rev:0",
        "later-source-rev:0",
    ]
    assert receipt["sources"][0]["status"] == "FAILED"
    assert receipt["sources"][0]["diagnostic_counts"] == {
        "SOURCE_PARSE_FAILED": 1
    }
    assert "array:2" in receipt["sources"][0]["failure"]
    assert receipt["sources"][1]["status"] == "SUCCEEDED"
    assert receipt["sources"][1]["counts"]["total"] == 1
    assert curate_training_records(**request)["digest"] == receipt["digest"]


def test_medium_jsonl_processing_keeps_python_memory_bounded(tmp_path: Path) -> None:
    source = tmp_path / "medium.jsonl"
    with source.open("w", encoding="utf-8") as stream:
        for index in range(5_000):
            stream.write(
                json.dumps(
                    {
                        "prompt": f"Please explain the unique task number {index:05d}.",
                        "completion": f"The result for item {index:05d} is available.",
                    }
                )
                + "\n"
            )
    tracemalloc.start()
    receipt = _curate(
        [_source(source, "medium-rev")],
        tmp_path / "medium-records.jsonl",
        tmp_path / "medium-checkpoint.json",
    )
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert receipt["counts"]["total"] == 5_000
    assert receipt["counts"]["eligible"] == 5_000
    assert peak_bytes < 12 * 1024 * 1024


def test_curation_action_uses_typed_path_based_rpc_and_rejects_policy_bypass(
    tmp_path: Path,
) -> None:
    source = tmp_path / "restricted.jsonl"
    source.write_text(
        json.dumps(
            {
                "instruction": "restricted",
                "output": "answer",
                "policy": {"allowTraining": False, "allowedUsePurposes": []},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    request = {
        "sources": [_source(source, "restricted-rev")],
        "result_path": str(tmp_path / "rpc-records.jsonl"),
        "checkpoint_path": str(tmp_path / "rpc-checkpoint.json"),
        "recipe_digest": RECIPE_DIGEST,
        "recipe": RECIPE,
    }

    ok, result = DatasetPreparationPlugin().on_invoke(
        "dataset.preparation.v1",
        "curate_training_records",
        json.dumps(request).encode(),
        request_type_url="type.cyrene.io/dataset.preparation.v1.curate_training_records.request",
    )

    assert ok is True
    assert not isinstance(result, str)
    receipt = json.loads(result.value)
    assert receipt["counts"]["total"] == 1
    assert receipt["counts"]["excluded"] == 1
    assert result.type_url.endswith("curate_training_records.response")


def test_empty_or_unrecognized_source_has_an_explicit_failure_receipt(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty.jsonl"
    source.write_bytes(b"")
    receipt = _curate(
        [_source(source, "empty-rev")],
        tmp_path / "empty-records.jsonl",
        tmp_path / "empty-checkpoint.json",
    )

    assert receipt["counts"]["total"] == 0
    assert receipt["sources"][0]["status"] == "FAILED"
    assert receipt["sources"][0]["diagnostic_counts"] == {"EMPTY_SOURCE": 1}
