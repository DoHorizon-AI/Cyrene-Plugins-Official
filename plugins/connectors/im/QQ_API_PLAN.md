# QQ client API coverage plan / QQ 客户端 API 覆盖计划

This document defines the QQ client API surface to verify. It contains only
client capabilities and acceptance conditions; it does not describe a product,
connector, or third-party runtime implementation.

本文定义需要验证的 QQ 客户端 API 范围，只包含客户端能力与验收条件，不描述 Product、
Connector 或第三方 Runtime 实现。

## 1. Capability scope / 能力范围

| Priority | Capability / 能力 | QQ client interfaces and methods / QQ 客户端接口与方法 |
| --- | --- | --- |
| P0 | Session lifecycle / 会话生命周期 | `NodeIQQNTWrapperSession.create`, `init`, `startNT`; `NodeIKernelLoginService.connect`, `online`, `offline` |
| P0 | Login state / 登录状态 | `getLoginList`, `quickLoginWithUin`, `passwordLogin`, `getQRCodePicture`, `startPolling`, `getSelfStatus` |
| P0 | Self identity / 当前账号身份 | `NodeIKernelProfileService.getCoreAndBaseInfo`, `getUserSimpleInfo` |
| P0 | Receive messages / 接收消息 | `NodeIKernelMsgService.addKernelMsgListener`; `NodeIKernelMsgListener.onRecvMsg` |
| P0 | Send messages / 发送消息 | `NodeIKernelMsgService.sendMsg`; completion through `onMsgInfoListUpdate` when required |
| P0 | Peer resolution / 会话对象解析 | `getUidByUin`, `getUinByUid`, `getUixConvertService().getUid`, `getUixConvertService().getUin` |
| P1 | Message lookup / 消息查询 | `getMsgsIncludeSelf`, `getMsgsBySeqAndCount`, `getMsgsByMsgId`, `getSingleMsg`, `queryMsgsWithFilterEx` |
| P1 | Recall and forward / 撤回与转发 | `recallMsg`, `forwardMsg`, `forwardMsgWithComment`, `multiForwardMsg` |
| P1 | Read state and emoji likes / 已读与表情点赞 | `setMsgRead`, `setAllC2CAndGroupMsgRead`, `setMsgEmojiLikes`, `getMsgEmojiLikesList` |
| P1 | Group discovery / 群发现 | `getGroupList`, `getGroupDetailInfo`, `getAllMemberList`, `getMemberInfo` |
| P1 | Friend discovery / 好友发现 | `getBuddyListV2`, `getBuddyListFromCache`, `getBuddyReq` |
| P1 | Media download / 媒体下载 | `getRichMediaElement`, `downloadRichMedia`, `getVideoPlayUrlV2`, `onRichMediaDownloadComplete` |
| P1 | Files / 文件 | `getGroupFileList`, `searchFile`, `downloadFile`, `forwardFile`, `saveAs` |
| P2 | Group administration / 群管理 | `modifyGroupName`, `modifyGroupRemark`, `setMemberShutUp`, `setGroupShutUp`, `kickMember`, `quitGroup` |
| P2 | Friend requests / 好友申请 | `approvalFriendRequest`, `getDoubtBuddyReq`, `approvalDoubtBuddyReq`, `reqToAddFriends`, `delBuddy` |
| P2 | Profile mutation / 资料修改 | `modifySelfProfile`, `setNickName`, `setLongNick`, `setBirthday`, `setGander`, `setHeader` |
| P2 | Search / 搜索 | `searchStranger`, `searchGroup`, `searchContact`, `searchMsgWithKeywords`, `searchFileWithKeywords` |
| P2 | Online state / 在线状态 | `setStatus`, `getOnLineDev`, `getLikeList`, `setLikeStatus`, `checkLikeStatus` |

## 2. Required request and result data / 必需请求与结果数据

| Area / 领域 | Required data / 必需数据 |
| --- | --- |
| Account / 账号 | Login state, QQ UIN, user UID, display name, and client version / 登录状态、QQ UIN、用户 UID、显示名称与客户端版本 |
| Peer / 会话对象 | Chat type, `peerUid`, group code or private-user identity / 会话类型、`peerUid`、群号或私聊用户身份 |
| Message / 消息 | Message id, sequence, random value, sender, timestamp, and ordered elements / 消息 ID、序列号、随机值、发送者、时间戳与有序消息元素 |
| Group / 群 | Group code, name, permissions, member identity, and moderation result / 群号、名称、权限、成员身份与管理结果 |
| Media / 媒体 | Element id, file id, codec or media type, progress, local result reference, and error / 元素 ID、文件 ID、编解码或媒体类型、进度、本地结果引用与错误 |
| Callback / 回调 | Account, peer, request, message, file, or search correlation identity / 账号、会话、请求、消息、文件或搜索关联标识 |

## 3. Validation order / 验证顺序

1. Confirm session creation, initialization, login-state observation, self
   identity, and clean offline behavior.
2. Validate one harmless read operation for friends, groups, and one known peer.
3. Validate private and group text receive, including account, peer, message,
   timestamp, and ordered element identities.
4. Validate private and group text send, including callback correlation and the
   returned message identity.
5. Validate message lookup, recall, forward, read state, and duplicate-callback
   handling.
6. Validate media and file download with bounded storage and explicit failure
   results.
7. Validate mutation and moderation methods only after read-only and messaging
   operations pass.

