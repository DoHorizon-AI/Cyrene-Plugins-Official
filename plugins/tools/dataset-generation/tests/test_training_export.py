"""Tests for the streaming Catalyst training snapshot exporter.

中文:流式 Catalyst 训练快照导出器测试。
"""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest
import training_export
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from dataset_generation import CAPABILITY_ID, DatasetGenerationPlugin
from training_export import prepare_training_sft

RECIPE_DIGEST = "sha256:" + "a" * 64
CURATION_RECIPE_DIGEST = "sha256:" + "d" * 64


def _record(
    index: int,
    messages: list[dict[str, str]],
    *,
    family: str | None = None,
    conversation: str | None = None,
    content_digest: str | None = None,
    disposition: str = "eligible",
    policy: dict | None = None,
    issues: list[dict] | None = None,
) -> dict:
    normalized = {"messages": messages}
    digest = (
        content_digest
        or "sha256:"
        + hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
    )
    return {
        "schemaVersion": "cyrene.training-record.v1",
        "id": f"record-{index}",
        "sampleId": f"sample-{index}",
        "sourceRevisionId": f"revision-{index}",
        "sourceFamilyId": family or f"family-{index}",
        "conversationId": conversation,
        "ordinal": index,
        "locator": {"line": index + 1},
        "rawRecord": {"prompt": "secret raw prompt", "internalReview": "private"},
        "rawLine": None,
        "detectedFormat": "messages",
        "normalized": normalized,
        "disposition": disposition,
        "issues": issues or [],
        "contentDigest": digest,
        "recipeDigest": CURATION_RECIPE_DIGEST,
        "processingHistory": [
            {"operation": "normalize", "recipeDigest": CURATION_RECIPE_DIGEST}
        ],
        "policy": policy
        or {"allowTraining": True, "allowedUsePurposes": ["model_training"]},
    }


def _request(snapshot, output_dir, output_format="sft") -> dict:
    return {
        "snapshot_path": str(snapshot),
        "output_dir": str(output_dir),
        "output_format": output_format,
        "recipe_digest": RECIPE_DIGEST,
        "recipe_version": "1",
        "split": {"train": 0.6, "validation": 0.2, "test": 0.2, "seed": 42},
        "dataset_id": "dataset-1",
        "content_revision_id": "revision-approved",
        "processing_run_id": "run-1",
    }


