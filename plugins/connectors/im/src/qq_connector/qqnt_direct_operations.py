"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 qqnt_direct_operations.py                                       │
│  Module: qq_connector.qqnt_direct_operations                         │
│  Role: Fixed QQ API operation allow-list and mapping authority.      │
│                                                                     │
│  模块职责：将公开扩展操作绑定到已登记的 QQ Service 方法。              │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


class QQOperationValidationError(ValueError):
    """Raised when a fixed QQ operation payload violates its public schema.

        中文:固定 QQ operation 的负载违反其公共 schema 时抛出的错误。
    """


@dataclass(frozen=True, slots=True)
class QQOperation:
    """One explicit, non-passthrough operation exposed by the connector.

        中文:connector 暴露的一项明确、不可透传的 operation。
    """

    name: str
    service: str
    method: str
    priority: str
    mapping: str
    requestable: bool = True


def _op(
    name: str,
    service: str,
    method: str,
    priority: str,
    mapping: str,
    *,
    requestable: bool = True,
) -> QQOperation:
    return QQOperation(name, service, method, priority, mapping, requestable)


# This table is the executable counterpart of QQNT_DIRECT_API_MATRIX.md. The
# host receives the stable operation name and never receives an arbitrary
# service/method pair from a Product request.
# 中文:此表是 `QQNT_DIRECT_API_MATRIX.md` 的可执行对应物。Host 接收稳定的 operation 名称,绝不会从 Product 请求中接收任意 service／method 名称对。
QQ_OPERATIONS = (
    _op("qq.session.create", "NodeIQQNTWrapperSession", "create", "P0", "session"),
    _op("qq.session.init", "NodeIQQNTWrapperSession", "init", "P0", "session"),
    _op("qq.session.start_nt", "NodeIQQNTWrapperSession", "startNT", "P0", "session"),
    _op("qq.login.connect", "NodeIKernelLoginService", "connect", "P0", "login"),
    _op("qq.login.online", "NodeIKernelLoginService", "online", "P0", "login"),
    _op("qq.login.offline", "NodeIKernelLoginService", "offline", "P0", "login"),
    _op("qq.login.list", "NodeIKernelLoginService", "getLoginList", "P0", "login"),
    _op(
        "qq.login.quick", "NodeIKernelLoginService", "quickLoginWithUin", "P0", "login"
    ),
    _op("qq.login.password", "NodeIKernelLoginService", "passwordLogin", "P0", "login"),
    _op("qq.login.qr", "NodeIKernelLoginService", "getQRCodePicture", "P0", "login"),
    _op("qq.login.poll", "NodeIKernelLoginService", "startPolling", "P0", "login"),
    _op(
        "qq.login.self_status",
        "NodeIKernelProfileService",
        "getSelfStatus",
        "P0",
        "login",
    ),
    _op(
        "qq.account.core",
        "NodeIKernelProfileService",
        "getCoreAndBaseInfo",
        "P0",
        "account",
    ),
    _op(
        "qq.account.simple",
        "NodeIKernelProfileService",
        "getUserSimpleInfo",
        "P0",
        "account",
    ),
    _op(
        "qq.message.subscribe",
        "NodeIKernelMsgService",
        "addKernelMsgListener",
        "P0",
        "message",
    ),
    _op("qq.message.send", "NodeIKernelMsgService", "sendMsg", "P0", "send_message"),
    _op(
        "qq.message.send_completion",
        "NodeIKernelMsgListener",
        "onMsgInfoListUpdate",
        "P0",
        "send_message",
        requestable=False,
    ),
    _op("qq.peer.uid_by_uin", "NodeIKernelProfileService", "getUidByUin", "P0", "peer"),
    _op("qq.peer.uin_by_uid", "NodeIKernelProfileService", "getUinByUid", "P0", "peer"),
    _op("qq.peer.uid", "NodeIKernelUixConvertService", "getUid", "P0", "peer"),
    _op("qq.peer.uin", "NodeIKernelUixConvertService", "getUin", "P0", "peer"),
    _op(
        "qq.message.history_include_self",
        "NodeIKernelMsgService",
        "getMsgsIncludeSelf",
        "P1",
        "lookup",
    ),
    _op(
        "qq.message.history_by_seq",
        "NodeIKernelMsgService",
        "getMsgsBySeqAndCount",
        "P1",
        "lookup",
    ),
    _op("qq.message.by_id", "NodeIKernelMsgService", "getMsgsByMsgId", "P1", "lookup"),
    _op("qq.message.single", "NodeIKernelMsgService", "getSingleMsg", "P1", "lookup"),
    _op(
        "qq.message.search",
        "NodeIKernelMsgService",
        "queryMsgsWithFilterEx",
        "P1",
        "lookup",
    ),
    _op("qq.message.recall", "NodeIKernelMsgService", "recallMsg", "P1", "message"),
    _op("qq.message.forward", "NodeIKernelMsgService", "forwardMsg", "P1", "message"),
    _op(
        "qq.message.forward_comment",
        "NodeIKernelMsgService",
        "forwardMsgWithComment",
        "P1",
        "message",
    ),
    _op(
        "qq.message.multi_forward",
        "NodeIKernelMsgService",
        "multiForwardMsg",
        "P1",
        "message",
    ),
    _op("qq.message.read", "NodeIKernelMsgService", "setMsgRead", "P1", "read"),
    _op(
        "qq.message.read_all",
        "NodeIKernelMsgService",
        "setAllC2CAndGroupMsgRead",
        "P1",
        "read",
    ),
    _op(
        "qq.message.emoji_likes",
        "NodeIKernelMsgService",
        "setMsgEmojiLikes",
        "P1",
        "emoji",
    ),
    _op(
        "qq.message.emoji_likes_list",
        "NodeIKernelMsgService",
        "getMsgEmojiLikesList",
        "P1",
        "emoji",
    ),
    _op("qq.group.list", "NodeIKernelGroupService", "getGroupList", "P1", "group"),
    _op(
        "qq.group.detail",
        "NodeIKernelGroupService",
        "getGroupDetailInfo",
        "P1",
        "group",
    ),
    _op(
        "qq.group.members", "NodeIKernelGroupService", "getAllMemberList", "P1", "group"
    ),
    _op("qq.group.member", "NodeIKernelGroupService", "getMemberInfo", "P1", "group"),
    _op("qq.friend.list", "NodeIKernelBuddyService", "getBuddyListV2", "P1", "friend"),
    _op(
        "qq.friend.cached",
        "NodeIKernelBuddyService",
        "getBuddyListFromCache",
        "P1",
        "friend",
    ),
    _op("qq.friend.requests", "NodeIKernelBuddyService", "getBuddyReq", "P1", "friend"),
    _op(
        "qq.media.element",
        "NodeIKernelMsgService",
        "getRichMediaElement",
        "P1",
        "media",
    ),
    _op(
        "qq.media.download", "NodeIKernelMsgService", "downloadRichMedia", "P1", "media"
    ),
    _op(
        "qq.media.video_url",
        "NodeIKernelRichMediaService",
        "getVideoPlayUrlV2",
        "P1",
        "media",
    ),
    _op(
        "qq.media.download_complete",
        "NodeIKernelRichMediaListener",
        "onRichMediaDownloadComplete",
        "P1",
        "media",
        requestable=False,
    ),
    _op(
        "qq.file.list", "NodeIKernelRichMediaService", "getGroupFileList", "P1", "file"
    ),
    _op(
        "qq.file.search", "NodeIKernelFileAssistantService", "searchFile", "P1", "file"
    ),
    _op(
        "qq.file.download",
        "NodeIKernelFileAssistantService",
        "downloadFile",
        "P1",
        "file",
    ),
    _op(
        "qq.file.forward",
        "NodeIKernelFileAssistantService",
        "forwardFile",
        "P1",
        "file",
    ),
    _op("qq.file.save", "NodeIKernelFileAssistantService", "saveAs", "P1", "file"),
    _op(
        "qq.group.modify_name",
        "NodeIKernelGroupService",
        "modifyGroupName",
        "P2",
        "group_mutation",
    ),
    _op(
        "qq.group.modify_remark",
        "NodeIKernelGroupService",
        "modifyGroupRemark",
        "P2",
        "group_mutation",
    ),
    _op(
        "qq.group.mute_member",
        "NodeIKernelGroupService",
        "setMemberShutUp",
        "P2",
        "group_mutation",
    ),
    _op(
        "qq.group.mute",
        "NodeIKernelGroupService",
        "setGroupShutUp",
        "P2",
        "group_mutation",
    ),
    _op(
        "qq.group.kick", "NodeIKernelGroupService", "kickMember", "P2", "group_mutation"
    ),
    _op(
        "qq.group.quit", "NodeIKernelGroupService", "quitGroup", "P2", "group_mutation"
    ),
    _op(
        "qq.group.approve",
        "NodeIKernelGroupService",
        "operateSysNotify",
        "P2",
        "group_mutation",
    ),
    _op(
        "qq.friend.approve",
        "NodeIKernelBuddyService",
        "approvalFriendRequest",
        "P2",
        "friend_mutation",
    ),
    _op(
        "qq.friend.approve_doubt",
        "NodeIKernelBuddyService",
        "approvalDoubtBuddyReq",
        "P2",
        "friend_mutation",
    ),
    _op(
        "qq.friend.doubt_requests",
        "NodeIKernelBuddyService",
        "getDoubtBuddyReq",
        "P2",
        "friend_mutation",
    ),
    _op(
        "qq.friend.add",
        "NodeIKernelBuddyService",
        "reqToAddFriends",
        "P2",
        "friend_mutation",
    ),
    _op(
        "qq.friend.delete",
        "NodeIKernelBuddyService",
        "delBuddy",
        "P2",
        "friend_mutation",
    ),
    _op(
        "qq.friend.set_remark",
        "NodeIKernelBuddyService",
        "setBuddyRemark",
        "P2",
        "friend_mutation",
    ),
    _op(
        "qq.profile.modify",
        "NodeIKernelProfileService",
        "modifySelfProfile",
        "P2",
        "profile",
    ),
    _op(
        "qq.profile.nickname",
        "NodeIKernelProfileService",
        "setNickName",
        "P2",
        "profile",
    ),
    _op(
        "qq.profile.long_nick",
        "NodeIKernelProfileService",
        "setLongNick",
        "P2",
        "profile",
    ),
    _op(
        "qq.profile.birthday",
        "NodeIKernelProfileService",
        "setBirthday",
        "P2",
        "profile",
    ),
    _op("qq.profile.gender", "NodeIKernelProfileService", "setGander", "P2", "profile"),
    _op("qq.profile.header", "NodeIKernelProfileService", "setHeader", "P2", "profile"),
    _op(
        "qq.search.stranger",
        "NodeIKernelSearchService",
        "searchStranger",
        "P2",
        "search",
    ),
    _op("qq.search.group", "NodeIKernelSearchService", "searchGroup", "P2", "search"),
    _op(
        "qq.search.contact", "NodeIKernelSearchService", "searchContact", "P2", "search"
    ),
    _op(
        "qq.search.message",
        "NodeIKernelSearchService",
        "searchMsgWithKeywords",
        "P2",
        "search",
    ),
    _op(
        "qq.search.file",
        "NodeIKernelSearchService",
        "searchFileWithKeywords",
        "P2",
        "search",
    ),
    _op(
        "qq.online.status",
        "NodeIKernelOnlineStatusService",
        "setStatus",
        "P2",
        "online",
    ),
    _op(
        "qq.online.devices",
        "NodeIKernelOnlineStatusService",
        "getOnLineDev",
        "P2",
        "online",
    ),
    _op("qq.online.likes", "NodeIKernelMsgService", "getLikeList", "P2", "online"),
    _op("qq.online.set_like", "NodeIKernelMsgService", "setLikeStatus", "P2", "online"),
    _op(
        "qq.online.check_like",
        "NodeIKernelMsgService",
        "checkLikeStatus",
        "P2",
        "online",
    ),
)