1. 先验证 Session 创建、初始化、登录状态读取、自身身份读取与安全离线。
2. 分别对好友、群和一个已知会话对象验证一次无副作用读取。
3. 验证私聊与群聊文本接收，并保留账号、会话、消息、时间戳与有序元素身份。
4. 验证私聊与群聊文本发送，包括回调关联与返回的消息身份。
5. 验证消息查询、撤回、转发、已读状态与重复回调处理。
6. 验证媒体和文件下载，要求存储有界且失败结果明确。
7. 只在只读与消息能力通过后验证修改和管理类方法。

## 4. Acceptance record / 验收记录

Every verified method must record the exact QQ client version, host platform,
service interface, method signature, input identifier types, direct return,
listener callback, timeout behavior, and observed error. Credentials, login
tickets, device secrets, and private message content must not be recorded.

每个已验证方法必须记录准确的 QQ 客户端版本、宿主平台、Service 接口、方法签名、输入标识
类型、直接返回、Listener 回调、超时行为与实际错误。不得记录凭据、登录票据、设备密钥或
私聊内容。

An API remains `NOT_RUN` until exercised against the named real client version.
A declaration, successful compilation, mock, or simulated peer does not change
that status.

API 在指定真实客户端版本上实际执行前保持 `NOT_RUN`。接口声明、编译成功、Mock 或模拟
Peer 均不能改变该状态。
---

<!-- Chinese Translation / 中文翻译 -->

## 中文翻译

# QQ 客户端 API 覆盖计划

本文定义需要验证的 QQ 客户端 API 范围。内容只涉及客户端能力和验收条件，不描述 Product、connector 或第三方 runtime 实现。

## 1. Capability 范围

| 优先级 | Capability | QQ 客户端接口与方法 |
| --- | --- | --- |
| P0 | Session 生命周期 | NodeIQQNTWrapperSession.create、init、startNT；NodeIKernelLoginService.connect、online、offline |
| P0 | 登录状态 | getLoginList、quickLoginWithUin、passwordLogin、getQRCodePicture、startPolling、getSelfStatus |
| P0 | 当前账号身份 | NodeIKernelProfileService.getCoreAndBaseInfo、getUserSimpleInfo |
| P0 | 接收消息 | NodeIKernelMsgService.addKernelMsgListener；NodeIKernelMsgListener.onRecvMsg |
| P0 | 发送消息 | NodeIKernelMsgService.sendMsg；必要时通过 onMsgInfoListUpdate 获取完成结果 |
| P0 | 会话对象解析 | getUidByUin、getUinByUid、getUixConvertService().getUid、getUixConvertService().getUin |
| P1 | 消息查询 | getMsgsIncludeSelf、getMsgsBySeqAndCount、getMsgsByMsgId、getSingleMsg、queryMsgsWithFilterEx |
| P1 | 撤回与转发 | recallMsg、forwardMsg、forwardMsgWithComment、multiForwardMsg |
| P1 | 已读状态与表情点赞 | setMsgRead、setAllC2CAndGroupMsgRead、setMsgEmojiLikes、getMsgEmojiLikesList |
| P1 | 群发现 | getGroupList、getGroupDetailInfo、getAllMemberList、getMemberInfo |
| P1 | 好友发现 | getBuddyListV2、getBuddyListFromCache、getBuddyReq |
| P1 | 媒体下载 | getRichMediaElement、downloadRichMedia、getVideoPlayUrlV2、onRichMediaDownloadComplete |
| P1 | 文件 | getGroupFileList、searchFile、downloadFile、forwardFile、saveAs |
| P2 | 群管理 | modifyGroupName、modifyGroupRemark、setMemberShutUp、setGroupShutUp、kickMember、quitGroup |
| P2 | 好友申请 | approvalFriendRequest、getDoubtBuddyReq、approvalDoubtBuddyReq、reqToAddFriends、delBuddy |
| P2 | 资料修改 | modifySelfProfile、setNickName、setLongNick、setBirthday、setGander、setHeader |
| P2 | 搜索 | searchStranger、searchGroup、searchContact、searchMsgWithKeywords、searchFileWithKeywords |
| P2 | 在线状态 | setStatus、getOnLineDev、getLikeList、setLikeStatus、checkLikeStatus |

## 2. 必需请求与结果数据

| 领域 | 必需数据 |
| --- | --- |
| 账号 | 登录状态、QQ UIN、用户 UID、显示名称和客户端版本 |
| 会话对象 | 聊天类型、peerUid、群号或私聊用户身份 |
| 消息 | 消息 ID、序列号、随机值、发送者、时间戳和有序消息元素 |
| 群 | 群号、名称、权限、成员身份和管理结果 |
| 媒体 | 元素 ID、文件 ID、编解码器或媒体类型、进度、本地结果引用和错误 |
| 回调 | 账号、会话、请求、消息、文件或搜索关联身份 |

## 3. 验证顺序

1. 确认 Session 创建、初始化、登录状态观测、自身身份读取和正常离线行为。
2. 分别对好友、群和一个已知会话对象验证一次无副作用读取。
3. 验证私聊和群聊文本接收，包括账号、会话、消息、时间戳及有序元素身份。
4. 验证私聊和群聊文本发送，包括回调关联和返回的消息身份。
5. 验证消息查询、撤回、转发、已读状态及重复回调处理。
6. 验证媒体和文件下载，要求存储有界且失败结果明确。
7. 只有只读和消息操作通过后，才验证修改及管理方法。

## 4. 验收记录

每个经过验证的方法都必须记录准确的 QQ 客户端版本、宿主平台、Service 接口、方法签名、输入标识符类型、直接返回值、Listener 回调、超时行为和观测到的错误。不得记录凭据、登录 ticket、设备 secret 或私聊内容。

只有在指定真实客户端版本上实际运行后，API 才能离开 NOT_RUN 状态。仅有接口声明、编译成功、mock 或模拟 peer 都不能改变该状态。
