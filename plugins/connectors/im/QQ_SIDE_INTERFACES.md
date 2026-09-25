# QQ client service interfaces / QQ 客户端 Service 接口

This document is an inventory of QQ client-side service interfaces, methods,
listeners, and identifier rules. Interface availability and signatures can vary
between QQ client versions; verify every method against the target client before
using it.

本文只记录 QQ 客户端侧的 Service 接口、方法、Listener 与标识符规则。接口可用性和签名
可能随 QQ 客户端版本变化，使用前必须针对目标客户端逐项验证。

## 1. Invocation model / 调用模型

The client exposes a session object that resolves typed services. A call may
return its data directly or complete through a matching listener callback.

客户端通过 Session 对象提供类型化 Service。调用结果可能直接返回，也可能通过对应的
Listener 回调完成。

```text
QQ client session
  -> get*Service()
    -> NodeIKernel*Service method
      -> direct result or NodeIKernel*Listener callback
```

Use a typed Service method when one is available. Low-level request methods are
fallbacks with a larger version-compatibility surface.

存在类型化 Service 方法时应优先使用。低层请求方法只作为 fallback，其版本兼容面更大。

## 2. Primary services / 主要 Service

| Capability / 能力 | Interface / 接口 | Main methods / 主要方法 |
| --- | --- | --- |
| Session and service lookup / 会话与 Service 获取 | `NodeIQQNTWrapperSession` | `create`, `init`, `startNT`, `getMsgService`, `getGroupService`, `getBuddyService`, `getProfileService`, `getRichMediaService` |
| Login / 登录 | `NodeIKernelLoginService` | `get`, `connect`, `initConfig`, `getLoginList`, `quickLoginWithUin`, `passwordLogin`, `getQRCodePicture`, `startPolling`, `online`, `offline` |
| Messages / 消息 | `NodeIKernelMsgService` | `addKernelMsgListener`, `sendMsg`, `recallMsg`, `forwardMsg`, `getMsgsIncludeSelf`, `getMsgsBySeqAndCount`, `getMsgsByMsgId`, `getSingleMsg`, `queryMsgsWithFilterEx`, `setMsgRead`, `getRichMediaElement`, `downloadRichMedia` |
| Groups / 群 | `NodeIKernelGroupService` | `getGroupList`, `getGroupDetailInfo`, `getAllMemberList`, `getMemberInfo`, `modifyGroupName`, `modifyGroupRemark`, `setMemberShutUp`, `setGroupShutUp`, `kickMember`, `quitGroup`, `getJoinGroupLink` |
| Friends and contacts / 好友与联系人 | `NodeIKernelBuddyService` | `getBuddyListV2`, `getBuddyListFromCache`, `getBuddyReq`, `approvalFriendRequest`, `reqToAddFriends`, `setBuddyRemark`, `delBuddy`, `isBuddy` |
| User profile / 用户资料 | `NodeIKernelProfileService` | `getCoreAndBaseInfo`, `fetchUserDetailInfo`, `getUserSimpleInfo`, `getUserDetailInfo`, `getUidByUin`, `getUinByUid`, `modifySelfProfile`, `setNickName`, `setLongNick`, `setHeader`, `getSelfStatus` |
| Rich media / 富媒体 | `NodeIKernelRichMediaService` | `getVideoPlayUrlV2`, `getRichMediaFileDir`, `downloadFileForModelId`, `downloadFileForFileUuid`, `getGroupFileList`, `createGroupFolder`, `moveGroupFile`, `deleteGroupFile`, `uploadRMFileWithoutMsg`, `getScreenOCR` |
| File assistant / 文件助手 | `NodeIKernelFileAssistantService` | `getFileAssistantList`, `getFileSessionList`, `searchFile`, `downloadFile`, `forwardFile`, `cancelFileAction`, `retryFileAction`, `deleteFile`, `saveAs` |
| Search / 搜索 | `NodeIKernelSearchService` | `searchStranger`, `searchGroup`, `searchContact`, `searchMsgWithKeywords`, `searchFileWithKeywords`, `searchMore*`, `cancel*` |
| Online state and likes / 在线状态与点赞 | `NodeIKernelMsgService`, `NodeIKernelOnlineStatusService` | `setStatus`, `getOnLineDev`, `getLikeList`, `setLikeStatus`, `checkLikeStatus` |
| Time, network, and low-level request / 时间、网络与低层请求 | `NodeIKernelMSFService` | `getServerTime`, `getMsfStatus`, `online`, `offline`, `setNetworkProxy`, `getNetworkProxy`, `sendMsfRequest` |
| Database maintenance / 数据库辅助 | `NodeIKernelDbToolsService` | `depositDatabase`, `backupDatabase`, `retrieveDatabase` |

