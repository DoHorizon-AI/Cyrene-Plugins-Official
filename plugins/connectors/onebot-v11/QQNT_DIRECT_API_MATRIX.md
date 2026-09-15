# QQNT Direct API Matrix / QQNT Direct API 映射矩阵

This matrix is the human-readable projection of the executable fixed-operation
table in `src/onebot_v11_connector/qqnt_direct_operations.py`. Its capability
source is [`../im/QQ_API_PLAN.md`](../im/QQ_API_PLAN.md). Every operation named
by that plan is represented below; the extra `qq.friend.set_remark` operation is
an explicitly named adapter extension and is not used to hide arbitrary native
calls.

本矩阵是 `src/onebot_v11_connector/qqnt_direct_operations.py` 固定操作表的人工投影，能力来源
是 [`../im/QQ_API_PLAN.md`](../im/QQ_API_PLAN.md)。计划中列出的每个操作都在下表出现；额外的
`qq.friend.set_remark` 是显式登记的适配器扩展，不会被用来隐藏任意原生调用。

## Target and evidence policy / 目标与证据策略

The first supported real target is Linux x86_64. A deployment must provide a
written authorization record, the exact QQ client build string, and the exact
Host ABI. Configuration carries the build string as `required_client_version`
and the ABI as `required_host_abi`; the Host hello carries `client_version`
and `abi`. Because this repository does
not contain an authorized QQ build or native Host, every real API row is
currently `NOT_RUN`. Fake Host tests prove only the worker boundary, mapping
mechanics, and failure handling.

首个真实目标是 Linux x86_64。部署必须提供书面授权、准确 QQ client build 字符串和准确 Host
ABI。配置中的 `required_client_version` 固定 build 字符串，`required_host_abi` 固定 Host
ABI，Host hello 返回 `client_version` 与 `abi`。当前仓库没有获授权的 QQ build 或原生
Host，因此所有真实 API 行仍为 `NOT_RUN`；
Fake Host 只证明 worker 边界、映射机制和失败处理。

The request/result columns below describe the normalized `qq.client.v1`
envelope. They are not invented native overload signatures. Before a row can
move to `PASS`, the acceptance ledger must record the exact native signature,
input identifier types, direct return, listener callback, timeout/cancel result,
and observed error for the configured build, without recording credentials,
tickets, session files, or private message bodies.

下表请求/结果列描述规范化的 `qq.client.v1` envelope，不臆造 native overload 签名。某行只有在
验收记录中补齐目标 build 的准确 native signature、输入标识类型、直接返回、Listener 回调、
超时/取消结果和实际错误后才能从 `NOT_RUN` 变为 `PASS`；验收记录不得写入凭据、票据、会话
文件或私聊正文。

## Coverage matrix / 覆盖矩阵

