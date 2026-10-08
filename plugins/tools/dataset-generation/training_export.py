"""Stream an approved Catalyst training snapshot into a versioned SFT bundle.

The Catalyst service owns approval, recipe and immutable artifact state. This
module only validates the approved snapshot contract, projects learned rows,
assigns lineage-safe splits, and writes the existing bundle profile.

中文:将已批准的 Catalyst 训练快照流式导出为版本化 SFT bundle。Catalyst 服务负责审
核、Recipe 和不可变制品状态；本模块只校验快照契约、投影训练行、按血缘切分并写入
现有 bundle profile。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

_FORMAT_MAP = {
    "sft": "instruction_history",
    "messages": "messages",
    "promptCompletion": "prompt_completion",
}
_SPLITS = ("train", "validation", "test")
_MAX_RECORD_BYTES = 16 * 1024 * 1024
_MAX_BLOCKERS = 100
_SPLIT_ALGORITHM = "sha256-ranked-lineage-component-v1"
_PROFILE = "CYRENE_SFT_BUNDLE_V1"
_FORMAT_ERROR_CODES = frozenset(
    {
        "INVALID_JSON",
        "training.invalid_json",
        "INVALID_ENCODING",
        "FORMAT_UNRECOGNIZED",
        "MESSAGE_STRUCTURE_INVALID",
        "FIELD_MISSING",
        "FIELD_TYPE_INVALID",
    }
)
_OUTPUT_FILES = (
    "train.jsonl",
    "validation.jsonl",
    "test.jsonl",
    "provenance.jsonl",
    "manifest.json",
    "bundle.zip",
    "result.json",
)


def prepare_training_sft(request: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded-memory bundle from canonical curation envelopes.

    中文:根据规范化的整理记录构建有内存上界的训练 bundle。
    """

    _keys(
        request,
        required={
            "snapshot_path",
            "output_dir",
            "output_format",
            "recipe_digest",
            "recipe_version",
            "split",
        },
        optional={"dataset_id", "content_revision_id", "processing_run_id"},
        field="request",
    )
    snapshot_path = _absolute_path(request["snapshot_path"], "snapshot_path")
    output_dir = _absolute_path(request["output_dir"], "output_dir")
    output_format = _text(request["output_format"], "output_format")
    if output_format not in _FORMAT_MAP:
        raise ValueError("output_format must be sft, messages, or promptCompletion")
    target_schema = _FORMAT_MAP[output_format]
    recipe_digest = _digest(request["recipe_digest"], "recipe_digest")
    recipe_version = _text(request["recipe_version"], "recipe_version")
    split = _split_config(request["split"])
    lineage = {
        name: _optional_text(request.get(name), name)
        for name in ("dataset_id", "content_revision_id", "processing_run_id")
    }
    if not snapshot_path.is_file():
        raise ValueError("snapshot_path does not exist")

    output_dir.mkdir(parents=True, exist_ok=True)
    existing_outputs = [name for name in _OUTPUT_FILES if (output_dir / name).exists()]
    if existing_outputs:
        raise ValueError(
            "refusing to overwrite existing training export artifact(s): "
            + ", ".join(existing_outputs)
        )
    with tempfile.NamedTemporaryFile(
        prefix="catalyst-training-export-",
        suffix=".sqlite3",
        dir=output_dir,
        delete=False,
    ) as temporary:
        database_path = Path(temporary.name)

    try:
        try:
            connection = sqlite3.connect(database_path)
            try:
                _prepare_tables(connection)
                totals = _read_snapshot(
                    connection,
                    snapshot_path,
                    target_schema=target_schema,
                )
                _raise_if_blocked(totals, output_dir / "export-diagnostics.json")
                _assign_splits(connection, split)
                result = _write_bundle(
                    connection,
                    output_dir,
                    output_format=output_format,
                    target_schema=target_schema,
                    recipe_digest=recipe_digest,
                    recipe_version=recipe_version,
                    split=split,
                    totals=totals,
                    lineage=lineage,
                )
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise ValueError(f"training export disk index failed: {exc}") from exc
    finally:
        database_path.unlink(missing_ok=True)
    _write_json(output_dir / "result.json", result)
    return result