## 3. Messaging methods / 消息方法

| Task / 任务 | Method / 方法 | Result rule / 结果规则 |
| --- | --- | --- |
| Send / 发送 | `NodeIKernelMsgService.sendMsg` | Supply message id, `Peer`, ordered elements, and attributes; correlate completion with `onMsgInfoListUpdate` when required. / 提供消息 ID、`Peer`、有序元素与属性；必要时用 `onMsgInfoListUpdate` 关联完成结果。 |
| Receive / 接收 | `NodeIKernelMsgService.addKernelMsgListener` | Inbound messages arrive through `NodeIKernelMsgListener.onRecvMsg`. / 入站消息通过 `NodeIKernelMsgListener.onRecvMsg` 到达。 |
| Recall / 撤回 | `NodeIKernelMsgService.recallMsg` | Match the result callback to the requested message identity. / 将结果回调与请求的消息身份匹配。 |
| History / 历史消息 | `getMsgsIncludeSelf`, `getMsgsBySeqAndCount`, `getMsgsByMsgId`, `getSingleMsg` | Keep peer, sequence, random value, and message id distinct. / 明确区分会话、序列号、随机值和消息 ID。 |
| Conversation search / 会话内搜索 | `queryMsgsWithFilterEx`, `queryMsgsWithFilter` | Use the declared `QueryMsgsParams` shape for the target version. / 使用目标版本声明的 `QueryMsgsParams` 结构。 |
| Forward / 转发 | `forwardMsg`, `forwardMsgWithComment`, `multiForwardMsg` | Pass source and destination `Peer` values explicitly. / 显式传入源与目标 `Peer`。 |
| Rich media / 消息媒体 | `getRichMediaElement`, `downloadRichMedia`, `getVideoPlayUrlV2` | Media completion may arrive through `onRichMediaDownloadComplete`. / 媒体完成结果可能通过 `onRichMediaDownloadComplete` 到达。 |
| Read state / 已读状态 | `setMsgRead`, `setAllC2CAndGroupMsgRead`, `setMsgReadAndReport` | Select single-conversation or global behavior explicitly. / 显式选择单会话或全局行为。 |
| Emoji likes / 表情点赞 | `setMsgEmojiLikes`, `getMsgEmojiLikesList` | Preserve the declared emoji identifier and type. / 保留声明中的表情标识与类型。 |

## 4. Group methods / 群方法

| Task / 任务 | Methods / 方法 | Result rule / 结果规则 |
| --- | --- | --- |
| Group list and details / 群列表与详情 | `getGroupList`, `getGroupDetailInfo`, `getGroupExt0xEF0Info` | Group-list data may arrive through `onGroupListUpdate`; request only declared detail fields. / 群列表数据可能通过 `onGroupListUpdate` 到达；详情只请求声明字段。 |
| Members / 群成员 | `getAllMemberList`, `getMemberInfo` | Match member callbacks by group code and member identity. / 按群号与成员身份匹配回调。 |
| Member moderation / 成员管理 | `kickMember`, `kickMemberV2`, `setMemberShutUp`, `modifyMemberCardName`, `modifyMemberRole` | Select the method signature supported by the target version. / 选择目标版本支持的方法签名。 |
| Group moderation / 群管理 | `setGroupShutUp`, `modifyGroupName`, `modifyGroupRemark`, `setHeader` | Keep group code explicit on every operation. / 每个操作都显式携带群号。 |
| Join requests / 入群请求 | `getSingleScreenNotifies`, `operateSysNotify` | Correlate the operation with the matching group notification. / 将操作与对应群通知关联。 |
| Announcements / 群公告 | `publishGroupBulletin`, `deleteGroupBulletin`, `uploadGroupBulletinPic` | Validate all required authentication material without logging it. / 验证所需认证材料且不得记录其内容。 |
| Essence messages / 精华消息 | `fetchGroupEssenceList`, `addGroupEssence`, `removeGroupEssence` | Add and remove operations require the resolved message record. / 添加和移除操作需要先解析消息记录。 |
| Share link / 群分享链接 | `getJoinGroupLink` | Preserve group code, source id, short-link flag, and additional parameters. / 保留群号、来源 ID、短链标记与附加参数。 |