| Priority | QQ API row / QQ API 行 | Direct operation(s) / 直连操作 | Native service and method / 原生 Service 与方法 | Request fields / 请求字段 | Result or callback / 结果或回调 | Target / evidence |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | Session lifecycle | `qq.session.create`, `qq.session.init`, `qq.session.start_nt`, `qq.login.connect`, `qq.login.online`, `qq.login.offline` | `NodeIQQNTWrapperSession.create/init/startNT`; `NodeIKernelLoginService.connect/online/offline` | `account_id?`, `platform`, `client_version`, `data_dir`, `login_policy` | `state`, `session_id`, `account_id`, `client_version`, `abi`; login/session state callback | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P0 | Login state | `qq.login.list`, `qq.login.quick`, `qq.login.password`, `qq.login.qr`, `qq.login.poll` | `NodeIKernelLoginService.getLoginList/quickLoginWithUin/passwordLogin/getQRCodePicture/startPolling` | `account_id?`, `uin?`, `secret_ref?`, `qr_code?`, `poll_interval_seconds?` | `login_state`, `account_id`, QQ UIN, user UID, display name, QR/progress result; login callback | Linux x86_64 + exact build/ABI; password is secret-ref only; `NOT_RUN` |
| P0 | Login status observation | `qq.login.self_status` | `NodeIKernelProfileService.getSelfStatus` | `account_id?`, `uin?`, `uid?` | `login_state`, `account_id`, QQ UIN, user UID, display name | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P0 | Self identity | `qq.account.core`, `qq.account.simple` | `NodeIKernelProfileService.getCoreAndBaseInfo/getUserSimpleInfo` | `account_id?`, `uid?`, `uin?` | account ID, QQ UIN, user UID, display name, profile result | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P0 | Receive messages | `qq.message.subscribe` | `NodeIKernelMsgService.addKernelMsgListener` + `NodeIKernelMsgListener.onRecvMsg` | `account_id?`, `events?`, `filter?` | `message.received` callback: account, peer, message ID, sequence, random, timestamp, ordered elements | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P0 | Send messages | `send_message`, `qq.message.send`, `qq.message.send_completion` (callback-only) | `NodeIKernelMsgService.sendMsg` + `onMsgInfoListUpdate` | canonical conversation/content/reply; `peer`, `elements` | accepted DeliveryResult, native message ID, sequence, random, peer; completion callback | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P0 | Peer resolution | `qq.peer.uid_by_uin`, `qq.peer.uin_by_uid`, `qq.peer.uid`, `qq.peer.uin` | `NodeIKernelProfileService.getUidByUin/getUinByUid`; `NodeIKernelUixConvertService.getUid/getUin` | `account_id?`, `uid?`, `uin?`, `user_uid?`, `user_uin?`, `peer_uid?` | explicit UID/UIN/peer identity result; no lossy ID substitution | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Message lookup | `qq.message.history_include_self`, `qq.message.history_by_seq`, `qq.message.by_id`, `qq.message.single`, `qq.message.search` | `getMsgsIncludeSelf/getMsgsBySeqAndCount/getMsgsByMsgId/getSingleMsg/queryMsgsWithFilterEx` | `account_id`, `peer`, `message_id?`, `sequence?`, `random?`, `offset?`, `count?`, `filter?`, `query?` | ordered messages with message ID, sender, timestamp, sequence, random, elements | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Recall and forward | `qq.message.recall`, `qq.message.forward`, `qq.message.forward_comment`, `qq.message.multi_forward` | `recallMsg/forwardMsg/forwardMsgWithComment/multiForwardMsg` | `account_id`, `source`, `destination`, `message_id?`, `message_ids?`, `comment?`, `messages?` | accepted/rejected result, source/destination identity, resulting message ID; completion when supplied | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Read and emoji likes | `qq.message.read`, `qq.message.read_all`, `qq.message.emoji_likes`, `qq.message.emoji_likes_list` | `setMsgRead/setAllC2CAndGroupMsgRead/setMsgEmojiLikes/getMsgEmojiLikesList` | `account_id`, `peer?`, `message_id?`, `message_ids?`, `like_id?`, `like_type?` | status, read marker, like list/count, message identity | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Group discovery | `qq.group.list`, `qq.group.detail`, `qq.group.members`, `qq.group.member` | `getGroupList/getGroupDetailInfo/getAllMemberList/getMemberInfo` | `account_id`, `group_id?`, `group_code?`, `member_uid?`, `member_uin?`, `offset?`, `count?` | group code, name, permissions, member UID/UIN, display data | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Friend discovery | `qq.friend.list`, `qq.friend.cached`, `qq.friend.requests` | `getBuddyListV2/getBuddyListFromCache/getBuddyReq` | `account_id`, `uid?`, `uin?`, `offset?`, `count?` | friend UID/UIN, display data, request identity/state | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Media download | `qq.media.element`, `qq.media.download`, `qq.media.video_url`, `qq.media.download_complete` (callback-only) | `getRichMediaElement/downloadRichMedia/getVideoPlayUrlV2/onRichMediaDownloadComplete` | `account_id`, `message_id?`, `element_id?`, `media_id?`, `media_type?`, `codec?`, `download?` | element/file ID, media type, progress, bounded `remote_uri` or QQ media ref, local result ref, error; download callback | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P1 | Files | `qq.file.list`, `qq.file.search`, `qq.file.download`, `qq.file.forward`, `qq.file.save` | `getGroupFileList/searchFile/downloadFile/forwardFile/saveAs` | `account_id`, `group_id?`, `folder_id?`, `file_id?`, `file_uuid?`, `file_name?`, `query?`, `source?`, `destination?` | bounded file reference, name/type, progress, local result ref, error; file callback | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Group administration | `qq.group.modify_name`, `qq.group.modify_remark`, `qq.group.mute_member`, `qq.group.mute`, `qq.group.kick`, `qq.group.quit` | `modifyGroupName/modifyGroupRemark/setMemberShutUp/setGroupShutUp/kickMember/quitGroup` | `account_id`, `group_id`, `member_uid?`, `member_uin?`, `name?`, `remark?`, `duration_seconds?` | accepted/rejected status, target identity, reason; admin callback if native API emits one | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Group join approval adapter | `qq.group.approve` | `NodeIKernelGroupService.operateSysNotify` | `account_id`, `request_id`, `group_id?`, `user_id?`, `approve`, `comment?`, `vendor_request?` | accepted/rejected request result, request/group/member identity | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Friend requests and remark | `qq.friend.approve`, `qq.friend.doubt_requests`, `qq.friend.approve_doubt`, `qq.friend.add`, `qq.friend.delete`, `qq.friend.set_remark` | `approvalFriendRequest/getDoubtBuddyReq/approvalDoubtBuddyReq/reqToAddFriends/delBuddy/setBuddyRemark` | `account_id`, `request_id?`, `uid?`, `uin?`, `approve?`, `comment?`, `remark?` | request/friend identity, decision/status, reason; request callback where native API emits one | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Profile mutation | `qq.profile.modify`, `qq.profile.nickname`, `qq.profile.long_nick`, `qq.profile.birthday`, `qq.profile.gender`, `qq.profile.header` | `modifySelfProfile/setNickName/setLongNick/setBirthday/setGander/setHeader` | `account_id`, `profile?`, `nickname?`, `long_nick?`, `birthday?`, `gender?`, `header?` | field-level accepted/rejected result and account identity | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Search | `qq.search.stranger`, `qq.search.group`, `qq.search.contact`, `qq.search.message`, `qq.search.file` | `searchStranger/searchGroup/searchContact/searchMsgWithKeywords/searchFileWithKeywords` | `account_id`, `query?`, `keywords?`, `scope?`, `offset?`, `count?`, `filter?` | typed result list preserving UID/UIN/group/file/message identities | Linux x86_64 + exact build/ABI; `NOT_RUN` |
| P2 | Online state and likes | `qq.online.status`, `qq.online.devices`, `qq.online.likes`, `qq.online.set_like`, `qq.online.check_like` | `setStatus/getOnLineDev/getLikeList/setLikeStatus/checkLikeStatus` | `account_id`, `status?`, `device_id?`, `like_id?`, `like_type?`, `target_id?` | status/device/like result with account and target identity | Linux x86_64 + exact build/ABI; `NOT_RUN` |