def _prepare_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE lineage(key TEXT PRIMARY KEY, parent TEXT NOT NULL, rank INTEGER NOT NULL);
        CREATE TABLE sample_links(sample_row INTEGER NOT NULL, key TEXT NOT NULL);
        CREATE TABLE samples(
            sample_row INTEGER PRIMARY KEY,
            record_id TEXT NOT NULL,
            sample_id TEXT NOT NULL,
            source_revision_id TEXT NOT NULL,
            source_family_id TEXT NOT NULL,
            conversation_id TEXT,
            locator_json TEXT NOT NULL,
            raw_digest TEXT,
            content_digest TEXT NOT NULL,
            curation_recipe_digest TEXT NOT NULL,
            processing_history_json TEXT NOT NULL,
            target_json TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            issues_json TEXT NOT NULL,
            split TEXT
        );
        CREATE TABLE group_assignment(lineage_root TEXT PRIMARY KEY, rank_digest TEXT NOT NULL, split TEXT);
        CREATE INDEX sample_links_row_idx ON sample_links(sample_row);
        CREATE INDEX sample_links_key_idx ON sample_links(key);
        CREATE INDEX samples_content_digest_idx ON samples(content_digest);
        CREATE INDEX samples_family_idx ON samples(source_family_id);
        CREATE INDEX samples_conversation_idx ON samples(conversation_id);
        """
    )


def _read_snapshot(
    connection: sqlite3.Connection,
    path: Path,
    *,
    target_schema: str,
) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "total": 0,
        "eligible": 0,
        "excluded": 0,
        "policyExcluded": 0,
        "review": 0,
        "published": 0,
        "recognized": 0,
        "formatErrors": 0,
        "duplicateCandidates": 0,
        "blockers": [],
    }
    try:
        stream = path.open("rb")
    except OSError as exc:
        raise ValueError(f"cannot open training snapshot: {exc}") from exc
    with stream:
        line_number = 0
        while raw_line := stream.readline(_MAX_RECORD_BYTES + 1):
            line_number += 1
            if len(raw_line) > _MAX_RECORD_BYTES:
                while not raw_line.endswith(b"\n"):
                    raw_line = stream.readline(_MAX_RECORD_BYTES + 1)
                    if not raw_line:
                        break
                totals["total"] += 1
                totals["formatErrors"] += 1
                _block(totals, line_number, "record exceeds the 16 MiB row limit")
                continue
            if not raw_line.strip():
                totals["total"] += 1
                totals["formatErrors"] += 1
                _block(
                    totals, line_number, "blank snapshot lines are not valid records"
                )
                continue
            totals["total"] += 1
            try:
                record = json.loads(
                    raw_line,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                RecursionError,
                ValueError,
            ) as exc:
                totals["formatErrors"] += 1
                _block(totals, line_number, f"invalid snapshot JSON: {exc}")
                continue
            try:
                _insert_snapshot_record(
                    connection,
                    record,
                    line_number=line_number,
                    target_schema=target_schema,
                    totals=totals,
                )
            except (TypeError, ValueError, KeyError) as exc:
                totals["formatErrors"] += 1
                _block(totals, line_number, str(exc))
    connection.commit()
    return totals


def _insert_snapshot_record(
    connection: sqlite3.Connection,
    record: Any,
    *,
    line_number: int,
    target_schema: str,
    totals: dict[str, Any],
) -> None:
    if (
        not isinstance(record, dict)
        or record.get("schemaVersion") != "cyrene.training-record.v1"
    ):
        raise ValueError("record does not use cyrene.training-record.v1")
    record_id = _text(record.get("id"), "record.id")
    sample_id = _text(record.get("sampleId"), "record.sampleId")
    source_revision_id = _text(
        record.get("sourceRevisionId"), "record.sourceRevisionId"
    )
    source_family_id = _text(record.get("sourceFamilyId"), "record.sourceFamilyId")
    record_recipe_digest = _digest(record.get("recipeDigest"), "record.recipeDigest")
    conversation_id = _optional_text(
        record.get("conversationId"), "record.conversationId"
    )
    locator = record.get("locator")
    policy = record.get("policy")
    issues = record.get("issues", [])
    processing_history = record.get("processingHistory", [])
    if not isinstance(locator, dict):
        raise TypeError("record.locator must be an object")
    if not isinstance(policy, dict):
        raise TypeError("record.policy must be an object")
    if not isinstance(issues, list) or any(
        not isinstance(issue, dict) for issue in issues
    ):
        raise TypeError("record.issues must be an array of objects")
    if not isinstance(processing_history, list) or any(
        not isinstance(item, dict) for item in processing_history
    ):
        raise TypeError("record.processingHistory must be an array of objects")
    if record.get("detectedFormat") not in {None, "", "unknown", "auto"}:
        totals["recognized"] += 1
    if any(
        isinstance(issue.get("code"), str) and issue["code"] in _FORMAT_ERROR_CODES
        for issue in issues
    ):
        totals["formatErrors"] += 1
    if any("duplicate" in str(issue.get("code", "")).lower() for issue in issues):
        totals["duplicateCandidates"] += 1
    disposition = record.get("disposition")
    if disposition == "review":
        totals["review"] += 1
        _block(totals, line_number, "record is still in review")
        return
    if disposition == "excluded":
        totals["excluded"] += 1
        return
    if disposition != "eligible":
        raise ValueError("record.disposition must be eligible, review, or excluded")
    if any(issue.get("severity") == "error" for issue in issues):
        _block(totals, line_number, "eligible record still contains an error issue")
        return
    if not _training_allowed(policy):
        totals["excluded"] += 1
        totals["policyExcluded"] += 1
        return
    totals["eligible"] += 1
    normalized = record.get("normalized")
    if not isinstance(normalized, dict):
        raise TypeError("eligible record.normalized must be an object")
    messages = normalized.get("messages")
    _validate_messages(messages, line_number)
    projected = _project_messages(messages, target_schema, record_id, totals)
    content_digest = _digest(record.get("contentDigest"), "record.contentDigest")
    issues_json = _canonical_json(issues).decode("utf-8")
    raw_line = record.get("rawLine")
    if raw_line is not None and not isinstance(raw_line, str):
        raise ValueError("record.rawLine must be text when present")
    raw_record = record.get("rawRecord")
    raw_digest = _raw_digest(raw_line, raw_record)
    sample_row = totals["total"]
    connection.execute(
        """INSERT INTO samples(
            sample_row, record_id, sample_id, source_revision_id, source_family_id,
            conversation_id, locator_json, raw_digest, content_digest, target_json,
            curation_recipe_digest, processing_history_json, policy_json, issues_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sample_row,
            record_id,
            sample_id,
            source_revision_id,
            source_family_id,
            conversation_id,
            _canonical_json(locator).decode("utf-8"),
            raw_digest or _raw_digest(raw_line, raw_record),
            content_digest,
            _canonical_json(projected).decode("utf-8"),
            record_recipe_digest,
            _canonical_json(processing_history).decode("utf-8"),
            _canonical_json(policy).decode("utf-8"),
            issues_json,
        ),
    )
    totals["published"] += 1
    keys = [f"family:{source_family_id}", f"content:{content_digest}"]
    if conversation_id:
        keys.append(f"conversation:{conversation_id}")
    for key in keys:
        _make_set(connection, key)
        connection.execute(
            "INSERT INTO sample_links(sample_row, key) VALUES (?, ?)", (sample_row, key)
        )
    for key in keys[1:]:
        _union(connection, keys[0], key)


