"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_activity.py                                                 │
│  Module: tests.test_activity                                         │
│  Role: Verify Teams event normalization and scope enforcement.       │
│                                                                      │
│  模块职责：验证 Teams event 归一化与范围校验                           │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json

import pytest

from teams_connector.activity import (
    ActivityRejected,
    normalize_activity,
    normalize_personal_installation,
)

BOT_APP_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
ACTOR_ID = "33333333-3333-4333-8333-333333333333"
TEAM_ID = "19:team-thread@thread.tacv2"
CHANNEL_ID = "19:channel-id@thread.tacv2"
CONVERSATION_ID = "19:channel-conversation@thread.tacv2"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"


def _activity(**overrides):
    """Return one fabricated Bot Framework channel message.

    中文：返回一个伪造的 Bot Framework 频道消息。
    """

    value = {
        "type": "message",
        "id": "1749000000000",
        "channelId": "msteams",
        "from": {"id": "29:actor", "aadObjectId": ACTOR_ID, "name": "Display only"},
        "recipient": {"id": "28:cyrene-bot", "type": "bot"},
        "conversation": {"id": CONVERSATION_ID, "tenantId": TENANT_ID},
        "serviceUrl": SERVICE_URL,
        "channelData": {
            "tenant": {"id": TENANT_ID},
            "team": {"id": TEAM_ID},
            "channel": {"id": CHANNEL_ID},
        },
        "text": "<at>Cyrene</at> inspect this",
        "entities": [
            {
                "type": "mention",
                "mentioned": {"id": "28:cyrene-bot", "name": "Cyrene"},
                "text": "<at>Cyrene</at>",
            }
        ],
        "attachments": [],
    }
    value.update(overrides)
    return value


def _normalize(value):
    """Normalize using the single allowed test tenant and team.

    中文：使用单一允许租户和团队完成归一化。
    """

    return normalize_activity(
        value,
        bot_app_id=BOT_APP_ID,
        tenant_id=TENANT_ID,
        allowed_team_ids=frozenset({TEAM_ID}),
    )


def test_initial_mention_creates_stable_channel_envelope():
    """Keep canonical scope, stable actor, root identity, and bot mention.

    中文：保留规范 scope、稳定 actor、根消息身份和 bot mention。
    """

    event = _normalize(_activity())

    assert event.payload["message_id"] == "1749000000000"
    assert event.payload["conversation"] == {
        "vendor": "msteams",
        "account_id": BOT_APP_ID,
        "conversation_id": CONVERSATION_ID,
        "kind": "channel",
    }
    assert event.payload["sender_id"] == ACTOR_ID
    assert "reply" not in event.payload
    assert event.trust == {
        "version": 1,
        "provider": "teams",
        "tenant_id": TENANT_ID,
        "actor_aad_object_id": ACTOR_ID,
        "team_id": TEAM_ID,
        "channel_id": CHANNEL_ID,
        "root_thread_id": "1749000000000",
        "message_id": "1749000000000",
        "bot_app_id": BOT_APP_ID,
        "service_url": SERVICE_URL,
    }
    assert event.payload["content"][0]["mention"]["target_id"] == BOT_APP_ID
    assert event.payload["content"][1] == {"text": {"text": "inspect this"}}


def test_two_actors_in_one_channel_thread_keep_distinct_authenticated_identity():
    """Keep actor identity separate when the shared route and thread are equal.

    中文：共享频道路由与根线程相同时，仍保留各自的已验证 actor 身份。
    """

    second_actor = "44444444-4444-4444-8444-444444444444"
    first = _normalize(_activity())
    second = _normalize(
        _activity(
            **{
                "from": {"id": "29:another-actor", "aadObjectId": second_actor},
                "id": "1749000000001",
                "replyToId": "1749000000000",
                "text": "another participant",
                "entities": [],
            }
        )
    )

    assert first.trust["root_thread_id"] == second.trust["root_thread_id"]
    assert first.payload["conversation"] == second.payload["conversation"]
    assert first.trust["actor_aad_object_id"] == ACTOR_ID
    assert second.trust["actor_aad_object_id"] == second_actor
    assert first.payload["sender_id"] != second.payload["sender_id"]