## 5. Friends, profiles, media, and files / 好友、资料、媒体与文件

| Task / 任务 | Methods / 方法 | Result rule / 结果规则 |
| --- | --- | --- |
| Friend list / 好友列表 | `getBuddyListV2`, `getBuddyListFromCache` | Handle version-dependent overloads explicitly. / 显式处理版本相关重载。 |
| Friend requests / 好友申请 | `getBuddyReq`, `approvalFriendRequest`, `getDoubtBuddyReq`, `approvalDoubtBuddyReq` | Listing and approval are separate operations. / 列表与审批是不同操作。 |
| User details / 用户详情 | `getCoreAndBaseInfo`, `fetchUserDetailInfo`, `getUserDetailInfo`, `getUserDetailInfoWithBizInfo` | Select the declared detail scope and `BizKey` values. / 选择声明的详情范围与 `BizKey`。 |
| UID/UIN conversion / UID 与 UIN 转换 | `getUidByUin`, `getUinByUid`, `getUixConvertService().getUid`, `getUixConvertService().getUin` | Never infer one identifier type from another. / 禁止在不同标识符类型之间隐式推断。 |
| Profile updates / 资料修改 | `modifySelfProfile`, `modifyDesktopMiniProfile`, `setNickName`, `setLongNick`, `setBirthday`, `setGander`, `setHeader` | Validate field support before mutation. / 修改前验证字段支持情况。 |
| Attachment download / 附件下载 | `NodeIKernelMsgService.downloadRichMedia` | Match `onRichMediaDownloadComplete` to the requested media identity. / 将 `onRichMediaDownloadComplete` 与请求的媒体身份匹配。 |
| Video URL / 视频 URL | `NodeIKernelRichMediaService.getVideoPlayUrlV2` | Supply peer, message id, element id, codec, and download parameters. / 提供会话、消息 ID、元素 ID、编解码格式与下载参数。 |
| Group files / 群文件 | `getGroupFileList`, `createGroupFolder`, `renameGroupFolder`, `moveGroupFile`, `deleteGroupFolder`, `deleteGroupFile`, `transGroupFile` | Keep group, file, and folder identifiers distinct. / 明确区分群、文件与目录标识。 |
| File assistant / 文件助手 | `searchFile`, `downloadFile`, `forwardFile`, `cancelFileAction`, `retryFileAction`, `deleteFile`, `saveAs` | Match search and transfer callbacks by their request identifiers. / 按请求标识匹配搜索与传输回调。 |
| Screen OCR / 屏幕 OCR | `NodeIKernelRichMediaService.getScreenOCR`, `NodeIKernelNodeMiscService.wantWinScreenOCR` | Select the service exposed by the target client. / 选择目标客户端实际暴露的 Service。 |

## 6. Additional service getters / 其他 Service 获取器

| Session getter / Session 获取器 | Interface / 接口 | Area / 领域 |
| --- | --- | --- |
| `getCollectionService` | `NodeIKernelCollectionService` | Favorites / 收藏 |
| `getAlbumService` | `NodeIKernelAlbumService` | Albums and Qzone media / 相册与空间媒体 |
| `getRecentContactService` | `NodeIKernelRecentContactService` | Recent contacts and unread state / 最近联系人与未读 |
| `getRobotService` | `NodeIKernelRobotService` | QQ robot store and sessions / QQ 机器人商店与会话 |
| `getTicketService` | `NodeIKernelTicketService` | Client key and ticket material / client key 与票据 |
| `getTipOffService` | `NodeIKernelTipOffService` | Report-related calls / 举报相关调用 |
| `getSettingService` | `NodeIKernelSettingService` | Settings and account switches / 设置与账号开关 |
| `getConfigMgrService` | `NodeIKernelConfigMgrService` | Client configuration / 客户端配置 |
| `getBaseEmojiService` | `NodeIKernelBaseEmojiService` | Emoji resources / 表情资源 |
| `getFlashTransferService` | `NodeIKernelFlashTransferService` | Flash file transfer / 闪传 |
| `getBdhUploadService` | `NodeIKernelBdhUploadService` | BDH upload lifecycle / BDH 上传生命周期 |
| `getUixConvertService` | `NodeIKernelUixConvertService` | UID/UIN conversion / UID/UIN 转换 |
| `getNodeMiscService` | `NodeIKernelNodeMiscService` | Mini App and desktop utilities / 小程序与桌面工具 |
| `getMiniAppService` | `NodeIKernelMiniAppService` | Mini App operations / 小程序 |
| `getThirdPartySigService` | `NodeIKernelThirdPartySigService` | Third-party signature material / 第三方签名材料 |
| `getUnifySearchService` | `NodeIKernelUnifySearchService` | Unified QQ search / QQ 统一搜索 |
| `getPersonalAlbumService` | `NodeIKernelPersonalAlbumService` | Personal album operations / 个人相册 |