def _project_messages(
    messages: list[dict[str, Any]],
    target_schema: str,
    record_id: str,
    totals: dict[str, Any],
) -> dict[str, Any]:
    if target_schema == "messages":
        return {"messages": messages}
    system: str | None = None
    turns = messages
    if turns and turns[0]["role"] == "system":
        system = turns[0]["content"]
        turns = turns[1:]
    pairs = [
        (turns[index]["content"], turns[index + 1]["content"])
        for index in range(0, len(turns), 2)
    ]
    if target_schema == "prompt_completion":
        if system is not None or len(pairs) != 1:
            _block(
                totals,
                record_id,
                "promptCompletion cannot represent system messages or multiple turns without loss",
            )
            return {}
        return {"prompt": pairs[0][0], "completion": pairs[0][1]}
    if target_schema != "instruction_history":
        raise ValueError("unsupported target schema")
    final_user, final_assistant = pairs[-1]
    content: dict[str, Any] = {
        "instruction": final_user,
        "input": "",
        "output": final_assistant,
        "system": system or "",
        "history": [list(pair) for pair in pairs[:-1]],
    }
    return content


def _validate_messages(messages: Any, line_number: int) -> None:
    if not isinstance(messages, list) or not messages:
        raise ValueError(
            f"record at snapshot line {line_number} has no canonical messages"
        )
    expected = "user"
    saw_user = False
    saw_assistant = False
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise TypeError(
                f"record at snapshot line {line_number} message {index} is not an object"
            )
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "user", "assistant"}:
            raise ValueError(
                f"record at snapshot line {line_number} uses unsupported role {role!r}; raw record remains in the snapshot"
            )
        if set(message) != {"role", "content"}:
            raise ValueError(
                f"record at snapshot line {line_number} has extra message fields; tool or multimodal data cannot be exported"
            )
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                f"record at snapshot line {line_number} has empty/non-text message content"
            )
        if role == "system":
            if index != 0 or saw_user:
                raise ValueError(
                    f"record at snapshot line {line_number} has a misplaced system message"
                )
        elif role != expected:
            raise ValueError(
                f"record at snapshot line {line_number} has an invalid role order at message {index}"
            )
        elif role == "user":
            saw_user = True
            expected = "assistant"
        else:
            saw_assistant = True
            expected = "user"
    if not saw_user or not saw_assistant or expected != "user":
        raise ValueError(
            f"record at snapshot line {line_number} must end with a complete assistant turn"
        )