## Cross-cutting contract profiles / 横切契约 profile

Each fixed operation selects one profile through the executable `mapping` value
in `QQOperation`. The profile is the request/result boundary for every operation
in that row; the native Host remains responsible for exact overload validation.

每个固定操作通过可执行 `QQOperation.mapping` 值选择一个 profile。该行就是该组所有操作的
请求/结果边界；准确 overload 校验仍由 native Host 负责。

| Mapping profile | Applies to | Request contract | Result/callback contract |
| --- | --- | --- | --- |
| `session` | session lifecycle | `account_id?`, `platform`, `client_version`, `data_dir`, `login_policy` | state/session/account/version/ABI; lifecycle callback |
| `login` | login state | account/UIN, `secret_ref?`, QR/poll fields | login state, account/UIN/UID, QR/progress; login callback |
| `account` | self identity | account/UIN/UID selector | account/UIN/UID/display name/profile |
| `message` | subscribe, recall, forward, multi-forward | account, peer/source/destination/message identity, ordered elements as applicable | message identity or accepted status; native completion callback as applicable |
| `send_message` | canonical send and callback record `qq.message.send_completion` | canonical conversation/content/reply plus QQ peer/elements | DeliveryResult, message ID/sequence/random/peer, completion callback; callback record is not requestable |
| `peer` | peer resolution | explicit UID/UIN/account selector | explicit UID/UIN/peer result |
| `lookup` | history/by-id/search | account, peer, message/sequence/filter/page fields | ordered typed messages with identity fields |
| `read` | read state | account, peer/message selectors | accepted status/read marker |
| `emoji` | emoji likes | account, message/like selectors | like list/count/status |
| `group` | group discovery | account, group/member selectors and paging | group/member identity, name, permissions |
| `friend` | friend discovery | account, friend selector and paging | friend/request identity and display data |
| `media` | media download | account, message/element/media selector and media options | bounded media reference, progress, local result ref, error; download callback |
| `file` | file operations | account, group/folder/file selector and source/destination | bounded file reference, metadata, progress, local result ref, error |
| `group_mutation` | group administration and approval | account, group/member/request target, decision or mutation fields | target identity, accepted/rejected status, reason |
| `friend_mutation` | friend request and remark mutation | account, request/friend target, decision/remark | request/friend identity, accepted/rejected status, reason |
| `profile` | profile mutation | account and field-specific profile values | field-level status and account identity |
| `search` | search operations | account, query/keywords/scope/filter/page | typed identity-preserving result list |
| `online` | online state and likes | account, status/device/like target | status/device/like result |