def _personal_activity(
    *, actor_aad_id=ACTOR_ID, activity_id="personal-message-1", **overrides
):
    """Return one fabricated authenticated one-person bot activity.

    中文：返回一条伪造的个人 bot activity。
    """

    value = {
        "type": "message",
        "id": activity_id,
        "from": {"id": "29:actor", "aadObjectId": actor_aad_id},
        "recipient": {"id": "28:cyrene-bot", "type": "bot"},
        "conversation": {
            "id": "a:personal-conversation-opaque",
            "tenantId": TENANT_ID,
            "conversationType": "personal",
        },
        "serviceUrl": SERVICE_URL,
        "channelData": {"tenant": {"id": TENANT_ID}},
        "text": "private question",
        "entities": [],
        "attachments": [],
    }
    value.update(overrides)
    return value


def test_personal_message_is_opt_in_and_keeps_private_route_in_trust():
    """Keep direct messages disabled by default and isolate enabled private scope.

    中文：个人消息默认关闭；启用后按 private scope 归一化并保留私有路由。
    """

    with pytest.raises(ActivityRejected, match="personal_chat_disabled"):
        _normalize(_personal_activity())

    event = normalize_activity(
        _personal_activity(),
        bot_app_id=BOT_APP_ID,
        tenant_id=TENANT_ID,
        allowed_team_ids=frozenset({TEAM_ID}),
        personal_chat_enabled=True,
    )

    assert event.payload["conversation"] == {
        "vendor": "msteams",
        "account_id": BOT_APP_ID,
        "conversation_id": "a:personal-conversation-opaque",
        "kind": "private",
    }
    assert event.payload["sender_id"] == ACTOR_ID
    assert event.payload["content"] == [{"text": {"text": "private question"}}]
    assert event.trust["conversation_kind"] == "private"
    assert event.trust["conversation_id"] == "a:personal-conversation-opaque"
    assert event.trust["actor_aad_object_id"] == ACTOR_ID
    assert "team_id" not in event.trust
    assert "channel_id" not in event.trust
    assert "root_thread_id" not in event.trust


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"conversation": {"id": "personal", "tenantId": TENANT_ID}},
            "team_or_channel_not_allowed",
        ),
        ({"from": {"id": "29:actor"}}, "missing_stable_actor_id"),
        (
            {
                "channelData": {
                    "tenant": {"id": TENANT_ID},
                    "team": {"id": TEAM_ID},
                    "channel": {"id": CHANNEL_ID},
                }
            },
            "invalid_personal_scope",
        ),
    ],
)
def test_personal_message_rejects_missing_actor_or_mixed_scope(overrides, reason):
    """Fail closed when a personal activity lacks stable scope or actor facts.

    中文：个人 activity 缺少稳定 scope 或 actor 身份时 fail closed。
    """

    with pytest.raises(ActivityRejected) as error:
        normalize_activity(
            _personal_activity(**overrides),
            bot_app_id=BOT_APP_ID,
            tenant_id=TENANT_ID,
            allowed_team_ids=frozenset({TEAM_ID}),
            personal_chat_enabled=True,
        )
    assert error.value.reason == reason


def _personal_installation(
    action, *, conversation_type="personal", actor_aad_id=ACTOR_ID
):
    """Return a fabricated personal install lifecycle activity.

    中文：返回伪造的个人安装生命周期 activity。
    """

    return {
        "type": "installationUpdate",
        "action": action,
        "id": f"installation-{action}-1",
        "from": {"id": "29:actor", "aadObjectId": actor_aad_id},
        "conversation": {
            "id": "a:personal-install-conversation",
            "tenantId": TENANT_ID,
            "conversationType": conversation_type,
        },
        "serviceUrl": SERVICE_URL,
        "channelData": {"tenant": {"id": TENANT_ID}},
    }


