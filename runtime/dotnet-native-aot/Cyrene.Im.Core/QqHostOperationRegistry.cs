// ┌─────────────────────────────────────────────────────────────────────────┐
// │  📄 QqHostOperationRegistry.cs                                           │
// │  Namespace: Cyrene.Im.Core                                                │
// │  Role: Fixed QQ operation allow-list and native mapping metadata.         │
// │                                                                         │
// │  模块职责：固定 QQ operation 白名单与原生 Service/Method 映射元数据           │
// └─────────────────────────────────────────────────────────────────────────┘

namespace Cyrene.Im.Core;

/// <summary>One explicitly registered QQ operation.</summary>
/// <remarks>中文：显式注册的一个 QQ 操作。</remarks>
public sealed record QqHostOperation(
    string Name,
    string Service,
    string Method,
    string Priority,
    string Mapping,
    bool Requestable = true);

/// <summary>Authoritative fixed operation registry for qq.client.v1.</summary>
/// <remarks>中文：qq.client.v1 的权威固定操作注册表。</remarks>
public static class QqHostOperationRegistry
{
    private static readonly IReadOnlyDictionary<string, QqHostOperation> Operations =
        new Dictionary<string, QqHostOperation>(StringComparer.Ordinal)
        {
            ["qq.session.create"] = Op("qq.session.create", "NodeIQQNTWrapperSession", "create", "P0", "session"),
            ["qq.session.init"] = Op("qq.session.init", "NodeIQQNTWrapperSession", "init", "P0", "session"),
            ["qq.session.start_nt"] = Op("qq.session.start_nt", "NodeIQQNTWrapperSession", "startNT", "P0", "session"),
            ["qq.login.connect"] = Op("qq.login.connect", "NodeIKernelLoginService", "connect", "P0", "login"),
            ["qq.login.online"] = Op("qq.login.online", "NodeIKernelLoginService", "online", "P0", "login"),
            ["qq.login.offline"] = Op("qq.login.offline", "NodeIKernelLoginService", "offline", "P0", "login"),
            ["qq.login.list"] = Op("qq.login.list", "NodeIKernelLoginService", "getLoginList", "P0", "login"),
            ["qq.login.quick"] = Op("qq.login.quick", "NodeIKernelLoginService", "quickLoginWithUin", "P0", "login"),
            ["qq.login.password"] = Op("qq.login.password", "NodeIKernelLoginService", "passwordLogin", "P0", "login"),
            ["qq.login.qr"] = Op("qq.login.qr", "NodeIKernelLoginService", "getQRCodePicture", "P0", "login"),
            ["qq.login.poll"] = Op("qq.login.poll", "NodeIKernelLoginService", "startPolling", "P0", "login"),
            ["qq.login.self_status"] = Op("qq.login.self_status", "NodeIKernelProfileService", "getSelfStatus", "P0", "login"),
            ["qq.account.core"] = Op("qq.account.core", "NodeIKernelProfileService", "getCoreAndBaseInfo", "P0", "account"),
            ["qq.account.simple"] = Op("qq.account.simple", "NodeIKernelProfileService", "getUserSimpleInfo", "P0", "account"),
            ["qq.message.subscribe"] = Op("qq.message.subscribe", "NodeIKernelMsgService", "addKernelMsgListener", "P0", "message"),
            ["qq.message.send"] = Op("qq.message.send", "NodeIKernelMsgService", "sendMsg", "P0", "send_message"),
            ["qq.message.send_completion"] = Op("qq.message.send_completion", "NodeIKernelMsgListener", "onMsgInfoListUpdate", "P0", "send_message", false),
            ["qq.peer.uid_by_uin"] = Op("qq.peer.uid_by_uin", "NodeIKernelProfileService", "getUidByUin", "P0", "peer"),
            ["qq.peer.uin_by_uid"] = Op("qq.peer.uin_by_uid", "NodeIKernelProfileService", "getUinByUid", "P0", "peer"),
            ["qq.peer.uid"] = Op("qq.peer.uid", "NodeIKernelUixConvertService", "getUid", "P0", "peer"),
            ["qq.peer.uin"] = Op("qq.peer.uin", "NodeIKernelUixConvertService", "getUin", "P0", "peer"),
            ["qq.message.history_include_self"] = Op("qq.message.history_include_self", "NodeIKernelMsgService", "getMsgsIncludeSelf", "P1", "lookup"),
            ["qq.message.history_by_seq"] = Op("qq.message.history_by_seq", "NodeIKernelMsgService", "getMsgsBySeqAndCount", "P1", "lookup"),
            ["qq.message.by_id"] = Op("qq.message.by_id", "NodeIKernelMsgService", "getMsgsByMsgId", "P1", "lookup"),
            ["qq.message.single"] = Op("qq.message.single", "NodeIKernelMsgService", "getSingleMsg", "P1", "lookup"),
            ["qq.message.search"] = Op("qq.message.search", "NodeIKernelMsgService", "queryMsgsWithFilterEx", "P1", "lookup"),
            ["qq.message.recall"] = Op("qq.message.recall", "NodeIKernelMsgService", "recallMsg", "P1", "message"),
            ["qq.message.forward"] = Op("qq.message.forward", "NodeIKernelMsgService", "forwardMsg", "P1", "message"),
            ["qq.message.forward_comment"] = Op("qq.message.forward_comment", "NodeIKernelMsgService", "forwardMsgWithComment", "P1", "message"),
            ["qq.message.multi_forward"] = Op("qq.message.multi_forward", "NodeIKernelMsgService", "multiForwardMsg", "P1", "message"),
            ["qq.message.read"] = Op("qq.message.read", "NodeIKernelMsgService", "setMsgRead", "P1", "read"),
            ["qq.message.read_all"] = Op("qq.message.read_all", "NodeIKernelMsgService", "setAllC2CAndGroupMsgRead", "P1", "read"),
            ["qq.message.emoji_likes"] = Op("qq.message.emoji_likes", "NodeIKernelMsgService", "setMsgEmojiLikes", "P1", "emoji"),
            ["qq.message.emoji_likes_list"] = Op("qq.message.emoji_likes_list", "NodeIKernelMsgService", "getMsgEmojiLikesList", "P1", "emoji"),
            ["qq.group.list"] = Op("qq.group.list", "NodeIKernelGroupService", "getGroupList", "P1", "group"),
            ["qq.group.detail"] = Op("qq.group.detail", "NodeIKernelGroupService", "getGroupDetailInfo", "P1", "group"),
            ["qq.group.members"] = Op("qq.group.members", "NodeIKernelGroupService", "getAllMemberList", "P1", "group"),
            ["qq.group.member"] = Op("qq.group.member", "NodeIKernelGroupService", "getMemberInfo", "P1", "group"),
            ["qq.friend.list"] = Op("qq.friend.list", "NodeIKernelBuddyService", "getBuddyListV2", "P1", "friend"),
            ["qq.friend.cached"] = Op("qq.friend.cached", "NodeIKernelBuddyService", "getBuddyListFromCache", "P1", "friend"),
            ["qq.friend.requests"] = Op("qq.friend.requests", "NodeIKernelBuddyService", "getBuddyReq", "P1", "friend"),
            ["qq.media.element"] = Op("qq.media.element", "NodeIKernelMsgService", "getRichMediaElement", "P1", "media"),
            ["qq.media.download"] = Op("qq.media.download", "NodeIKernelMsgService", "downloadRichMedia", "P1", "media"),
            ["qq.media.video_url"] = Op("qq.media.video_url", "NodeIKernelRichMediaService", "getVideoPlayUrlV2", "P1", "media"),
            ["qq.media.download_complete"] = Op("qq.media.download_complete", "NodeIKernelRichMediaListener", "onRichMediaDownloadComplete", "P1", "media", false),
            ["qq.file.list"] = Op("qq.file.list", "NodeIKernelRichMediaService", "getGroupFileList", "P1", "file"),
            ["qq.file.search"] = Op("qq.file.search", "NodeIKernelFileAssistantService", "searchFile", "P1", "file"),
            ["qq.file.download"] = Op("qq.file.download", "NodeIKernelFileAssistantService", "downloadFile", "P1", "file"),
            ["qq.file.forward"] = Op("qq.file.forward", "NodeIKernelFileAssistantService", "forwardFile", "P1", "file"),
            ["qq.file.save"] = Op("qq.file.save", "NodeIKernelFileAssistantService", "saveAs", "P1", "file"),
            ["qq.group.modify_name"] = Op("qq.group.modify_name", "NodeIKernelGroupService", "modifyGroupName", "P2", "group_mutation"),
            ["qq.group.modify_remark"] = Op("qq.group.modify_remark", "NodeIKernelGroupService", "modifyGroupRemark", "P2", "group_mutation"),
            ["qq.group.mute_member"] = Op("qq.group.mute_member", "NodeIKernelGroupService", "setMemberShutUp", "P2", "group_mutation"),
            ["qq.group.mute"] = Op("qq.group.mute", "NodeIKernelGroupService", "setGroupShutUp", "P2", "group_mutation"),
            ["qq.group.kick"] = Op("qq.group.kick", "NodeIKernelGroupService", "kickMember", "P2", "group_mutation"),
            ["qq.group.quit"] = Op("qq.group.quit", "NodeIKernelGroupService", "quitGroup", "P2", "group_mutation"),
            ["qq.group.approve"] = Op("qq.group.approve", "NodeIKernelGroupService", "operateSysNotify", "P2", "group_mutation"),
            ["qq.friend.approve"] = Op("qq.friend.approve", "NodeIKernelBuddyService", "approvalFriendRequest", "P2", "friend_mutation"),
            ["qq.friend.approve_doubt"] = Op("qq.friend.approve_doubt", "NodeIKernelBuddyService", "approvalDoubtBuddyReq", "P2", "friend_mutation"),
            ["qq.friend.doubt_requests"] = Op("qq.friend.doubt_requests", "NodeIKernelBuddyService", "getDoubtBuddyReq", "P2", "friend_mutation"),
            ["qq.friend.add"] = Op("qq.friend.add", "NodeIKernelBuddyService", "reqToAddFriends", "P2", "friend_mutation"),
            ["qq.friend.delete"] = Op("qq.friend.delete", "NodeIKernelBuddyService", "delBuddy", "P2", "friend_mutation"),
            ["qq.friend.set_remark"] = Op("qq.friend.set_remark", "NodeIKernelBuddyService", "setBuddyRemark", "P2", "friend_mutation"),
            ["qq.profile.modify"] = Op("qq.profile.modify", "NodeIKernelProfileService", "modifySelfProfile", "P2", "profile"),
            ["qq.profile.nickname"] = Op("qq.profile.nickname", "NodeIKernelProfileService", "setNickName", "P2", "profile"),
            ["qq.profile.long_nick"] = Op("qq.profile.long_nick", "NodeIKernelProfileService", "setLongNick", "P2", "profile"),
            ["qq.profile.birthday"] = Op("qq.profile.birthday", "NodeIKernelProfileService", "setBirthday", "P2", "profile"),
            ["qq.profile.gender"] = Op("qq.profile.gender", "NodeIKernelProfileService", "setGander", "P2", "profile"),
            ["qq.profile.header"] = Op("qq.profile.header", "NodeIKernelProfileService", "setHeader", "P2", "profile"),
            ["qq.search.stranger"] = Op("qq.search.stranger", "NodeIKernelSearchService", "searchStranger", "P2", "search"),
            ["qq.search.group"] = Op("qq.search.group", "NodeIKernelSearchService", "searchGroup", "P2", "search"),
            ["qq.search.contact"] = Op("qq.search.contact", "NodeIKernelSearchService", "searchContact", "P2", "search"),
            ["qq.search.message"] = Op("qq.search.message", "NodeIKernelSearchService", "searchMsgWithKeywords", "P2", "search"),
            ["qq.search.file"] = Op("qq.search.file", "NodeIKernelSearchService", "searchFileWithKeywords", "P2", "search"),
            ["qq.online.status"] = Op("qq.online.status", "NodeIKernelOnlineStatusService", "setStatus", "P2", "online"),
            ["qq.online.devices"] = Op("qq.online.devices", "NodeIKernelOnlineStatusService", "getOnLineDev", "P2", "online"),
            ["qq.online.likes"] = Op("qq.online.likes", "NodeIKernelMsgService", "getLikeList", "P2", "online"),
            ["qq.online.set_like"] = Op("qq.online.set_like", "NodeIKernelMsgService", "setLikeStatus", "P2", "online"),
            ["qq.online.check_like"] = Op("qq.online.check_like", "NodeIKernelMsgService", "checkLikeStatus", "P2", "online")
        };

    public static IReadOnlyCollection<QqHostOperation> All => Operations.Values.ToArray();

    public static bool TryGet(string name, out QqHostOperation operation) =>
        Operations.TryGetValue(name, out operation!);

    private static QqHostOperation Op(
        string name,
        string service,
        string method,
        string priority,
        string mapping,
        bool requestable = true) => new(name, service, method, priority, mapping, requestable);
}