## 7. Parameter and callback rules / 参数与回调规则

- Keep group code, QQ UIN, user UID, `Peer.peerUid`, message id, message
  sequence, and message random value as distinct typed values.
- Treat a success status as acceptance only; when a listener carries the useful
  result, wait for and correlate that callback.
- Correlate concurrent callbacks by account, peer, request, message, file, or
  search identity as appropriate.
- Resolve overloads and optional parameters from the exact target-client
  declaration; do not infer parameter order from method names.
- Prefer typed Service methods. Use `sendSsoCmdReqByContend` or
  `sendMsfRequest` only when no suitable typed method exists.
- Before declaring an interface usable, verify it against a real target-client
  version and record the observed result or failure.

- 群号、QQ UIN、用户 UID、`Peer.peerUid`、消息 ID、消息序列号和消息随机值必须作为不同
  类型处理。
- 成功状态只代表调用被接受；有效结果由 Listener 携带时，必须等待并关联该回调。
- 并发回调应根据实际场景按账号、会话、请求、消息、文件或搜索标识关联。
- 重载与可选参数必须以目标客户端的准确声明为准，不能根据方法名推断参数顺序。
- 优先使用类型化 Service；只有不存在合适方法时才使用 `sendSsoCmdReqByContend` 或
  `sendMsfRequest`。
- 宣称接口可用前，必须针对真实目标客户端版本验证，并记录观察结果或失败。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# QQ 客户端 Service 接口

本文清点 QQ 客户端侧的 Service 接口、方法、Listener 和标识符规则。接口可用性和签名可能随 QQ 客户端版本变化，使用前必须针对目标客户端逐项验证。

## 1. 调用模型

客户端公开一个可解析类型化 Service 的 session 对象。调用可能直接返回数据，也可能通过匹配的 Listener 回调完成。

```text
QQ client session
  -> get*Service()
    -> NodeIKernel*Service method
      -> direct result or NodeIKernel*Listener callback
```

如果存在类型化 Service 方法，就应优先使用。低层 request method 只是兼容范围更大的备用方法。

## 2. 主要 Service