def _training_allowed(policy: dict[str, Any]) -> bool:
    return (
        policy.get("allowTraining") is True
        and isinstance(policy.get("allowedUsePurposes"), list)
        and "model_training" in policy["allowedUsePurposes"]
    )


def _raise_if_blocked(totals: dict[str, Any], diagnostics_path: Path) -> None:
    if totals["blockers"]:
        totals["published"] = 0
        _write_json(diagnostics_path, totals)
        first = totals["blockers"][0]
        raise ValueError(
            "training export blocked: "
            f"{len(totals['blockers'])} unresolved conversion/review issue(s); first: {first['message']}"
        )
    if totals["published"] == 0:
        _write_json(diagnostics_path, totals)
        raise ValueError("training export has no approved, policy-eligible records")


def _write_bundle(
    connection: sqlite3.Connection,
    output_dir: Path,
    *,
    output_format: str,
    target_schema: str,
    recipe_digest: str,
    recipe_version: str,
    split: dict[str, Any],
    totals: dict[str, Any],
    lineage: dict[str, str | None],
) -> dict[str, Any]:
    # A split component is one connected set of source-family, conversation,
    # and exact-content digests, so none of those lineages can cross splits.
    split_stats = {
        "algorithm": _SPLIT_ALGORITHM,
        "ratios": {key: split[key] for key in _SPLITS},
        "seed": split["seed"],
        "samples": {},
        "lineage_components": {},
    }
    receipts: dict[str, dict[str, Any]] = {}
    learned_names = {
        "instruction_history": ["instruction", "input", "output", "system", "history"],
        "messages": ["messages"],
        "prompt_completion": ["prompt", "completion"],
    }[target_schema]
    for split_name in _SPLITS:
        target_path = output_dir / f"{split_name}.jsonl"
        count, digest, size = _write_split(connection, target_path, split_name)
        receipts[f"{split_name}.jsonl"] = {
            "digest": digest,
            "size_bytes": size,
            "row_count": count,
            "schema_fields": learned_names,
        }
        split_stats["samples"][split_name] = count
        split_stats["lineage_components"][split_name] = _count_components(
            connection, split_name
        )

    provenance_path = output_dir / "provenance.jsonl"
    provenance_count, provenance_digest, provenance_size = _write_provenance(
        connection, provenance_path
    )
    receipts["provenance.jsonl"] = {
        "digest": provenance_digest,
        "size_bytes": provenance_size,
        "row_count": provenance_count,
    }
    curation_recipe_digests = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT curation_recipe_digest FROM samples ORDER BY curation_recipe_digest"
        )
    ]
    manifest = {
        "schema_version": "cyrene.sft.bundle.v1",
        "profile": _PROFILE,
        "profile_version": 1,
        "dataset_id": lineage["dataset_id"],
        "content_revision_id": lineage["content_revision_id"],
        "processing_run_id": lineage["processing_run_id"],
        "mode": output_format,
        "schema": target_schema,
        "split_algorithm": _SPLIT_ALGORITHM,
        "split_ratios": {key: split[key] for key in _SPLITS},
        "split_seed": split["seed"],
        "split_stats": split_stats,
        "counts": {
            "total": totals["total"],
            "recognized": totals["recognized"],
            "formatErrors": totals["formatErrors"],
            "duplicateCandidates": totals["duplicateCandidates"],
            "pendingReview": totals["review"],
            "eligible": totals["eligible"],
            "excluded": totals["excluded"],
            "policyExcluded": totals["policyExcluded"],
            "published": totals["published"],
            "train": split_stats["samples"]["train"],
            "validation": split_stats["samples"]["validation"],
            "test": split_stats["samples"]["test"],
        },
        "recipe": {
            "capability": "dataset.generation.v1",
            "method": "prepare_training_sft",
            "version": recipe_version,
            "digest": recipe_digest,
        },
        "source_recipe_digests": curation_recipe_digests,
        "files": receipts,
    }
    manifest_path = output_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    receipts["manifest.json"] = _file_receipt(manifest_path, 1)
    bundle_path = output_dir / "bundle.zip"
    _write_deterministic_zip(
        bundle_path,
        [output_dir / f"{name}.jsonl" for name in _SPLITS]
        + [provenance_path, manifest_path],
    )
    bundle_digest = _sha256_file(bundle_path)
    return {
        "profile": _PROFILE,
        "bundle_path": str(bundle_path),
        "bundle_digest": bundle_digest,
        "bundle_size_bytes": bundle_path.stat().st_size,
        "sample_count": totals["published"],
        "counts": {
            "total": totals["total"],
            "recognized": totals["recognized"],
            "formatErrors": totals["formatErrors"],
            "duplicateCandidates": totals["duplicateCandidates"],
            "pendingReview": totals["review"],
            "eligible": totals["eligible"],
            "excluded": totals["excluded"],
            "policyExcluded": totals["policyExcluded"],
            "review": totals["review"],
            "published": totals["published"],
        },
        "files": receipts,
        "split_stats": split_stats,
        "recipe_digest": recipe_digest,
        "source_recipe_digests": curation_recipe_digests,
        "output_format": output_format,
        "warnings": [],
    }