def _write_snapshot(path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_sft_export_preserves_system_and_all_prior_turns(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    _write_snapshot(
        snapshot,
        [
            _record(
                1,
                [
                    {"role": "system", "content": "Use Chinese."},
                    {"role": "user", "content": "第一问"},
                    {"role": "assistant", "content": "第一答"},
                    {"role": "user", "content": "第二问"},
                    {"role": "assistant", "content": "第二答"},
                ],
            )
        ],
    )

    receipt = prepare_training_sft(_request(snapshot, tmp_path / "out"))
    archive = zipfile.ZipFile(receipt["bundle_path"])
    rows = [
        json.loads(line)
        for line in archive.read("train.jsonl").decode("utf-8").splitlines()
        if line
    ]

    assert len(rows) == 1
    assert rows[0] == {
        "instruction": "第二问",
        "input": "",
        "output": "第二答",
        "system": "Use Chinese.",
        "history": [["第一问", "第一答"]],
    }
    assert receipt["sample_count"] == 1
    assert receipt["files"]["provenance.jsonl"]["row_count"] == 1
    assert receipt["recipe_digest"] == RECIPE_DIGEST
    assert receipt["source_recipe_digests"] == [CURATION_RECIPE_DIGEST]
    with zipfile.ZipFile(receipt["bundle_path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        provenance = json.loads(archive.read("provenance.jsonl").decode("utf-8"))
    assert manifest["recipe"]["digest"] == RECIPE_DIGEST
    assert manifest["source_recipe_digests"] == [CURATION_RECIPE_DIGEST]
    assert provenance["curation_recipe_digest"] == CURATION_RECIPE_DIGEST
    assert provenance["processing_history"][0]["recipeDigest"] == CURATION_RECIPE_DIGEST


def test_messages_export_keeps_full_role_order_and_no_metadata(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    messages = [
        {"role": "system", "content": "约束"},
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ]
    _write_snapshot(snapshot, [_record(1, messages)])

    receipt = prepare_training_sft(
        _request(snapshot, tmp_path / "out", output_format="messages")
    )
    with zipfile.ZipFile(receipt["bundle_path"]) as archive:
        row = json.loads(archive.read("train.jsonl").decode("utf-8"))
        provenance = json.loads(archive.read("provenance.jsonl").decode("utf-8"))

    assert row == {"messages": messages}
    assert "rawRecord" not in row and "policy" not in row
    assert provenance["source_family_id"] == "family-1"
    raw_json = json.dumps(
        {"prompt": "secret raw prompt", "internalReview": "private"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (
        provenance["raw_digest"]
        == "sha256:" + hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    )
    assert "rawRecord" not in provenance


def test_provenance_derives_raw_digest_from_retained_raw_record(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    record = _record(
        1,
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ],
    )
    _write_snapshot(snapshot, [record])

    receipt = prepare_training_sft(_request(snapshot, tmp_path / "out"))
    with zipfile.ZipFile(receipt["bundle_path"]) as archive:
        provenance = json.loads(archive.read("provenance.jsonl").decode("utf-8"))

    expected = hashlib.sha256(
        json.dumps(
            record["rawRecord"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert provenance["raw_digest"] == f"sha256:{expected}"


def test_published_bundle_path_is_immutable(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    _write_snapshot(
        snapshot,
        [
            _record(
                1,
                [
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                ],
            )
        ],
    )
    output_dir = tmp_path / "out"
    receipt = prepare_training_sft(_request(snapshot, output_dir))
    bundle = output_dir / "bundle.zip"
    original = bundle.read_bytes()

    with pytest.raises(ValueError, match="refusing to overwrite"):
        prepare_training_sft(_request(snapshot, output_dir))

    assert bundle.read_bytes() == original
    assert receipt["bundle_digest"] == "sha256:" + hashlib.sha256(original).hexdigest()


def test_prompt_completion_rejects_multiturn_instead_of_flattening(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    _write_snapshot(
        snapshot,
        [
            _record(
                1,
                [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "user", "content": "second"},
                    {"role": "assistant", "content": "answer 2"},
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="promptCompletion cannot represent"):
        prepare_training_sft(
            _request(snapshot, tmp_path / "out", output_format="promptCompletion")
        )
    diagnostics = json.loads((tmp_path / "out" / "export-diagnostics.json").read_text())
    assert diagnostics["published"] == 0


def test_policy_denied_and_excluded_rows_never_enter_learned_rows(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    _write_snapshot(
        snapshot,
        [
            _record(
                1,
                [
                    {"role": "user", "content": "allowed"},
                    {"role": "assistant", "content": "yes"},
                ],
            ),
            _record(
                2,
                [
                    {"role": "user", "content": "private"},
                    {"role": "assistant", "content": "no"},
                ],
                policy={"allowTraining": False},
            ),
            _record(
                3,
                [
                    {"role": "user", "content": "excluded"},
                    {"role": "assistant", "content": "no"},
                ],
                disposition="excluded",
            ),
        ],
    )

    receipt = prepare_training_sft(_request(snapshot, tmp_path / "out"))
    with zipfile.ZipFile(receipt["bundle_path"]) as archive:
        learned = "\n".join(
            archive.read(name).decode("utf-8")
            for name in ("train.jsonl", "validation.jsonl", "test.jsonl")
        )

    assert "allowed" in learned
    assert "private" not in learned and "excluded" not in learned
    assert "internalReview" not in learned
    assert receipt["counts"]["policyExcluded"] == 1
    assert receipt["counts"]["excluded"] == 2


@pytest.mark.parametrize(
    "policy",
    [
        {"allowedUsePurposes": ["model_training"]},
        {"allowTraining": 1, "allowedUsePurposes": ["model_training"]},
        {"allowTraining": "true", "allowedUsePurposes": ["model_training"]},
        {"allowTraining": True},
        {"allowTraining": True, "allowedUsePurposes": "model_training"},
        {"allow_training": True, "allowed_use_purposes": ["model_training"]},
    ],
)
def test_malformed_or_incomplete_training_policy_is_denied(tmp_path, policy) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    allowed = _record(
        1,
        [
            {"role": "user", "content": "allowed control"},
            {"role": "assistant", "content": "yes"},
        ],
    )
    candidate = _record(
        2,
        [
            {"role": "user", "content": "must stay out"},
            {"role": "assistant", "content": "no"},
        ],
        policy=policy,
    )
    _write_snapshot(snapshot, [allowed, candidate])

    receipt = prepare_training_sft(_request(snapshot, tmp_path / "out"))
    with zipfile.ZipFile(receipt["bundle_path"]) as archive:
        learned = "\n".join(
            archive.read(name).decode("utf-8")
            for name in ("train.jsonl", "validation.jsonl", "test.jsonl")
        )

    assert "allowed control" in learned
    assert "must stay out" not in learned
    assert receipt["counts"]["policyExcluded"] == 1


def test_policy_only_snapshot_writes_counts_to_failure_diagnostics(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    _write_snapshot(
        snapshot,
        [
            _record(
                1,
                [
                    {"role": "user", "content": "private"},
                    {"role": "assistant", "content": "answer"},
                ],
                policy={"allowTraining": False},
            )
        ],
    )

    with pytest.raises(ValueError, match="no approved, policy-eligible records"):
        prepare_training_sft(_request(snapshot, tmp_path / "out"))

    diagnostics = json.loads(
        (tmp_path / "out" / "export-diagnostics.json").read_text(encoding="utf-8")
    )
    assert diagnostics["total"] == 1
    assert diagnostics["policyExcluded"] == 1
    assert diagnostics["excluded"] == 1
    assert diagnostics["published"] == 0


@pytest.mark.parametrize("disposition", ["review", "excluded"])
def test_format_error_counts_use_curation_code_classifier(
    tmp_path, disposition
) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    format_codes = [
        "INVALID_JSON",
        "INVALID_ENCODING",
        "FORMAT_UNRECOGNIZED",
        "MESSAGE_STRUCTURE_INVALID",
        "FIELD_MISSING",
        "FIELD_TYPE_INVALID",
    ]
    non_format_error_codes = [
        "UNSUPPORTED_ROLE",
        "UNSUPPORTED_TOOL_CALL",
        "UNSUPPORTED_MULTIMODAL",
        "POLICY_BLOCKED",
        "EMPTY_RESPONSE",
    ]
    records = [
        _record(
            index,
            messages,
            disposition=disposition,
            issues=[{"code": code, "message": code, "severity": "error"}],
        )
        for index, code in enumerate(format_codes)
    ]
    records.extend(
        _record(
            len(format_codes) + index,
            messages,
            disposition="excluded",
            issues=[{"code": code, "message": code, "severity": "error"}],
        )
        for index, code in enumerate(non_format_error_codes)
    )
    records.append(_record(99, messages))
    _write_snapshot(snapshot, records)

    if disposition == "review":
        with pytest.raises(ValueError, match="record is still in review"):
            prepare_training_sft(_request(snapshot, tmp_path / "out"))
        counts = json.loads(
            (tmp_path / "out" / "export-diagnostics.json").read_text(encoding="utf-8")
        )
    else:
        receipt = prepare_training_sft(_request(snapshot, tmp_path / "out"))
        counts = receipt["counts"]

    assert counts["total"] == 12
    assert counts["formatErrors"] == 6
    assert counts["review"] == (6 if disposition == "review" else 0)
    assert counts["excluded"] == (5 if disposition == "review" else 11)


def test_format_error_counts_include_legacy_invalid_json_code(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    _write_snapshot(
        snapshot,
        [
            _record(
                1,
                [
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                ],
                disposition="review",
                issues=[
                    {
                        "code": "training.invalid_json",
                        "message": "legacy invalid JSON issue",
                        "severity": "error",
                    }
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="record is still in review"):
        prepare_training_sft(_request(snapshot, tmp_path / "out"))

    diagnostics = json.loads(
        (tmp_path / "out" / "export-diagnostics.json").read_text(encoding="utf-8")
    )
    assert diagnostics["formatErrors"] == 1


def test_review_records_block_export_until_explicitly_resolved(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    _write_snapshot(
        snapshot,
        [
            _record(
                1,
                [
                    {"role": "user", "content": "pending"},
                    {"role": "assistant", "content": "answer"},
                ],
                disposition="review",
            )
        ],
    )

    with pytest.raises(ValueError, match="record is still in review"):
        prepare_training_sft(_request(snapshot, tmp_path / "out"))


def test_blank_snapshot_line_is_reported_instead_of_silently_skipped(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    record = _record(
        1,
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ],
    )
    _write_snapshot(snapshot, [record])
    snapshot.write_text("\n" + snapshot.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ValueError, match="blank snapshot lines"):
        prepare_training_sft(_request(snapshot, tmp_path / "out"))

    diagnostics = json.loads(
        (tmp_path / "out" / "export-diagnostics.json").read_text(encoding="utf-8")
    )
    assert diagnostics["total"] == 2
    assert diagnostics["formatErrors"] == 1


def test_snapshot_reader_bounds_single_line_memory_and_continues(
    tmp_path, monkeypatch
) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    row = json.dumps(
        _record(
            2,
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ],
        )
    )
    monkeypatch.setattr(training_export, "_MAX_RECORD_BYTES", len(row) + 10)
    snapshot.write_text(
        "x" * 10_000 + "\n" + row + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="record exceeds the 16 MiB row limit"):
        prepare_training_sft(_request(snapshot, tmp_path / "out"))
    diagnostics = json.loads(
        (tmp_path / "out" / "export-diagnostics.json").read_text(encoding="utf-8")
    )
    assert diagnostics["total"] == 2
    assert diagnostics["formatErrors"] == 1
    assert diagnostics["eligible"] == 1


def test_source_family_conversation_and_duplicate_digest_stay_in_one_split(
    tmp_path,
) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    duplicate_digest = "sha256:" + "c" * 64
    records = [
        _record(
            0,
            [{"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"}],
            family="family-shared",
        ),
        _record(
            1,
            [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}],
            family="family-shared",
        ),
        _record(
            2,
            [{"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"}],
            conversation="conversation-shared",
        ),
        _record(
            3,
            [{"role": "user", "content": "q3"}, {"role": "assistant", "content": "a3"}],
            conversation="conversation-shared",
        ),
        _record(
            4,
            [
                {"role": "user", "content": "duplicate"},
                {"role": "assistant", "content": "same"},
            ],
            content_digest=duplicate_digest,
        ),
        _record(
            5,
            [
                {"role": "user", "content": "duplicate"},
                {"role": "assistant", "content": "same"},
            ],
            content_digest=duplicate_digest,
        ),
    ]
    _write_snapshot(snapshot, records)

    receipt = prepare_training_sft(_request(snapshot, tmp_path / "out", "messages"))
    with zipfile.ZipFile(receipt["bundle_path"]) as archive:
        splits = {}
        for split_name in ("train", "validation", "test"):
            for line in archive.read(f"{split_name}.jsonl").decode().splitlines():
                if line:
                    row = json.loads(line)
                    prompt = row["messages"][0]["content"]
                    splits[prompt] = split_name
        provenance = [
            json.loads(line)
            for line in archive.read("provenance.jsonl").decode().splitlines()
        ]

    assert splits["q0"] == splits["q1"]
    assert splits["q2"] == splits["q3"]
    duplicate_splits = {
        item["split"]
        for item in provenance
        if item["sample_id"] in {"sample-4", "sample-5"}
    }
    assert len(duplicate_splits) == 1
    assert sum(receipt["split_stats"]["samples"].values()) == receipt["sample_count"]


def test_unknown_roles_and_message_metadata_are_reported_not_rewritten(
    tmp_path,
) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    _write_snapshot(
        snapshot,
        [
            _record(
                1,
                [
                    {"role": "user", "content": "question"},
                    {
                        "role": "assistant",
                        "content": "answer",
                        "tool_calls": [{"id": "t1"}],
                    },
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="extra message fields"):
        prepare_training_sft(
            _request(snapshot, tmp_path / "out", output_format="messages")
        )


def test_prepare_training_sft_is_available_through_direct_runtime(tmp_path) -> None:
    snapshot = tmp_path / "snapshot.jsonl"
    _write_snapshot(
        snapshot,
        [
            _record(
                1,
                [
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": "a"},
                ],
            )
        ],
    )
    server, connection_ref = serve(
        DatasetGenerationPlugin(), CAPABILITY_ID, "1", "127.0.0.1:0"
    )
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        response = client.invoke(
            capability=CAPABILITY_ID,
            interface_version="1",
            method="prepare_training_sft",
            request=DirectPayload(
                type_url="type.cyrene.io/dataset.generation.v1.prepare_training_sft.request",
                value=json.dumps(_request(snapshot, tmp_path / "out")).encode(),
            ),
            deadline_seconds=5,
        )
    finally:
        client.close()
        server.stop(grace=None).wait()

    receipt = json.loads(response.value)
    assert response.type_url == (
        "type.cyrene.io/dataset.generation.v1.prepare_training_sft.response"
    )
    assert receipt["profile"] == "CYRENE_SFT_BUNDLE_V1"