@pytest.mark.parametrize("action", ["add", "remove"])
def test_personal_installation_add_and_remove_are_host_only_trust_events(action):
    """Carry lifecycle action to Work without placing actor or route in payload.

    中文：将安装生命周期传给 Work；actor 与路由只进入 host trust。
    """

    event = normalize_personal_installation(
        _personal_installation(action),
        action=action,
        bot_app_id=BOT_APP_ID,
        tenant_id=TENANT_ID,
        personal_chat_enabled=True,
    ).as_event()

    assert event["event_type"] == "personal_installation"
    assert event["payload"] == {"action": action}
    assert event["trust"]["installation_action"] == action
    assert event["trust"]["actor_aad_object_id"] == ACTOR_ID
    assert event["trust"]["conversation_id"] == "a:personal-install-conversation"
    assert event["trust"]["service_url"] == SERVICE_URL


def test_personal_installation_rejects_disabled_or_nonpersonal_install():
    """Never create an installation binding outside enabled personal scope.

    中文：个人 scope 未启用或安装发生在其他 scope 时不得创建 binding。
    """

    with pytest.raises(ActivityRejected, match="personal_chat_disabled"):
        normalize_personal_installation(
            _personal_installation("add"),
            action="add",
            bot_app_id=BOT_APP_ID,
            tenant_id=TENANT_ID,
        )
    with pytest.raises(ActivityRejected, match="personal_installation_required"):
        normalize_personal_installation(
            _personal_installation("add", conversation_type="channel"),
            action="add",
            bot_app_id=BOT_APP_ID,
            tenant_id=TENANT_ID,
            personal_chat_enabled=True,
        )


def test_reply_without_mention_keeps_root_thread_and_direct_parent():
    """A no-mention reply remains a normal inbound event in the same root thread.

    中文：无 mention 的回复仍作为同一根线程的普通入站事件。
    """

    event = _normalize(
        _activity(
            id="1749000000001",
            replyToId="1749000000000",
            text="continue",
            entities=[],
        )
    )

    assert event.trust["root_thread_id"] == "1749000000000"
    assert event.trust["message_id"] == "1749000000001"
    assert event.payload["message_id"] == "1749000000001"
    assert event.payload["reply"] == {"message_id": "1749000000000"}
    assert event.payload["content"] == [{"text": {"text": "continue"}}]


def test_conversation_messageid_routes_root_activity_without_reply_to_id():
    """Use the Teams conversation suffix as the root for the initial post.

    中文：初始帖子没有 replyToId 时，以 Teams conversation 后缀确定根帖子。
    """

    root_id = "1749000000000"
    conversation_id = f"{CONVERSATION_ID};messageid={root_id}"
    event = _normalize(
        _activity(
            id=root_id,
            conversation={"id": conversation_id, "tenantId": TENANT_ID},
        )
    )

    assert event.payload["conversation"]["conversation_id"] == conversation_id
    assert event.payload["message_id"] == root_id
    assert "reply" not in event.payload
    assert event.trust["root_thread_id"] == root_id


def test_conversation_messageid_routes_unmentioned_child_to_original_root():
    """Keep child-message deduplication while routing its reply to the root.

    中文：子消息仍用自身 ID 去重，并路由到原根帖子。
    """

    root_id = "1749000000000"
    child_id = "1749000000001"
    conversation_id = f"{CONVERSATION_ID};messageid={root_id}"
    event = _normalize(
        _activity(
            id=child_id,
            conversation={"id": conversation_id, "tenantId": TENANT_ID},
            text="continue without mention",
            entities=[],
        )
    )

    assert event.payload["conversation"]["conversation_id"] == conversation_id
    assert event.payload["message_id"] == child_id
    assert event.payload["reply"] == {"message_id": root_id}
    assert event.trust["message_id"] == child_id
    assert event.trust["root_thread_id"] == event.payload["reply"]["message_id"]