QQ_OPERATION_BY_NAME = {operation.name: operation for operation in QQ_OPERATIONS}
QQ_OPERATION_NAMES = tuple(operation.name for operation in QQ_OPERATIONS)
CALLBACK_ONLY_OPERATION_NAMES = frozenset(
    operation.name for operation in QQ_OPERATIONS if not operation.requestable
)

# These are the only top-level parameter names that may cross the worker/Host
# boundary.  The native Host still owns exact overload validation for the
# configured QQ build, but the worker rejects fields from unrelated operation
# families before IPC.  This keeps the operation allow-list meaningful without
# inventing a generic service/method or raw-payload escape hatch.
# 中文:只有下列顶层参数名称可以穿过 Worker／Host 边界。原生 Host 仍负责校验当前 QQ build 对应的精确重载;但 Worker 会在 IPC 之前拒绝来自无关 operation 家族的字段。这样可以确保 operation allow-list 有效,同时不引入通用 service／method 或原始负载逃逸口。
_PARAMETER_FIELDS_BY_MAPPING = {
    "session": frozenset(
        {
            "account_id",
            "platform",
            "client_version",
            "data_dir",
            "login_policy",
            "session_id",
        }
    ),
    "login": frozenset(
        {
            "account_id",
            "uin",
            "uid",
            "secret_ref",
            "qr_code",
            "poll_interval_seconds",
            "login_id",
        }
    ),
    "account": frozenset({"account_id", "uid", "uin", "user_uid", "user_uin"}),
    "message": frozenset(
        {
            "account_id",
            "peer",
            "source",
            "destination",
            "message_id",
            "message_ids",
            "sequence",
            "random",
            "timestamp",
            "comment",
            "messages",
            "events",
            "filter",
            "request_id",
        }
    ),
    "send_message": frozenset(
        {
            "account_id",
            "peer",
            "elements",
            "attributes",
            "reply",
            "message",
            "message_id",
            "request_id",
            "peer_uid",
            "sequence",
            "random",
            "timestamp",
        }
    ),
    "peer": frozenset(
        {
            "account_id",
            "uid",
            "uin",
            "user_uid",
            "user_uin",
            "peer_uid",
        }
    ),
    "lookup": frozenset(
        {
            "account_id",
            "peer",
            "message_id",
            "message_ids",
            "sequence",
            "random",
            "offset",
            "count",
            "page",
            "page_size",
            "filter",
            "query",
            "start_time",
            "end_time",
        }
    ),
    "read": frozenset({"account_id", "peer", "message_id", "message_ids", "sequence"}),
    "emoji": frozenset({"account_id", "peer", "message_id", "like_id", "like_type"}),
    "group": frozenset(
        {
            "account_id",
            "group_id",
            "group_code",
            "member_uid",
            "member_uin",
            "offset",
            "count",
            "page",
            "page_size",
        }
    ),
    "friend": frozenset(
        {"account_id", "uid", "uin", "offset", "count", "page", "page_size"}
    ),
    "media": frozenset(
        {
            "account_id",
            "peer",
            "message_id",
            "element_id",
            "media_id",
            "file_id",
            "media_type",
            "codec",
            "download",
            "model_id",
            "file_uuid",
            "local_result_reference",
        }
    ),
    "file": frozenset(
        {
            "account_id",
            "group_id",
            "folder_id",
            "file_id",
            "file_uuid",
            "file_name",
            "query",
            "source",
            "destination",
            "offset",
            "count",
            "page",
            "page_size",
            "local_result_reference",
        }
    ),
    "group_mutation": frozenset(
        {
            "account_id",
            "group_id",
            "group_code",
            "member_uid",
            "member_uin",
            "user_id",
            "request_id",
            "name",
            "remark",
            "duration_seconds",
            "duration",
            "approve",
            "comment",
            "sub_type",
            "notify_id",
            "vendor_request",
        }
    ),
    "friend_mutation": frozenset(
        {
            "account_id",
            "request_id",
            "uid",
            "uin",
            "user_id",
            "approve",
            "comment",
            "remark",
            "vendor_request",
        }
    ),
    "profile": frozenset(
        {
            "account_id",
            "profile",
            "nickname",
            "long_nick",
            "birthday",
            "gender",
            "header",
        }
    ),
    "search": frozenset(
        {
            "account_id",
            "query",
            "keywords",
            "scope",
            "offset",
            "count",
            "page",
            "page_size",
            "filter",
        }
    ),
    "online": frozenset(
        {
            "account_id",
            "status",
            "device_id",
            "like_id",
            "like_type",
            "target_id",
        }
    ),
}