| Capability | 接口 | 主要方法 |
| --- | --- | --- |
| Session 和 Service 查询 | NodeIQQNTWrapperSession | create、init、startNT、getMsgService、getGroupService、getBuddyService、getProfileService、getRichMediaService |
| 登录 | NodeIKernelLoginService | get、connect、initConfig、getLoginList、quickLoginWithUin、passwordLogin、getQRCodePicture、startPolling、online、offline |
| 消息 | NodeIKernelMsgService | addKernelMsgListener、sendMsg、recallMsg、forwardMsg、getMsgsIncludeSelf、getMsgsBySeqAndCount、getMsgsByMsgId、getSingleMsg、queryMsgsWithFilterEx、setMsgRead、getRichMediaElement、downloadRichMedia |
| 群 | NodeIKernelGroupService | getGroupList、getGroupDetailInfo、getAllMemberList、getMemberInfo、modifyGroupName、modifyGroupRemark、setMemberShutUp、setGroupShutUp、kickMember、quitGroup、getJoinGroupLink |
| 好友和联系人 | NodeIKernelBuddyService | getBuddyListV2、getBuddyListFromCache、getBuddyReq、approvalFriendRequest、reqToAddFriends、setBuddyRemark、delBuddy、isBuddy |
| 用户资料 | NodeIKernelProfileService | getCoreAndBaseInfo、fetchUserDetailInfo、getUserSimpleInfo、getUserDetailInfo、getUidByUin、getUinByUid、modifySelfProfile、setNickName、setLongNick、setHeader、getSelfStatus |
| 富媒体 | NodeIKernelRichMediaService | getVideoPlayUrlV2、getRichMediaFileDir、downloadFileForModelId、downloadFileForFileUuid、getGroupFileList、createGroupFolder、moveGroupFile、deleteGroupFile、uploadRMFileWithoutMsg、getScreenOCR |
| 文件助手 | NodeIKernelFileAssistantService | getFileAssistantList、getFileSessionList、searchFile、downloadFile、forwardFile、cancelFileAction、retryFileAction、deleteFile、saveAs |
| 搜索 | NodeIKernelSearchService | searchStranger、searchGroup、searchContact、searchMsgWithKeywords、searchFileWithKeywords、searchMore*、cancel* |
| 在线状态与点赞 | NodeIKernelMsgService、NodeIKernelOnlineStatusService | setStatus、getOnLineDev、getLikeList、setLikeStatus、checkLikeStatus |
| 时间、网络和低层 request | NodeIKernelMSFService | getServerTime、getMsfStatus、online、offline、setNetworkProxy、getNetworkProxy、sendMsfRequest |
| 数据库维护 | NodeIKernelDbToolsService | depositDatabase、backupDatabase、retrieveDatabase |

## 3. 消息方法

| 任务 | 方法 | 结果规则 |
| --- | --- | --- |
| 发送 | NodeIKernelMsgService.sendMsg | 提供消息 ID、Peer、有序元素和属性；必要时通过 onMsgInfoListUpdate 关联完成结果。 |
| 接收 | NodeIKernelMsgService.addKernelMsgListener | 入站消息通过 NodeIKernelMsgListener.onRecvMsg 到达。 |
| 撤回 | NodeIKernelMsgService.recallMsg | 将结果回调与请求的消息身份匹配。 |
| 历史消息 | getMsgsIncludeSelf、getMsgsBySeqAndCount、getMsgsByMsgId、getSingleMsg | 明确区分 peer、sequence、random value 和 message ID。 |
| 会话内搜索 | queryMsgsWithFilterEx、queryMsgsWithFilter | 使用目标版本声明的 QueryMsgsParams 结构。 |
| 转发 | forwardMsg、forwardMsgWithComment、multiForwardMsg | 显式传入源与目标 Peer。 |
| 富媒体消息 | getRichMediaElement、downloadRichMedia、getVideoPlayUrlV2 | 媒体完成结果可能通过 onRichMediaDownloadComplete 到达。 |
| 已读状态 | setMsgRead、setAllC2CAndGroupMsgRead、setMsgReadAndReport | 显式选择单会话或全局行为。 |
| 表情点赞 | setMsgEmojiLikes、getMsgEmojiLikesList | 保留声明中的 emoji 标识符和类型。 |

## 4. 群方法

| 任务 | 方法 | 结果规则 |
| --- | --- | --- |
| 群列表与详情 | getGroupList、getGroupDetailInfo、getGroupExt0xEF0Info | 群列表数据可能通过 onGroupListUpdate 到达；仅请求已声明的详情字段。 |
| 群成员 | getAllMemberList、getMemberInfo | 按群号和成员身份匹配回调。 |
| 成员管理 | kickMember、kickMemberV2、setMemberShutUp、modifyMemberCardName、modifyMemberRole | 选择目标版本支持的方法签名。 |
| 群管理 | setGroupShutUp、modifyGroupName、modifyGroupRemark、setHeader | 每个操作都显式提供群号。 |
| 入群申请 | getSingleScreenNotifies、operateSysNotify | 将操作与匹配的群通知关联。 |
| 群公告 | publishGroupBulletin、deleteGroupBulletin、uploadGroupBulletinPic | 验证所有必需认证材料，但不要记录其内容。 |
| 精华消息 | fetchGroupEssenceList、addGroupEssence、removeGroupEssence | 添加和删除操作都需要已解析的消息记录。 |
| 群分享链接 | getJoinGroupLink | 保留群号、source ID、短链接标记和附加参数。 |