def _assign_splits(connection: sqlite3.Connection, split: dict[str, Any]) -> None:
    groups = connection.execute("SELECT DISTINCT key FROM sample_links ORDER BY key")
    for (key,) in groups:
        root = _find(connection, key)
        digest = hashlib.sha256(
            f"{_SPLIT_ALGORITHM}:{split['seed']}:{root}".encode()
        ).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO group_assignment(lineage_root, rank_digest) VALUES (?, ?)",
            (root, digest),
        )
    group_count = connection.execute(
        "SELECT COUNT(*) FROM group_assignment"
    ).fetchone()[0]
    counts = {name: math.floor(group_count * split[name]) for name in _SPLITS}
    remainder = group_count - sum(counts.values())
    by_remainder = sorted(
        _SPLITS,
        key=lambda name: (
            -(group_count * split[name] - counts[name]),
            _SPLITS.index(name),
        ),
    )
    for name in by_remainder[:remainder]:
        counts[name] += 1
    active = [name for name in _SPLITS if split[name] > 0]
    if group_count >= len(active):
        for name in active:
            if counts[name] == 0:
                donors = [candidate for candidate in _SPLITS if counts[candidate] > 1]
                if not donors:
                    break
                donor = max(
                    donors,
                    key=lambda candidate: (
                        counts[candidate],
                        -_SPLITS.index(candidate),
                    ),
                )
                counts[donor] -= 1
                counts[name] = 1
    ranked = connection.execute(
        "SELECT lineage_root FROM group_assignment ORDER BY rank_digest, lineage_root"
    )
    name_index = 0
    end = counts[_SPLITS[name_index]]
    for index, (root,) in enumerate(ranked):
        while index >= end and name_index < len(_SPLITS) - 1:
            name_index += 1
            end += counts[_SPLITS[name_index]]
        connection.execute(
            "UPDATE group_assignment SET split = ? WHERE lineage_root = ?",
            (_SPLITS[name_index], root),
        )
    # A representative key may differ from the union-find root. Resolve each
    # sample's first lineage key before looking up its connected component.
    for sample_row, key in connection.execute(
        "SELECT sample_row, MIN(key) FROM sample_links GROUP BY sample_row ORDER BY sample_row"
    ):
        root = _find(connection, key)
        assignment = connection.execute(
            "SELECT split FROM group_assignment WHERE lineage_root = ?", (root,)
        ).fetchone()
        if assignment is not None:
            connection.execute(
                "UPDATE samples SET split = ? WHERE sample_row = ?",
                (assignment[0], sample_row),
            )
    connection.commit()