def _fields(*names: str) -> frozenset[str]:
    """Build one immutable operation-specific parameter vocabulary.

        中文:构造一组不可变、专属于单个 operation 的参数词汇。
    """

    return frozenset(names)


# The matrix describes families, but a public operation must not inherit every
# field from its family.  This table is intentionally conservative: it only
# exposes names declared by QQ_API_PLAN.md/QQ_SIDE_INTERFACES.md and leaves
# exact version-specific overload validation to the authorized Host.
# 中文:matrix 描述的是参数类别,但公共 operation 不能继承其所属类别的全部字段。此表有意保持保守:只公开 `QQ_API_PLAN.md`／`QQ_SIDE_INTERFACES.md` 中声明的名称;版本相关的精确重载校验仍由已授权 Host 负责。
_OPERATION_PARAMETER_FIELDS = {
    "qq.session.create": _fields(
        "account_id",
        "platform",
        "client_version",
        "data_dir",
        "login_policy",
        "session_id",
    ),
    "qq.session.init": _fields(
        "account_id",
        "platform",
        "client_version",
        "data_dir",
        "login_policy",
        "session_id",
    ),
    "qq.session.start_nt": _fields("account_id", "login_policy", "session_id"),
    "qq.login.connect": _fields("account_id", "uin", "uid", "login_id"),
    "qq.login.online": _fields("account_id", "login_id"),
    "qq.login.offline": _fields("account_id", "login_id"),
    "qq.login.list": _fields("account_id"),
    "qq.login.quick": _fields("account_id", "uin", "login_id"),
    "qq.login.password": _fields("account_id", "uin", "secret_ref"),
    "qq.login.qr": _fields("account_id", "login_id"),
    "qq.login.poll": _fields(
        "account_id", "login_id", "qr_code", "poll_interval_seconds"
    ),
    "qq.login.self_status": _fields("account_id"),
    "qq.account.core": _fields("account_id", "uid", "uin", "user_uid", "user_uin"),
    "qq.account.simple": _fields("account_id", "uid", "uin", "user_uid", "user_uin"),
    "qq.message.subscribe": _fields("account_id", "events", "filter"),
    "qq.message.send": _fields(
        "account_id", "peer", "elements", "attributes", "reply", "message"
    ),
    "qq.message.send_completion": frozenset(),
    "qq.peer.uid_by_uin": _fields("account_id", "uin", "user_uin"),
    "qq.peer.uin_by_uid": _fields("account_id", "uid", "user_uid"),
    "qq.peer.uid": _fields("account_id", "uin", "user_uin"),
    "qq.peer.uin": _fields("account_id", "uid", "user_uid"),
    "qq.message.history_include_self": _fields(
        "account_id", "peer", "offset", "count", "page", "page_size"
    ),
    "qq.message.history_by_seq": _fields(
        "account_id", "peer", "sequence", "count", "offset"
    ),
    "qq.message.by_id": _fields("account_id", "peer", "message_id"),
    "qq.message.single": _fields("account_id", "peer", "message_id"),
    "qq.message.search": _fields(
        "account_id", "peer", "filter", "query", "offset", "count", "page", "page_size",
        "start_time", "end_time"
    ),
    "qq.message.recall": _fields(
        "account_id", "peer", "message_id", "sequence", "random"
    ),
    "qq.message.forward": _fields("account_id", "source", "destination", "message_id"),
    "qq.message.forward_comment": _fields(
        "account_id", "source", "destination", "message_id", "comment"
    ),
    "qq.message.multi_forward": _fields(
        "account_id", "source", "destination", "messages", "message_ids"
    ),
    "qq.message.read": _fields(
        "account_id", "peer", "message_id", "message_ids", "sequence"
    ),
    "qq.message.read_all": _fields("account_id"),
    "qq.message.emoji_likes": _fields(
        "account_id", "peer", "message_id", "like_id", "like_type"
    ),
    "qq.message.emoji_likes_list": _fields(
        "account_id", "peer", "message_id", "like_id", "like_type"
    ),
    "qq.group.list": _fields("account_id", "offset", "count", "page", "page_size"),
    "qq.group.detail": _fields("account_id", "group_id", "group_code"),
    "qq.group.members": _fields(
        "account_id", "group_id", "group_code", "offset", "count", "page", "page_size"
    ),
    "qq.group.member": _fields(
        "account_id", "group_id", "group_code", "member_uid", "member_uin"
    ),
    "qq.friend.list": _fields("account_id", "offset", "count", "page", "page_size"),
    "qq.friend.cached": _fields("account_id", "offset", "count", "page", "page_size"),
    "qq.friend.requests": _fields("account_id", "offset", "count", "page", "page_size"),
    "qq.media.element": _fields(
        "account_id", "peer", "message_id", "element_id", "media_id", "media_type"
    ),
    "qq.media.download": _fields(
        "account_id", "peer", "message_id", "element_id", "media_id", "media_type",
        "download", "model_id", "file_uuid", "local_result_reference"
    ),
    "qq.media.video_url": _fields(
        "account_id",
        "peer",
        "message_id",
        "element_id",
        "media_id",
        "codec",
        "download",
    ),
    "qq.media.download_complete": frozenset(),
    "qq.file.list": _fields(
        "account_id", "group_id", "folder_id", "offset", "count", "page", "page_size"
    ),
    "qq.file.search": _fields(
        "account_id", "group_id", "folder_id", "query", "file_name", "offset", "count",
        "page", "page_size"
    ),
    "qq.file.download": _fields(
        "account_id",
        "group_id",
        "file_id",
        "file_uuid",
        "file_name",
        "local_result_reference",
    ),
    "qq.file.forward": _fields(
        "account_id", "group_id", "file_id", "file_uuid", "source", "destination"
    ),
    "qq.file.save": _fields(
        "account_id", "group_id", "file_id", "file_uuid", "file_name", "folder_id"
    ),
    "qq.group.modify_name": _fields("account_id", "group_id", "group_code", "name"),
    "qq.group.modify_remark": _fields("account_id", "group_id", "group_code", "remark"),
    "qq.group.mute_member": _fields(
        "account_id",
        "group_id",
        "group_code",
        "member_uid",
        "member_uin",
        "duration_seconds",
        "duration",
    ),
    "qq.group.mute": _fields(
        "account_id", "group_id", "group_code", "duration_seconds", "duration"
    ),
    "qq.group.kick": _fields(
        "account_id",
        "group_id",
        "group_code",
        "member_uid",
        "member_uin",
        "user_id",
        "comment",
    ),
    "qq.group.quit": _fields("account_id", "group_id", "group_code"),
    "qq.group.approve": _fields(
        "account_id",
        "request_id",
        "group_id",
        "group_code",
        "user_id",
        "approve",
        "comment",
        "sub_type", "notify_id", "vendor_request"
    ),
    "qq.friend.approve": _fields(
        "account_id",
        "request_id",
        "uid",
        "uin",
        "user_id",
        "approve",
        "comment",
        "vendor_request",
    ),
    "qq.friend.approve_doubt": _fields(
        "account_id",
        "request_id",
        "uid",
        "uin",
        "user_id",
        "approve",
        "comment",
        "vendor_request",
    ),
    "qq.friend.doubt_requests": _fields(
        "account_id", "offset", "count", "page", "page_size"
    ),
    "qq.friend.add": _fields("account_id", "uid", "uin", "user_id", "comment"),
    "qq.friend.delete": _fields("account_id", "uid", "uin", "user_id"),
    "qq.friend.set_remark": _fields("account_id", "uid", "uin", "user_id", "remark"),
    "qq.profile.modify": _fields("account_id", "profile"),
    "qq.profile.nickname": _fields("account_id", "nickname"),
    "qq.profile.long_nick": _fields("account_id", "long_nick"),
    "qq.profile.birthday": _fields("account_id", "birthday"),
    "qq.profile.gender": _fields("account_id", "gender"),
    "qq.profile.header": _fields("account_id", "header"),
    "qq.search.stranger": _fields(
        "account_id",
        "query",
        "keywords",
        "scope",
        "offset",
        "count",
        "page",
        "page_size",
        "filter",
    ),
    "qq.search.group": _fields(
        "account_id",
        "query",
        "keywords",
        "scope",
        "offset",
        "count",
        "page",
        "page_size",
        "filter",
    ),
    "qq.search.contact": _fields(
        "account_id",
        "query",
        "keywords",
        "scope",
        "offset",
        "count",
        "page",
        "page_size",
        "filter",
    ),
    "qq.search.message": _fields(
        "account_id",
        "query",
        "keywords",
        "scope",
        "offset",
        "count",
        "page",
        "page_size",
        "filter",
    ),
    "qq.search.file": _fields(
        "account_id",
        "query",
        "keywords",
        "scope",
        "offset",
        "count",
        "page",
        "page_size",
        "filter",
    ),
    "qq.online.status": _fields("account_id", "status"),
    "qq.online.devices": _fields("account_id", "device_id"),
    "qq.online.likes": _fields("account_id", "target_id", "like_id", "like_type"),
    "qq.online.set_like": _fields("account_id", "target_id", "like_id", "like_type"),
    "qq.online.check_like": _fields("account_id", "target_id", "like_id", "like_type"),
}