def test_direct_reply_parent_is_fallback_when_conversation_has_no_root_suffix():
    """Retain the validated direct-parent fallback for older activity shapes.

    中文：conversation ID 无根帖后缀时，保留经过验证的直接父消息回退。
    """

    event = _normalize(
        _activity(
            id="1749000000001",
            replyToId="1749000000000",
            text="continue",
            entities=[],
        )
    )

    assert event.payload["reply"] == {"message_id": "1749000000000"}
    assert event.trust["root_thread_id"] == event.payload["reply"]["message_id"]


@pytest.mark.parametrize(
    "root_id",
    ["", "letters", "0", "0000", "١٢٣"],
)
def test_malformed_conversation_root_fails_closed(root_id):
    """Reject malformed or unbounded root metadata instead of misrouting.

    中文：根帖 metadata 格式错误或超长时直接拒绝，避免错误路由。
    """

    with pytest.raises(ActivityRejected) as error:
        _normalize(
            _activity(
                conversation={
                    "id": f"{CONVERSATION_ID};messageid={root_id}",
                    "tenantId": TENANT_ID,
                }
            )
        )

    assert error.value.reason == "invalid_thread_identity"


def test_oversized_conversation_identity_fails_closed():
    """Reject an overlong conversation ID before it can define a thread route.

    中文：conversation ID 超出上限时拒绝，不允许其定义线程路由。
    """

    with pytest.raises(ActivityRejected) as error:
        _normalize(
            _activity(
                conversation={
                    "id": f"{CONVERSATION_ID};messageid={'9' * 513}",
                    "tenantId": TENANT_ID,
                }
            )
        )

    assert error.value.reason == "missing_conversation_identity"


@pytest.mark.parametrize(
    "suffix",
    [
        ";messageid",
        ";messageid=1749000000000;messageid=1749000000000",
        ";messageid=1749000000000;messageid=1749000000001",
        ";messageid-extra=1749000000000",
    ],
)
def test_ambiguous_conversation_root_fails_closed(suffix):
    """Reject missing, duplicate, or malformed root metadata segments.

    中文：根帖 metadata 缺失、重复或格式含糊时直接拒绝。
    """

    with pytest.raises(ActivityRejected) as error:
        _normalize(
            _activity(
                conversation={
                    "id": f"{CONVERSATION_ID}{suffix}",
                    "tenantId": TENANT_ID,
                }
            )
        )

    assert error.value.reason == "invalid_thread_identity"


def test_conflicting_direct_parent_and_conversation_root_fail_closed():
    """Reject contradictory root sources rather than choosing one silently.

    中文：直接父消息与 conversation 根帖不一致时拒绝，不静默选择其中一方。
    """

    with pytest.raises(ActivityRejected) as error:
        _normalize(
            _activity(
                id="1749000000002",
                replyToId="1749000000001",
                conversation={
                    "id": f"{CONVERSATION_ID};messageid=1749000000000",
                    "tenantId": TENANT_ID,
                },
            )
        )

    assert error.value.reason == "invalid_thread_identity"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "tenant",
            {"id": "44444444-4444-4444-8444-444444444444"},
            "tenant_not_allowed",
        ),
        ("team", {"id": "other-team"}, "team_or_channel_not_allowed"),
    ],
)
def test_other_tenant_or_team_is_rejected(field, value, reason):
    """A valid bot-authenticated activity cannot cross configured tenant/team scope.

    中文：JWT 有效的 activity 仍不能跨越配置的 tenant/team 范围。
    """

    message = _activity()
    message["channelData"][field] = value

    with pytest.raises(ActivityRejected) as error:
        _normalize(message)

    assert error.value.reason == reason


def test_self_authored_messages_are_dropped():
    """Do not forward the bot's own channel echo to Navigator.

    中文：不将 bot 自己发出的频道 echo 转发给 Navigator。
    """

    message = _activity(**{"from": {"id": "28:cyrene-bot", "type": "bot"}})

    with pytest.raises(ActivityRejected) as error:
        _normalize(message)

    assert error.value.reason == "self_authored"


