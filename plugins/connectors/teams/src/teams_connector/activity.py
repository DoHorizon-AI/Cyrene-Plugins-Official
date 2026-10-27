"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 activity.py                                                      │
│  Module: teams_connector.activity                                   │
│  Role: Normalize verified Teams messages and personal installs.      │
│                                                                      │
│  模块职责：归一化经过验证的 Teams 消息与个人安装事件                   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .service_url import ServiceUrlError, validate_bot_service_url

_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,127}/[A-Za-z0-9!#$&^_.+-]{1,127}$")


class ActivityRejected(ValueError):
    """A validly delivered activity is outside this connector binding.

    中文：Teams 已投递但不属于当前 connector binding 的 activity。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class NormalizedTeamsActivity:
    """Normalized Teams payload and host-only identity envelope.

    中文：规范化 Teams payload 与仅供主机使用的身份信任 envelope。
    """

    payload: dict[str, Any]
    trust: dict[str, Any]
    event_type: str = "inbound_message"

    def as_event(self) -> dict[str, Any]:
        """Return the Navigator connector-event request body.

        中文：生成 Navigator connector event 请求体。
        """

        return {
            "event_type": self.event_type,
            "payload": self.payload,
            "trust": self.trust,
        }


def normalize_activity(
    activity: Mapping[str, Any],
    *,
    bot_app_id: str,
    tenant_id: str,
    allowed_team_ids: frozenset[str],
    personal_chat_enabled: bool = False,
) -> NormalizedTeamsActivity:
    """Normalize one authenticated message within channel or personal scope.

    The caller must pass an activity only after the Microsoft Teams SDK has
    validated its Bot Framework authorization header.

    中文：归一化一个已认证的消息 activity 并校验 channel/personal binding 范围。
    调用者必须先使用微软 Teams SDK 验证 Bot Framework authorization header。
    """

    if not isinstance(activity, Mapping):
        raise ActivityRejected("invalid_activity")
    if _string(_get(activity, "type")) != "message":
        raise ActivityRejected("unsupported_activity")

    sender = _mapping(_get(activity, "from", "from_"))
    recipient = _mapping(_get(activity, "recipient"))
    sender_id = _bounded_text(_get(sender, "id"), "sender_id")
    recipient_id = _optional_text(_get(recipient, "id"), limit=512)
    raw_actor_aad_id = _optional_text(
        _get(sender, "aadObjectId", "aad_object_id"), limit=512
    )
    actor_aad_id = _canonical_uuid(raw_actor_aad_id)
    if _is_self_authored(sender, sender_id, recipient_id, actor_aad_id, bot_app_id):
        raise ActivityRejected("self_authored")
    if actor_aad_id is None:
        raise ActivityRejected("missing_stable_actor_id")

    channel_data = _mapping(_get(activity, "channelData", "channel_data"))
    team = _mapping(_get(channel_data, "team"))
    channel = _mapping(_get(channel_data, "channel"))
    tenant = _mapping(_get(channel_data, "tenant"))
    conversation = _mapping(_get(activity, "conversation"))
    conversation_type = (
        _optional_text(_get(conversation, "conversationType", "conversation_type"))
        or ""
    ).casefold()
    is_personal = conversation_type == "personal"
    if conversation_type and not is_personal and conversation_type != "channel":
        raise ActivityRejected("unsupported_conversation_scope")
    if is_personal and not personal_chat_enabled:
        raise ActivityRejected("personal_chat_disabled")

    raw_tenant = _optional_text(_get(tenant, "id"), limit=512) or _optional_text(
        _get(conversation, "tenantId", "tenant_id"), limit=512
    )
    observed_tenant = _canonical_uuid(raw_tenant)
    observed_team = _optional_text(_get(team, "id"), limit=512)
    channel_id = _optional_text(_get(channel, "id"), limit=512)
    conversation_id = _optional_text(_get(conversation, "id"), limit=512)
    message_id = _optional_text(_get(activity, "id"), limit=512)

    if observed_tenant is None or observed_tenant != tenant_id.lower():
        raise ActivityRejected("tenant_not_allowed")
    if conversation_id is None or message_id is None:
        raise ActivityRejected("missing_conversation_identity")
    if is_personal:
        if observed_team is not None or channel_id is not None:
            raise ActivityRejected("invalid_personal_scope")
    elif (
        observed_team is None
        or channel_id is None
        or observed_team not in allowed_team_ids
    ):
        raise ActivityRejected("team_or_channel_not_allowed")

    raw_service_url = _get(activity, "serviceUrl", "service_url")
    if not isinstance(raw_service_url, str) or not raw_service_url:
        raise ActivityRejected("missing_service_url")
    try:
        service_url = validate_bot_service_url(
            raw_service_url,
            tenant_id=observed_tenant,
        )
    except ServiceUrlError as exc:
        raise ActivityRejected("service_url_not_allowed") from exc

    root_thread_id: str | None = None
    if not is_personal:
        direct_parent = _optional_thread_id(_get(activity, "replyToId", "reply_to_id"))
        conversation_root = _conversation_root_thread_id(conversation_id)
        if (
            conversation_root is not None
            and direct_parent is not None
            and direct_parent != conversation_root
        ):
            raise ActivityRejected("invalid_thread_identity")
        root_thread_id = (
            conversation_root or direct_parent or _validated_thread_id(message_id)
        )

    content, omitted_attachments = _content_parts(activity, bot_app_id)
    facts = [{"name": "tenant_id", "value": observed_tenant}]
    if not is_personal:
        facts.extend(
            [
                {"name": "team_id", "value": observed_team},
                {"name": "channel_id", "value": channel_id},
            ]
        )
    if omitted_attachments:
        facts.append({"name": "attachments_omitted", "value": str(omitted_attachments)})
    if len(content) > 128:
        raise ActivityRejected("too_many_content_parts")

    payload: dict[str, Any] = {
        "message_id": message_id,
        "conversation": {
            "vendor": "msteams",
            "account_id": bot_app_id,
            "conversation_id": conversation_id,
            "kind": "private" if is_personal else "channel",
        },
        "sender_id": actor_aad_id,
        "content": content,
        "vendor_extension": {"vendor": "msteams", "facts": facts},
    }
    if root_thread_id is not None and root_thread_id != message_id:
        payload["reply"] = {"message_id": root_thread_id}

    trust = {
        "version": 1,
        "provider": "teams",
        "tenant_id": observed_tenant,
        "actor_aad_object_id": actor_aad_id,
        "message_id": message_id,
        "bot_app_id": bot_app_id,
        "service_url": service_url,
    }
    if is_personal:
        trust.update(
            {
                "conversation_kind": "private",
                "conversation_id": conversation_id,
            }
        )
    else:
        trust.update(
            {
                "team_id": observed_team,
                "channel_id": channel_id,
                "root_thread_id": root_thread_id,
            }
        )
    return NormalizedTeamsActivity(payload=payload, trust=trust)


def normalize_personal_installation(
    activity: Mapping[str, Any],
    *,
    action: str,
    bot_app_id: str,
    tenant_id: str,
    personal_chat_enabled: bool = False,
) -> NormalizedTeamsActivity:
    """Normalize an authenticated one-person bot install or removal event.

    Only private host trust metadata carries the route and actor identity. The
    Work API owns authorization, persistence, and revocation for that binding.

    中文：归一化经过鉴权的个人 bot 安装或移除事件。只有私有 host trust metadata 包含
    路由与 actor 身份；Work API 负责该 binding 的授权、持久化与撤销。
    """

    if not personal_chat_enabled:
        raise ActivityRejected("personal_chat_disabled")
    if not isinstance(activity, Mapping):
        raise ActivityRejected("invalid_activity")
    if _string(_get(activity, "type")) != "installationUpdate":
        raise ActivityRejected("unsupported_activity")
    observed_action = _string(_get(activity, "action")).casefold()
    if action not in {"add", "remove"} or observed_action != action:
        raise ActivityRejected("invalid_installation_action")

    sender = _mapping(_get(activity, "from", "from_"))
    actor_aad_id = _canonical_uuid(
        _optional_text(_get(sender, "aadObjectId", "aad_object_id"), limit=512)
    )
    if actor_aad_id is None:
        raise ActivityRejected("missing_stable_actor_id")

    channel_data = _mapping(_get(activity, "channelData", "channel_data"))
    tenant = _mapping(_get(channel_data, "tenant"))
    conversation = _mapping(_get(activity, "conversation"))
    observed_tenant = _canonical_uuid(
        _optional_text(_get(tenant, "id"), limit=512)
        or _optional_text(_get(conversation, "tenantId", "tenant_id"), limit=512)
    )
    conversation_type = (
        _optional_text(_get(conversation, "conversationType", "conversation_type"))
        or ""
    ).casefold()
    if observed_tenant != tenant_id.lower():
        raise ActivityRejected("tenant_not_allowed")
    if conversation_type != "personal":
        raise ActivityRejected("personal_installation_required")
    if (
        _mapping(_get(channel_data, "team")).get("id") is not None
        or _mapping(_get(channel_data, "channel")).get("id") is not None
    ):
        raise ActivityRejected("invalid_personal_scope")

    conversation_id = _optional_text(_get(conversation, "id"), limit=512)
    activity_id = _optional_text(_get(activity, "id"), limit=512)
    if conversation_id is None or activity_id is None:
        raise ActivityRejected("missing_installation_identity")
    raw_service_url = _get(activity, "serviceUrl", "service_url")
    if not isinstance(raw_service_url, str) or not raw_service_url:
        raise ActivityRejected("missing_service_url")
    try:
        service_url = validate_bot_service_url(
            raw_service_url,
            tenant_id=observed_tenant,
        )
    except ServiceUrlError as exc:
        raise ActivityRejected("service_url_not_allowed") from exc

    return NormalizedTeamsActivity(
        payload={"action": action},
        trust={
            "version": 1,
            "provider": "teams",
            "conversation_kind": "private",
            "installation_action": action,
            "tenant_id": observed_tenant,
            "actor_aad_object_id": actor_aad_id,
            "bot_app_id": bot_app_id,
            "conversation_id": conversation_id,
            "activity_id": activity_id,
            "service_url": service_url,
        },
        event_type="personal_installation",
    )


def _content_parts(
    activity: Mapping[str, Any], bot_app_id: str
) -> tuple[list[dict[str, Any]], int]:
    """Build bounded canonical text, mention, image, and file content parts.

    中文：生成受限的规范文本、mention、图片与文件内容片段。
    """

    parts: list[dict[str, Any]] = []
    entities = _get(activity, "entities")
    if isinstance(entities, Sequence) and not isinstance(entities, (str, bytes)):
        for entity_value in entities:
            entity = _mapping(entity_value)
            if _string(_get(entity, "type")).lower() != "mention":
                continue
            mentioned = _mapping(_get(entity, "mentioned"))
            mentioned_aad_id = _optional_text(
                _get(mentioned, "aadObjectId", "aad_object_id"), limit=512
            )
            mentioned_id = _optional_text(_get(mentioned, "id"), limit=512)
            recipient = _mapping(_get(activity, "recipient"))
            recipient_id = _optional_text(_get(recipient, "id"), limit=512)
            target_id = (
                bot_app_id
                if (mentioned_aad_id or "").casefold() == bot_app_id.casefold()
                or (mentioned_id or "").casefold() == bot_app_id.casefold()
                or (
                    recipient_id is not None
                    and mentioned_id is not None
                    and mentioned_id.casefold() == recipient_id.casefold()
                )
                else mentioned_aad_id or mentioned_id
            )
            if target_id is None:
                continue
            parts.append(
                {
                    "mention": {
                        "target": "user",
                        "target_id": target_id,
                        "display_name": _optional_text(
                            _get(mentioned, "name"), limit=512
                        )
                        or "",
                    }
                }
            )

    raw_text = _get(activity, "text")
    if isinstance(raw_text, str) and len(raw_text) > 16_384:
        raise ActivityRejected("message_too_large")
    text = _optional_text(raw_text)
    if text:
        # The mention part is canonical; remove its presentation tag from text.
        without_mentions = re.sub(
            r"<at\b[^>]*>.*?</at>",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        plain_text = html.unescape(
            re.sub(r"</?at\b[^>]*>", "", without_mentions, flags=re.IGNORECASE)
        ).strip()
        if plain_text:
            parts.append({"text": {"text": plain_text[:16_384]}})

    omitted = 0
    attachments = _get(activity, "attachments")
    if isinstance(attachments, Sequence) and not isinstance(attachments, (str, bytes)):
        for attachment_value in attachments:
            attachment = _mapping(attachment_value)
            content_type = (
                _optional_text(_get(attachment, "contentType", "content_type"))
                or "application/octet-stream"
            )
            if len(content_type) > 255 or not _MEDIA_TYPE.fullmatch(content_type):
                omitted += 1
                continue
            attachment_content = _mapping(_get(attachment, "content"))
            media_id = (
                _optional_text(_get(attachment_content, "uniqueId", "unique_id"))
                or _optional_text(_get(attachment_content, "id"))
                or _optional_text(_get(attachment, "id"))
            )
            if not _safe_media_id(media_id):
                # A signed contentUrl can contain a credential, so it is never copied.
                omitted += 1
                continue
            reference = {
                "vendor_media": {
                    "vendor": "msteams",
                    "account_id": bot_app_id,
                    "media_id": media_id,
                }
            }
            if content_type.lower().startswith("image/"):
                parts.append(
                    {"image": {"reference": reference, "mime_type": content_type}}
                )
            else:
                file_name = (
                    _optional_text(_get(attachment, "name"))
                    or _optional_text(_get(attachment_content, "name"))
                    or "attachment"
                )
                parts.append(
                    {
                        "file": {
                            "reference": reference,
                            "file_name": file_name[:512],
                            "mime_type": content_type,
                        }
                    }
                )
    return parts, omitted


def _safe_media_id(value: str | None) -> bool:
    """Accept only bounded opaque media IDs, never URL-like values.

    中文：只接受有界不透明 media ID，拒绝 URL 形式的值。
    """

    return (
        value is not None
        and len(value) <= 512
        and not re.search(r"(?i)https?://", value)
        and "?" not in value
        and "#" not in value
        and not any(ord(character) < 32 for character in value)
    )


def _conversation_root_thread_id(conversation_id: str) -> str | None:
    """Extract a Teams channel thread root from its conversation identifier.

    Teams channel conversation IDs carry the top-level message as a numeric
    ``;messageid=`` segment. Keep the full conversation ID for replies while
    using that segment as the canonical thread root.

    中文：从 Teams 频道 conversation ID 中提取顶层帖子 ID。回复仍保留完整
    conversation ID，规范线程根使用 ``;messageid=`` 对应的数字 ID。
    """

    roots: list[str] = []
    for segment in conversation_id.split(";")[1:]:
        key, separator, value = segment.partition("=")
        if key.casefold() == "messageid":
            if not separator:
                raise ActivityRejected("invalid_thread_identity")
            roots.append(_validated_thread_id(value))
        elif segment.casefold().startswith("messageid"):
            raise ActivityRejected("invalid_thread_identity")
    if len(roots) > 1:
        raise ActivityRejected("invalid_thread_identity")
    return roots[0] if roots else None


def _validated_thread_id(value: Any) -> str:
    """Require an ASCII numeric, non-zero Teams root ID within contract bounds.

    中文：要求 Teams 线程根 ID 为有界、非零 ASCII 数字。
    """

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or re.fullmatch(r"[0-9]{1,512}", value) is None
        or not any(character != "0" for character in value)
    ):
        raise ActivityRejected("invalid_thread_identity")
    return value


def _optional_thread_id(value: Any) -> str | None:
    """Validate a direct reply parent when the activity provides one.

    中文：验证 activity 提供的直接回复父消息 ID。
    """

    return None if value is None else _validated_thread_id(value)


def _is_self_authored(
    sender: Mapping[str, Any],
    sender_id: str,
    recipient_id: str | None,
    actor_aad_id: str | None,
    bot_app_id: str,
) -> bool:
    """Drop the current bot's own activities before any host dispatch.

    中文：在任何主机分发前丢弃当前 bot 自己发出的 activity。
    """

    sender_type = (_optional_text(_get(sender, "type")) or "").lower()
    sender_role = (_optional_text(_get(sender, "role")) or "").lower()
    return (
        sender_type == "bot"
        or sender_role == "bot"
        or sender_id.casefold() == bot_app_id.casefold()
        or (
            recipient_id is not None and sender_id.casefold() == recipient_id.casefold()
        )
        or (
            actor_aad_id is not None
            and actor_aad_id.casefold() == bot_app_id.casefold()
        )
    )


def _canonical_uuid(value: str | None) -> str | None:
    """Return a normalized UUID or no stable identity.

    中文：返回规范 UUID；无效值不作为稳定身份。
    """

    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return a mapping value or an empty immutable mapping.

    中文：返回 mapping；其他类型转换为空 mapping。
    """

    return value if isinstance(value, Mapping) else {}


def _get(value: Mapping[str, Any], *names: str) -> Any:
    """Read the first present snake_case or camelCase field.

    中文：按候选 snake_case 或 camelCase 字段读取首个存在值。
    """

    for name in names:
        if name in value:
            return value[name]
    return None


def _string(value: Any) -> str:
    """Return a string value or the empty string.

    中文：返回字符串，否则返回空字符串。
    """

    return value if isinstance(value, str) else ""


def _optional_text(value: Any, *, limit: int = 16_384) -> str | None:
    """Return a trimmed, bounded optional text field.

    中文：返回经过 trim 的可选文本字段。
    """

    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    return value.strip()


def _bounded_text(value: Any, field: str) -> str:
    """Require a non-empty identifier within the contract size limit.

    中文：要求标识非空且不超过契约长度限制。
    """

    text = _optional_text(value)
    if text is None or len(text) > 512:
        raise ActivityRejected(f"invalid_{field}")
    return text