def allowed_qq_parameter_fields(operation: str) -> frozenset[str]:
    """Return the declared top-level parameter fields for one operation.

    The exact native overload remains a version-specific Host concern.  This
    worker-level envelope nevertheless has a closed vocabulary, so callers
    cannot smuggle arbitrary native requests through an allow-listed action.

        中文:返回某个 operation 声明的顶层参数字段。原生重载的精确规则仍由特定版本的 Host 负责。尽管如此,Worker 层封套仍采用封闭词汇表,调用方不能借助已允许的 action 偷带任意原生请求。
    """

    spec = get_qq_operation(operation)
    if spec is None:
        return frozenset()
    return _OPERATION_PARAMETER_FIELDS.get(
        operation, _PARAMETER_FIELDS_BY_MAPPING.get(spec.mapping, frozenset())
    )


def get_qq_operation(name: str) -> QQOperation | None:
    """Return a fixed operation definition, never an arbitrary native call.

        中文:返回一个固定 operation 定义,绝不接受任意原生调用。
    """

    return QQ_OPERATION_BY_NAME.get(name)


_IDENTIFIER_PARAMETER_FIELDS = frozenset(
    {
        "account_id",
        "uin",
        "uid",
        "user_id",
        "user_uid",
        "user_uin",
        "peer_uid",
        "conversation_id",
        "group_id",
        "group_code",
        "message_id",
        "request_id",
        "device_id",
        "like_id",
        "target_id",
        "member_uid",
        "member_uin",
        "file_id",
        "media_id",
        "folder_id",
        "file_uuid",
        "model_id",
        "element_id",
        "notify_id",
        "session_id",
        "login_id",
        "source_id",
    }
)
_NON_NEGATIVE_INTEGER_PARAMETER_FIELDS = frozenset(
    {
        "sequence",
        "random",
        "timestamp",
        "count",
        "offset",
        "page",
        "page_size",
        "start_time",
        "end_time",
    }
)
_NON_NEGATIVE_NUMBER_PARAMETER_FIELDS = frozenset({"duration_seconds", "duration"})
_POSITIVE_NUMBER_PARAMETER_FIELDS = frozenset({"poll_interval_seconds"})
_BOOLEAN_PARAMETER_FIELDS = frozenset({"approve", "download", "short_link"})
_STRING_PARAMETER_FIELDS = frozenset(
    {
        "comment",
        "request_kind",
        "sub_type",
        "scope",
        "query",
        "keywords",
        "name",
        "remark",
        "nickname",
        "long_nick",
        "birthday",
        "gender",
        "header",
        "status",
        "like_type",
        "file_name",
        "mime_type",
        "media_type",
        "codec",
        "local_result_reference",
        "secret_ref",
        "login_policy",
        "platform",
        "data_dir",
        "client_version",
        "qr_code",
        "folder_name",
        "role",
    }
)
_STRING_LIST_PARAMETER_FIELDS = frozenset({"events"})
_IDENTIFIER_LIST_PARAMETER_FIELDS = frozenset({"message_ids"})
_OBJECT_LIST_PARAMETER_FIELDS = frozenset({"elements", "messages"})
_OBJECT_PARAMETER_FIELDS = frozenset(
    {
        "peer",
        "source",
        "destination",
        "attributes",
        "reply",
        "message",
        "filter",
        "profile",
        "vendor_request",
        "permissions",
    }
)
_RESERVED_PARAMETER_FIELDS = frozenset(
    {"service", "method", "raw_payload", "binding_id", "generation"}
)
_MAX_PARAMETER_DEPTH = 8
_MAX_PARAMETER_ITEMS = 4_096
_MAX_PARAMETER_STRING_BYTES = 64 * 1024
_MAX_RESULT_REFERENCE_BYTES = 4 * 1024
_RESULT_SENSITIVE_KEY_MARKERS = (
    "password",
    "token",
    "secret",
    "ticket",
    "cookie",
)
_RESULT_REFERENCE_SCHEMES = ("qq://", "staging://")


