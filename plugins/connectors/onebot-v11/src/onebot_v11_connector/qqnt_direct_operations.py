"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 qqnt_direct_operations.py                                       │
│  Module: onebot_v11_connector.qqnt_direct_operations                │
│  Role: Fixed QQ API operation allow-list and mapping authority.      │
│                                                                     │
│  模块职责：将公开扩展操作绑定到已登记的 QQ Service 方法。              │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QQOperation:
    """One explicit, non-passthrough operation exposed by the connector."""

    name: str
    service: str
    method: str
    priority: str
    mapping: str


def _op(
    name: str,
    service: str,
    method: str,
    priority: str,
    mapping: str,
) -> QQOperation:
    return QQOperation(name, service, method, priority, mapping)


# This table is the executable counterpart of QQNT_DIRECT_API_MATRIX.md. The
# host receives the stable operation name and never receives an arbitrary
# service/method pair from a Product request.
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
        "NodeIKernelLoginService",
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


def get_qq_operation(name: str) -> QQOperation | None:
    """Return a fixed operation definition, never an arbitrary native call."""

    return QQ_OPERATION_BY_NAME.get(name)
