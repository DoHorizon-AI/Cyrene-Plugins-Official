"""
┌────────────────────────────────────────────────────────────────────┐
│  Graph target and lease-argument digest projection vectors            │
│  验证资源标识由 target 绑定，其余参数由摘要绑定                        │
└────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from typing import Any

from cyrene_account_center_contracts import payload_sha256

from microsoft_graph_provider.plugin import _arguments_for_lease, _target

TENANT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SITE_ID = (
    "tenant.sharepoint.com,"
    "11111111-1111-4111-8111-111111111111,"
    "22222222-2222-4222-8222-222222222222"
)


def _mutate(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "_changed"
    if isinstance(value, dict):
        return {key: _mutate(item) for key, item in value.items()}
    raise AssertionError(f"test vector has unsupported parameter type: {type(value)!r}")


def test_all_graph_operations_bind_target_ids_and_digest_every_remaining_field() -> None:
    vectors: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = (
        (
            "microsoft_graph.planner.plan.get",
            {"plan_id": "plan_01"},
            {},
        ),
        (
            "microsoft_graph.planner.tasks.list",
            {"plan_id": "plan_01", "page_size": 25, "page_cursor": "cursor_a"},
            {"page_size": 25, "page_cursor": "cursor_a"},
        ),
        (
            "microsoft_graph.planner.task.get",
            {"plan_id": "plan_01", "task_id": "task_01"},
            {},
        ),
        (
            "microsoft_graph.planner.task.create",
            {
                "plan_id": "plan_01",
                "bucket_id": "bucket_01",
                "title": "Review plan",
                "due_date_time": "2026-10-31T12:00:00Z",
            },
            {"title": "Review plan", "due_date_time": "2026-10-31T12:00:00Z"},
        ),
        (
            "microsoft_graph.planner.task.update",
            {
                "plan_id": "plan_01",
                "task_id": "task_01",
                "if_match": 'W/"etag-1"',
                "changes": {"title": "Done", "priority": 3},
            },
            {"if_match": 'W/"etag-1"', "changes": {"title": "Done", "priority": 3}},
        ),
        (
            "microsoft_graph.sharepoint.site.get",
            {"site_id": SITE_ID},
            {},
        ),
        (
            "microsoft_graph.sharepoint.list.get",
            {
                "site_id": SITE_ID,
                "list_id": "33333333-3333-4333-8333-333333333333",
            },
            {},
        ),
        (
            "microsoft_graph.sharepoint.list.items.delta",
            {
                "site_id": SITE_ID,
                "list_id": "33333333-3333-4333-8333-333333333333",
                "page_size": 10,
                "page_cursor": "cursor_b",
            },
            {"page_size": 10, "page_cursor": "cursor_b"},
        ),
    )
    target_fields = {
        "microsoft_graph.planner.plan.get": {"plan_id"},
        "microsoft_graph.planner.tasks.list": {"plan_id"},
        "microsoft_graph.planner.task.get": {"plan_id", "task_id"},
        "microsoft_graph.planner.task.create": {"plan_id", "bucket_id"},
        "microsoft_graph.planner.task.update": {"plan_id", "task_id"},
        "microsoft_graph.sharepoint.site.get": {"site_id"},
        "microsoft_graph.sharepoint.list.get": {"site_id", "list_id"},
        "microsoft_graph.sharepoint.list.items.delta": {"site_id", "list_id"},
    }

    assert set(target_fields) == {tool_id for tool_id, _, _ in vectors}
    for tool_id, arguments, expected_digest_payload in vectors:
        projected = _arguments_for_lease(tool_id, arguments)
        assert projected == expected_digest_payload, tool_id
        original_digest = payload_sha256(projected)
        outer_payload_digest = payload_sha256(arguments)
        original_target = _target({"tenant_id": TENANT_ID}, tool_id, arguments)
        changed_tenant_target = _target(
            {"tenant_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
            tool_id,
            arguments,
        )
        assert original_target != changed_tenant_target, tool_id

        for field in target_fields[tool_id]:
            changed_target = dict(arguments)
            changed_target[field] = _mutate(changed_target[field])
            assert _arguments_for_lease(tool_id, changed_target) == projected, (tool_id, field)
            assert payload_sha256(_arguments_for_lease(tool_id, changed_target)) == original_digest
            assert payload_sha256(changed_target) != outer_payload_digest, (tool_id, field)
            assert _target({"tenant_id": TENANT_ID}, tool_id, changed_target) != original_target, (
                tool_id,
                field,
            )

        for field in expected_digest_payload:
            changed_parameter = dict(arguments)
            changed_parameter[field] = _mutate(changed_parameter[field])
            assert (
                payload_sha256(_arguments_for_lease(tool_id, changed_parameter)) != original_digest
            ), (
                tool_id,
                field,
            )