## Shared safety rules / 共享安全规则

| Area / 领域 | Rule / 规则 | Evidence state / 证据状态 |
| --- | --- | --- |
| Correlation | `binding_id + generation + request_id`; UID, UIN, peer UID, group code, message ID, sequence, and random remain separate values. | Fake Host tested; real `NOT_RUN` |
| Timeout/cancel | Bounded deadline; cancellation sends a control frame; removed requests ignore late responses; no implicit side-effect retry. | Fake Host tested; real `NOT_RUN` |
| Account/session | Exact configured platform/build/ABI and optional expected account; missing or mismatched ready account fails closed. | Fake Host tested; real `NOT_RUN` |
| Media/files | Only HTTP(S) URI or binding-private QQ media reference crosses the canonical seam; no Product-local path or unbounded content. | Schema and mapper tested; real `NOT_RUN` |
| Security | `qq.login.password` accepts `secret_ref`, never `password`; diagnostics redact credential-like fields and are bounded. | Static/test boundary; real `NOT_RUN` |
| Process isolation | One binding owns one data directory and process group; shutdown reaps binding-local descendants and does not use TCP/WS/OneBot transport. | Fake Host tested; real `NOT_RUN` |
| Installation selection | Linux x86_64 operator path or one exact installation manifest; zero/multiple candidates and build drift fail closed. | Discovery tests; real `NOT_RUN` |
| Crash supervision | Unexpected exit uses a bounded binding-local restart budget and circuit; failed operations are never implicitly replayed. | Fake Host recovery/circuit tests; real `NOT_RUN` |

The additional getters in `../im/QQ_SIDE_INTERFACES.md` (collection, album,
robot, ticket, setting, mini-app, third-party signature, and similar services)
are not claimed by this matrix. They require a separate fixed operation, schema,
exact-version mapping, authorization record, and test before being added.

`../im/QQ_SIDE_INTERFACES.md` 中 collection、album、robot、ticket、setting、mini-app、
third-party signature 等额外 getter 不在本矩阵声明范围内。只有新增固定操作、Schema、精确版本
映射、授权记录和测试后，才可加入。
