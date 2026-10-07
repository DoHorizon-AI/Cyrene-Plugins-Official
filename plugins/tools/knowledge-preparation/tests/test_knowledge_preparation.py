"""Focused capability, package-integrity and independent-consumer tests.

中文:覆盖真实 DirectPluginRuntime 调用、知识包哈希和 ACL 检索边界。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from cyrene_plugin_runtime import DirectPayload, DirectPluginClient, serve
from knowledge_preparation import CAPABILITY_ID, KnowledgePreparationPlugin
from knowledge_reference import search_bundle, verify_bundle

DATASET_ID = "00000000-0000-4000-8000-000000000001"
CONTENT_REVISION_ID = "00000000-0000-4000-8000-000000000002"
PROCESSING_RUN_ID = "00000000-0000-4000-8000-000000000003"
SOURCE_IDS = [
    "00000000-0000-4000-8000-000000000011",
    "00000000-0000-4000-8000-000000000012",
    "00000000-0000-4000-8000-000000000013",
]
SOURCE_REVISION_IDS = [
    "00000000-0000-4000-8000-000000000021",
    "00000000-0000-4000-8000-000000000022",
    "00000000-0000-4000-8000-000000000023",
]


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _source_revisions(raw: bytes) -> list[dict[str, object]]:
    digest = _digest(raw)
    return [
        {
            "source_id": source_id,
            "source_revision_id": revision_id,
            "revision": 1,
            "digest": digest,
            "artifact": {
                "uri": f"artifact://sha256/{digest.removeprefix('sha256:')}",
                "digest": digest,
                "size_bytes": len(raw),
                "kind": "source-revision",
            },
        }
        for source_id, revision_id in zip(SOURCE_IDS, SOURCE_REVISION_IDS, strict=True)
    ]


def _block(
    index: int,
    revision_id: str,
    principal: str,
    *,
    allow_knowledge: bool = True,
    allowed_use_purposes: list[str] | None = None,
    text: str = "Shared handbook: account recovery requires manager approval. 恢复代码 ORCHID-42。",
) -> dict[str, object]:
    return {
        "id": f"block-{index}",
        "sourceRevisionId": revision_id,
        "ordinal": 0,
        "kind": "paragraph",
        "text": text,
        "locator": {
            "sourcePages": [2],
            "sectionPath": ["Operations", "Account recovery"],
            "itemRef": f"#/texts/{index}",
            "provenance": [{"pageNo": 2}],
        },
        "origin": "EXTRACTED",
        "policy": {
            "allowKnowledge": allow_knowledge,
            "allowTraining": not allow_knowledge,
            "allowedPrincipalRefs": [principal],
            "allowedUsePurposes": allowed_use_purposes
            if allowed_use_purposes is not None
            else (["knowledge_retrieval"] if allow_knowledge else ["model_training"]),
        },
        "assetRefs": [
            {
                "uri": "artifact://sha256/" + "a" * 64,
                "digest": "sha256:" + "a" * 64,
                "size_bytes": 12,
                "kind": "page-image",
            }
        ],
        "warnings": [],
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _request(
    blocks_path: Path, bundle_path: Path, result_path: Path
) -> dict[str, object]:
    return {
        "blocks_path": str(blocks_path),
        "bundle_path": str(bundle_path),
        "result_path": str(result_path),
        "dataset_id": DATASET_ID,
        "content_revision_id": CONTENT_REVISION_ID,
        "processing_run_id": PROCESSING_RUN_ID,
        "source_revisions": _source_revisions(b"same source bytes"),
        "policy": {"use_purpose": "knowledge_retrieval"},
    }


def test_direct_runtime_build_and_reference_consumer_enforce_source_acl(
    tmp_path: Path,
) -> None:
    blocks_path = tmp_path / "approved-blocks.jsonl"
    bundle_path = tmp_path / "knowledge.zip"
    result_path = tmp_path / "receipt.json"
    blocks = [
        _block(
            1,
            SOURCE_REVISION_IDS[0],
            "principal:reader-a",
            allowed_use_purposes=["knowledge_retrieval", "model_training"],
        ),
        _block(2, SOURCE_REVISION_IDS[1], "principal:reader-b"),
        _block(
            3,
            SOURCE_REVISION_IDS[2],
            "principal:reader-a",
            allow_knowledge=False,
            text="Training-only content must not enter a knowledge package.",
        ),
    ]
    blocks[0]["locator"] = {
        "source_pages": [2],
        "section_path": ["Operations", "Account recovery"],
        "item_ref": "#/texts/1",
        "tree_level": 1,
        "provenance": [{"page_no": 2}],
    }
    _write_jsonl(blocks_path, blocks)

    server, connection_ref = serve(
        KnowledgePreparationPlugin(), CAPABILITY_ID, "1", "127.0.0.1:0"
    )
    client = DirectPluginClient.for_local_connection_ref(connection_ref)
    try:
        response = client.invoke(
            capability=CAPABILITY_ID,
            interface_version="1",
            method="build",
            request=DirectPayload(
                type_url="type.cyrene.io/dataset.knowledge.v1.build.request",
                value=json.dumps(
                    _request(blocks_path, bundle_path, result_path)
                ).encode(),
            ),
            deadline_seconds=10,
        )
    finally:
        client.close()
        server.stop(grace=None).wait()

    receipt = json.loads(response.value)
    assert response.type_url == "type.cyrene.io/dataset.knowledge.v1.build.response"
    assert receipt["chunk_count"] == 2
    assert receipt["source_count"] == 2
    assert receipt["conversion_report"]["excludedBlockCount"] == 1
    assert receipt["conversion_report"]["skippedByPolicySourceCount"] == 1
    assert receipt["bundle_size"] == bundle_path.stat().st_size
    assert receipt["bundle_digest"] == _digest(bundle_path.read_bytes())
    assert receipt["result_digest"] == _digest(result_path.read_bytes())
    assert receipt["manifest_digest"]

    with zipfile.ZipFile(bundle_path) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "chunks.jsonl",
            "sources.jsonl",
            "hierarchy.json",
            "checksums.json",
        }
        manifest = json.loads(archive.read("manifest.json"))
        sources = [
            json.loads(line) for line in archive.read("sources.jsonl").splitlines()
        ]
        chunks = [
            json.loads(line) for line in archive.read("chunks.jsonl").splitlines()
        ]
        hierarchy = json.loads(archive.read("hierarchy.json"))
        assert len({source["digest"] for source in sources}) == 1
        assert len({source["sourceId"] for source in sources}) == 2
        assert len({chunk["chunkId"] for chunk in chunks}) == 2
        assert all(chunk["locator"]["sourcePages"] == [2] for chunk in chunks)
        assert "policy" in chunks[0]
        assert {"knowledge_retrieval", "model_training"} in [
            set(chunk["policy"]["allowedUsePurposes"]) for chunk in chunks
        ]
        assert SOURCE_REVISION_IDS[2] not in manifest["sourceRevisionIds"]
        first_source_tree = next(
            item
            for item in hierarchy["sources"]
            if item["sourceRevisionId"] == SOURCE_REVISION_IDS[0]
        )
        assert first_source_tree["sections"][0]["name"] == "Operations"
        assert (
            first_source_tree["sections"][0]["children"][0]["name"]
            == "Account recovery"
        )
        checksums = json.loads(archive.read("checksums.json"))["files"]
        assert checksums["manifest.json"] == receipt["manifest_digest"]

    verified = verify_bundle(bundle_path, package_digest=receipt["bundle_digest"])
    assert verified["chunkCount"] == 2
    assert verified["sourceCount"] == 2

    reader_a = search_bundle(
        bundle_path,
        "恢复代码 ORCHID-42",
        principal_refs=["principal:reader-a"],
        use_purpose="knowledge_retrieval",
        package_digest=receipt["bundle_digest"],
        limit=5,
    )
    assert len(reader_a["hits"]) == 1
    assert reader_a["hits"][0]["chunk"]["sourceRevisionId"] == SOURCE_REVISION_IDS[0]
    assert reader_a["hits"][0]["chunk"]["locator"]["sectionPath"] == [
        "Operations",
        "Account recovery",
    ]
    reader_without_grant = search_bundle(
        bundle_path,
        "account recovery",
        principal_refs=["principal:unrelated"],
        use_purpose="knowledge_retrieval",
        package_digest=receipt["bundle_digest"],
    )
    assert reader_without_grant["hits"] == []

    cli_env = os.environ.copy()
    cli_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    cli = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "knowledge_reference",
            "search",
            "--bundle",
            str(bundle_path),
            "--package-digest",
            receipt["bundle_digest"],
            "--principal-ref",
            "principal:reader-b",
            "--use-purpose",
            "knowledge_retrieval",
            "--query",
            "account recovery",
            "--limit",
            "2",
        ],
        capture_output=True,
        check=False,
        env=cli_env,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    cli_result = json.loads(cli.stdout)
    assert len(cli_result["hits"]) == 1
    assert cli_result["hits"][0]["chunk"]["sourceRevisionId"] == SOURCE_REVISION_IDS[1]


def test_build_is_deterministic_and_chunk_ranges_are_traceable(tmp_path: Path) -> None:
    blocks_path = tmp_path / "blocks.jsonl"
    long_text = ("approved words from a source section " * 140).strip()
    _write_jsonl(
        blocks_path,
        [_block(1, SOURCE_REVISION_IDS[0], "principal:reader-a", text=long_text)],
    )
    plugin = KnowledgePreparationPlugin()
    first_bundle = tmp_path / "first.zip"
    second_bundle = tmp_path / "second.zip"
    common = {
        "blocks_path": blocks_path,
        "result_path": tmp_path / "result.json",
        "dataset_id": DATASET_ID,
        "content_revision_id": CONTENT_REVISION_ID,
        "processing_run_id": PROCESSING_RUN_ID,
        "source_revisions": _source_revisions(b"same source bytes"),
        "policy": {"use_purpose": "knowledge_retrieval"},
    }
    first = plugin.build(bundle_path=first_bundle, **common)
    second = plugin.build(bundle_path=second_bundle, **common)

    assert first["bundle_digest"] == second["bundle_digest"]
    with zipfile.ZipFile(first_bundle) as archive:
        chunks = [
            json.loads(line) for line in archive.read("chunks.jsonl").splitlines()
        ]
    assert len(chunks) > 1
    assert all(chunk["partCount"] == len(chunks) for chunk in chunks)
    assert chunks[0]["textRange"]["start"] == 0
    assert chunks[-1]["textRange"]["end"] == len(long_text)
    assert first["conversion_report"]["splitBlockCount"] == 1


def test_missing_policy_and_resealed_internal_hash_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    blocks_path = tmp_path / "blocks.jsonl"
    bundle_path = tmp_path / "knowledge.zip"
    result_path = tmp_path / "receipt.json"
    row = _block(1, SOURCE_REVISION_IDS[0], "principal:reader-a")
    row.pop("policy")
    _write_jsonl(blocks_path, [row])
    plugin = KnowledgePreparationPlugin()
    success, error = plugin.on_invoke(
        CAPABILITY_ID,
        "build",
        json.dumps(_request(blocks_path, bundle_path, result_path)).encode(),
        request_type_url="type.cyrene.io/dataset.knowledge.v1.build.request",
    )
    assert not success
    assert "missing fields" in error
    assert not bundle_path.exists()

    _write_jsonl(blocks_path, [_block(1, SOURCE_REVISION_IDS[0], "principal:reader-a")])
    valid = plugin.build(
        blocks_path=blocks_path,
        bundle_path=bundle_path,
        result_path=result_path,
        dataset_id=DATASET_ID,
        content_revision_id=CONTENT_REVISION_ID,
        processing_run_id=PROCESSING_RUN_ID,
        source_revisions=_source_revisions(b"same source bytes"),
        policy={"use_purpose": "knowledge_retrieval"},
    )
    with zipfile.ZipFile(bundle_path, "r") as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["chunks.jsonl"] += b" "
    resealed = tmp_path / "resealed.zip"
    with zipfile.ZipFile(resealed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    with pytest.raises(ValueError, match="package member digest mismatch"):
        search_bundle(
            resealed,
            "account recovery",
            principal_refs=["principal:reader-a"],
            use_purpose="knowledge_retrieval",
            package_digest=_digest(resealed.read_bytes()),
        )
    with pytest.raises(ValueError, match="package digest does not match"):
        verify_bundle(bundle_path, package_digest="sha256:" + "0" * 64)
    assert valid["bundle_digest"] == _digest(bundle_path.read_bytes())