def _write_split(
    connection: sqlite3.Connection, path: Path, split_name: str
) -> tuple[int, str, int]:
    digest = hashlib.sha256()
    size = 0
    count = 0
    with path.open("wb") as stream:
        for (target_json,) in connection.execute(
            "SELECT target_json FROM samples WHERE split = ? ORDER BY sample_row",
            (split_name,),
        ):
            data = target_json.encode("utf-8") + b"\n"
            stream.write(data)
            digest.update(data)
            size += len(data)
            count += 1
    return count, f"sha256:{digest.hexdigest()}", size


def _write_provenance(
    connection: sqlite3.Connection, path: Path
) -> tuple[int, str, int]:
    digest = hashlib.sha256()
    size = 0
    count = 0
    with path.open("wb") as stream:
        query = """SELECT record_id, sample_id, source_revision_id, source_family_id,
                conversation_id, locator_json, raw_digest, content_digest,
                curation_recipe_digest, processing_history_json, policy_json, issues_json,
                split FROM samples ORDER BY sample_row"""
        for row in connection.execute(query):
            (
                record_id,
                sample_id,
                source_revision_id,
                source_family_id,
                conversation_id,
                locator_json,
                raw_digest,
                content_digest,
                curation_recipe_digest,
                processing_history_json,
                policy_json,
                issues_json,
                split,
            ) = row
            receipt = {
                "record_id": record_id,
                "sample_id": sample_id,
                "source_revision_id": source_revision_id,
                "source_family_id": source_family_id,
                "conversation_id": conversation_id,
                "locator": json.loads(locator_json),
                "raw_digest": raw_digest,
                "content_digest": content_digest,
                "curation_recipe_digest": curation_recipe_digest,
                "processing_history": json.loads(processing_history_json),
                "policy": json.loads(policy_json),
                "issues": json.loads(issues_json),
                "split": split,
            }
            data = _canonical_json(receipt) + b"\n"
            stream.write(data)
            digest.update(data)
            size += len(data)
            count += 1
    return count, f"sha256:{digest.hexdigest()}", size