def test_missing_stable_actor_id_fails_closed():
    """Do not fall back to a display name or Teams channel-local sender ID.

    中文：不回退使用 display name 或频道本地 sender ID。
    """

    message = _activity(**{"from": {"id": "29:actor", "name": "Display only"}})

    with pytest.raises(ActivityRejected) as error:
        _normalize(message)

    assert error.value.reason == "missing_stable_actor_id"


def test_missing_or_untrusted_service_url_fails_closed():
    """Do not create a durable route without the SDK-verified service URL.

    中文：没有 SDK 验证的 service URL 时不创建持久 route。
    """

    missing = _activity()
    missing.pop("serviceUrl")
    with pytest.raises(ActivityRejected) as missing_error:
        _normalize(missing)

    untrusted = _activity(serviceUrl="https://attacker.example.invalid/amer/")
    with pytest.raises(ActivityRejected) as untrusted_error:
        _normalize(untrusted)

    assert missing_error.value.reason == "missing_service_url"
    assert untrusted_error.value.reason == "service_url_not_allowed"


@pytest.mark.parametrize("message_id", ["message-id", "0", "0000", "١٢٣"])
def test_channel_message_id_must_support_sdk_thread_routing(message_id):
    """Reject IDs that the official Teams SDK cannot use as a thread root.

    中文：拒绝官方 Teams SDK 无法用作线程根的 message ID。
    """

    with pytest.raises(ActivityRejected) as error:
        _normalize(_activity(id=message_id))

    assert error.value.reason == "invalid_thread_identity"


def test_attachment_uses_opaque_media_reference_and_never_copies_signed_url():
    """Normalize a safe media ID and omit a signed attachment URL.

    中文：归一化安全 media ID，并省略带签名的附件 URL。
    """

    signed_url = "https://files.example.invalid/download?tempauth=private-value"
    event = _normalize(
        _activity(
            text="review file",
            entities=[],
            attachments=[
                {
                    "id": "attachment-1",
                    "name": "plan.pdf",
                    "contentType": "application/vnd.microsoft.teams.file.download.info",
                    "contentUrl": signed_url,
                    "content": {"uniqueId": "opaque-file-1", "fileType": "pdf"},
                }
            ],
        )
    )
    serialized = json.dumps(event.as_event(), sort_keys=True)

    assert (
        event.payload["content"][1]["file"]["reference"]["vendor_media"]["media_id"]
        == "opaque-file-1"
    )
    assert signed_url not in serialized
    assert "private-value" not in serialized


def test_attachment_without_opaque_id_is_omitted_and_counted():
    """Never substitute an attachment URL when no safe media ID exists.

    中文：缺少安全 media ID 时不以附件 URL 替代，并记录省略数量。
    """

    event = _normalize(
        _activity(
            text="review file",
            entities=[],
            attachments=[
                {
                    "name": "private.docx",
                    "contentType": "application/vnd.microsoft.teams.file.download.info",
                    "contentUrl": "https://files.example.invalid/?token=secret",
                    "content": {
                        "downloadUrl": "https://files.example.invalid/?token=secret"
                    },
                }
            ],
        )
    )
    serialized = json.dumps(event.as_event(), sort_keys=True)

    assert all("file" not in part for part in event.payload["content"])
    assert {"name": "attachments_omitted", "value": "1"} in event.payload[
        "vendor_extension"
    ]["facts"]
    assert "files.example.invalid" not in serialized
    assert "token=secret" not in serialized


def test_url_like_attachment_identifier_is_not_persisted():
    """A URL in an unexpected vendor ID field is still omitted.

    中文：意外出现在 vendor ID 字段中的 URL 仍会被省略。
    """

    private_url = "https://files.example.invalid/?tempauth=secret"
    event = _normalize(
        _activity(
            text="review",
            entities=[],
            attachments=[
                {
                    "id": private_url,
                    "name": "private.pdf",
                    "contentType": "application/pdf",
                }
            ],
        )
    )
    serialized = json.dumps(event.as_event(), sort_keys=True)

    assert private_url not in serialized
    assert {"name": "attachments_omitted", "value": "1"} in event.payload[
        "vendor_extension"
    ]["facts"]