## 5. 好友、资料、媒体与文件

| 任务 | 方法 | 结果规则 |
| --- | --- | --- |
| 好友列表 | getBuddyListV2、getBuddyListFromCache | 显式处理随版本变化的重载。 |
| 好友申请 | getBuddyReq、approvalFriendRequest、getDoubtBuddyReq、approvalDoubtBuddyReq | 列表查询和审批是两个独立操作。 |
| 用户详情 | getCoreAndBaseInfo、fetchUserDetailInfo、getUserDetailInfo、getUserDetailInfoWithBizInfo | 选择明确声明的详情范围和 BizKey 值。 |
| UID/UIN 转换 | getUidByUin、getUinByUid、getUixConvertService().getUid、getUixConvertService().getUin | 不得从一种标识符类型推断另一种。 |
| 资料更新 | modifySelfProfile、modifyDesktopMiniProfile、setNickName、setLongNick、setBirthday、setGander、setHeader | 修改前验证字段是否受支持。 |
| 附件下载 | NodeIKernelMsgService.downloadRichMedia | 将 onRichMediaDownloadComplete 与请求的媒体身份匹配。 |
| 视频 URL | NodeIKernelRichMediaService.getVideoPlayUrlV2 | 提供 peer、message ID、element ID、codec 和下载参数。 |
| 群文件 | getGroupFileList、createGroupFolder、renameGroupFolder、moveGroupFile、deleteGroupFolder、deleteGroupFile、transGroupFile | 明确区分群、文件和目录标识符。 |
| 文件助手 | searchFile、downloadFile、forwardFile、cancelFileAction、retryFileAction、deleteFile、saveAs | 按请求标识符匹配搜索和传输回调。 |
| 屏幕 OCR | NodeIKernelRichMediaService.getScreenOCR、NodeIKernelNodeMiscService.wantWinScreenOCR | 使用目标客户端实际公开的 Service。 |

## 6. 其他 Service 获取器

| Session getter | 接口 | 领域 |
| --- | --- | --- |
| getCollectionService | NodeIKernelCollectionService | 收藏 |
| getAlbumService | NodeIKernelAlbumService | 相册与 Qzone 媒体 |
| getRecentContactService | NodeIKernelRecentContactService | 最近联系人与未读状态 |
| getRobotService | NodeIKernelRobotService | QQ 机器人商店与会话 |
| getTicketService | NodeIKernelTicketService | client key 和 ticket 材料 |
| getTipOffService | NodeIKernelTipOffService | 举报相关调用 |
| getSettingService | NodeIKernelSettingService | 设置与账号开关 |
| getConfigMgrService | NodeIKernelConfigMgrService | 客户端配置 |
| getBaseEmojiService | NodeIKernelBaseEmojiService | emoji 资源 |
| getFlashTransferService | NodeIKernelFlashTransferService | 闪传 |
| getBdhUploadService | NodeIKernelBdhUploadService | BDH 上传生命周期 |
| getUixConvertService | NodeIKernelUixConvertService | UID/UIN 转换 |
| getNodeMiscService | NodeIKernelNodeMiscService | Mini App 和桌面工具 |
| getMiniAppService | NodeIKernelMiniAppService | Mini App 操作 |
| getThirdPartySigService | NodeIKernelThirdPartySigService | 第三方签名材料 |
| getUnifySearchService | NodeIKernelUnifySearchService | QQ 统一搜索 |
| getPersonalAlbumService | NodeIKernelPersonalAlbumService | 个人相册操作 |

## 7. 参数和回调规则

- 群号、QQ UIN、用户 UID、Peer.peerUid、message ID、message sequence 和 message random value 必须保持为不同的类型化值。
- 成功状态只代表请求被接受；如果 Listener 才会返回有效结果，就要等待并关联相应回调。
- 并发回调要按适用的账号、会话、请求、消息、文件或搜索身份进行关联。
- 重载和可选参数以目标客户端的准确声明为准；不能根据方法名推断参数顺序。
- 优先使用类型化 Service 方法。只有不存在合适的类型化方法时，才使用 sendSsoCmdReqByContend 或 sendMsfRequest。
- 在宣布某个接口可用前，必须针对真实目标客户端版本进行验证，并记录实际结果或失败。
