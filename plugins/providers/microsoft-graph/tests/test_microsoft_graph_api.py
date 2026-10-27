"""
┌────────────────────────────────────────────────────────────────────┐
│  Offline tests for the fixed Microsoft Graph v1.0 provider          │
│  使用合成响应验证固定 Graph 路由与失败边界                           │
└────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from microsoft_graph_provider.microsoft_graph_api import (
    GRAPH_HOST,
    GraphApiFailure,
    HttpResponse,
    MicrosoftGraphApi,
)

TENANT_ID = "11111111-1111-4111-8111-111111111111"
ACTOR_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ACTOR_ID = "33333333-3333-4333-8333-333333333333"
PROFILE_REF = "44444444-4444-4444-8444-444444444444"
BINDING_ID = "graph-binding-1"
PLAN_ID = "plan_01"
TASK_ID = "task_01"
BUCKET_ID = "bucket_01"
SITE_ID = (
    "tenant.sharepoint.com,"
    "55555555-5555-4555-8555-555555555555,"
    "66666666-6666-4666-8666-666666666666"
)
LIST_ID = "77777777-7777-4777-8777-777777777777"
SYNTHETIC_TOKEN = "synthetic-delegated-token"


class FakeTransport:
    """Queue deterministic responses without a network connection."""

    def __init__(self, responses: list[HttpResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, str, dict[str, str], bytes | None]] = []

    def request(
        self,
        method: str,
        host: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        del timeout_seconds
        self.calls.append((method, host, path, dict(headers), body))
        if not self.responses:
            raise AssertionError("unexpected Graph request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _json_response(value: object, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status, {"content-type": "application/json"}, json.dumps(value).encode())


def _empty_response(*, status: int) -> HttpResponse:
    return HttpResponse(status, {}, b"")


def _api(transport: FakeTransport) -> MicrosoftGraphApi:
    return MicrosoftGraphApi(cursor_key=b"k" * 32, transport=transport)


def _open(transport: FakeTransport):
    return _api(transport).open_session(
        SYNTHETIC_TOKEN,
        f"{TENANT_ID}:{ACTOR_ID}",
        binding_id=BINDING_ID,
        profile_ref=PROFILE_REF,
    )


def _me() -> HttpResponse:
    return _json_response({"id": ACTOR_ID})


def _task(
    *,
    task_id: str = TASK_ID,
    plan_id: str = PLAN_ID,
    title: str = "Review approved task",
    etag: str = 'W/"etag-1"',
    created_by: str = ACTOR_ID,
    created_at: str = "2026-10-27T12:00:00Z",
) -> dict[str, object]:
    return {
        "id": task_id,
        "planId": plan_id,
        "bucketId": BUCKET_ID,
        "title": title,
        "percentComplete": 0,
        "priority": 5,
        "createdDateTime": created_at,
        "lastModifiedDateTime": created_at,
        "@odata.etag": etag,
        "createdBy": {"user": {"id": created_by}},
    }


def test_same_token_me_mismatch_stops_before_any_resource_request() -> None:
    transport = FakeTransport([_json_response({"id": OTHER_ACTOR_ID})])

    with pytest.raises(GraphApiFailure, match="GRAPH_ACTOR_MISMATCH"):
        _open(transport)

    assert len(transport.calls) == 1
    method, host, path, headers, _ = transport.calls[0]
    assert (method, host) == ("GET", GRAPH_HOST)
    assert urlsplit(path).path == "/v1.0/me"
    assert headers["Authorization"] == f"Bearer {SYNTHETIC_TOKEN}"


def test_planner_pages_use_fixed_route_and_profile_bound_cursor() -> None:
    first_link = (
        "https://graph.microsoft.com/v1.0/planner/plans/plan_01/tasks?$skiptoken=next-page&$top=2"
    )
    transport = FakeTransport(
        [
            _me(),
            _json_response({"value": [_task()], "@odata.nextLink": first_link}),
            _json_response({"value": [_task(task_id="task_02")]}),
        ]
    )
    session = _open(transport)

    page_one = session.list_tasks(PLAN_ID, page_size=2)
    page_two = session.list_tasks(
        PLAN_ID,
        page_size=2,
        page_cursor=page_one["next_cursor"],
    )

    assert page_one["complete"] is False
    assert page_one["tasks"][0]["task_id"] == TASK_ID
    assert page_two["complete"] is True
    assert page_two["tasks"][0]["task_id"] == "task_02"
    assert "next-page" not in page_one["next_cursor"]
    _, host, second_path, second_headers, _ = transport.calls[2]
    second_query = parse_qs(urlsplit(second_path).query)
    assert host == GRAPH_HOST
    assert urlsplit(second_path).path == "/v1.0/planner/plans/plan_01/tasks"
    assert second_query == {"$top": ["2"], "$skiptoken": ["next-page"]}
    assert second_headers["Authorization"] == f"Bearer {SYNTHETIC_TOKEN}"


def test_planner_cursor_cannot_be_reused_for_another_profile() -> None:
    transport = FakeTransport(
        [
            _me(),
            _json_response(
                {
                    "value": [_task()],
                    "@odata.nextLink": (
                        "https://graph.microsoft.com/v1.0/planner/plans/plan_01/tasks"
                        "?$skiptoken=opaque"
                    ),
                }
            ),
            _me(),
        ]
    )
    session = _open(transport)
    cursor = session.list_tasks(PLAN_ID)["next_cursor"]
    other_session = _api(transport).open_session(
        SYNTHETIC_TOKEN,
        f"{TENANT_ID}:{ACTOR_ID}",
        binding_id=BINDING_ID,
        profile_ref="88888888-8888-4888-8888-888888888888",
    )

    with pytest.raises(GraphApiFailure, match="INVALID_PROVIDER_CURSOR"):
        other_session.list_tasks(PLAN_ID, page_cursor=cursor)


def test_planner_rejects_off_host_next_link() -> None:
    transport = FakeTransport(
        [
            _me(),
            _json_response(
                {
                    "value": [_task()],
                    "@odata.nextLink": (
                        "https://attacker.example/v1.0/planner/plans/plan_01/tasks"
                        "?$skiptoken=opaque"
                    ),
                }
            ),
        ]
    )

    with pytest.raises(GraphApiFailure, match="INVALID_PROVIDER_CURSOR"):
        _open(transport).list_tasks(PLAN_ID)


def test_update_uses_fresh_etag_and_confirms_with_readback() -> None:
    updated = _task(title="Task closed", etag='W/"etag-2"')
    updated["percentComplete"] = 100
    transport = FakeTransport(
        [
            _me(),
            _json_response(_task()),
            _empty_response(status=204),
            _json_response(updated),
        ]
    )

    result = _open(transport).update_task(
        PLAN_ID,
        TASK_ID,
        'W/"etag-1"',
        {"title": "Task closed", "percent_complete": 100},
    )

    assert result["status"] == "CONFIRMED"
    assert result["task"]["etag"] == 'W/"etag-2"'
    method, _, patch_path, headers, body = transport.calls[2]
    assert method == "PATCH"
    assert patch_path == "/v1.0/planner/tasks/task_01"
    assert headers["If-Match"] == 'W/"etag-1"'
    assert json.loads(body) == {"title": "Task closed", "percentComplete": 100}


def test_update_stale_etag_stops_before_patch() -> None:
    transport = FakeTransport([_me(), _json_response(_task())])

    with pytest.raises(GraphApiFailure, match="CONCURRENCY_CONFLICT"):
        _open(transport).update_task(
            PLAN_ID,
            TASK_ID,
            'W/"stale"',
            {"title": "Task closed"},
        )

    assert [call[0] for call in transport.calls] == ["GET", "GET"]


def test_timed_out_create_stays_unknown_when_interleaved_same_title_task_appears() -> None:
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    # This row represents B's identical create landing before A's readback.
    row = _task(task_id="task_from_b", title="Review approved task", created_at=created_at)
    transport = FakeTransport(
        [
            _me(),
            _json_response({"id": PLAN_ID, "title": "Pilot plan"}),
            _json_response({"id": BUCKET_ID, "planId": PLAN_ID}),
            TimeoutError("synthetic timeout after POST dispatch"),
            _json_response({"value": [row]}),
        ]
    )

    result = _open(transport).create_task(PLAN_ID, BUCKET_ID, "Review approved task")

    assert result["status"] == "UNKNOWN"
    assert result["task"] is None
    assert result["reconciliation"] == "ambiguous_remote_create"
    assert result["possible_matches"] == [
        {
            "task_id": "task_from_b",
            "bucket_id": BUCKET_ID,
            "created_date_time": created_at,
        }
    ]
    assert result["possible_match_count"] == 1
    assert result["possible_matches_complete"] is True
    assert [call[0] for call in transport.calls].count("POST") == 1


def test_ambiguous_create_with_multiple_matches_stays_unknown_and_is_not_replayed() -> None:
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    transport = FakeTransport(
        [
            _me(),
            _json_response({"id": PLAN_ID, "title": "Pilot plan"}),
            _json_response({"id": BUCKET_ID, "planId": PLAN_ID}),
            TimeoutError("synthetic timeout after POST dispatch"),
            _json_response(
                {
                    "value": [
                        _task(task_id="task_a", created_at=created_at),
                        _task(task_id="task_b", created_at=created_at),
                    ]
                }
            ),
        ]
    )

    result = _open(transport).create_task(PLAN_ID, BUCKET_ID, "Review approved task")

    assert result["status"] == "UNKNOWN"
    assert result["reconciliation"] == "ambiguous_remote_create"
    assert result["possible_match_count"] == 2
    assert [call[0] for call in transport.calls].count("POST") == 1


def test_sharepoint_delta_projects_metadata_only_and_commits_only_final_delta_link() -> None:
    base = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/lists/{LIST_ID}/items/delta"
    row = {
        "id": "42",
        "name": "Quarterly plan.docx",
        "eTag": '"item-etag"',
        "createdDateTime": "2026-10-26T08:30:00Z",
        "lastModifiedDateTime": "2026-10-27T08:30:00Z",
        "contentType": {"name": "Document", "id": "synthetic-type"},
        "deleted": None,
        "parentReference": {"path": "/private/path"},
        "fields": {"SensitiveColumn": "must-not-escape"},
        "webUrl": "https://tenant.sharepoint.com/private/path",
        "createdBy": {"user": {"displayName": "Synthetic Person"}},
    }
    transport = FakeTransport(
        [
            _me(),
            _json_response({"value": [row], "@odata.nextLink": f"{base}?token=page-1"}),
            _json_response({"value": [], "@odata.deltaLink": f"{base}?token=delta-1"}),
        ]
    )
    session = _open(transport)

    first = session.list_items_delta(SITE_ID, LIST_ID, page_size=20)
    second = session.list_items_delta(
        SITE_ID,
        LIST_ID,
        page_size=20,
        page_cursor=first["next_cursor"],
    )

    item = first["items"][0]
    assert first["complete"] is False
    assert second["complete"] is True
    assert second["next_cursor"] is not None
    assert item == {
        "item_id": "42",
        "name": "Quarterly plan.docx",
        "etag": '"item-etag"',
        "created_date_time": "2026-10-26T08:30:00Z",
        "last_modified_date_time": "2026-10-27T08:30:00Z",
        "content_type": "Document",
        "deleted": False,
    }
    assert "must-not-escape" not in json.dumps(first)
    query = parse_qs(urlsplit(transport.calls[1][2]).query)
    assert query["$select"] == [
        "id,name,eTag,createdDateTime,lastModifiedDateTime,contentType,deleted,parentReference"
    ]


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "GRAPH_AUTHENTICATION_FAILED"), (403, "GRAPH_ACCESS_DENIED")],
)
def test_graph_auth_and_permission_failures_are_explicit(status: int, code: str) -> None:
    transport = FakeTransport([_me(), _empty_response(status=status)])

    with pytest.raises(GraphApiFailure) as raised:
        _open(transport).get_plan(PLAN_ID)

    assert raised.value.code == code


def test_expired_sharepoint_delta_token_requires_rebaseline_without_following_location() -> None:
    transport = FakeTransport(
        [_me(), HttpResponse(410, {"location": "https://attacker.example/reset"}, b"{}")]
    )

    with pytest.raises(GraphApiFailure) as raised:
        _open(transport).list_items_delta(SITE_ID, LIST_ID)

    assert raised.value.code == "GRAPH_DELTA_RESYNC_REQUIRED"
    assert len(transport.calls) == 2


def test_rate_limit_is_explicit_and_provider_does_not_retry() -> None:
    transport = FakeTransport([_me(), HttpResponse(429, {"retry-after": "17"}, b"{}")])

    with pytest.raises(GraphApiFailure) as raised:
        _open(transport).get_plan(PLAN_ID)

    assert raised.value.code == "GRAPH_RATE_LIMITED"
    assert raised.value.retryable is True
    assert raised.value.retry_after == 17
    assert len(transport.calls) == 2