def validate_qq_parameters(
    operation: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate and copy one fixed operation's bounded JSON parameter object.

    The public JSON Schema is checked in CI, but the runtime cannot assume a
    schema validator is installed in the production package.  This small
    standard-library validator therefore enforces the same primitive kinds,
    finite numeric values, collection bounds, and closed top-level vocabulary
    before a request reaches the native Host.

        中文:校验并复制一个固定 operation 使用的有界 JSON 参数对象。公共 JSON Schema 会在 CI 中校验,但生产 package 不能假设安装了 schema validator。因此,这个精简的标准库 validator 会在请求到达原生 Host 前,执行相同的基本类型、有限数值、集合边界和封闭顶层词汇校验。
    """

    spec = get_qq_operation(operation)
    if spec is None:
        raise QQOperationValidationError(f"unsupported QQ operation {operation}")
    if not spec.requestable:
        raise QQOperationValidationError(f"QQ operation {operation} is callback-only")
    if not isinstance(params, Mapping):
        raise QQOperationValidationError("QQ operation params must be an object")
    if len(params) > 128:
        raise QQOperationValidationError("QQ operation params have too many fields")
    if any(not isinstance(field, str) for field in params):
        raise QQOperationValidationError("QQ operation parameter names must be text")
    if _RESERVED_PARAMETER_FIELDS.intersection(params):
        raise QQOperationValidationError(
            "QQ operation params contain reserved fields"
        )
    allowed = allowed_qq_parameter_fields(operation)
    unknown = set(params).difference(allowed)
    if unknown:
        raise QQOperationValidationError(
            f"QQ operation params contain undeclared fields: {sorted(unknown)}"
        )
    for field, value in params.items():
        _validate_parameter_value(field, value)
    return dict(params)


def validate_qq_result(operation: str, result: Any) -> dict[str, Any]:
    """Validate one bounded, normalized result returned by the QQ Host.

    The native Host is version-specific and therefore owns overload handling,
    but its result still crosses a public worker boundary.  This validator
    keeps that boundary typed enough to reject malformed or credential-bearing
    data without guessing every field exposed by a future QQ build.

    Args:
        operation: Fixed QQ operation that produced the result.
        result: JSON-compatible Host result object.
    Returns:
        A shallow copy of the validated result object.
    Raises:
        QQOperationValidationError: If the result is not a safe normalized
            object for the fixed operation.

        中文:校验 QQ Host 返回的一个有界、规范化结果。原生 Host 按版本区分,因此由它负责重载处理;但结果仍要穿过公共 Worker 边界。此 validator 会拒绝格式错误或包含凭据的数据,并保持足够的类型约束,而不会猜测未来 QQ build 可能公开的所有字段。

参数:`operation` 是产生此结果的固定 QQ operation;`result` 是与 JSON 兼容的 Host 结果对象。返回经过校验结果对象的浅拷贝。若结果不是适用于该固定 operation 的安全规范化对象,则抛出 `QQOperationValidationError`。
    """

    spec = get_qq_operation(operation)
    if spec is None:
        raise QQOperationValidationError(f"unsupported QQ operation {operation}")
    if not spec.requestable:
        raise QQOperationValidationError(f"QQ operation {operation} is callback-only")
    if not isinstance(result, Mapping):
        raise QQOperationValidationError(
            f"QQ operation result for {operation} must be an object"
        )
    _validate_json_value(
        result, "result", depth=0, label="result", allow_null=True
    )
    _reject_sensitive_result_fields(result, "result")
    _validate_operation_result_shape(operation, result)
    if spec.mapping in {"media", "file"}:
        _validate_result_references(result)
    return dict(result)


def _validate_operation_result_shape(
    operation: str, result: Mapping[str, Any]
) -> None:
    """Require identity that the direct send contract cannot safely invent.

        中文:要求提供 direct send contract 无法安全自行生成的身份信息。
    """

    reported_operation = result.get("operation")
    if reported_operation is not None and reported_operation != operation:
        raise QQOperationValidationError(
            f"QQ operation result operation does not match {operation}"
        )
    if operation == "qq.message.send" and _missing_identifier(result.get("message_id")):
        raise QQOperationValidationError(
            "QQ operation result for qq.message.send must contain message_id"
        )


def _missing_identifier(value: Any) -> bool:
    """Return whether a result identifier is absent or invalid.

        中文:返回结果标识符是否缺失或无效。
    """

    return (
        isinstance(value, bool)
        or not isinstance(value, (str, int))
        or not str(value).strip()
    )


def _reject_sensitive_result_fields(value: Any, path: str) -> None:
    """Reject credential-like keys anywhere in a Host result tree.

        中文:拒绝 Host 结果树中任意位置出现的疑似凭据键。
    """

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = key.casefold().replace("_", "").replace("-", "")
            if any(marker in normalized for marker in _RESULT_SENSITIVE_KEY_MARKERS):
                raise QQOperationValidationError(
                    f"QQ operation result contains a sensitive field: {path}.{key}"
                )
            _reject_sensitive_result_fields(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_sensitive_result_fields(item, f"{path}[{index}]")


def _validate_result_references(result: Mapping[str, Any]) -> None:
    """Keep media/file references remote-or-binding-private at the seam.

        中文:确保接口处的媒体／文件引用仍指向远程资源或 binding 私有资源。
    """

    remote_uri = result.get("remote_uri")
    if remote_uri is not None:
        if not isinstance(remote_uri, str) or not remote_uri.strip():
            raise QQOperationValidationError(
                "QQ operation result remote_uri must be text"
            )
        if len(remote_uri.encode("utf-8")) > _MAX_RESULT_REFERENCE_BYTES:
            raise QQOperationValidationError(
                "QQ operation result remote_uri is too long"
            )
        parsed = urlparse(remote_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise QQOperationValidationError(
                "QQ operation result remote_uri must be http(s)"
            )

    local_reference = result.get("local_result_reference")
    if local_reference is not None:
        if not isinstance(local_reference, str) or not local_reference.strip():
            raise QQOperationValidationError(
                "QQ operation result local_result_reference must be text"
            )
        if len(local_reference.encode("utf-8")) > _MAX_RESULT_REFERENCE_BYTES:
            raise QQOperationValidationError(
                "QQ operation result local_result_reference is too long"
            )
        if not local_reference.startswith(_RESULT_REFERENCE_SCHEMES):
            raise QQOperationValidationError(
                "QQ operation result local_result_reference must be binding-private"
            )


def _validate_parameter_value(field: str, value: Any) -> None:
    """Validate one parameter value against the shared operation schema.

        中文:根据共享 operation schema 校验一个参数值。
    """

    _validate_json_value(value, field, depth=0)
    if field in _IDENTIFIER_PARAMETER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be a string or integer"
            )
        if isinstance(value, int) and value < 1:
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be positive"
            )
        if isinstance(value, str) and not value.strip():
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be non-empty"
            )
    elif field in _NON_NEGATIVE_INTEGER_PARAMETER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be a non-negative integer"
            )
    elif field in _NON_NEGATIVE_NUMBER_PARAMETER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be a non-negative number"
            )
        if value < 0:
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be a non-negative number"
            )
    elif field in _POSITIVE_NUMBER_PARAMETER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be a positive number"
            )
        if value <= 0:
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be a positive number"
            )
    elif field in _BOOLEAN_PARAMETER_FIELDS:
        if not isinstance(value, bool):
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be boolean"
            )
    elif field in _STRING_PARAMETER_FIELDS:
        if not isinstance(value, str):
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be text"
            )
        if field == "comment" and len(value) > 2_048:
            raise QQOperationValidationError(
                "QQ operation parameter comment exceeds 2048 characters"
            )
    elif field in _STRING_LIST_PARAMETER_FIELDS:
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be a list of non-empty text"
            )
    elif field in _IDENTIFIER_LIST_PARAMETER_FIELDS:
        if not isinstance(value, (list, tuple)):
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be a list"
            )
        for item in value:
            _validate_parameter_value("message_id", item)
    elif field in _OBJECT_LIST_PARAMETER_FIELDS:
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in value
        ):
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be a list of objects"
            )
    elif field in _OBJECT_PARAMETER_FIELDS:
        if not isinstance(value, Mapping):
            raise QQOperationValidationError(
                f"QQ operation parameter {field} must be an object"
            )


def _validate_json_value(
    value: Any,
    field: str,
    *,
    depth: int,
    label: str = "parameter",
    allow_null: bool = False,
) -> None:
    """Reject non-JSON values and unbounded nested structures.

        中文:拒绝非 JSON 值和无界嵌套结构。
    """

    if depth > _MAX_PARAMETER_DEPTH:
        raise QQOperationValidationError(
            f"QQ operation {label} {field} is nested too deeply"
        )
    if value is None:
        if allow_null:
            return
        raise QQOperationValidationError(
            f"QQ operation {label} {field} must not be null"
        )
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QQOperationValidationError(
                f"QQ operation {label} {field} must be finite"
            )
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _MAX_PARAMETER_STRING_BYTES:
            raise QQOperationValidationError(
                f"QQ operation {label} {field} is too large"
            )
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_PARAMETER_ITEMS:
            raise QQOperationValidationError(
                f"QQ operation {label} {field} has too many object fields"
            )
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise QQOperationValidationError(
                    f"QQ operation {label} {field} has an invalid object key"
                )
            _validate_json_value(
                item,
                f"{field}.{key}",
                depth=depth + 1,
                label=label,
                allow_null=allow_null,
            )
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_PARAMETER_ITEMS:
            raise QQOperationValidationError(
                f"QQ operation {label} {field} has too many list items"
            )
        for index, item in enumerate(value):
            _validate_json_value(
                item,
                f"{field}[{index}]",
                depth=depth + 1,
                label=label,
                allow_null=allow_null,
            )
        return
    raise QQOperationValidationError(
        f"QQ operation {label} {field} contains a non-JSON value"
    )