def _count_components(connection: sqlite3.Connection, split_name: str) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM group_assignment WHERE split = ?", (split_name,)
    ).fetchone()[0]


def _make_set(connection: sqlite3.Connection, key: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO lineage(key,parent,rank) VALUES (?, ?, 0)", (key, key)
    )


def _find(connection: sqlite3.Connection, key: str) -> str:
    parent = connection.execute(
        "SELECT parent FROM lineage WHERE key = ?", (key,)
    ).fetchone()[0]
    if parent == key:
        return key
    root = _find(connection, parent)
    connection.execute("UPDATE lineage SET parent = ? WHERE key = ?", (root, key))
    return root


def _union(connection: sqlite3.Connection, first: str, second: str) -> None:
    first_root = _find(connection, first)
    second_root = _find(connection, second)
    if first_root == second_root:
        return
    first_rank = connection.execute(
        "SELECT rank FROM lineage WHERE key = ?", (first_root,)
    ).fetchone()[0]
    second_rank = connection.execute(
        "SELECT rank FROM lineage WHERE key = ?", (second_root,)
    ).fetchone()[0]
    if first_rank < second_rank:
        first_root, second_root = second_root, first_root
    connection.execute(
        "UPDATE lineage SET parent = ? WHERE key = ?", (first_root, second_root)
    )
    if first_rank == second_rank:
        connection.execute(
            "UPDATE lineage SET rank = rank + 1 WHERE key = ?", (first_root,)
        )


def _split_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("split must be an object")
    if set(value) != {"train", "validation", "test", "seed"}:
        raise ValueError("split must contain train, validation, test, and seed")
    ratios: dict[str, float] = {}
    for name in _SPLITS:
        item = value[name]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            or not 0 <= item <= 1
        ):
            raise ValueError(f"split.{name} must be a finite ratio from 0 to 1")
        ratios[name] = float(item)
    if not math.isclose(sum(ratios.values()), 1.0, abs_tol=1e-9):
        raise ValueError("split ratios must sum to 1")
    seed = value["seed"]
    if (
        isinstance(seed, bool)
        or not isinstance(seed, (int, str))
        or (isinstance(seed, str) and not seed)
    ):
        raise ValueError("split.seed must be a non-empty string or integer")
    return {**ratios, "seed": seed}


def _write_deterministic_zip(bundle_path: Path, files: list[Path]) -> None:
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(files, key=lambda item: item.name):
            info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o600 << 16
            with path.open("rb") as source, archive.open(info, "w") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)


def _file_receipt(path: Path, count: int) -> dict[str, Any]:
    return {
        "digest": _sha256_file(path),
        "size_bytes": path.stat().st_size,
        "row_count": count,
    }


def _block(totals: dict[str, Any], location: int | str, message: str) -> None:
    if len(totals["blockers"]) < _MAX_BLOCKERS:
        totals["blockers"].append({"location": location, "message": message[:500]})


def _keys(
    value: dict[str, Any], *, required: set[str], optional: set[str], field: str
) -> None:
    missing = required - value.keys()
    extra = value.keys() - required - optional
    if missing or extra:
        raise ValueError(
            f"{field} keys invalid; missing={sorted(missing)}, extra={sorted(extra)}"
        )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _absolute_path(value: Any, field: str) -> Path:
    path = Path(_text(value, field))
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    return path


def _digest(value: Any, field: str) -> str:
    text = _text(value, field)
    if not text.startswith("sha256:") or len(text) != 71 or text != text.lower():
        raise ValueError(f"{field} must be a sha256 digest")
    try:
        int(text.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be a sha256 digest") from exc
    return text


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number is not allowed: {value}")


def _raw_digest(raw_line: str | None, raw_record: Any) -> str:
    raw_bytes = (
        raw_line.encode("utf-8")
        if raw_line is not None
        else _canonical_json(raw_record)
    )
    return _sha256(raw_bytes)


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(_canonical_json(value) + b"\n")


__all__ = ["prepare_training_sft"]
